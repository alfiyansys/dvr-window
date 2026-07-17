# Architecture

Technical reference for how this system is built and why. Stable
reference material — unlike `PLAN.md` (roadmap/status) and `MEMORY.md`
(current device state), this doesn't change as phases complete, only as
the design itself changes.

## Why ISAPI + RTSP, not HCNetSDK

Rejected Hikvision's official Linux SDK (HCNetSDK): closed-source blob,
its own licensing, limited architecture support, and a Windows-shaped C
API that doesn't map cleanly to "run a local web service." ISAPI (HTTP)
+ RTSP are documented, open protocols that cover everything needed
(live view, playback, search, snapshot, download) with a normal
HTTP/RTSP client stack — portable, no licensing entanglement.

Also explicitly not attempting a reverse-engineered clone of the
Windows `LocalServiceControl.exe` local-service plugin: closed-source,
undocumented wire protocol, no Windows box available to sniff it, no
installer at hand.

## Components

Two separate paths — control-plane (ISAPI, via the backend) and
media-plane (RTSP, via the media bridge, not proxied through the
backend at all):

```
CONTROL PLANE (channels, search, snapshot, download)
-----------------------------------------------------

  Browser  -- HTTP request -->  Backend (FastAPI)  -- HTTP digest -->  DVR :80 (ISAPI)
  Browser  <-- JSON response --  app/main.py            <-- XML --    DVR :80 (ISAPI)
                                 app/isapi.py (client)


MEDIA PLANE (live view + playback)
-----------------------------------------------------

  DVR :554 (RTSP)  -- RTSP -->  Media bridge: mediamtx  -- HLS / WebRTC -->  Browser <video>
                                app/mediabridge.py
                                (sidecar process)
```

`<video>` in the browser talks to mediamtx directly on its own ports
(`:8888` HLS, `:8889` WebRTC) — the FastAPI backend only tells the
browser *which* mediamtx path to use (via `/api/streams` and
`/api/playback/start`), it doesn't proxy the media itself. Browsers
can't play raw RTSP/H.264 elementary streams directly, so this bridge
is required; using **mediamtx** (single Go binary, RTSP-in /
HLS+WebRTC-out, has a runtime control API for dynamic paths) instead
of writing our own RTSP-to-browser transcoder.

- **Backend** (`app/`): config loading (`config.py`), ISAPI client
  (`isapi.py`), mediamtx process + dynamic path management
  (`mediabridge.py`), FastAPI routes (`main.py`).
- **Frontend** (`static/`): plain HTML/JS, no build step. `index.html`
  (live grid), `playback.html` (search + play recordings). hls.js
  vendored locally (no CDN dependency, keeps the local service
  self-contained).
- **Packaging** (not built yet): systemd service, install script.

## ISAPI reference

- HTTP (not HTTPS-only on this firmware), **digest auth**, **XML-only**
  responses (`Accept: application/json` is ignored) — confirmed on
  `deviceInfo`, channel list, `CMSearch`.
- No digest-auth quirks — handles 10+ concurrent requests fine,
  standard RFC handshake, no special `qop`/`nc` workarounds needed. Any
  standard HTTP digest-auth client library works.
- Channels: `GET /ISAPI/System/Video/inputs/channels` (analog inputs),
  `GET /ISAPI/Streaming/channels` (per-stream codec/resolution/audio),
  `GET /ISAPI/PTZCtrl/channels/<id>/capabilities` (PTZ).
