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
| 6 | ⬜ next | Polish/packaging — systemd service, playback-path GC, event/alarm stream. mediamtx process supervision/health check (see design below, found via a real production incident) is ✅ done: split into its own Swarm service, deployed to production (`sm-qohelet`/`sw-david01`/`daya-regia.invis`) and verified — both containers healthy on separate nodes, real traffic flowing, all 6 channels reaching live in a browser check against the real domain. Remaining Phase 6 items (systemd, playback-path GC, event/alarm stream, memory-limit re-check) still open. |
| 7 | ✅ done | Continuous playback across recording-segment boundaries — auto-advance into the next segment instead of freezing at the end of one; skip forward over a real recording gap instead of stopping. See `ARCHITECTURE.md` "Continuous playback across recording segments". |
| 8 | ✅ done | Day timeline scrubber for playback — horizontal bar showing the loaded day's recorded segments/gaps, click-to-seek, reusing the existing playback-start/gap-clamp mechanism. See `ARCHITECTURE.md` "Day timeline scrubber". |
| 9 | ✅ done | Single shared-key auth for the local UI + API + mediamtx's own HLS/WebRTC listeners (video bypasses FastAPI entirely, so protecting only the API wouldn't secure the live view). Design below, implementation details in `ARCHITECTURE.md` "Auth". |
| 10 | ✅ done | Live view overlay UX: fullscreen button in the detail modal, auto-reconnect on HLS stream error, stream status (live/reconnecting/error) surfaced in the modal. Design below. |
| 11 | ✅ done | Detect a *lagging* stream (still connected, no fatal hls.js error, but frames have stopped advancing) as a status distinct from live/reconnecting/error. Design below. |
| 12 | ⬜ next | Fix silent live-view drift: status shows `live` while playback is steadily advancing but stuck well behind the actual live edge (confirmed via a real screenshot — DVR's burned-in timestamp ~26 min behind the wall clock while the badge stayed green). The Phase 11 watchdog only catches *frozen* frames, not this. Design below. |

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

## Phase 10 design: overlay fullscreen + stream auto-reconnect

- **Fullscreen button**: new button in `.overlay-box .label .actions`
  (`static/index.html`, alongside Snapshot/Playback/Prev/Next/Close),
  calling `requestFullscreen()` on `.overlay-box` itself rather than
  the bare `<video>` — the overlay already puts label/PTZ pad/zoom
  controls beside the video (`.overlay-side`), and those need to stay
  reachable while fullscreen, not get replaced by the browser's
  native video-only fullscreen chrome. Toggle the button's label/icon
  off a `fullscreenchange` listener rather than tracked state, so it
  stays correct however fullscreen was exited (button click, `Esc`,
  browser chrome). iOS Safari has no `Element.requestFullscreen`
  (video-only `webkitEnterFullscreen`) — needs an explicit fallback
  or an accepted-limitation note, confirm against a real iOS device
  before deciding which.

- **Auto-reconnect on stream error**: today `hls.on(Hls.Events.ERROR,
  ...)` (`static/index.html`, in `main()`) only flips the per-cell
  `.status` text to "error" on a fatal error and stops — the feed
  stays dead until a manual page reload. Recovery needs to branch on
  `data.type` per hls.js's own documented pattern:
  - `NETWORK_ERROR` → `hls.startLoad()`.
  - `MEDIA_ERROR` → `hls.recoverMediaError()`.
  - anything else fatal → destroy and recreate the `Hls` instance,
    with a backoff (start ~2s, cap ~30s, reset on the next successful
    `MANIFEST_PARSED`) so a rebooting DVR doesn't get hammered at full
    speed.
  - Must keep working on whichever `<video>` is currently live,
    including one already moved into `#overlaySlot` — `openOverlay()`
    relocates the real `<video>` DOM node (not a clone), so reconnect
    logic has to act on the existing `hls`/video reference in place,
    not assume it's still a child of `.cell`.
  - The Safari-native-HLS fallback path (no hls.js, plain `video.src
    = hlsUrl`) has no `Hls.Events.ERROR` to hook — reconnect there via
    the video element's own `error` event, reassigning `video.src`
    after the same backoff.
  - Give "reconnecting…" its own status state, distinct from the
    initial "connecting…" and a terminal "error" — so glancing at the
    grid shows actively-retrying vs. actually stuck.

Confirmed against the real DVR by actually killing mediamtx (SIGSTOP,
~90-100s) and separately by stopping the whole backend, both with the
browser tab left open. That testing found `startLoad()` alone doesn't
actually recover a `NETWORK_ERROR`: if mediamtx tore down the HLS
muxer session while it was down, `startLoad()` keeps retrying the same
now-dead session id forever (permanent `401`) — it never re-requests
the manifest to pick up a fresh session, so the feed stayed on
"reconnecting…" indefinitely even after mediamtx came back. Fix: a
`consecutiveErrors` counter gives a given fatal-error kind the cheap
in-place fix (`startLoad`/`recoverMediaError`) only once per outage;
any fatal error after that escalates straight to the full
destroy-and-rebuild path, which does re-fetch the manifest from
scratch. Recovered automatically within ~10-20s of mediamtx/the
backend coming back, both times.

The overlay modal (and fullscreen, since the sidebar stays in the DOM
there) also mirrors the grid cell's live/reconnecting/error status
next to the channel name, via a `MutationObserver` on the cell's
`.status` span rather than threading channel state through
`setupHlsPlayer`, which has no notion of the overlay.

## Phase 11 design: lagging-stream detection

A stream can be "connected" with no fatal hls.js error yet still
useless — frames stopped advancing (DVR-side encoder hiccup, a slow
upstream link) without hls.js's own buffer/network logic ever
declaring it fatal. Previously that read as "live" indefinitely.

- **Detection**: `setupHlsPlayer` (`static/index.html`) watches
  `video.currentTime` via two chained one-shot timers reset on every
  `timeupdate` — `armLagWatchdog()`, cleared and re-armed on each
  timeupdate rather than a polling loop, so it costs nothing while the
  stream is healthy. If `LAG_STATUS_MS` passes with no `timeupdate` at
  all, status flips to "lagging…" (distinct from "reconnecting…",
  which means hls.js already declared the stream fatally broken); if
  it's still stuck `LAG_REBUILD_MS` after that, it escalates to the
  same destroy-and-rebuild-with-backoff path Phase 10 already uses for
  fatal errors, rather than inventing a third recovery mechanism.
- **Reasserting "live"**: every `timeupdate` also explicitly sets
  status back to `live`, not just re-arms the timer — otherwise a
  stream that stutters (keeps progressing, just slower than
  `LAG_STATUS_MS`) gets stuck showing "lagging…" forever, since each
  fresh timeupdate cancels the pending escalation without ever
  reverting the label. Found this for real against channel 9/10's
  H.265→H.264 transcode, which shows real `dup=`/`drop=` counters
  climbing in ffmpeg's own log under load without ever fully stalling.
- **Threshold tuning — multi-hop wireless**: two of this deployment's
  cameras (the IP-proxy channels, 9/10) reach the DVR over a multi-hop
  wireless link rather than a wired one, so multi-second jitter is
  normal and usually self-heals. Rebuilding the local HLS pipeline
  doesn't fix a slow upstream wireless hop — it would just add local
  churn on top of an already-strained link. Set generously to avoid
  that: `LAG_STATUS_MS` = 10s (show "lagging…"), `LAG_REBUILD_MS` =
  15s more with zero progress (25s total) before forcing a rebuild. A
  genuinely dead stream still gets caught, just without being
  trigger-happy about brief wireless hiccups.
- **"live" only from an actual playing frame**: `MANIFEST_PARSED`
  (hls.js) / `loadedmetadata` (Safari-native fallback) used to set
  "live" directly — both fire well before frames actually render, so
  that was a claim the lag watchdog couldn't tell apart from the real
  thing. Caught this for real too: a rebuild on channel 9/10 landed
  `MANIFEST_PARSED` with the manifest structure in place but zero
  media actually flowing, and the grid showed "live" for a stalled
  video (`paused === true`, `currentTime` stuck at 0) until the
  watchdog eventually caught up ~10-20s later. Moved the one place
  "live" gets set to the `playing` event instead — standard, fires
  identically whichever transport is driving the `<video>` element,
  and is the actual "frames are flowing" signal. Before that point the
  status now honestly stays "connecting…".
- **Backgrounded-tab false positives**: browsers throttle
  timers/media callbacks for hidden tabs, which looks identical to a
  real stall. `armLagWatchdog()` refuses to arm at all while
  `document.visibilityState !== 'visible'`, and a page-level
  `visibilitychange` listener clears any pending timers on hide and
  re-arms fresh (not mid-countdown) on return, so switching tabs
  doesn't flag every camera as lagging the moment you tab back.
- Verified against the real DVR/network per `AGENTS.md` (not just
  reasoned about): the initial version was caught live in the grid
  showing channel 9/10 genuinely stuck on "lagging…" without ever
  recovering, which is what surfaced both the reassert-live gap and
  the too-tight threshold above; the "live"-from-`playing` fix was
  likewise caught live, from a rebuild that reported "live" for a
  stream that had never actually started playing. One branch is
  *not* independently confirmed this way: the browser-automation tab
  used for this testing reports `document.visibilityState` as
  `'hidden'` (no real OS window focus in that environment), so the
  visibility guard itself was observed correctly refusing to arm —
  but that also means the final "still stuck after `LAG_REBUILD_MS`
  more, rebuild" escalation was only exercised by forcing
  `visibilityState` via a JS override for the test, not by a genuine
  multi-hop-wireless dropout lasting the full ~25s. That escalation
  reuses the exact `hls.destroy()`/`scheduleRestart()` path Phase 10
  already proved against real outages, so risk is low, but worth a
  real 25s+ dropout check the next time one happens naturally.

## Phase 6 design: mediamtx process supervision & health check

Triggered by a real production incident (Swarm deployment on
`sm-qohelet`/`sw-david01`, 2026-08-04): `mediamtx` was OOM-killed
inside the (then-shared) container at ~2026-08-03 16:05 UTC and sat as
an unreaped zombie for ~15 hours, completely undetected. HLS/WebRTC/
mediamtx's own control API all stopped accepting connections — live
view was fully dead — while `/healthz` kept returning `{"status":
"ok"}` (it only ever proved the FastAPI process itself was alive) and
Swarm kept reporting the task `1/1 Running`, since PID 1 (FastAPI)
never exits just because its mediamtx sidecar did. Only found by
chance during an unrelated manual check, not by any alerting.

**Fixed**: split mediamtx into its own Swarm service instead of a
`subprocess.Popen` child of the FastAPI process — an OOM-killed
*container* is something Swarm already detects and acts on
(`restart_policy: condition: on-failure`), closing the actual gap this
incident exposed at the platform level instead of reimplementing
subprocess supervision in Python. Deployed to the real cluster and
verified (2026-08-04): both services converged healthy on separate
worker nodes, mediamtx's own `HEALTHCHECK` passing, real LAN traffic
flowing, all 6 channels reaching live in a browser check against the
real domain. Full design — config delivery, the auth model change, the
two empirical findings that ruled out a pure env-var config, the
add-vs-replace path-registration bug this caught, placement
constraints — is in `ARCHITECTURE.md` "mediamtx as a separate Swarm
service", not duplicated here.

Still open, unrelated to the split itself: re-checking each
container's memory limit against real peak usage (the original `384M`
guess was for both processes conflated together; splitting at least
makes this measurable per-service now) and surfacing a log line if
mediamtx ever exits unexpectedly.

