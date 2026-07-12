import subprocess
import time
from pathlib import Path

import httpx
import yaml

from app.config import DeviceConfig

MEDIAMTX_DIR = Path(__file__).resolve().parent.parent / "mediamtx"
MEDIAMTX_BIN = MEDIAMTX_DIR / "mediamtx"
RUNTIME_CONFIG_PATH = MEDIAMTX_DIR / "runtime.yml"

HLS_PORT = 8888
WEBRTC_PORT = 8889


def stream_path_name(channel_id: int, stream_id: str) -> str:
    suffix = "main" if stream_id.endswith("01") else "sub"
    return f"ch{channel_id}_{suffix}"


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
            "rtsp": False,
            "rtmp": False,
            "srt": False,
            "moq": False,
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

    def stream_paths(self, name: str) -> dict:
        """Path portion of the mediamtx URLs for this stream — the frontend
        combines these with the request host and the ports below, since
        mediamtx serves HLS/WebRTC directly (not proxied through FastAPI)."""
        return {
            "hlsPath": f"/{name}/index.m3u8",
            "webrtcPath": f"/{name}/whep",
        }
