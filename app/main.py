import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.config import load_settings
from app.isapi import ISAPIClient
from app.mediabridge import HLS_PORT, WEBRTC_PORT, MediaBridge, stream_path_name

settings = load_settings()

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "tmp" / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_DOWNLOAD_SECONDS = 300

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "tmp" / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


async def _gc_playback_paths_loop(bridge: MediaBridge) -> None:
    while True:
        await asyncio.sleep(30)
        removed = await asyncio.to_thread(bridge.gc_playback_paths)
        for name in removed:
            print(f"[gc] removed abandoned playback path: {name}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    isapi = ISAPIClient(settings.device)
    app.state.isapi = isapi

    channels = _discover_channels(isapi)
    app.state.channels = channels

    bridge = MediaBridge()
    bridge.start(settings.device, channels)
    app.state.bridge = bridge

    gc_task = asyncio.create_task(_gc_playback_paths_loop(bridge))

    try:
        yield
    finally:
        gc_task.cancel()
        bridge.stop()
        isapi.close()


app = FastAPI(title="dvr-window", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# Single choke point for API auth — deliberately middleware, not a
# per-route Depends(), since there's no APIRouter here, just 14 flat
# routes on the bare `app`; this is the one place a new route can't
# slip through unauthenticated. Everything NOT under /api/ (the page
# shells, /static/* assets, /healthz) stays open — no session/cookie/
# redirect machinery, just a header check on the actual data/control
# endpoints. This only covers the FastAPI side — the live video itself
# flows DVR -> mediamtx -> browser directly (never through here), so
# mediamtx has its own matching auth, see app/mediabridge.py.
@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        if request.headers.get("X-Auth-Key") != settings.auth_key:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-Auth-Key"})
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/playback")
def playback_page():
    return FileResponse("static/playback.html")


def _streams_for_channel(streaming_channels: list[dict], match_key: str, channel_id: int) -> list[dict]:
    return [
        {
            "streamId": sc["id"],
            "codec": sc["Video"]["videoCodecType"],
            "width": int(sc["Video"]["videoResolutionWidth"]),
            "height": int(sc["Video"]["videoResolutionHeight"]),
            "audioEnabled": sc["Audio"]["enabled"] == "true",
        }
        for sc in streaming_channels
        if match_key in sc["Video"] and int(sc["Video"][match_key]) == channel_id
    ]


def _build_ptz(isapi: ISAPIClient, channel_id: int) -> dict | None:
    caps = isapi.get_ptz_capabilities(channel_id)
    if caps is None:
        return None
    return {
        "enabled": caps["enabled"] == "true",
        "controlProtocol": caps["controlProtocol"]["#text"] if isinstance(caps["controlProtocol"], dict) else caps["controlProtocol"],
    }


def _build_channel(isapi: ISAPIClient, video_input: dict, streaming_channels: list[dict]) -> dict:
    channel_id = int(video_input["id"])
    enabled = video_input["videoInputEnabled"] == "true"
    streams = _streams_for_channel(streaming_channels, "videoInputChannelID", channel_id)
    ptz = _build_ptz(isapi, channel_id) if enabled else None

    return {
        "id": channel_id,
        "name": video_input["name"],
        "enabled": enabled,
        "resolution": video_input["resDesc"],
        "streams": streams,
        "ptz": ptz,
    }


def _build_ip_channel(isapi: ISAPIClient, proxy_channel: dict, streaming_channels: list[dict]) -> dict:
    """IP-camera slots (ONVIF-proxied through the DVR — channels 9/10 on
    this unit) are discovered via a separate ISAPI list
    (/ISAPI/ContentMgmt/InputProxy/channels), not
    /ISAPI/System/Video/inputs/channels (analog-only), and key their
    streaming channels by dynVideoInputChannelID instead of
    videoInputChannelID. Confirmed once these channels came online — see
    MEMORY.md."""
    channel_id = int(proxy_channel["id"])
    streams = _streams_for_channel(streaming_channels, "dynVideoInputChannelID", channel_id)
    enabled = len(streams) > 0
    # get_ptz_capabilities always returns None for these channels (the DVR
    # rejects that specific endpoint for proxy-channel IDs), but the DVR
    # does proxy real PTZ *control* for them — confirmed by physically
    # observing a continuous-move command turn the camera (channel 9). Not
    # independently re-verified per-channel; assumed true for any enabled
    # proxy channel since it's the same ISAPI mechanism. See
    # ARCHITECTURE.md "PTZ for IP-proxy channels".
    ptz = {"enabled": True, "controlProtocol": "ONVIF"} if enabled else None
    source = proxy_channel["sourceInputPortDescriptor"]

    return {
        "id": channel_id,
        "name": proxy_channel["name"],
        "enabled": enabled,
        "resolution": f"{streams[0]['width']}*{streams[0]['height']}" if streams else "NO VIDEO",
        "streams": streams,
        "ptz": ptz,
        # Only IP-proxy channels have a network hop to measure — analog
        # channels (_build_channel above) never set these, which is what
        # get_ping below uses to tell the two apart generically, not a
        # hardcoded channel id.
        "ipAddress": source["ipAddress"],
        "managePort": int(source["managePortNo"]),
    }


def _discover_channels(isapi: ISAPIClient) -> list[dict]:
    streaming_channels = isapi.get_streaming_channels()
    channels = [_build_channel(isapi, vi, streaming_channels) for vi in isapi.get_video_input_channels()]
    channels += [_build_ip_channel(isapi, pc, streaming_channels) for pc in isapi.get_input_proxy_channels()]
    return channels


@app.get("/api/channels")
def get_channels():
    isapi: ISAPIClient = app.state.isapi
    return _discover_channels(isapi)


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
        channels.append({
            "id": channel["id"],
            "name": channel["name"],
            "streams": streams,
            "ptzEnabled": channel["ptz"]["enabled"] if channel["ptz"] else False,
        })

    return {"hlsPort": HLS_PORT, "webrtcPort": WEBRTC_PORT, "channels": channels}


async def _tcp_ping(host: str, port: int, timeout: float = 2.0) -> float | None:
    """Round-trip time of a bare TCP connect, as a network-responsiveness
    proxy for a camera — the DVR's own ISAPI has no ping/network-delay
    endpoint on this firmware (confirmed: `/ISAPI/System/Network/ping` and
    `/testNetworkDelay` both 404), so this measures the backend's own path
    to the camera's ONVIF manage port instead of the DVR's. Not literally
    ICMP: a plain TCP connect needs no elevated container privileges
    (CAP_NET_RAW), unlike raw ICMP sockets, and is close enough to ICMP RTT
    for a LAN link. None on timeout/refused/unreachable rather than
    raising — a single unreachable camera shouldn't break the whole
    response for the others being pinged concurrently."""
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    elapsed_ms = (time.monotonic() - start) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return round(elapsed_ms, 1)


@app.get("/api/ping")
async def get_ping():
    """Only IP-proxy channels (ipAddress set by _build_ip_channel) have a
    network hop to measure — generic to however many a given DVR has, not
    hardcoded to channels 9/10 on this one. Pinged concurrently so one slow/
    unreachable camera doesn't delay the others."""
    targets = [ch for ch in app.state.channels if ch.get("ipAddress")]
    results = await asyncio.gather(*[_tcp_ping(ch["ipAddress"], ch["managePort"]) for ch in targets])
    return {"channels": [{"id": ch["id"], "pingMs": ms} for ch, ms in zip(targets, results)]}


def _main_track_id(channel_id: int) -> int:
    return channel_id * 100 + 1


class PTZMoveRequest(BaseModel):
    pan: int = Field(0, ge=-100, le=100)
    tilt: int = Field(0, ge=-100, le=100)
    zoom: int = Field(0, ge=-100, le=100)


@app.put("/api/ptz/{channel_id}/continuous")
def ptz_continuous(channel_id: int, req: PTZMoveRequest):
    isapi: ISAPIClient = app.state.isapi
    isapi.ptz_continuous_move(channel_id, req.pan, req.tilt, req.zoom)
    return {"status": "ok"}


@app.put("/api/ptz/{channel_id}/stop")
def ptz_stop(channel_id: int):
    isapi: ISAPIClient = app.state.isapi
    isapi.ptz_stop(channel_id)
    return {"status": "ok"}


def _to_dvr_compact(iso: str) -> str:
    """ISO "2026-07-12T14:05:00Z" -> DVR query-param form "20260712T140500Z"
    — pure string reformatting of the same literal digits, no timezone math
    (see the fake-UTC note in start_playback)."""
    return datetime.fromisoformat(iso.rstrip("Z")).strftime("%Y%m%dT%H%M%SZ")


def _rewrite_playback_window(playback_uri: str, start_time: str, end_time: str) -> str:
    """CMSearch matches return a segment's *own* full start/end in its
    playbackURI, not the caller's requested window (confirmed during Phase
    5: a search scoped to a 20s window still returned a segment spanning
    27 minutes, with starttime at the segment's own beginning). For a
    precise clip, keep the segment's name/size tokens (they identify which
    stored file to read) but overwrite starttime/endtime with what was
    actually requested."""
    parts = urlsplit(playback_uri)
    query = parse_qs(parts.query)
    query["starttime"] = [_to_dvr_compact(start_time)]
    query["endtime"] = [_to_dvr_compact(end_time)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def _nudge_time(iso: str, seconds: int) -> str:
    """Add `seconds` to an ISAPI timestamp, treating it as a literal
    wall-clock value (no timezone math) — see the note in start_playback
    about why these "Z"-suffixed strings aren't actually UTC on this DVR."""
    naive = datetime.fromisoformat(iso.rstrip("Z"))
    return (naive + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/api/recordings")
def search_recordings(channelId: int, start: str, end: str):
    isapi: ISAPIClient = app.state.isapi
    track_id = _main_track_id(channelId)
    matches = isapi.search_recordings(track_id, start, end)
    return {"channelId": channelId, "trackId": track_id, "segments": matches}


class PlaybackStartRequest(BaseModel):
    channelId: int
    startTime: str
    endTime: str


@app.post("/api/playback/start")
def start_playback(req: PlaybackStartRequest):
    isapi: ISAPIClient = app.state.isapi
    bridge: MediaBridge = app.state.bridge

    # Playback URIs go stale after a while (confirmed during Phase 4 recon:
    # an hour-old URI got a 400 from the DVR's RTSP server) — re-search
    # right before playing so we hand mediamtx a fresh one.
    #
    # Nudge the start a couple seconds past the segment's exact boundary:
    # the DVR's CMSearch has a boundary bug where a startTime that exactly
    # matches a segment's start returns the *previous* segment instead
    # (confirmed by extracting a frame and reading the DVR's on-screen
    # timestamp — landed on the prior segment until nudged).
    track_id = _main_track_id(req.channelId)
    search_start = _nudge_time(req.startTime, seconds=2)
    matches = isapi.search_recordings(track_id, search_start, req.endTime, max_pages=1, page_size=1)
    if not matches:
        raise HTTPException(status_code=404, detail="No recording found for that time range")

    # Same as /api/download: a match's playbackURI carries the segment's
    # own full start/end, not the caller's requested window (see
    # _rewrite_playback_window and ARCHITECTURE.md) — rewrite starttime to
    # the caller's actual requested time so playback (e.g. "jump to time")
    # seeks there instead of always starting from the segment's beginning.
    # Keep the segment's own endTime rather than the caller's (which may
    # just be a loose upper bound used to locate the segment, e.g. "jump
    # to time" passes end-of-day) so playback runs to the segment's real end.
    #
    # Clamp to the match's own start rather than trusting req.startTime
    # blindly: continuous playback (chaining from one segment into the
    # next) deliberately requests a startTime that can fall in a real
    # recording gap before the next segment. Without this clamp, the DVR
    # would be asked to stream from a timestamp with no footage, which
    # reproduces the segment-end freeze bug (see ARCHITECTURE.md) instead
    # of cleanly landing on the next segment's actual start.
    match = matches[0]
    match_start = datetime.fromisoformat(match["startTime"].rstrip("Z"))
    requested_start = datetime.fromisoformat(req.startTime.rstrip("Z"))
    effective_start = max(requested_start, match_start).strftime("%Y-%m-%dT%H:%M:%SZ")

    playback_uri = _rewrite_playback_window(match["playbackURI"], effective_start, match["endTime"])
    device = settings.device
    source_url = playback_uri.replace(
        "rtsp://", f"rtsp://{device.username}:{device.password}@", 1
    )

    name = f"pb_ch{req.channelId}_{uuid.uuid4().hex[:8]}"
    bridge.add_playback_path(name, source_url)

    return {
        "name": name,
        "hlsPath": f"/{name}/index.m3u8",
        "hlsPort": HLS_PORT,
        "segmentStartTime": effective_start,
        "segmentEndTime": match["endTime"],
    }


class PlaybackStopRequest(BaseModel):
    name: str


@app.post("/api/playback/stop")
def stop_playback(req: PlaybackStopRequest):
    bridge: MediaBridge = app.state.bridge
    bridge.remove_playback_path(req.name)
    return {"status": "stopped"}


@app.get("/api/snapshot")
def get_snapshot(channelId: int):
    # Not ISAPI's own /picture endpoint — see MediaBridge.capture_frame for
    # why (fixed 704x576 capture on this firmware, wrong aspect ratio).
    bridge: MediaBridge = app.state.bridge
    name = stream_path_name(channelId, str(_main_track_id(channelId)))
    output_path = SNAPSHOT_DIR / f"{name}_{uuid.uuid4().hex[:8]}.jpg"
    try:
        bridge.capture_frame(name, output_path)
        jpeg = output_path.read_bytes()
    finally:
        output_path.unlink(missing_ok=True)
    return Response(content=jpeg, media_type="image/jpeg")


class DownloadRequest(BaseModel):
    channelId: int
    startTime: str
    endTime: str


@app.post("/api/download")
def download_clip(req: DownloadRequest):
    isapi: ISAPIClient = app.state.isapi
    bridge: MediaBridge = app.state.bridge

    start = datetime.fromisoformat(req.startTime.rstrip("Z"))
    end = datetime.fromisoformat(req.endTime.rstrip("Z"))
    duration = (end - start).total_seconds()
    if duration <= 0:
        raise HTTPException(status_code=400, detail="endTime must be after startTime")
    if duration > MAX_DOWNLOAD_SECONDS:
        raise HTTPException(status_code=400, detail=f"Range too long — max {MAX_DOWNLOAD_SECONDS}s per download")

    # Same staleness/boundary caveats as playback (see start_playback) — a
    # fresh, nudged-forward search keeps the name/size token valid.
    track_id = _main_track_id(req.channelId)
    search_start = _nudge_time(req.startTime, seconds=2)
    matches = isapi.search_recordings(track_id, search_start, req.endTime, max_pages=1, page_size=1)
    if not matches:
        raise HTTPException(status_code=404, detail="No recording found for that time range")

    precise_uri = _rewrite_playback_window(matches[0]["playbackURI"], req.startTime, req.endTime)
    device = settings.device
    source_url = precise_uri.replace(
        "rtsp://", f"rtsp://{device.username}:{device.password}@", 1
    )

    name = f"dl_ch{req.channelId}_{uuid.uuid4().hex[:8]}"
    bridge.add_playback_path(name, source_url)
    try:
        output_path = DOWNLOAD_DIR / f"{name}.mp4"
        bridge.capture_clip(name, duration_seconds=duration, output_path=output_path)
    finally:
        bridge.remove_playback_path(name)

    filename = f"ch{req.channelId}_{start.strftime('%Y%m%d_%H%M%S')}.mp4"
    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(output_path.unlink),
    )


@app.get("/api/device")
def get_device():
    isapi: ISAPIClient = app.state.isapi
    return isapi.get_device_info()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
