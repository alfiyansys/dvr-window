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

    # All DVR-specific config (host, ports, username, password) comes from
    # the environment, not config.yaml — nothing identifying this specific
    # device ends up in git history. Ports have standard defaults (80/554);
    # host/username/password don't and must be set explicitly.
    required = {
        "HIKVISION_HOST": os.environ.get("HIKVISION_HOST"),
        "HIKVISION_USERNAME": os.environ.get("HIKVISION_USERNAME"),
        "HIKVISION_PASSWORD": os.environ.get("HIKVISION_PASSWORD"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} not set. Copy .env.example to .env and fill it in."
        )

    device = DeviceConfig(
        host=required["HIKVISION_HOST"],
        http_port=int(os.environ.get("HIKVISION_HTTP_PORT", 80)),
        rtsp_port=int(os.environ.get("HIKVISION_RTSP_PORT", 554)),
        username=required["HIKVISION_USERNAME"],
        password=required["HIKVISION_PASSWORD"],
    )
    server = ServerConfig(
        host=raw["server"].get("host", "127.0.0.1"),
        port=raw["server"].get("port", 8896),
    )
    return Settings(device=device, server=server)
