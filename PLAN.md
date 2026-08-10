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
| 6 | ⬜ next | Polish — mediamtx process supervision/health check is ✅ done: split into its own Swarm service, deployed to production (`sm-qohelet`/`sw-david01`/`daya-regia.invis`) and verified — both containers healthy on separate nodes, real traffic flowing, all 6 channels reaching live in a browser check against the real domain. Playback-path GC and the memory/CPU limit re-check are ✅ done — design below. Packaging is done and Docker-only by decision — see `ARCHITECTURE.md`. Event/alarm stream is **blocked**, not attempted: real-device recon found the ISAPI account gets `403 lowPrivilege` on both the push event stream and the poll-based motion-detection status, motion detection itself is disabled, and the DVR has no alarm inputs configured — nothing to build against under the current account/config. Design below. |
| 7 | ✅ done | Continuous playback across recording-segment boundaries — auto-advance into the next segment instead of freezing at the end of one; skip forward over a real recording gap instead of stopping. See `ARCHITECTURE.md` "Continuous playback across recording segments". |
| 8 | ✅ done | Day timeline scrubber for playback — horizontal bar showing the loaded day's recorded segments/gaps, click-to-seek, reusing the existing playback-start/gap-clamp mechanism. See `ARCHITECTURE.md` "Day timeline scrubber". |
| 9 | ✅ done | Single shared-key auth for the local UI + API + mediamtx's own HLS/WebRTC listeners (video bypasses FastAPI entirely, so protecting only the API wouldn't secure the live view). Design below, implementation details in `ARCHITECTURE.md` "Auth". |
| 10 | ✅ done | Live view overlay UX: fullscreen button in the detail modal, auto-reconnect on HLS stream error, stream status (live/reconnecting/error) surfaced in the modal. Design below. |
| 11 | ✅ done | Detect a *lagging* stream (still connected, no fatal hls.js error, but frames have stopped advancing) as a status distinct from live/reconnecting/error. Design below. |
| 12 | ✅ done | Fix silent live-view drift: status shows `live` while playback is steadily advancing but stuck well behind the actual live edge (confirmed via a real screenshot — DVR's burned-in timestamp ~26 min behind the wall clock while the badge stayed green). The Phase 11 watchdog only caught *frozen* frames, not this. Design below. |
| 13 | ✅ done | Per-camera network latency (`GET /api/ping`), shown next to each IP-proxy channel's status badge. Only channels with a network hop to measure get a number — analog channels (coax, no IP) never appear in the response. Design below. |
| 14 | ✅ done | Prioritize the focused camera's stream speed while its detail modal is open — throttle (not pause) the other grid cells' background streams so the focused one gets more client/network/DVR-link bandwidth. Duty-cycle mechanism, the `reconnecting…`-status bug found during verification, and a second stale-lag-timer bug found while re-verifying the fix are all fixed and confirmed against the real DVR (2026-08-10) — background cells held `live` across 5+ minutes/many duty cycles, Prev/Next handoff and modal close both restore correctly. Design below. |
| 15 | ⬜ next | Client-side real-time video enhancement for the focused/modal stream only (WebGL2 shader pipeline; user-selectable mode, staged incrementally toward an optional ML mode). Design below. |

Detailed findings for each completed phase (exact endpoints, bugs
found and fixed, design decisions) are in `ARCHITECTURE.md` rather than
duplicated here — this file tracks *what's done and what's next*, not
*how it works*.

## Phase 6 design: playback-path GC, memory/CPU re-check, event/alarm stream

Three remaining Phase 6 items, two done this round:

- **Playback-path GC** (done): a client abandoning playback without
  calling `/api/playback/stop` (closed tab, dead network) used to leak
  its mediamtx path forever — `ARCHITECTURE.md`'s own documented known
  gap. `MediaBridge` now tracks every path it registers and sweeps
  every 30s, cross-checking mediamtx's own `GET /v3/paths/list`
  readers to delete anything idle 60s+. Verified against the real DVR:
  an abandoned path was gone within ~90s, an actively-read path
  survived 110s+ untouched, explicit stop still removes immediately.
  Full design and verification detail in `ARCHITECTURE.md` under
  "mediamtx as a separate Swarm service" (the playback-path bullet).
- **Memory/CPU limit re-check** (done): the `384M`/`1.0` CPU limits
  were an unmeasured guess from before the Swarm split. Real-load
  testing (standalone compose against the real DVR, all 6 channels'
  LL-HLS connections plus a playback and download session) found
  mediamtx pinned at the ceiling continuously under just that load —
  already throttled, not merely close. Raised to `768M`/`1.5` CPU
  (roughly double the confirmed clean-connection floor). Full numbers,
  methodology, and the reconnect-churn caveat in `ARCHITECTURE.md`
  under "Memory/CPU limit re-check (Phase 6)".
- **Event/alarm stream** (blocked, not attempted): real-device recon
  against the actual DVR found the ISAPI account gets `403
  lowPrivilege` on both `/ISAPI/Event/notification/alertStream` (the
  push event stream) and the poll-based
  `.../motionDetection/status` alternative — the same class of
  privilege limit `MEMORY.md` already documents for `PUT` config
  changes, now also covering the one real event mechanism this
  firmware exposes. Motion detection is currently disabled on the DVR
  itself, and its IO alarm-input list is empty (no physical alarm
  inputs configured), so even a privilege upgrade alone wouldn't be
  enough today. Revisit if/when there's both an elevated account and
  motion detection turned on.

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

**Prev/Next layout (cosmetic-only)**: `overlayPrev`/`overlayNext` are
wrapped in their own `.actions-pager` div inside `.actions`
(`static/index.html`), with `display: flex; flex-direction: row`
outside the `min-width: 700px` media query so it always wins over the
sidebar's `.actions { flex-direction: column }` stacking — the two
buttons render as one row of compact `◂`/`▸` arrows instead of two
full-width stacked rows on desktop, with `aria-label`s replacing the
now-dropped "Prev"/"Next" text. Snapshot/Playback/Fullscreen/Close are
unaffected (still stack on desktop, flow normally on mobile). No
behavior change — button IDs and click handlers untouched.

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

**Fixed, two parts**, both in `setupHlsPlayer` (`static/index.html`):

1. `seekToLiveEdge()` — on `visibilitychange` returning to visible,
   seeks to `hls.liveSyncPosition` (confirmed present in the vendored
   hls.js 1.5.17 build, `static/vendor/hls.min.js`) instead of only
   re-arming the watchdog — snaps playback back to the live edge
   immediately on tab foreground rather than waiting for hls.js's own
   gradual catchup or for someone to notice a stale picture.
2. `checkDrift()` — an ongoing drift check as defense-in-depth for
   causes other than backgrounding, piggybacking on the existing
   `timeupdate` handler rather than a separate polling interval
   (matching Phase 11's own "costs nothing while healthy" reasoning):
   compares `hls.liveSyncPosition - video.currentTime` against a 20s
   threshold on every `timeupdate`; sustained past that, escalates
   through `lagging…` then a full rebuild via the same
   destroy-and-reconnect path Phase 10 already has, rather than a
   third recovery mechanism.

Verified against a real live stream from the real DVR (bare-metal
`run.sh`, alternate ports to not collide with the production Swarm
deployment already on this host): manually rewound a live cell's
`video.currentTime` by 25s via the browser console to simulate drift —
status correctly held `lagging…` while `currentTime` kept advancing
normally the whole time (not frozen), then escalated to a full rebuild
and recovered to `live` with a fresh low `currentTime`, fully
automatically. Separately confirmed the visibility-return snap: after
simulating drift then a hidden→visible cycle (same
`document.visibilityState` override trick already used for Phase 11's
own testing), `currentTime` jumped immediately back near the live edge
instead of continuing to crawl from the drifted position. No console
errors, other channels unaffected throughout.

## Phase 13 design: per-camera network latency

Requested as "ping in ms per camera." The DVR's own ISAPI has no such
metric on this firmware — checked directly against the real device:
`/ISAPI/ContentMgmt/InputProxy/channels/<id>/status` reports `online`/
`chanDetectResult` but no latency field, and `/ISAPI/System/Network/
ping`/`testNetworkDelay` both `404 Can't locate the url`. So this
measures the *backend's* own network path to each camera instead of
the DVR's — not identical if the DVR reaches a camera over a different
hop than the backend does, but the closest available proxy.

Only meaningful for IP-proxy channels (9/10 on this DVR) — analog
channels connect over coax, no IP hop to measure at all. Generic to
whatever a given DVR reports, not hardcoded to those two: `GET
/api/ping` (`app/main.py`) filters `app.state.channels` for whichever
ones have an `ipAddress` set, which `_build_ip_channel` populates from
`sourceInputPortDescriptor` (only present for IP-proxy channels,
already discovered dynamically per-DVR the same way channel names/
resolutions are) and `_build_channel` (analog) never sets — pointing
this at a different DVR with a different number of IP-proxy cameras at
different IPs needs zero code changes.

- **Metric**: plain TCP-connect time to each camera's own ONVIF manage
  port (`managePortNo`, already returned by the DVR's own channel
  list — 5000 on this deployment). Not raw ICMP: that needs
  `CAP_NET_RAW` on the container, a real infra change touching every
  deployment (Swarm, standalone Docker, bare-metal); a bare TCP socket
  needs no elevated privileges anywhere and is close enough to ICMP RTT
  on a LAN link. `_tcp_ping` (`app/main.py`) times a raw
  `asyncio.open_connection`, `None` on timeout/refused rather than
  raising — one unreachable camera shouldn't break the response for
  the others, pinged concurrently via `asyncio.gather`.
- **Frontend**: `pingLoop()` (`static/index.html`) polls `GET
  /api/ping` every 5s, writes into a `.ping` span next to each cell's
  status badge, keyed by `data-channel-id` (already set in `main()`
  for click handling). Channels absent from the response (analog) just
  keep an empty span — hidden entirely via a `.ping:empty` CSS rule
  rather than needing an explicit "not applicable" state.

Verified against the real DVR/cameras (bare-metal `run.sh`): `GET
/api/ping` returned real, plausible values for both IP-proxy channels
(e.g. `{"id": 9, "pingMs": 40.6}`, `{"id": 10, "pingMs": 39.1}` on one
call; single-digit ms on another — consistent with real jitter already
documented elsewhere for these same multi-hop-wireless channels) and
correctly omitted all 4 analog channels. In the browser, both IP-proxy
cells showed a live-updating `Xms` next to their status badge; all 4
analog cells showed nothing. No console errors across multiple poll
cycles.

## Phase 14 design: prioritize focused-camera stream speed (background throttling)

**Problem**: all 6 grid cells (`static/index.html`, `main()`) run their own
continuous `hls.js` instance all the time, even while the detail modal
(`openOverlay`) is open showing just one of them full-size.
`openOverlay` doesn't create a second player for the focused stream —
it relocates the *same* `<video>`/`hls` instance the grid cell already
owns into `#overlaySlot` (`slot.appendChild(video)`), so the focused
stream was never actually a separate, prioritizable thing from the
grid cell's own player. What competes with it is the other 5 cells'
players, still pulling segments continuously the whole time the modal
is open — client network/decode contention, and, for channels 9/10,
contention on the already-strained multi-hop wireless link back to the
DVR documented elsewhere in this file (see Phase 11/13 designs).

**Approach: throttle, not pause**, the non-focused cells while a modal
is open — duty-cycle their `hls.js` loading instead of stopping it
outright, so grid thumbnails stay semi-live instead of freezing on the
last frame, while cutting their steady-state bandwidth/CPU draw
sharply.

- New per-player controls on the object `setupHlsPlayer` already owns
  (`hls`, `video`) — `throttleBackground()` / `restoreForeground()`,
  called from `openOverlay`/`closeOverlay`/`showAdjacent` rather than
  duplicating hls.js lifecycle logic outside `setupHlsPlayer`.
- `throttleBackground()`: `hls.stopLoad()` + `video.pause()`
  immediately (stop new segment fetches, stop decode of whatever's
  already buffered), then a repeating timer — every `BG_OFF_MS`
  (10s), `startLoad()` + `play()` for `BG_ON_MS` (2s) to pull a fresh
  segment or two, then `stopLoad()` + `pause()` again. A ~20% duty
  cycle: enough to keep a thumbnail visibly current, far less constant
  draw than today's every-cell-always-on baseline. Exact numbers to be
  tuned against real measurements, not treated as final here.
- `restoreForeground()`: clear the duty-cycle timer, `startLoad()` +
  `play()`, back to normal continuous playback.
- Wiring: `openOverlay(cell, ...)` throttles every other `.cell`'s
  player; `closeOverlay()` restores all of them; `showAdjacent(step)`
  (Prev/Next) throttles the cell being left and restores the one being
  entered directly, instead of restoring everything then re-throttling
  — avoids a needless full-bandwidth blip on the outgoing cell mid-transition.
- **Must suppress the Phase 11/12 watchdogs while throttled** —
  `armLagWatchdog`/`checkDrift`/the `timeupdate` handler already exist
  specifically to detect and escalate a stalled stream, and the
  duty-cycle's "off" phase is a deliberate stall by that same
  definition. Without a guard, a throttled background cell would trip
  its own lag/drift detection and escalate into a full rebuild every
  duty cycle, defeating the point. Add a `backgrounded` flag on the
  player, checked the same way `document.visibilityState !== 'visible'`
  already gates `armLagWatchdog` — set on `throttleBackground()`,
  cleared on `restoreForeground()`.
- **Status label while throttled**: leave whatever the cell's
  `.status` last said (almost always `live`) rather than fabricating a
  new state — the stream genuinely is fine, just deliberately
  deprioritized by this app, not by anything wrong with it.

**Open risk to check before calling this done — channel 10's
on-demand transcode**: `ch10_main`'s path uses mediamtx's
`runOnDemand` hook to spawn the H.265→H.264 `ffmpeg` transcode only
while a client is actually reading the path (`ARCHITECTURE.md`,
"H.265→H.264 transcode for main streams"). If `hls.stopLoad()`
actually drops the browser's underlying HTTP connection to mediamtx
(not just pauses hls.js's own segment processing while a request stays
open), mediamtx may see zero active readers during every "off" phase
and tear the transcode down — meaning every `BG_ON_MS` burst pays a
fresh `ffmpeg` spin-up (already confirmed elsewhere to take a couple
of seconds) instead of resuming an already-warm stream, actively worse
than not throttling that one channel at all. Needs checking against
the real DVR (mediamtx's own logs, per this project's usual
verification standard) before shipping; if confirmed, channel 10
likely needs a longer, less frequent duty cycle (or exemption from
throttling entirely) rather than sharing the other channels' constants.

**Verification plan** (per `AGENTS.md` — against the real DVR, not
just reasoned about):

- Open the modal for one channel, confirm the other 5 cells'
  thumbnails keep updating roughly every `BG_OFF_MS` instead of
  freezing, and confirm (browser Network tab / `docker stats`) that
  segment-request volume and CPU from the throttled cells drop
  sharply.
- Confirm the focused stream doesn't regress — same live/lagging/error
  behavior as before this change, ideally recovering faster / staying
  steadier once the other 5 aren't competing for bandwidth, checked
  specifically on the wireless-hop channels (9/10) where contention
  would show up first.
- Confirm `showAdjacent` (Prev/Next) correctly hands throttling off
  the outgoing cell and onto the incoming one, with no cell left
  fully throttled after `closeOverlay()`.
- Confirm channel 10 specifically (the transcode risk above) — check
  mediamtx's logs for repeated `ffmpeg` spin-up/teardown during a
  modal session, not just that the picture looks fine.

**Verified against the real DVR (2026-08-10) — confirmed working, plus
one real bug found:**

- **Working as designed**: duty-cycle timing matches spec exactly —
  sampled `video.paused`/`currentTime` directly (not just read the
  code) on both a normal background cell and channel 10, both showed
  the expected ~10s-paused / ~2s-resumed-and-advancing pattern. The
  focused cell (`Teras`) kept advancing continuously and unthrottled
  throughout. `closeOverlay()` correctly restored all 5 background
  cells back to `live`.
- **Bug found — throttled cells get stuck on "reconnecting…"
  indefinitely, not just channel 10**: after a few minutes with a
  modal open, *all five* background cells (not only the wireless-hop
  9/10 ones) ended up permanently showing `reconnecting…` in the
  status badge, even though their video was demonstrably still alive
  and advancing every `BG_ON_MS` burst (confirmed via direct
  `currentTime` sampling — the picture itself was fine, only the label
  was wrong). Root cause, traced through both the code and mediamtx's
  own log:
  1. `bgOff()`'s `hls.stopLoad()` can itself trigger a fatal `hls.js`
     `NETWORK_ERROR` — mediamtx's log showed a muxer/on-demand-source
     teardown (`[muxer ch9_main] destroyed: muxer error...` →
     `[RTSP source] stopped: not needed by anyone`) lining up exactly
     with an off-cycle.
  2. The `Hls.Events.ERROR` handler (`static/index.html:541-559`) has
     **no `backgrounded` guard** — unlike the Phase 11/12 watchdogs and
     the `playing`/`timeupdate` handlers, which this phase's design
     *did* remember to guard (see the bullet above). So the fatal
     error unconditionally calls `setStatus('reconnecting…', ...)`.
  3. There is then **no path back to `live`** while still
     backgrounded: the only code that reasserts `live` (the
     `playing`/`timeupdate` handlers) explicitly `return`s early
     whenever `backgrounded` is true (by design, to avoid relabeling a
     deliberate pause as broken) — so once the error handler sets
     `reconnecting…`, nothing clears it until `restoreForeground()`
     runs at modal-close.
  
  This directly contradicts this phase's own stated goal ("leave
  whatever the cell's `.status` last said... the stream genuinely is
  fine") — in practice it does the opposite, since `stopLoad()` itself
  is exactly what's prone to causing that first fatal error.

**Fixed and re-verified against the real DVR (2026-08-10), in three commits
— the first attempt turned out to be wrong and needed a second pass:**

1. Guarded the `Hls.Events.ERROR` handler so a fatal error while
   `backgrounded` doesn't call `setStatus(...)`. **First attempt made it
   a full no-op instead** (skipped `startLoad()`/`recoverMediaError()`/
   rebuild too, not just the label) — re-verifying that version found it
   left `hls`'s underlying `MediaSource` permanently stuck after enough
   duty-cycle churn (`currentTime` frozen, unresponsive even to a manual
   `restoreForeground()` call), which is worse than a wrong label. Fixed
   by keeping the exact same tiered recovery (`startLoad()` →
   `recoverMediaError()` → full rebuild) unconditionally, and only
   wrapping the `setStatus(...)` calls (in the handler itself,
   `scheduleRestart()`, and the Safari-native fallback's `error`
   listener) in `if (!backgrounded)`. A rebuild triggered while
   backgrounded now also immediately re-`bgOff()`s so it doesn't blast
   at full speed until the next scheduled duty-cycle boundary.
2. `restoreForeground()` now explicitly calls `setStatus('live', 'ok')`
   + `armLagWatchdog()` right after `bgOn()`, instead of waiting for the
   next `playing`/`timeupdate` event — belt-and-suspenders against any
   stale label.
3. **Second bug found while re-verifying fix #1**: `throttleBackground()`
   never cancelled an already-in-flight lag-watchdog timer — one armed
   moments before backgrounding began would still fire mid-throttle,
   setting `lagging…` and then, 15s later, tearing down and rebuilding
   `hls` entirely for a stream that was only ever intentionally paused.
   Fixed by adding `clearLagTimers()` to `throttleBackground()`, the same
   cancellation the `visibilitychange`-hidden branch already did for the
   identical reason.
4. Re-verified end to end: modal left open 5+ minutes / many duty
   cycles, all five background cells held `live` throughout with
   `currentTime` independently confirmed still advancing each burst (no
   recurrence of the stuck-`reconnecting…` or frozen-`MediaSource`
   failure modes); Prev/Next handoff and modal close both restored
   every cell correctly.

## Phase 15 design: client-side real-time stream enhancement (focused stream only, staged toward ML)

Builds directly on Phase 14: once the focused camera's stream is the
one getting priority bandwidth/CPU while its modal is open, that same
single stream is also the only one where spending client GPU/CPU on
*enhancing* the picture (not just delivering it faster) is affordable.
Applying this to all 6 grid cells at once would compete with the exact
resource contention Phase 14 exists to relieve — so this is explicitly
scoped to the one `<video>` currently sitting in `#overlaySlot`, never
the grid.

**Architecture — one pipeline, pluggable processors, so "which method"
is a config choice, not a rewrite:**

- `openOverlay()` creates a WebGL2 `<canvas>` sized to match the
  `<video>` and stacks it directly over it in `#overlaySlot`; the
  `<video>` itself stays in the DOM and keeps decoding (it's the frame
  source) but becomes visually hidden, canvas shows the processed
  output. `closeOverlay()` and `showAdjacent()` tear the canvas down /
  recreate it against the new channel's video the same way Phase 14
  already re-targets `throttleBackground`/`restoreForeground` per cell
  — the two features share the modal's open/close/switch lifecycle
  hooks but don't otherwise interact (enhancement only ever touches
  whichever cell is currently *not* throttled).
- Frame feed via `video.requestVideoFrameCallback` (falls back to
  skipping enhancement entirely, not to a `requestAnimationFrame`
  polling loop, if unsupported — see fallback policy below) — fires
  once per actually-decoded frame, texture-uploads straight from the
  `<video>` element (`texImage2D` accepts a video element directly, no
  intermediate 2D-canvas copy needed).
- A small `EnhancementPipeline` abstraction: takes the uploaded frame
  texture, runs whichever **processor** is currently selected
  (`off` / `classical` / later `ml`), writes to the visible canvas.
  Processors are swappable behind this one interface specifically so
  the incremental stages below (classical now, ML later) don't require
  redoing the canvas/texture/lifecycle plumbing — only a new processor
  gets added each time.

**Method selector — user picks, not auto-decided:**

- A control in `.overlay-side` (alongside Snapshot/Playback/Fullscreen)
  cycling `Off` (default) / `Classical` — `AI` is added to this same
  list only once the ML stage below actually ships, not built as a
  disabled placeholder now (a "coming soon" option that does nothing
  is worse than no option). Choice persists in `localStorage`
  (`enhanceMode`, same pattern `static/auth.js` already uses for the
  auth key) — one global preference applied to whichever camera is
  currently focused, not remembered per-channel; simplest thing that
  works, revisit only if real usage shows people want different modes
  per camera (e.g. always-classical on the noisy IR channels, off on
  the already-sharp ones).
- `off` is a true passthrough (canvas layer not even created) — zero
  overhead for anyone who doesn't want this, and the default for
  everyone until they opt in.

**Classical processor (this phase's actual deliverable) — two cheap
single-pass shaders:**

- **Auto-levels / gamma boost**: stretches the frame's black/white
  point and applies a configurable gamma lift — targets this DVR's
  dark analog/IR-night footage specifically (real examples already
  documented elsewhere in this file: the wireless IP-proxy channels
  and channel 10's transcoded feed).
- **Unsharp mask**: a small-radius blur subtracted back from the
  original to boost edge contrast — targets the softness the
  H.265→H.264 transcode on channel 10 already introduces (`ARCHITECTURE.md`,
  "H.265→H.264 transcode for main streams").
- Both run as one combined WebGL2 fragment shader pass (not two
  separate render targets) — a single 1080p pass is comfortably 60fps
  on modest client hardware, so this is not a performance concern for
  one stream; keeping it one pass avoids the extra framebuffer
  ping-pong two separate passes would need.

**Fallback policy — enhancement is always optional, never
load-bearing**, matching this codebase's existing pattern for
non-critical browser-capability gaps (the Safari-native-HLS path
already accepts not being able to attach auth headers rather than
blocking playback): no WebGL2, no `requestVideoFrameCallback`, or any
runtime error inside a processor all fall back to plain unenhanced
`<video>` — never to a broken or blank picture, and never by retrying
in a loop.

**Incremental roadmap toward ML — staged so nothing downstream is
built before the stage that justifies it is proven:**

- **15.1 (this round's scope)** — the pipeline/canvas/lifecycle
  plumbing above, the mode selector (`Off`/`Classical` only), and the
  classical processor. Ships alone as a complete, useful feature —
  intentionally not blocked on any ML work below.
- **15.2 — ML groundwork, not yet user-facing**: pick a lightweight
  browser-capable model (a small super-resolution net like ESPCN/FSRCNN,
  or a denoise-focused one — final pick needs benchmarking against
  real footage from this DVR, not assumed) and a runtime
  (TensorFlow.js or ONNX Runtime Web, WebGL/WebGPU backend). Whatever
  is chosen must be **vendored locally** (`static/vendor/`,
  gitignored-model-weights-or-not decided at that time) per `AGENTS.md`'s
  existing no-CDN-dependency rule for frontend deps — the model weights
  themselves only fetched lazily when a user actually selects `AI`
  mode later, never on page load, so nobody pays that download for a
  feature they never turn on. This stage lands the `ml` processor
  behind a dev-only flag, not in the public selector yet, so real FPS/
  quality can be measured against this DVR's actual streams before
  committing to ship it.
- **15.3 — `AI` becomes a real selector option**, gated by a runtime
  capability/performance check (e.g. a brief benchmark pass on
  pipeline init) that auto-falls-back to `Classical` if the device
  can't sustain acceptable frame rate — the same "always have an
  accepted fallback" policy as WebGL2-unavailable above, applied to
  "WebGL2 exists but this device's GPU is too weak for the ML pass
  specifically." Requires real-device verification (per `AGENTS.md`)
  on more than one client, not just the dev machine, before this stage
  is considered done — ML inference cost varies far more by hardware
  than the classical shader pass does.
- **15.4 (explicit non-goal until 15.1-15.3 are proven)** — per-camera
  default modes, auto-switching heuristics (e.g. auto-`Classical` on
  known-dark channels), or any server-side involvement. Not speced
  further here; premature ahead of real usage data from the earlier
  stages.

**Verification plan** (per `AGENTS.md` — real DVR and real client
hardware, not just reasoned about):

- Confirm the classical pass visibly improves at least one genuinely
  dark/soft real feed from this DVR (before/after screenshots), not
  just that the shader compiles.
- Confirm zero measurable impact on the grid's other 5 cells or
  Phase 14's throttling behavior while enhancement runs in the modal.
- Confirm the selector persists across a page reload and correctly
  falls back to plain `<video>` when WebGL2/`requestVideoFrameCallback`
  is unavailable (test via a real capability gap, not just reading the
  code).
- Confirm Prev/Next (`showAdjacent`) correctly re-targets the canvas to
  the newly-focused channel's video with no stale frame or leaked
  WebGL context from the previous one.
- (15.2/15.3 only, once reached) confirm the ML processor's vendored
  weights load with no network calls beyond this LAN/the local
  service, and that the perf-fallback genuinely engages on a
  deliberately underpowered test client.

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.
- Bare-metal packaging (systemd unit, install script). Docker is the
  only supported deployment path — it's already built, working, and
  running in production (`docker-compose.yml` standalone,
  `docker-compose.swarm.yml` for the mediamtx-split Swarm setup).

## Next step

Phase 14 (prioritize focused-camera stream speed via background
throttling — design above) is next up for implementation, followed by
Phase 15's first stage (15.1: client-side classical enhancement for
the focused stream, with the ML stages 15.2-15.4 staged for later —
design above). Playback-path GC and the memory/CPU limit re-check are
done (see "Phase 6 design" above). Event/alarm stream is blocked on
DVR account privilege and config, not code — revisit if that changes.
No other Phase 6 items are currently open.