- `CMSearch`: `POST /ISAPI/ContentMgmt/search`, XML body (`searchID`,
  `trackList`, `timeSpanList`, `maxResults`, `searchResultPostion`).
  Paginated: re-issuing the identical request with the **same
  `searchID`** auto-advances to the next page (server tracks position
  per searchID, client doesn't manage offsets). Each match includes a
  ready-to-use `playbackURI` under `/Streaming/tracks/<trackID>/`.
- Account privilege: `PUT` requests (e.g. changing a channel's codec)
  can return `403 lowPrivilege` depending on the account — read-only
  accounts can't make config changes. See `MEMORY.md` for which account
  this project currently uses and its privilege level.
- Snapshot: `GET /ISAPI/Streaming/channels/<streamID>/picture` returns
  a JPEG directly — no wrapping, just proxy the bytes.
- `CMSearch` matches return a segment's **own full start/end** in their
  `playbackURI`, not whatever time window was searched for — a search
  scoped to a 20-second window still returned a segment spanning 27
  minutes, with `starttime` at the segment's own beginning. Fine for
  "play this whole segment" (Phase 4), but for a precise clip (Phase 5
  download) the match's `name`/`size` tokens must be kept (they
  identify which stored file to read) while `starttime`/`endtime` are
  overwritten with what was actually requested.
- `ContentMgmt/download` (`POST /ISAPI/ContentMgmt/download`, tried
  during Phase 5) **ignores `starttime`/`endtime` entirely** and
  streams the whole segment file (~1GB for a ~1.5 hour segment on this
  DVR) — not usable for "download a clip." Downloads are built on RTSP
  playback instead (see Media bridge design below), which does respect
  a custom `starttime`.

## RTSP reference

- Live: `rtsp://user:pass@host:554/Streaming/Channels/<streamID>`
  (capital `Channels`), Digest auth, same realm as ISAPI HTTP.
- Playback: `rtsp://host/Streaming/tracks/<trackID>/?starttime=...&endtime=...&name=...&size=...`
  — this exact URI comes from a `CMSearch` match's `playbackURI`, not
  hand-constructed.

## Media bridge design

mediamtx is spawned as a child process by the backend (`MediaBridge` in
`app/mediabridge.py`). Its YAML config is generated at startup from the
live channel list and written to a gitignored runtime path (it embeds
DVR credentials in RTSP source URLs — must never be committed).

- **Live channels**: one static mediamtx path per stream
  (`ch{id}_main`/`ch{id}_sub`), `sourceOnDemand: true` so the DVR isn't
  connected to until a client actually watches — avoids holding N
  permanent RTSP connections to the DVR.
- **Playback**: mediamtx's **control API** (`127.0.0.1:9997`, `api:
  true` in the generated config) registers/deregisters paths at
  runtime — `POST /v3/config/paths/add/{name}` (JSON: `source`,
  `sourceOnDemand`), `DELETE /v3/config/paths/delete/{name}`. Each
  playback session gets a throwaway path created on
  `/api/playback/start` and removed on `/api/playback/stop`, since
  playback URLs are per-time-range and can't be pre-declared like live
  channels.
- **Output**: HLS on `:8888`, WebRTC on `:8889`. Frontend currently
  uses HLS via hls.js (works cross-browser without WebRTC signaling
  complexity); WebRTC paths are exposed by the API but not yet used.
- **RTSP re-serve** (`127.0.0.1:8554`, loopback-only) exists purely so
  the backend can capture download clips with ffmpeg — see "Clip
  download design" below. Not for LAN/browser use.
- Known gap: no TTL/GC for playback paths if a client abandons playback
  without calling stop (e.g. tab closed). Fine for single-user local
  use; revisit before packaging (Phase 6).

### H.265→H.264 transcode for channel 10

Chrome can't play H.265 (see "Known device quirks and bugs"), and
unlike the analog channels, channel 10 (IPCamera 02) has no H.264
option to switch to at all — see "IP-proxy channels (9/10)" above.
mediamtx can't transcode on its own, so `ch10_main`'s path has no
direct `source`; instead it uses mediamtx's `runOnDemand` hook (`source:
publisher`, runs only while a client is actually reading the path) to
spawn `ffmpeg -i <DVR RTSP H.265 source> -c:v libx264 ... -f rtsp
rtsp://127.0.0.1:8554/ch10_main` — pulling the real source and pushing
the re-encoded stream back into the same path over mediamtx's own
loopback RTSP re-serve (`_transcode_path` in `app/mediabridge.py`).

Scoped to just this one stream (`TRANSCODE_TO_H264 = {"ch10_main"}`),
not generalized to every H.265 stream: the live-view frontend only
ever requests each channel's `main` stream, so the analog channels'
H.265 sub-streams (never requested by the UI) don't need transcoding.

Verified by extracting frames from both the raw source and the
transcoded HLS output with `ffmpeg -frames:v 1` and comparing — same
content, correct colors, no corruption from the re-encode. Sustained
transcode speed measured at **~1.0-1.05x real-time** on the dev
machine (`veryfast` libx264 preset) — keeps up, but with little CPU
headroom; revisit the preset/resolution if this box is resource
constrained or multiple viewers watch channel 10 concurrently.

## Clip download design

`/api/download` doesn't use `ContentMgmt/download` (see ISAPI
reference above — it ignores the requested time range) or the DVR's
playback RTSP `endtime` (found unreliable, see below). Instead:

1. Fresh, nudged `CMSearch` to get a valid `name`/`size` token for the
   segment covering the requested window (same staleness/boundary
   handling as playback).
2. Rewrite that match's `playbackURI` (`_rewrite_playback_window` in
   `app/main.py`) to use the **caller's own** `starttime`, keeping the
   fresh `name`/`size`.
