# Hikvision Local Service — Linux Port (Plan)

## Target device

- Model: **DS-7208HQHI-K1** (Turbo HD hybrid DVR, 8x HD-TVI/analog channels + up to 12 additional IP channels)
- Serial: E0820210814CCWRG52897731WCVU
- Firmware: V4.30.300, build 210520

This is not a pure IP NVR — analog PTZ (if any) is controlled by the DVR itself via UTC (up-the-coax) commands, not directly by the camera. IP channels (if added) are controlled the normal ISAPI way. Both paths go through the same DVR ISAPI/RTSP endpoints, so the app doesn't need to special-case analog vs IP channels — the DVR abstracts that.

## Goal (per discussion)

Not a byte-for-byte reverse-engineered clone of Windows `LocalServiceControl.exe` (closed-source, undocumented wire protocol, no Windows box available to sniff it). Instead: a **standalone Linux service + local web UI** that gives the same practical capabilities — live view, playback, PTZ, snapshot, download — built entirely on Hikvision's **documented, open protocols**:

- **ISAPI** (HTTP/XML or HTTP/JSON REST, digest auth) — control plane: device/channel info, PTZ, recording search, download, event/alarm stream.
- **RTSP** — media plane: live streams and time-ranged playback streams.

This avoids depending on any closed-source Hikvision binary (HCNetSDK), keeps the app portable across Linux architectures (x86_64/ARM), and keeps licensing clean.

## Why not HCNetSDK

HCNetSDK Linux exists but is a closed-source blob with its own licensing, limited architecture support, and a Windows-shaped C API that doesn't map cleanly to "run a local web service." ISAPI + RTSP gives ~everything needed (per architecture decision already made) with a normal HTTP/RTSP client stack.

## High-level architecture

```
Browser (localhost:PORT)
   │  HTML/JS UI (live grid, PTZ joystick, playback timeline, download)
   ▼
Backend service (Python)
   ├─ ISAPI client  ──HTTP digest──▶  DVR :80   (channels, PTZ, search, snapshot, download, events)
   ├─ RTSP handled indirectly via media bridge, not fetched raw in the backend
   └─ Local web server (REST + WebSocket for events/status)

Media bridge (mediamtx, sidecar process)
   DVR :554 (RTSP live + playback) ──▶ mediamtx ──▶ WebRTC/HLS/MSE ──▶ <video> in browser
```

Browsers can't play raw RTSP/H.264 elementary streams directly, so a bridge is required. Using an existing, well-maintained bridge (mediamtx or go2rtc — both single Go binaries, RTSP-in / WebRTC+HLS-out) instead of writing our own RTSP-to-browser transcoder.

## Components