## Phase 12 design: fix silent live-view drift (advancing but stale)

Found from a real screenshot (2026-08-04 17:13 desktop time): the
"Car Port" channel's overlay showed status `live` (green) while the
DVR's own burned-in on-screen timestamp read `16:47:21` — about 26
minutes behind the desktop's system clock (`17:13:47`) at the moment
of the screenshot. Not a frozen frame — `video.currentTime` was
genuinely still advancing, just from a position stuck well behind the
actual live edge.

**Why Phase 11's watchdog doesn't catch this**: it only tracks whether
`video.currentTime` is *advancing* via `timeupdate`
(`armLagWatchdog`/`clearLagTimers` in `static/index.html`) — not
whether the content itself is close to real time. Steady `timeupdate`
events reassert `live` (`static/index.html:419-421`, deliberately
added in Phase 11 so a merely-stuttering stream doesn't get stuck
showing "lagging…" forever) even when what's advancing is a large
backlog permanently behind the live edge. Phase 11 solved "frozen
frames masquerading as live"; this is a different failure mode it was
never designed to catch — "steadily advancing frames masquerading as
live."

**Most likely trigger**: tab backgrounding. Chrome throttles decode/
rendering hard on hidden tabs; on `visibilitychange` back to visible
(`static/index.html:423-424`), the code only re-arms the lag
watchdog — it never seeks the video forward, so playback just resumes
crawling forward from wherever it stalled, stuck behind by however
much piled up while the tab was hidden.

