# Hikvision Local Service — Linux Port (Plan)

Device identity, credentials, and current progress snapshot live in
`MEMORY.md`. This file is the architecture + phase plan + technical
reference (exact ISAPI/RTSP paths, protocol quirks) needed to keep building.

## Goal

Not a reverse-engineered clone of Windows `LocalServiceControl.exe`
(closed-source, undocumented wire protocol, no Windows box to sniff it,
no installer at hand). Instead: a **standalone Linux service + local web
UI** giving the same practical capabilities — live view, playback,
snapshot, download — built entirely on Hikvision's documented, open
protocols:

- **ISAPI** (HTTP/XML, digest auth) — control plane: channel info, recording search, snapshot, download, events.
- **RTSP** — media plane: live streams and time-ranged playback streams.

Rejected **HCNetSDK** (Hikvision's official Linux SDK): closed-source
blob, its own licensing, limited architecture support, Windows-shaped C
API that doesn't map cleanly to "run a local web service." ISAPI + RTSP
covers everything needed with a normal HTTP/RTSP client stack.

## Architecture

```
Browser (localhost:PORT)
   │  HTML/JS UI (live grid, playback timeline, download)
   ▼
Backend service (Python / FastAPI, app/)
   ├─ ISAPI client (app/isapi.py) ──HTTP digest──▶ DVR :80  (channels, search, snapshot, download)
   └─ Local web server: REST API + static frontend

Media bridge (mediamtx, sidecar process, app/mediabridge.py)
   DVR :554 (RTSP live + playback) ──▶ mediamtx ──▶ HLS/WebRTC ──▶ <video> in browser
```

Browsers can't play raw RTSP/H.264 elementary streams directly, so a
bridge is required. Using **mediamtx** (single Go binary, RTSP-in /
HLS+WebRTC-out, has a runtime control API for dynamic paths) instead of
writing our own RTSP-to-browser transcoder.

- **Backend** (`app/`): config loading (`config.py`), ISAPI client
  (`isapi.py`), mediamtx process + dynamic path management
  (`mediabridge.py`), FastAPI routes (`main.py`).
- **Frontend** (`static/`): plain HTML/JS, no build step. `index.html`
  (live grid), `playback.html` (search + play recordings). hls.js
  vendored locally (no CDN dependency).
- **Packaging** (not started): systemd service, install script.

## Milestones

| Phase | Status | What |
|---|---|---|
| 0 | ✅ done | Device recon — confirm ISAPI/RTSP paths, auth, channel/PTZ capability against the physical DVR |
| 1 | ✅ done | Backend core — config, ISAPI client, `/api/channels` |
| 2 | ✅ done | Live view — mediamtx bridge, `/api/streams`, live grid UI |
| 3 | ⬜ skipped | PTZ — no PTZ hardware attached to any active channel; revisit only if that changes |
| 4 | ✅ done | Playback & search — `/api/recordings`, `/api/playback/{start,stop}`, playback UI |
| 5 | ⬜ next | Snapshot & download |
| 6 | ⬜ not started | Polish/packaging — systemd service, playback-path GC, event/alarm stream, basic auth on the local UI |

### Phase 0 — device recon

- ISAPI: HTTP (not HTTPS-only), digest auth, **XML-only** responses (`Accept: application/json` is ignored on this firmware) — confirmed on `deviceInfo`, channel list, `CMSearch`.
- Channels: `GET /ISAPI/System/Video/inputs/channels` (analog inputs), `GET /ISAPI/Streaming/channels` (per-stream codec/resolution/audio), `GET /ISAPI/PTZCtrl/channels/<id>/capabilities` (PTZ). Current channel state is in `MEMORY.md`.
- RTSP: `rtsp://user:pass@host:554/Streaming/Channels/<streamID>` (capital `Channels`), Digest auth, same realm as ISAPI HTTP.
- `CMSearch`: `POST /ISAPI/ContentMgmt/search`, XML body (`searchID`, `trackList`, `timeSpanList`, `maxResults`, `searchResultPostion`). Paginated: re-issuing the identical request with the **same `searchID`** auto-advances to the next page (server tracks position). Each match includes a ready-to-use `playbackURI` under `/Streaming/tracks/<trackID>/`.
- Digest auth: no quirks — handles 10+ concurrent requests fine, standard RFC handshake, no special `qop`/`nc` workarounds needed.
- `<redacted-username>` account is **read/live-view only** — any `PUT` (e.g. changing a channel's codec) returns `403 lowPrivilege`. Config changes need either elevated privilege on this account or separate admin credentials.

### Phase 2 — live view

- mediamtx spawned as child process, config generated from the live channel list (`sourceOnDemand: true` so the DVR isn't connected to until someone's actually watching), HLS on `:8888`, WebRTC on `:8889`.
- Chrome/Chromium has **no HEVC-via-MSE support** — H.265 channels showed a black frame with `error` status until their codec was switched to H.264 on the DVR itself (done manually via the DVR menu, since the ISAPI account lacks privilege for that PUT). mediamtx needed no restart — it follows whatever codec the RTSP source actually sends.

### Phase 4 — playback & search

- `ISAPIClient.search_recordings()` loops `CMSearch` pages automatically (same-searchID pagination).
- **`playbackURI` goes stale**: an hour-old one got `400 Bad Request` from the DVR's RTSP server. `/api/playback/start` always re-searches immediately before handing the URI to mediamtx.
- mediamtx's **control API** (`127.0.0.1:9997`, `api: true` in generated config) registers/deregisters paths at runtime: `POST /v3/config/paths/add/{name}` (JSON: `source`, `sourceOnDemand`), `DELETE /v3/config/paths/delete/{name}`. Playback = one throwaway path per session, created on start, removed on stop.
- Known gap: no TTL/GC if a client abandons playback without calling stop (e.g. tab closed). Fine for single-user local use; revisit at Phase 6.
- **Two bugs found via user report** ("jam di playback list tidak sesuai dengan timestamp video"), both diagnosed by extracting an HLS frame with `ffmpeg -frames:v 1` and reading the DVR's own on-screen timestamp overlay (the only reliable way to verify what the DVR actually played — ISAPI/RTSP responses alone don't prove it):
  1. **CMSearch exact-boundary bug**: a search `startTime` that exactly equals a segment's start returns the *previous* segment instead. Fixed by nudging the search start 2 seconds forward (`_nudge_time` in `app/main.py`) before re-searching.
  2. **ISAPI timestamps aren't real UTC** despite the "Z" suffix — they're the DVR's own local wall-clock digits (WIB, UTC+7), unconverted (confirmed against `/ISAPI/System/time`, which is correctly NTP-synced). Fixed by reading/writing these strings as literal digits everywhere (regex extraction, plain string formatting) instead of routing them through `Date`/`toISOString()`/timezone-aware datetime math, which silently applies the browser's or server's real timezone on top and only looked right by accident when the test browser happened to be UTC. **Applies to all future code touching ISAPI timestamps** (Phase 5 download-by-time-range, any future event timestamps).

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.

## Next step

Phase 5 — snapshot (`/ISAPI/Streaming/channels/<ID>/picture`) and download-by-time-range (ISAPI content-mgmt download, proxied through the backend so the browser can save-to-disk).
