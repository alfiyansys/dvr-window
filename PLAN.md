# DVR Window (Plan)

Roadmap and phase status. Technical design (protocols, media bridge,
known device quirks) is in `ARCHITECTURE.md`; current device state and
credentials pointer are in `MEMORY.md`.

## Goal

A **standalone Linux service + local web UI** replacing Windows'
closed-source `LocalServiceControl.exe` local-service plugin — live
view, playback, snapshot, download, PTZ — built entirely on Hikvision's
documented, open protocols (ISAPI + RTSP), not any closed-source SDK.
Rationale in `ARCHITECTURE.md`.

## Milestones

| Phase | Status | What |
|---|---|---|
| 0 | ✅ done | Device recon — confirm ISAPI/RTSP paths, auth, channel/PTZ capability against the physical DVR |
| 1 | ✅ done | Backend core — config, ISAPI client, `/api/channels` |
| 2 | ✅ done | Live view — mediamtx bridge, `/api/streams`, live grid UI |
| 3 | ✅ done | PTZ — channels 9/10 (IP-proxy cameras) got PTZ hardware; `/api/ptz/{channelId}/{continuous,stop}` + live-view D-pad. Analog channels 1-4 still have no PTZ hardware. |
| 4 | ✅ done | Playback & search — `/api/recordings`, `/api/playback/{start,stop}`, playback UI |
| 5 | ✅ done | Snapshot & download — `/api/snapshot`, `/api/download` |
| 6 | ⬜ next | Polish/packaging — systemd service, playback-path GC, event/alarm stream |
| 7 | ✅ done | Continuous playback across recording-segment boundaries — auto-advance into the next segment instead of freezing at the end of one; skip forward over a real recording gap instead of stopping. See `ARCHITECTURE.md` "Continuous playback across recording segments". |
| 8 | ✅ done | Day timeline scrubber for playback — horizontal bar showing the loaded day's recorded segments/gaps, click-to-seek, reusing the existing playback-start/gap-clamp mechanism. See `ARCHITECTURE.md` "Day timeline scrubber". |
| 9 | ✅ done | Single shared-key auth for the local UI + API + mediamtx's own HLS/WebRTC listeners (video bypasses FastAPI entirely, so protecting only the API wouldn't secure the live view). Design below, implementation details in `ARCHITECTURE.md` "Auth". |

Detailed findings for each completed phase (exact endpoints, bugs
found and fixed, design decisions) are in `ARCHITECTURE.md` rather than
duplicated here — this file tracks *what's done and what's next*, not
*how it works*.

## Phase 9 design: shared-key auth

One `AUTH_KEY` (`.env`, fail-closed like the DVR credentials) protects
two independent layers, since video flows DVR → mediamtx → browser
directly, never through FastAPI:

- **API**: a single `@app.middleware("http")` in `app/main.py` checks
  `X-Auth-Key` against `AUTH_KEY` for any `/api/*` path — chosen over
  per-route `Depends()` since there's no `APIRouter` today, just 14
  flat routes on the bare `app`; middleware is the one place that
  can't be forgotten on a new route. `/`, `/playback`, `/static/*`,
  `/healthz` stay open (no session/cookie/redirect machinery needed).
- **mediamtx**: `authInternalUsers` in the generated config, with a
  fixed username (`viewer` — not a secret, `AUTH_KEY` is) granted
  `read`/path:`""` using `AUTH_KEY` as the password. Critical detail
  confirmed against mediamtx's own stock config: this list *replaces*
  the defaults rather than merging, so the loopback-exempt `api`-only
  entry mediamtx ships by default must be re-added explicitly or our
  own backend's control-API calls (`add_playback_path`/
  `remove_playback_path`, `127.0.0.1:9997`) start getting `401`s.
  `capture_clip`'s ffmpeg RTSP pull (`127.0.0.1:8554`) does need
  updating though — RTSP read falls under the `read` action, not the
  api-only carve-out.
- **Frontend**: new shared `static/auth.js` (avoids duplicating
  security-relevant code across `index.html`/`playback.html`) —
  `ensureAuthKey()` shows a small styled login form if no key is
  cached, validating it live against `/api/device` before accepting;
  `authFetch()` wraps `fetch` with the header and clears+re-prompts on
  a `401` (debounced so several concurrent failing requests don't
  cause a flicker loop); `hlsXhrSetup()` attaches the same key as
  Basic Auth to hls.js's requests via its `xhrSetup` hook (verified
  against the actual vendored hls.js 1.5.17, not assumed). The
  Safari-native-HLS fallback path can't attach custom headers — an
  accepted, documented limitation of that already-secondary path, not
  fixed.

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.

## Next step

Phase 6 — packaging: systemd service + install script, playback-path
garbage collection (see `ARCHITECTURE.md` known gaps), event/alarm
stream, basic auth on the local UI.
