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

```mermaid
flowchart TB
    Browser["Browser (localhost:PORT)<br/>live grid · playback timeline · download"]

    subgraph Backend["Backend service (Python / FastAPI, app/)"]
        WebServer["Local web server<br/>REST API + static frontend"]
        ISAPIClient["ISAPI client<br/>(app/isapi.py)"]
    end

    DVR_HTTP["DVR :80 — ISAPI<br/>channels · search · snapshot · download"]

    Bridge["Media bridge: mediamtx<br/>sidecar process (app/mediabridge.py)"]
    DVR_RTSP["DVR :554 — RTSP<br/>live + playback streams"]

    Browser <-->|HTTP| WebServer
    WebServer --> ISAPIClient
    ISAPIClient <-->|HTTP digest| DVR_HTTP
    DVR_RTSP <-->|RTSP| Bridge
    Bridge -->|HLS / WebRTC| Browser
```

Browsers can't play raw RTSP/H.264 elementary streams directly, so the
media bridge is a separate path from the ISAPI control-plane calls —
`<video>` in the browser talks to mediamtx directly (not proxied
through the FastAPI backend). Using **mediamtx** (single Go binary,
RTSP-in / HLS+WebRTC-out, has a runtime control API for dynamic paths)
instead of writing our own RTSP-to-browser transcoder.

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
- Known gap: no TTL/GC for playback paths if a client abandons playback
  without calling stop (e.g. tab closed). Fine for single-user local
  use; revisit before packaging (Phase 6).

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
