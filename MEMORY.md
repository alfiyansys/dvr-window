# Project Memory

Durable context for this project, tracked in git so it's available on every
device this repo is cloned to. (Distinct from Claude Code's own per-machine
memory system — this is a plain file, intentionally.) Short-form "pick up
where we left off" — device identity and current state only. See
`PLAN.md` for the phase roadmap/status, `ARCHITECTURE.md` for technical
design and protocol/device-quirk reference, and `AGENTS.md` for
setup/run instructions and conventions if you're an agent working in
this repo.

## Target device

- Model: DS-7208HQHI-K1/E (Turbo HD hybrid DVR, 8 analog channels + IP channel slots), name on unit "AL-Home Net DVR"
- Serial: E0820210814CCWRG52897731WCVU · Firmware: V4.30.300 build 210520 (encoder V5.0) · Hardware: 0xc0ec220
- LAN IP: <redacted-dvr-host> (re-verify if unreachable)
- ISAPI/RTSP account: `<redacted-username>` — **read/live-view only**, no privilege for config changes (`PUT` → `403 lowPrivilege`)

## Credentials

**Never commit passwords to this repo.** Password for `<redacted-username>` goes
in a local, gitignored `.env` (`HIKVISION_PASSWORD=...`, see `.env.example`).

## Device state (as of 2026-07-17)

- 4 active analog channels, all main streams now H.264 (2-4 were switched from H.265 manually via the DVR menu, needed for Chrome playback — see `ARCHITECTURE.md` "Known device quirks and bugs"): 1 Teras, 2 Car Port, 3 Garasi, 4 Ruang Tamu. Sub-streams may still be H.265, not yet used by the UI. Channels 5-8: no video input.
- Channels 9-10 are now **online**: ONVIF-proxied IP cameras (`/ISAPI/ContentMgmt/InputProxy/channels`, not the analog `/ISAPI/System/Video/inputs/channels` list) — 9 "IPCamera 01" at `<redacted-camera-host>:5000`, 10 "IPCamera 02" at `<redacted-camera-host>:5000`. Channel 9's main stream is H.264 (browser-playable); channel 10's is H.265 (black frame in Chrome, same known quirk as the analog channels — can't fix from this read-only account). See `ARCHITECTURE.md` "IP-proxy channels (9/10)" for the app-side support and its gaps.
- No PTZ hardware attached to any active channel — Phase 3 skipped.

## Progress

- [x] Phase 0 (device recon), 1 (backend core), 2 (live view), 4 (playback & search), 5 (snapshot & download) — see `PLAN.md` Milestones table for details.
- [x] Channels 9/10 (IP-proxy) support added to `/api/channels` and the live-view media bridge — 2026-07-17.
- [~] Phase 3 (PTZ) — skipped, no hardware to support it.
- [ ] Phase 6 (packaging) — next up.
- [ ] Snapshot for channels 9/10 is broken on this firmware (`400 badXmlContent` from `/ISAPI/Streaming/channels/90x/picture`) — not fixed, see `ARCHITECTURE.md`.

## Before touching ISAPI timestamps or playback

Read `ARCHITECTURE.md` → "Known device quirks and bugs" first — fake-UTC
timestamps, a `CMSearch` boundary bug, and `playbackURI` staleness have
already bitten this project once each. Setup/run instructions are in
`AGENTS.md`.
