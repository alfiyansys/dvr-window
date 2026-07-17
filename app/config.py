import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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
    # All config comes from the environment now, not any tracked file —
    # nothing identifying this specific device (or even this local
    # service's own bind address) ends up in git history. Ports have
    # standard defaults; host/username/password don't and must be set
    # explicitly.
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
        host=os.environ.get("SERVER_HOST", "127.0.0.1"),
        port=int(os.environ.get("SERVER_PORT", 8896)),
    )
    return Settings(device=device, server=server)