**Confirmed (user report): it does eventually self-correct** if the
tab is left active for a while, without a full rebuild — consistent
with hls.js's own internal low-latency live-sync catchup logic
(`lowLatencyMode: true`, already set in `start()`) eventually kicking
in on its own. This matters for the fix: the drift is real but not
permanently stuck the way a genuinely dead stream is, so escalating
straight to a full destroy-and-rebuild (Phase 10's mechanism) would be
needless churn for something that already self-heals given time — the
fix should just make the snap-back happen immediately instead of
waiting on it.

**Fix, two parts** (not yet implemented):

1. On `visibilitychange` returning to visible, seek to
   `hls.liveSyncPosition` (confirmed present in the vendored hls.js
   1.5.17 build, `static/vendor/hls.min.js`) instead of only re-arming
   the watchdog — snaps playback back to the live edge immediately on
   tab foreground rather than waiting for hls.js's own gradual catchup
   or for someone to notice a stale picture.
2. An ongoing drift check as defense-in-depth for causes other than
   backgrounding (e.g. a real network slowdown that lets the buffer
   grow without hls.js's fatal-error logic ever tripping): compare
   `hls.liveSyncPosition - video.currentTime` against a threshold; if
   too large for too long, treat as `lagging` even though frames are
   technically still advancing, escalating through the same
   lag-watchdog path Phase 11 already has rather than inventing a
   third mechanism.

Not yet decided: the exact drift threshold for part 2, and how often
to check it (piggybacking on the existing `timeupdate` handler is the
obvious first choice over a separate polling interval, matching Phase
11's own "costs nothing while healthy" reasoning).

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.

## Next step

Phase 12 — fix the silent live-view drift bug (design above), since
it's a currently-visible correctness issue on a real deployment.
Remaining Phase 6 items after that: systemd service + install script,
playback-path garbage collection (see `ARCHITECTURE.md` known gaps),
event/alarm stream, memory-limit re-check.
