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

## Open questions to verify against the real device (Phase 0)

- [ ] Confirm ISAPI is reachable and enabled (`GET /ISAPI/System/deviceInfo`), and whether it's HTTP-only or HTTPS-only on this firmware.
- [ ] Confirm actual channel count/IDs in use (analog 1-8, plus any IP channels added) via `GET /ISAPI/System/Video/inputs/channels`.
- [ ] Confirm which channels report PTZ capability (`GET /ISAPI/PTZCtrl/channels/<ID>/capabilities`) — many analog Turbo HD setups have no PTZ camera attached.
- [ ] Confirm RTSP auth mode (basic vs digest) and exact stream path format for this firmware (`/Streaming/channels/<ID>01`).
- [ ] Confirm recording search response format (XML vs JSON) and whether `V4.30.300` supports `application/json` via `Accept` header (older firmwares are XML-only).
- [ ] Check whether digest auth requires a specific `WWW-Authenticate` quirk (some old Hikvision firmwares have known digest-auth bugs requiring workarounds — check when we hit real requests).

Phase 0 is a short, hands-on step against the physical unit (`curl`/Postman-style checks) before writing real backend code, so later phases build on confirmed facts instead of assumptions from datasheets.

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
