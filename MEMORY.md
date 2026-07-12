# Project Memory

Durable context for this project, tracked in git so it's available on every
device this repo is cloned to. (Distinct from Claude Code's own per-machine
memory system — this is a plain file, intentionally.)

For the detailed step-by-step plan and full Phase 0 protocol findings
(exact ISAPI paths, RTSP path/auth, CMSearch behavior), see `PLAN.md`.
This file is the short-form "pick up where we left off" summary.

## Target device

- Model: DS-7208HQHI-K1/E (Turbo HD hybrid DVR, 8 analog channels + IP channel slots)
- Serial: E0820210814CCWRG52897731WCVU
- Firmware: V4.30.300, build 210520 (encoder V5.0, build 210412)
- Hardware version: 0xc0ec220
- Device name on unit: "AL-Home Net DVR"
- LAN IP: <redacted-dvr-host> (assume static reservation; re-verify if unreachable)

## Credentials

**Never commit passwords to this repo.** ISAPI/RTSP account username is
`<redacted-username>` — password is known to the project owner and must be kept
in a local, gitignored file (e.g. `.env` or `config.local.yaml`) once the
backend exists, not in any tracked file or commit message.

## Key architecture decisions

- Building on **ISAPI (HTTP/XML) + RTSP** — Hikvision's documented, open
  protocols — not the closed-source HCNetSDK, and not a reverse-engineered
  clone of the Windows `LocalServiceControl.exe` (undocumented wire
  protocol, no Windows machine available to sniff it, no installer at hand).
- Browsers can't play raw RTSP, so a media bridge (mediamtx or go2rtc,
  RTSP-in / WebRTC+HLS-out) sits between the DVR and the browser UI.
- Backend: Python service exposing a local web UI + REST/WebSocket API,
  talking ISAPI to the DVR for control (channels, PTZ, search, snapshot,
  download) and pointing the media bridge at RTSP URLs for video.
- PTZ is an **optional, capability-gated** feature, not a required v1
  milestone — see below.

## Device state as of 2026-07-12

- 4 active analog channels: 1 Teras, 2 Car Port, 3 Garasi, 4 Ruang Tamu.
  Channel 1 main stream is H.264, channels 2-4 main streams are H.265.
  Full per-channel stream/resolution/codec table is in `PLAN.md`.
- Channels 5-8: no video input connected.
- Channels 9-10: exist (likely IP-camera channel slots on this hybrid DVR)
  but were **offline/disconnected** at last check — protocol shape not
  yet verified, don't assume it matches the analog channels.
- No PTZ hardware currently attached to any active channel (`enabled=false`
  on all four via `/ISAPI/PTZCtrl/channels/<id>/capabilities`).

## Progress

- [x] Phase 0 — device recon: ISAPI reachability, channel list, PTZ
      capability, RTSP auth/path, CMSearch format, digest-auth behavior
      (no quirks, standard client library works, handles 10+ concurrent
      requests fine) all confirmed (see `PLAN.md` "Phase 0 findings" for
      exact details).
- [ ] Re-verify channels 9/10 once back online.
- [x] Phase 1 — backend core: FastAPI app (`app/`), digest-auth ISAPI
      client (`app/isapi.py`, httpx + xmltodict), config loading
      (`config.yaml` for non-secrets, `HIKVISION_PASSWORD` env var for
      the credential via `.env`, see `.env.example`). `/api/channels`
      and `/api/device` verified end-to-end against the real device.
- [x] Phase 2 — live view: mediamtx sidecar (`app/mediabridge.py`) bridges
      RTSP → HLS/WebRTC, static grid UI at `/static/index.html`. All 4
      active channels (Teras, Car Port, Garasi, Ruang Tamu) verified
      live in Chrome after user switched channels 2-4 from H.265 to
      H.264 on the DVR (Chromium has no HEVC-via-MSE support — see
      `PLAN.md` Phase 2 findings). Channel encoding on the DVR is now:
      **all 4 active channels' main streams are H.264** (sub-streams may
      still be H.265, not yet checked/used by the UI).

- [x] Phase 4 — playback & search: `/api/recordings` (CMSearch, paginated)
      and `/api/playback/{start,stop}` (dynamic mediamtx paths via its
      control API on `:9997`). Verified live in Chrome against real
      recordings. Known gap: no auto-cleanup if a client abandons
      playback without calling stop — fine for now, revisit at Phase 6.

## Important device quirk: ISAPI timestamps aren't real UTC

The DVR's ISAPI/CMSearch timestamps carry a "Z" (UTC) suffix but are
actually **its own local wall-clock digits (WIB, UTC+7), unconverted**.
Confirmed by extracting an HLS playback frame and reading the DVR's
own on-screen timestamp overlay against what was requested. Any code
touching these timestamps must treat them as opaque local digits, not
true UTC — never round-trip them through JS `Date`/`toISOString()` or
Python timezone-aware datetime math, or you'll silently get a 7-hour
(or browser-timezone-dependent) offset. See `PLAN.md` Phase 4 findings
for the full story and the fix in `static/playback.html` / `app/main.py`.

Also: CMSearch has a boundary bug — a search `startTime` that exactly
equals a segment's start returns the *previous* segment instead.
`/api/playback/start` works around it by nudging the search start 2
seconds forward before re-searching.

## Known account limitation

`<redacted-username>` is a **read/live-view-only** account — confirmed it can
GET device info, channels, PTZ caps, and CMSearch, but a `PUT` to change
streaming channel config (e.g. codec) returns `403 lowPrivilege`. Any
future feature needing remote config changes (not just reading) will
hit this same wall — either request elevated privilege for this account
or get a separate admin credential for those specific calls.

## Running the backend

```
python3 -m venv .venv --without-pip   # this box has no system pip/ensurepip
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in HIKVISION_PASSWORD
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8896
```
