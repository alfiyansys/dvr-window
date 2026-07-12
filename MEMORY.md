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
      capability, RTSP auth/path, CMSearch format all confirmed (see
      `PLAN.md` "Phase 0 findings" for exact details).
- [ ] Re-verify channels 9/10 once back online.
- [ ] Phase 1 — backend core (not started).