1. **Backend service** (Python, FastAPI or Flask)
   - Config: DVR host/port/credentials, list of channels, RTSP port, web UI port.
   - ISAPI client: reuse/adapt an existing library (e.g. `hikvisionapi` on PyPI) rather than writing digest-auth XML parsing from scratch; wrap it for our needs.
   - Endpoints for: channel list/capabilities, PTZ (continuous move + presets), recording search, snapshot, download (proxy ISAPI content-mgmt download), alarm/event stream (Server-Sent Events or WebSocket relayed from ISAPI's long-poll alert stream).
   - On startup, tells mediamtx (via its config/API) which DVR RTSP URLs to expose as local sources.

2. **Media bridge** (mediamtx, run as child process / systemd sidecar)
   - Source: `rtsp://user:pass@dvr:554/Streaming/channels/<ID>01` (main stream) and `...<ID>02` (substream) for live.
   - Playback: RTSP playback URL with `starttime`/`endtime` params for recorded segments.
   - Output to browser: WebRTC (low latency, preferred) with HLS/MSE fallback.

3. **Frontend** (static HTML/JS/CSS served by backend)
   - Live view grid (1/4/8/16 layout), per-channel stream select (main/sub).
   - PTZ control overlay (only shown/enabled for channels that report PTZ capability).
   - Playback: calendar/timeline picker → recording search (ISAPI CMSearch) → play matching segment.
   - Download button for a selected time range.

4. **Packaging**: systemd user service, single `config.yaml`, run on `localhost:<port>` (default TBD, e.g. 8896 — avoiding assumptions about any specific "expected" port since none is documented).

## Phase 0 findings (device recon, in progress)

Device: <redacted-dvr-host>, ISAPI reachable, digest auth confirmed working with `<redacted-username>`.

- `GET /ISAPI/System/deviceInfo` confirms model/firmware match, response is **XML only** (no JSON via Accept header on this firmware).
- `GET /ISAPI/System/Video/inputs/channels` — 8 analog input slots, only 4 have video connected:
  | ID | Name | Main stream | Sub stream | Audio |
  |----|------|------|------|-------|
  | 1 | Teras | 101: H.264 1920x1080 | 102: H.265 960x576 | disabled |
  | 2 | Car Port | 201: H.265 1920x1080 | 202: H.265 960x576 | enabled |
  | 3 | Garasi | 301: H.265 1280x720 | 302: H.265 960x576 | enabled |
  | 4 | Ruang Tamu | 401: H.265 1280x720 | 402: H.265 960x576 | enabled |
  | 5-8 | — | `NO VIDEO` | — | — |
  Mixed codec: channel 1 main is H.264, channels 2-4 main are H.265 — media bridge must support both.
- **CMSearch confirmed**: `POST /ISAPI/ContentMgmt/search` with an XML `CMSearchDescription` body (`trackID`, `timeSpanList`, `maxResults`, `searchResultPostion`). Response is **XML only** (`Accept: application/json` is ignored, same as deviceInfo). Response is paginated (`responseStatusStrg=MORE` when more results exist beyond `maxResults`) — re-issuing the identical request with the **same `searchID`** returns the next page automatically (server tracks position per searchID; client doesn't need to manage offsets manually). Each match includes a ready-to-use `playbackURI`, e.g. `rtsp://<redacted-dvr-host>/Streaming/tracks/101/?starttime=20260707T062453Z&endtime=20260707T075140Z&name=...&size=...` — note the path is `/Streaming/tracks/<trackID>/` (not `/Streaming/Channels/` used for live) — so Phase 4 can hand this URI straight to the media bridge instead of constructing playback URLs manually. Recorded codec for channel 1 (track 101) is `H.264-BP` (Baseline Profile), matching its live main-stream codec.
- **RTSP confirmed**: `rtsp://<user>:<pass>@<redacted-dvr-host>:554/Streaming/Channels/<streamID>` (capital `Channels`, e.g. `101`, `102`, `201`...). Auth is **Digest** (verified via raw `OPTIONS` request — `WWW-Authenticate: Digest realm="cdedce8cf482442bb5b60fda"`, same realm as ISAPI HTTP). No credentials → `401` on `OPTIONS`. Verified both H.264 (channel 101) and H.265 (channel 201, `hevc` + `pcm_mulaw` audio) decode fine via `ffprobe -rtsp_transport tcp`. Minor harmless ffmpeg warning on HEVC (`PPS id out of range: 0`) — cosmetic, doesn't block playback, shouldn't matter for mediamtx (repackages, doesn't decode).
- **Channels 9 and 10 also exist** (beyond the 8 analog inputs — these would be the hybrid DVR's IP-camera channel slots) but are **currently disconnected/offline**. Not yet queried via ISAPI (likely under `/ISAPI/ContentMgmt/InputProxy/channels` for IP channel management, separate from `/ISAPI/System/Video/inputs/channels` which only covers analog). Need to re-check once they're back online — don't assume their stream/PTZ shape until then.
- `GET /ISAPI/PTZCtrl/channels/<1-4>/capabilities` — all four report `enabled=false`, `controlProtocol=UTC`. **No PTZ camera currently attached to any active channel.** Phase 3 (PTZ) is not needed for current hardware; keep it in the plan as an optional/gated feature only (UI should hide PTZ controls when `enabled=false`), not a required v1 milestone.

## Open questions to verify against the real device (Phase 0)

- [x] Confirm ISAPI is reachable and enabled, HTTP (not HTTPS-only) on this firmware — confirmed.
- [x] Confirm actual channel count/IDs in use — confirmed, 4 active analog channels (see findings above); channels 9/10 exist but offline, needs re-check later.
- [x] Confirm which channels report PTZ capability — confirmed, none of the 4 active channels have PTZ attached.
- [x] Confirm RTSP auth mode and exact stream path format — confirmed, Digest auth, `/Streaming/Channels/<streamID>` (see findings above).
- [x] Confirm recording search response format — confirmed, XML-only (see findings above); also confirmed pagination behavior and that the server returns ready-to-use playback URIs.
- [x] Check for digest-auth quirks — none found. 10 concurrent digest-authenticated requests to `deviceInfo` all succeeded (~20-48ms each), server supports keep-alive (`timeout=60, max=99`), and standard RFC digest handshake worked across every endpoint tested (deviceInfo, channels, PTZ, CMSearch, RTSP OPTIONS) with no special `qop`/`nc`/session workarounds needed. A standard HTTP digest-auth client library is sufficient.
- [ ] Re-check channels 9/10 (likely `/ISAPI/ContentMgmt/InputProxy/channels`) once they're back online.

Phase 0 is a short, hands-on step against the physical unit (`curl`/Postman-style checks) before writing real backend code, so later phases build on confirmed facts instead of assumptions from datasheets.

## Phase 2 findings (live view, in progress)

- **mediamtx bridge works end-to-end**: backend spawns mediamtx as a child process (`app/mediabridge.py`), generates its config from the live channel list (RTSP source per stream, `sourceOnDemand: true` so the DVR isn't connected to until a client actually watches), exposes HLS (`:8888`) and WebRTC (`:8889`). Verified live in an actual Chrome browser via the static grid UI (`static/index.html`, `/api/streams`) — channel 1 (H.264) renders live video successfully.
- **Browser HEVC/H.265 playback doesn't work in Chrome**: channels 2-4 (Car Port, Garasi, Ruang Tamu) fail to play — confirmed root cause is the browser, not our pipeline: `MediaSource.isTypeSupported('video/mp4; codecs=hvc1...')` returns `false` in Chrome/Chromium on this Linux box (HEVC decode isn't available via MSE — a licensing limitation of Chromium, not a bug in mediamtx or our code; mediamtx correctly serves the HEVC HLS stream, confirmed earlier via `ffprobe`).
- **Fix decided**: change channels 2-4's live/recording codec from H.265 to H.264 via ISAPI (`PUT /ISAPI/Streaming/channels/<id>`, confirmed H.264 is an available option via `/ISAPI/Streaming/channels/<id>/capabilities`). **Blocked**: the `<redacted-username>` account got `403 lowPrivilege` / `subStatusCode: lowPrivilege` on the PUT — this account is read/live-view-only, not authorized for remote config changes. User will change the codec manually via the DVR's own on-screen menu (Configuration → Video/Audio) instead. **Follow-up**: once changed, re-verify channels 2-4 render in the browser (repeat the same `/static/index.html` check), and re-run the Phase 0 streaming-channel query to refresh the recorded codec facts in this doc.

## Milestones

**Phase 0 — Device recon** (no app code)
Verify all "Open questions" above against the physical DVR. Produce a short findings note (exact ISAPI paths/response formats that work).

**Phase 1 — Backend core**
Config loading, ISAPI client wrapper, `/api/channels` endpoint returning real channel list + capabilities from the device.

**Phase 2 — Live view**
Stand up mediamtx as sidecar, wire DVR RTSP channels as sources, serve a live-view grid page in the browser (WebRTC first, HLS fallback).

**Phase 3 — PTZ**
Continuous-move + stop + presets via ISAPI, joystick UI overlay, gated by per-channel capability check from Phase 1.

**Phase 4 — Playback & search**
ISAPI recording search (`CMSearch`), timeline/calendar UI, play a selected segment through mediamtx using RTSP playback URL with time range.

**Phase 5 — Snapshot & download**
Snapshot endpoint (`/ISAPI/Streaming/channels/<ID>/picture`), download-by-time-range proxy, save-to-disk in browser.

**Phase 6 — Polish / packaging**
systemd service file, install script, alarm/event stream (motion/etc.) surfaced as live notifications in the UI, basic auth for the local web UI itself (since it holds DVR credentials).

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.

## Next step

Run Phase 0 against the physical DS-7208HQHI-K1 (need its LAN IP + admin credentials) to confirm the open questions above, then start Phase 1.
