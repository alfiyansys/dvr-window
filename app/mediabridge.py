import os
import shlex
import subprocess
import tempfile
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

# Unset (bare-metal run.sh, or a single-container deployment) means
# "127.0.0.1, and this MediaBridge spawns/owns the mediamtx process
# itself" — today's behavior, unchanged. Explicitly set (the Swarm
# split — MEDIAMTX_HOST=mediamtx, see docker-compose.swarm.yml) means
# mediamtx runs in its own container/service already managing itself;
# this MediaBridge only talks to it over the network and pushes paths
# via its config API instead of spawning/writing its config file. See
# PLAN.md "Chosen fix: split mediamtx into its own Swarm service".
_env_mediamtx_host = os.environ.get("MEDIAMTX_HOST")
MEDIAMTX_HOST = _env_mediamtx_host or "127.0.0.1"
MEDIAMTX_SELF_MANAGED = _env_mediamtx_host is None

# Gates mediamtx's own HLS/WebRTC listeners with the same AUTH_KEY the
# FastAPI side uses (see app/main.py) — video flows DVR -> mediamtx ->
# browser directly, never through FastAPI, so protecting only the API
# wouldn't secure the live view. AUTH_KEY presence is already validated
# by app.config.load_settings() before this module's start() ever runs.
# Usernames aren't secrets (AUTH_KEY is) — fixed since there's exactly
# one caller class per role (this app's own frontend for `viewer`,
# this app's own backend for `backend`).
MEDIAMTX_AUTH_USER = "viewer"
MEDIAMTX_BACKEND_USER = "backend"
AUTH_KEY = os.environ.get("AUTH_KEY")


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

        if not MEDIAMTX_SELF_MANAGED:
            # mediamtx is already running as its own Swarm service (see
            # docker-compose.swarm.yml/mediamtx/base.yml) — just wait
            # for it and push this deployment's live-view paths onto
            # it, the same pattern add_playback_path already uses for
            # playback paths.
            self._wait_until_ready()
            for name, path_config in paths.items():
                self._add_path(name, path_config)
            return

        self._start_self_managed(paths)

    def _start_self_managed(self, paths: dict) -> None:
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
            # authInternalUsers REPLACES mediamtx's own default user list,
            # it doesn't merge with it (confirmed directly against the
            # binary — env-var overrides merge onto the compiled-in
            # defaults per-index instead, which is why the Swarm-split
            # deployment's own config, mediamtx/base.yml, uses a full
            # file too rather than env vars — see PLAN.md). Same 3-entry
            # shape as that file, kept identical here so self-managed
            # (bare-metal/single-container) and network mode (the Swarm
            # split) behave the same way:
            #   - `viewer`/read: real LAN browsers, via static/auth.js.
            #   - `backend`/api: add_playback_path/remove_playback_path
            #     and (network mode only) pushing this same `paths` dict
            #     after boot — see MediaBridge.start(). Unrestricted by
            #     IP: in network mode these calls come from a different
            #     container's overlay-network IP, not loopback, so it
            #     can't be an IP-based exemption the way it used to be.
            #   - `any`/publish, loopback-only, passwordless: the H.265
            #     transcode's ffmpeg (_transcode_path below) publishing
            #     back into this same RTSP server — always spawned by
            #     mediamtx itself in its own process/container, so this
            #     one stays genuinely loopback in both modes.
            # capture_clip's ffmpeg *reads* from the RTSP re-serve —
            # needs `read`, not covered by any of the above, so it
            # explicitly authenticates as `viewer` instead (see
            # capture_clip below).
            "authMethod": "internal",
            "authInternalUsers": [
                {
                    "user": MEDIAMTX_AUTH_USER,
                    "pass": AUTH_KEY,
                    "ips": [],
                    "permissions": [{"action": "read", "path": ""}],
                },
                {
                    "user": MEDIAMTX_BACKEND_USER,
                    "pass": AUTH_KEY,
                    "ips": [],
                    "permissions": [{"action": "api", "path": ""}],
                },
                {
                    "user": "any",
                    "pass": "",
                    "ips": ["127.0.0.1", "::1"],
                    "permissions": [{"action": "publish", "path": ""}],
                },
            ],
            "paths": paths,
        }
        RUNTIME_CONFIG_PATH.write_text(yaml.safe_dump(config))

        self._proc = subprocess.Popen(
            [str(MEDIAMTX_BIN), str(RUNTIME_CONFIG_PATH)],
            cwd=MEDIAMTX_DIR,
        )

        self._wait_until_ready()

    def _wait_until_ready(self, timeout: float = 30.0) -> None:
        # Longer than self-managed mode strictly needs (mediamtx starts
        # in well under a second as a local subprocess) — network mode
        # (the Swarm split) also has to tolerate mediamtx's own image
        # still being pulled/scheduled on a cold `docker stack deploy`,
        # since Swarm doesn't guarantee inter-service start order.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://{MEDIAMTX_HOST}:{HLS_PORT}/", timeout=1.0)
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

    def _add_path(self, name: str, path_config: dict) -> None:
        # Shared by add_playback_path below and by start()'s network-mode
        # branch, which pushes the live-view paths the same way. Always
        # authenticates as `backend` — no loopback exemption for `api`
        # in either mode's authInternalUsers now (see start()'s config
        # comment / mediamtx/base.yml), so this needs real credentials
        # regardless of whether mediamtx is self-managed or a separate
        # Swarm service.
        resp = httpx.post(
            f"http://{MEDIAMTX_HOST}:{API_PORT}/v3/config/paths/add/{name}",
            json=path_config,
            auth=(MEDIAMTX_BACKEND_USER, AUTH_KEY),
            timeout=5.0,
        )
        resp.raise_for_status()

    def add_playback_path(self, name: str, source_url: str) -> None:
        self._add_path(name, {"source": source_url, "sourceOnDemand": True})

    def remove_playback_path(self, name: str) -> None:
        httpx.delete(
            f"http://{MEDIAMTX_HOST}:{API_PORT}/v3/config/paths/delete/{name}",
            auth=(MEDIAMTX_BACKEND_USER, AUTH_KEY),
            timeout=5.0,
        )

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
        # RTSP read falls under the `read` permission, which the
        # loopback-exempt authInternalUsers entry deliberately doesn't
        # grant (see the comment in start() above) — needs real
        # credentials here, unlike the control-API calls above.
        cmd = [
            "ffmpeg", "-y", "-v", "warning",
            "-rtsp_transport", "tcp",
            "-i", f"rtsp://{MEDIAMTX_AUTH_USER}:{AUTH_KEY}@{MEDIAMTX_HOST}:{RTSP_PORT}/{name}",
            "-t", str(duration_seconds),
            "-c:v", "copy", "-an",
            str(output_path),
        ]
        result = subprocess.run(cmd, timeout=duration_seconds + 20, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg capture failed: {result.stderr.decode(errors='replace')}")

    def capture_frame(self, name: str, output_path: Path) -> None:
        """Grab one real frame from a live path via the RTSP re-serve, for
        `/api/snapshot`.

        Not using the DVR's own ISAPI `/picture` endpoint: confirmed
        empirically that on this firmware it always returns a fixed
        704x576 (4:3) capture regardless of the channel's actual
        configured main-stream resolution (1920x1080/1280x720, 16:9) —
        squashing the real 16:9 sensor image into that frame produces a
        visibly distorted snapshot. Grabbing a frame from the same
        stream the live view actually plays avoids this entirely.
        """
        url = f"rtsp://{MEDIAMTX_AUTH_USER}:{AUTH_KEY}@{MEDIAMTX_HOST}:{RTSP_PORT}/{name}"

        fast_cmd = [
            "ffmpeg", "-y", "-v", "warning",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-frames:v", "1",
            str(output_path),
        ]
        result = subprocess.run(fast_cmd, timeout=15, capture_output=True)
        if result.returncode == 0:
            return

        # One IP-proxy channel (confirmed empirically, not every source)
        # only sends H.264 parameter sets every few seconds — too
        # infrequent for an instant single-frame grab, which fails fast
        # with "Could not find codec parameters" instead of waiting.
        # Fall back to a short stream-copy capture (reliably long enough
        # to catch a parameter set/keyframe) and extract the still frame
        # from that local file instead.
        with tempfile.NamedTemporaryFile(suffix=".mp4", dir=output_path.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            clip_cmd = [
                "ffmpeg", "-y", "-v", "warning",
                "-rtsp_transport", "tcp",
                "-i", url,
                "-t", "4",
                "-c:v", "copy", "-an",
                str(tmp_path),
            ]
            clip_result = subprocess.run(clip_cmd, timeout=20, capture_output=True)
            if clip_result.returncode != 0:
                raise RuntimeError(f"ffmpeg snapshot capture failed: {clip_result.stderr.decode(errors='replace')}")

            frame_cmd = ["ffmpeg", "-y", "-v", "warning", "-i", str(tmp_path), "-frames:v", "1", str(output_path)]
            frame_result = subprocess.run(frame_cmd, timeout=15, capture_output=True)
            if frame_result.returncode != 0:
                raise RuntimeError(f"ffmpeg snapshot capture failed: {frame_result.stderr.decode(errors='replace')}")
        finally:
            tmp_path.unlink(missing_ok=True)

    def stream_paths(self, name: str) -> dict:
        """Path portion of the mediamtx URLs for this stream — the frontend
        combines these with the request host and the ports below, since
        mediamtx serves HLS/WebRTC directly (not proxied through FastAPI)."""
        return {
            "hlsPath": f"/{name}/index.m3u8",
            "webrtcPath": f"/{name}/whep",
        }
