# Project Memory

Durable context for this project, tracked in git so it's available on every
device this repo is cloned to. (Distinct from Claude Code's own per-machine
memory system — this is a plain file, intentionally.) Short-form "pick up
where we left off" — architecture, phase plan, and full technical/protocol
findings live in `PLAN.md`.

## Target device

- Model: DS-7208HQHI-K1/E (Turbo HD hybrid DVR, 8 analog channels + IP channel slots), name on unit "AL-Home Net DVR"
- Serial: E0820210814CCWRG52897731WCVU · Firmware: V4.30.300 build 210520 (encoder V5.0) · Hardware: 0xc0ec220
- LAN IP: <redacted-dvr-host> (re-verify if unreachable)
- ISAPI/RTSP account: `<redacted-username>` — **read/live-view only**, no privilege for config changes (`PUT` → `403 lowPrivilege`)

## Credentials

**Never commit passwords to this repo.** Password for `<redacted-username>` goes
in a local, gitignored `.env` (`HIKVISION_PASSWORD=...`, see `.env.example`).

## Device state (as of 2026-07-12)

- 4 active analog channels, all main streams now H.264 (2-4 were switched from H.265 manually via the DVR menu, needed for Chrome playback — see `PLAN.md` Phase 2): 1 Teras, 2 Car Port, 3 Garasi, 4 Ruang Tamu. Sub-streams may still be H.265, not yet used by the UI. Channels 5-8: no video input.
- Channels 9-10 exist (likely IP-camera slots on this hybrid DVR) but were **offline** at last check — re-verify before assuming their protocol shape matches the analog channels.
- No PTZ hardware attached to any active channel — Phase 3 skipped.

## Progress

- [x] Phase 0 (device recon), 1 (backend core), 2 (live view), 4 (playback & search) — see `PLAN.md` Milestones table for details.
- [~] Phase 3 (PTZ) — skipped, no hardware to support it.
- [ ] Phase 5 (snapshot & download) — next up.
- [ ] Phase 6 (packaging).
- [ ] Re-verify channels 9/10 once back online.

## Gotchas to remember before touching ISAPI timestamps or playback

- DVR's ISAPI/CMSearch timestamps are labeled "Z" (UTC) but are actually its own **local wall-clock digits (WIB, UTC+7), unconverted**. Never round-trip them through `Date`/`toISOString()`/timezone-aware datetime math — read/write them as literal digits. Full story in `PLAN.md` Phase 4.
- CMSearch has a boundary bug: a search `startTime` exactly matching a segment's start returns the *previous* segment. Workaround: nudge the search start a couple seconds forward.
- Recording `playbackURI`s go stale (an hour-old one got `400` from the DVR) — always re-search right before playing, don't cache and reuse.

## Running the backend

```
python3 -m venv .venv --without-pip   # this box has no system pip/ensurepip
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in HIKVISION_PASSWORD
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8896
```
