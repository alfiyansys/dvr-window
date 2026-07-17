import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(os.environ.get("HIKVISION_CONFIG", "config.yaml"))


@dataclass(frozen=True)
class DeviceConfig:
    host: str
    http_port: int
    rtsp_port: int
    username: str
    password: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.http_port}"


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


@dataclass(frozen=True)
class Settings:
    device: DeviceConfig
    server: ServerConfig


def load_settings() -> Settings:
    raw = yaml.safe_load(CONFIG_PATH.read_text())

    # Host/username/password all come from the environment, not config.yaml
    # — none of this DVR's identifying/account info ends up in git history,
    # only non-secret protocol config (ports) does.
    env = {
        "HIKVISION_HOST": os.environ.get("HIKVISION_HOST"),
        "HIKVISION_USERNAME": os.environ.get("HIKVISION_USERNAME"),
        "HIKVISION_PASSWORD": os.environ.get("HIKVISION_PASSWORD"),
    }
    missing = [name for name, value in env.items() if not value]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} not set. Copy .env.example to .env and fill it in."
        )

    device = DeviceConfig(
        host=env["HIKVISION_HOST"],
        http_port=raw["device"].get("http_port", 80),
        rtsp_port=raw["device"].get("rtsp_port", 554),
        username=env["HIKVISION_USERNAME"],
        password=env["HIKVISION_PASSWORD"],
    )
    server = ServerConfig(
        host=raw["server"].get("host", "127.0.0.1"),
        port=raw["server"].get("port", 8896),
    )
    return Settings(device=device, server=server)
