from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import load_settings
from app.isapi import ISAPIClient

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.isapi = ISAPIClient(settings.device)
    try:
        yield
    finally:
        app.state.isapi.close()


app = FastAPI(title="hikvision-localservice", lifespan=lifespan)


def _build_channel(isapi: ISAPIClient, video_input: dict, streaming_channels: list[dict]) -> dict:
    channel_id = int(video_input["id"])
    enabled = video_input["videoInputEnabled"] == "true"

    streams = [
        {
            "streamId": sc["id"],
            "codec": sc["Video"]["videoCodecType"],
            "width": int(sc["Video"]["videoResolutionWidth"]),
            "height": int(sc["Video"]["videoResolutionHeight"]),
            "audioEnabled": sc["Audio"]["enabled"] == "true",
        }
        for sc in streaming_channels
        if int(sc["Video"]["videoInputChannelID"]) == channel_id
    ]

    ptz = None
    if enabled:
        caps = isapi.get_ptz_capabilities(channel_id)
        if caps is not None:
            ptz = {
                "enabled": caps["enabled"] == "true",
                "controlProtocol": caps["controlProtocol"]["#text"] if isinstance(caps["controlProtocol"], dict) else caps["controlProtocol"],
            }

    return {
        "id": channel_id,
        "name": video_input["name"],
        "enabled": enabled,
        "resolution": video_input["resDesc"],
        "streams": streams,
        "ptz": ptz,
    }


@app.get("/api/channels")
def get_channels():
    isapi: ISAPIClient = app.state.isapi
    video_inputs = isapi.get_video_input_channels()
    streaming_channels = isapi.get_streaming_channels()
    return [_build_channel(isapi, vi, streaming_channels) for vi in video_inputs]


@app.get("/api/device")
def get_device():
    isapi: ISAPIClient = app.state.isapi
    return isapi.get_device_info()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