3. Register that as a dynamic mediamtx path (same mechanism as
   playback), then run `ffmpeg` against mediamtx's **RTSP re-serve**
   (not its HLS output) with `-t <duration>` to enforce the exact
   cutoff, `-c:v copy -an` (video-only, see below), writing an MP4.
4. Return the file (`FileResponse` with a `BackgroundTask` that
   deletes the temp file after it's sent), tear down the dynamic path.

Two dead ends hit along the way, kept here so they aren't retried:

- **Pulling directly from the DVR's playback RTSP with ffmpeg hung**
  (connected fine, got valid SDP, then zero packets until timeout) —
  even though mediamtx's RTSP client pulled the *identical* source
  URL without issue. Specific to how ffmpeg's RTSP client negotiates
  with this DVR's playback endpoint; not investigated further since
  routing through mediamtx works. So capture always goes DVR → mediamtx
  → (RTSP re-serve) → ffmpeg, never DVR → ffmpeg directly.
- **Capturing from mediamtx's HLS output** (instead of its RTSP
  re-serve) hit segment-pruning 404s mid-capture — LL-HLS prunes old
  segments faster than a capture can reliably keep up. RTSP re-serve
  has no such windowing.
- **Transcoding this DVR's G.711 audio to AAC for the mp4 container
  reliably hung ffmpeg** (packets stopped flowing entirely, even
  though video-only capture from the same source worked immediately).
  Not root-caused; downloads are video-only for now. Most channels
  here don't have audio enabled on their main stream anyway.
- The DVR **doesn't reliably stop at the RTSP source's `endtime`**
  once mediamtx is in the loop — a 30-second requested window kept
  streaming for 39+ seconds until the client's own timeout killed it.
  The real cutoff is enforced by ffmpeg's `-t`, not by trusting the
  DVR/mediamtx to stop on their own.

## IP-proxy channels (9/10)

Channels 9/10 on this DVR are ONVIF cameras proxied through it, not
analog inputs — discovered via a completely separate ISAPI list,
`/ISAPI/ContentMgmt/InputProxy/channels` (`InputProxyChannel` entries),
which `/ISAPI/System/Video/inputs/channels` (analog-only) never
returns. Their streaming-channel entries (`/ISAPI/Streaming/channels`)
also key back to the input differently — `dynVideoInputChannelID`
instead of `videoInputChannelID` — confirmed once these channels came
online (`app/main.py`: `_build_ip_channel`/`_streams_for_channel` keys
off whichever field is present).

Once discovered, these channels reuse the same channel-ID numbering
scheme as everything else (`_main_track_id` = `channel_id * 100 + 1`,
so channel 9 → track/stream `901`) and the same RTSP live path
(`rtsp://host:554/Streaming/Channels/901`) — live view, search
(`CMSearch`), playback, and download all work against them unmodified.

Two endpoints don't, though, both firmware-side on this DVR (not
account-privilege related — same read-only account works fine for
channels 1-4 on these same endpoints):

- **PTZ capabilities** (`/ISAPI/PTZCtrl/channels/9/capabilities`) →
  `400 badXmlContent`, not `404`. `get_ptz_capabilities` treats this
  the same as "no PTZ" rather than raising — but PTZ *control* for
  these channels works via a different endpoint anyway, see "PTZ for
  IP-proxy channels" below.
- **Snapshot** (`/ISAPI/Streaming/channels/901/picture`) → same `400
  badXmlContent`. Not fixed — `/api/snapshot?channelId=9` currently
  surfaces this as a raw 500. Revisit if snapshot support for these
  channels is needed; no workaround found yet.

