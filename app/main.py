from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.isapi import ISAPIClient
from app.mediabridge import HLS_PORT, WEBRTC_PORT, MediaBridge, stream_path_name

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    isapi = ISAPIClient(settings.device)
    app.state.isapi = isapi

    channels = [_build_channel(isapi, vi, isapi.get_streaming_channels()) for vi in isapi.get_video_input_channels()]
    app.state.channels = channels

    bridge = MediaBridge()
    bridge.start(settings.device, channels)
    app.state.bridge = bridge

    try:
        yield
    finally:
        bridge.stop()
        isapi.close()


app = FastAPI(title="hikvision-localservice", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


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


@app.get("/api/streams")
def get_streams():
    bridge: MediaBridge = app.state.bridge
    channels = []
    for channel in app.state.channels:
        if not channel["enabled"]:
            continue
        streams = []
        for stream in channel["streams"]:
            name = stream_path_name(channel["id"], stream["streamId"])
            kind = "main" if stream["streamId"].endswith("01") else "sub"
            streams.append({
                "kind": kind,
                "streamId": stream["streamId"],
                "hlsPath": bridge.stream_paths(name)["hlsPath"],
                "webrtcPath": bridge.stream_paths(name)["webrtcPath"],
            })
        channels.append({"id": channel["id"], "name": channel["name"], "streams": streams})

    return {"hlsPort": HLS_PORT, "webrtcPort": WEBRTC_PORT, "channels": channels}


@app.get("/api/device")
def get_device():
    isapi: ISAPIClient = app.state.isapi
    return isapi.get_device_info()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
