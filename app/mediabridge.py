import os
import shlex
import subprocess
import time
from pathlib import Path

import httpx
import yaml

from app.config import DeviceConfig

MEDIAMTX_DIR = Path(__file__).resolve().parent.parent / "mediamtx"
MEDIAMTX_BIN = MEDIAMTX_DIR / "mediamtx"
RUNTIME_CONFIG_PATH = MEDIAMTX_DIR / "runtime.yml"

# This mediamtx sidecar's own local listener ports — not DVR config, just
# where this local service happens to expose HLS/WebRTC/its control API/
# the RTSP re-serve. Configurable in case one of these conflicts with
# something else already running on the host; defaults match what this
# project has always used.
HLS_PORT = int(os.environ.get("MEDIAMTX_HLS_PORT", 8888))
WEBRTC_PORT = int(os.environ.get("MEDIAMTX_WEBRTC_PORT", 8889))
API_PORT = int(os.environ.get("MEDIAMTX_API_PORT", 9997))
RTSP_PORT = int(os.environ.get("MEDIAMTX_RTSP_PORT", 8554))


def stream_path_name(channel_id: int, stream_id: str) -> str:
    suffix = "main" if stream_id.endswith("01") else "sub"
    return f"ch{channel_id}_{suffix}"


def _transcode_path(source_url: str, name: str) -> dict:
    """mediamtx can't transcode on its own — this path has no direct
    'source', so mediamtx instead runs ffmpeg on demand (only while a
    client is actually reading it) to pull the H.265 RTSP source and push
    a re-encoded H.264 stream back into this same path over its own
    loopback RTSP server."""
    push_url = f"rtsp://127.0.0.1:{RTSP_PORT}/{name}"
    cmd = (
        f"ffmpeg -rtsp_transport tcp -i {shlex.quote(source_url)} "
        f"-c:v libx264 -preset veryfast -tune zerolatency -an "
        f"-f rtsp {push_url}"
    )
    return {
        "source": "publisher",
        "runOnDemand": cmd,
        "runOnDemandRestart": True,
    }


def _build_paths(device: DeviceConfig, channels: list[dict]) -> dict:
    paths = {}
    for channel in channels:
        if not channel["enabled"]:
            continue
        for stream in channel["streams"]:
            name = stream_path_name(channel["id"], stream["streamId"])
            source = (
                f"rtsp://{device.username}:{device.password}@"
                f"{device.host}:{device.rtsp_port}/Streaming/Channels/{stream['streamId']}"
            )
            # Chrome/Chromium has no HEVC-via-MSE support (see
            # ARCHITECTURE.md "Known device quirks and bugs"), so any main
            # stream reporting H.265 needs server-side transcoding to play
            # in the browser — detected from the DVR's own reported codec
            # rather than a hardcoded channel, so this holds for whatever
            # channels/DVR happen to be configured, not just this one.
            # Limited to "main": the live-view frontend never requests a
            # sub-stream, so there's nothing to gain transcoding those too.
            if stream["codec"] == "H.265" and name.endswith("_main"):
                paths[name] = _transcode_path(source, name)
            else:
                paths[name] = {"source": source, "sourceOnDemand": True}
    return paths


class MediaBridge:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self.stream_names: list[str] = []

    def start(self, device: DeviceConfig, channels: list[dict]) -> None:
        paths = _build_paths(device, channels)
        self.stream_names = list(paths.keys())

        config = {
            "logLevel": "info",
            # RTSP re-serve is loopback-only and exists purely so the
            # backend can capture download clips with ffmpeg without
            # HLS's segment-pruning getting in the way (see
            # ARCHITECTURE.md) — not meant for LAN/browser access.
            "rtsp": True,
            "rtspAddress": f"127.0.0.1:{RTSP_PORT}",
            "rtspTransports": ["tcp"],
            "rtmp": False,
            "srt": False,
            "moq": False,
            "api": True,
            "apiAddress": f"127.0.0.1:{API_PORT}",
            # HLS/WebRTC bind on all interfaces (no host prefix), unlike
            # the two above — the browser (possibly from elsewhere on the
            # LAN) needs to reach these directly, not just this host.
            "hlsAddress": f":{HLS_PORT}",
            "webrtcAddress": f":{WEBRTC_PORT}",
            "paths": paths,
        }
        RUNTIME_CONFIG_PATH.write_text(yaml.safe_dump(config))

        self._proc = subprocess.Popen(
            [str(MEDIAMTX_BIN), str(RUNTIME_CONFIG_PATH)],
            cwd=MEDIAMTX_DIR,
        )

        self._wait_until_ready()

    def _wait_until_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{HLS_PORT}/", timeout=1.0)
                return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("mediamtx did not become ready in time")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def add_playback_path(self, name: str, source_url: str) -> None:
        resp = httpx.post(
            f"http://127.0.0.1:{API_PORT}/v3/config/paths/add/{name}",
            json={"source": source_url, "sourceOnDemand": True},
            timeout=5.0,
        )
        resp.raise_for_status()

    def remove_playback_path(self, name: str) -> None:
        httpx.delete(f"http://127.0.0.1:{API_PORT}/v3/config/paths/delete/{name}", timeout=5.0)

    def capture_clip(self, name: str, duration_seconds: float, output_path: Path) -> None:
        """Capture `duration_seconds` of video from a playback path into an
        MP4 file.

        The DVR doesn't reliably stop at the RTSP source's `endtime` (found
        during Phase 5 testing — playback kept going well past a requested
        30-second window), so the cutoff is enforced here with ffmpeg's
        `-t` instead. Video-only: transcoding this DVR's G.711 audio to
        AAC for the mp4 container reliably hung ffmpeg (packets stopped
        flowing entirely) — dropping audio was the reliable fix, and most
        channels here don't have it enabled on their main stream anyway.
        """
        cmd = [
            "ffmpeg", "-y", "-v", "warning",
            "-rtsp_transport", "tcp",
            "-i", f"rtsp://127.0.0.1:{RTSP_PORT}/{name}",
            "-t", str(duration_seconds),
            "-c:v", "copy", "-an",
            str(output_path),
        ]
        result = subprocess.run(cmd, timeout=duration_seconds + 20, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg capture failed: {result.stderr.decode(errors='replace')}")

    def stream_paths(self, name: str) -> dict:
        """Path portion of the mediamtx URLs for this stream — the frontend
        combines these with the request host and the ports below, since
        mediamtx serves HLS/WebRTC directly (not proxied through FastAPI)."""
        return {
            "hlsPath": f"/{name}/index.m3u8",
            "webrtcPath": f"/{name}/whep",
        }