Channel 10's video-encoding type (H.265) can't be switched to H.264
from the DVR's web UI even with a full admin account — confirmed the
field is disabled there regardless of privilege. Root cause: this
proxy channel's `InputProxyChannel` entry has `streamType: auto`,
meaning the DVR mirrors whatever encoding the camera itself is set to
rather than encoding the stream on its own hardware, unlike analog
channels where the DVR *is* the encoder. Not fixable on the camera
side either — no encoding menu in its companion app (Yoosee). Instead,
`ch10_main` is transcoded server-side (see "H.265→H.264 transcode for
channel 10" in the media bridge design below).

## PTZ for IP-proxy channels

Channels 9/10 got PTZ-capable cameras. The DVR's PTZ subsystem doesn't
model them at all, though — `/ISAPI/PTZCtrl/channels` (the full list,
no ID filter) only ever returns entries for the 4 analog channels.
Querying channel 9 directly is inconsistent: `capabilities` and
`status` both fail (`400`/`403`), but `presets` succeeds (returns an
empty list) — and, surprisingly, actually issuing a **continuous move**
command (`PUT /ISAPI/PTZCtrl/channels/9/continuous`) returns `200 OK`
*and* really moves the camera. This was confirmed by direct physical
observation (not just trusting the DVR's response, per this project's
usual verification standard) — an initial frame-comparison check
looked identical before/after, but that was almost certainly a false
negative from a low-texture scene, not a real absence of movement.

So: don't trust `/capabilities` or `/status` for these channels — they
don't reflect what `/continuous` can actually do. `_build_ip_channel`
(`app/main.py`) hardcodes `ptz.enabled = true` for any enabled
IP-proxy channel rather than relying on the broken capabilities check.

Sign convention: **positive `pan` turns the camera right**, confirmed
by observation for channel 9. Tilt/zoom sign follow the same
documented ISAPI convention (positive tilt up, positive zoom in) but
haven't been independently verified the same way.

Separately, both cameras were probed directly over ONVIF
(`GetCapabilities` against `http://192.168.25.20{1,2}:5000/onvif/device_service`,
no auth needed for this call) — they do advertise a PTZ service, at
`.../onvif/deviceio_service`. Not used by this project (the DVR
relay above works and needs no camera credentials, which we don't
have), but noted here since it confirms these are genuinely
PTZ-capable devices, not just a DVR fluke. That same probe also
turned up a device quirk worth flagging: the `DeviceIO` extension in
the capabilities response advertises a stale internal XAddr
(`http://192.168.1.33:5000/...`) instead of the camera's real
reachable IP — leftover config from before the camera moved onto this
subnet. Not relevant to PTZ, but a trap for any ONVIF client that
blindly follows advertised XAddrs instead of the known camera IP.

**API**: `PUT /api/ptz/{channelId}/continuous` (body: `pan`/`tilt`/`zoom`,
each -100..100) and `PUT /api/ptz/{channelId}/stop` — both just proxy
straight to the DVR's own endpoint (`ISAPIClient.ptz_continuous_move`/
`ptz_stop` in `app/isapi.py`), no special handling needed since the DVR
already does the right thing once you skip the broken capability
checks. The live-view overlay (`static/index.html`) shows a D-pad +
zoom buttons for any channel with `ptzEnabled`, using pointerdown/up to
start/stop continuous movement (hold to move, release to stop).

## Known device quirks and bugs

These are DVR-firmware behaviors (V4.30.300), not bugs in this
codebase — documented here so they don't get "fixed" again or
accidentally reintroduced.

- **Chrome/Chromium has no HEVC-via-MSE support.** H.265 live channels
  show a black frame in the browser (confirmed via
  `MediaSource.isTypeSupported('video/mp4; codecs=hvc1...')` →
  `false`); mediamtx correctly serves the HEVC HLS stream regardless
  (verified with `ffprobe`), so this isn't fixable in our pipeline —
  the channel's codec has to be H.264 for browser playback. See
  `MEMORY.md` for which channels are currently set to which codec.
- **`playbackURI` goes stale.** An hour-old one from an earlier
  `CMSearch` got `400 Bad Request` from the DVR's RTSP server.
  `/api/playback/start` always re-searches immediately before handing
  the URI to mediamtx rather than reusing a URI a user looked at
  minutes earlier.
- **`CMSearch` exact-boundary bug.** A search `startTime` that exactly
  equals a segment's start returns the *previous* segment instead of
  the requested one. Found by extracting an HLS frame with `ffmpeg
  -frames:v 1` and reading the DVR's own on-screen timestamp overlay —
  the only reliable way to verify what the DVR actually played, since
  ISAPI/RTSP responses alone don't prove it. Fixed by nudging the
  search start 2 seconds forward (`_nudge_time` in `app/main.py`)
  before re-searching, landing inside the segment instead of on its edge.
- **ISAPI timestamps aren't real UTC despite the "Z" suffix.** They're
  the DVR's own local wall-clock digits (WIB, UTC+7), unconverted —
  confirmed against `/ISAPI/System/time` (correctly NTP-synced) and a
  playback frame's on-screen overlay. Any code touching these
  timestamps (search results, playback requests, future snapshot/event
  timestamps) must read/write them as **literal digits** — regex
  extraction, plain string formatting — never through `Date`/
  `toISOString()`/timezone-aware datetime math, which silently applies
  the browser's or server's real timezone on top and only looks
  correct by accident when that timezone happens to be UTC.
