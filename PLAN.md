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
| 6 | ✅ done | Polish — mediamtx process supervision/health check is ✅ done: split into its own Swarm service, deployed to production (`sm-qohelet`/`sw-david01`/`daya-regia.invis`) and verified — both containers healthy on separate nodes, real traffic flowing, all 6 channels reaching live in a browser check against the real domain. Playback-path GC and the memory/CPU limit re-check are ✅ done — design below. Packaging is done and Docker-only by decision — see `ARCHITECTURE.md`. Event/alarm stream is deferred — needs DVR-side account privilege and config changes outside this codebase; see Non-goals. Design below. |
| 7 | ✅ done | Continuous playback across recording-segment boundaries — auto-advance into the next segment instead of freezing at the end of one; skip forward over a real recording gap instead of stopping. See `ARCHITECTURE.md` "Continuous playback across recording segments". |
| 8 | ✅ done | Day timeline scrubber for playback — horizontal bar showing the loaded day's recorded segments/gaps, click-to-seek, reusing the existing playback-start/gap-clamp mechanism. See `ARCHITECTURE.md` "Day timeline scrubber". |
| 9 | ✅ done | Single shared-key auth for the local UI + API + mediamtx's own HLS/WebRTC listeners (video bypasses FastAPI entirely, so protecting only the API wouldn't secure the live view). Design below, implementation details in `ARCHITECTURE.md` "Auth". |
| 10 | ✅ done | Live view overlay UX: fullscreen button in the detail modal, auto-reconnect on HLS stream error, stream status (live/reconnecting/error) surfaced in the modal. Design below. |
| 11 | ✅ done | Detect a *lagging* stream (still connected, no fatal hls.js error, but frames have stopped advancing) as a status distinct from live/reconnecting/error. Design below. |
| 12 | ✅ done | Fix silent live-view drift: status shows `live` while playback is steadily advancing but stuck well behind the actual live edge (confirmed via a real screenshot — DVR's burned-in timestamp ~26 min behind the wall clock while the badge stayed green). The Phase 11 watchdog only caught *frozen* frames, not this. Design below. |
| 13 | ✅ done | Per-camera network latency (`GET /api/ping`), shown next to each IP-proxy channel's status badge. Only channels with a network hop to measure get a number — analog channels (coax, no IP) never appear in the response. Design below. |
| 14 | ✅ done | Prioritize the focused camera's stream speed while its detail modal is open — throttle (not pause) the other grid cells' background streams so the focused one gets more client/network/DVR-link bandwidth. Duty-cycle mechanism, the `reconnecting…`-status bug found during verification, and a second stale-lag-timer bug found while re-verifying the fix are all fixed and confirmed against the real DVR (2026-08-10) — background cells held `live` across 5+ minutes/many duty cycles, Prev/Next handoff and modal close both restore correctly. Design below. |
| 15.1 | ⬜ next | Classical real-time stream enhancement for the focused/modal stream only — WebGL2 shader pipeline (auto-levels/gamma + unsharp mask combined pass), `Off`/`Classical` selector, always-optional fallback to plain `<video>`. First, independently-shippable increment of the Phase 15 enhancement pipeline — not blocked on any ML work below. Design below. |
| 15.2 | ⬜ later | ML groundwork for stream enhancement, not yet user-facing — pick and vendor a lightweight browser-capable model + runtime (`static/vendor/`, no CDN), land it as a dev-only `ml` processor behind a flag so real FPS/quality can be measured against this DVR's actual streams before committing to ship it. Depends on 15.1's pipeline/canvas/lifecycle plumbing. Design below. |
| 15.3 | ⬜ later | `AI` becomes a real, user-facing selector option — gated by a runtime capability/performance check that auto-falls-back to `Classical` on a device too weak for the ML pass. Depends on 15.2 having already proven the model/runtime choice on real hardware. Design below. |
| 16 | ✅ done | Self-heal `mediamtx` live-view paths after it restarts independently of `dvr-window` — closes the "Known gap" from the Phase 6 split (`ARCHITECTURE.md`), which previously required a manual `dvr-window` restart to recover. Triggered by a real incident (2026-08-10, `sm-qohelet`/`sw-david01`): `mediamtx` was OOM-killed (exit 137) under a stale resource limit, lost all path registrations, and stayed unreachable — read by users as an endless reconnect loop — until manually forced. Implemented and verified (2026-08-10): recreated the exact incident locally (`docker-compose.yml`'s network-mode split, killed and fully recreated the `mediamtx` container while `dvr-window` kept running) — confirmed the fresh container came up with zero paths, then self-healed within one 30s sweep with no `dvr-window` restart, HLS confirmed actually serving again afterward. Design below. |
| 17.1 | ⬜ next | Browser-side FPS/perf instrumentation for the live grid — no such tooling exists yet, and Phase 15.1's own "zero measurable impact on the grid" verification item was never completed for exactly that reason. Foundational: nothing else in Phase 17 can be honestly verified without it. Design below. |
| 17.2 | ⬜ later | Low-risk grid tuning: hls.js buffer/back-buffer config, staggered initial connection storm, CSS containment on grid cells. Depends on 17.1 for before/after numbers. Design below. |
| 17.3 | ⬜ later | Backend prerequisite for 17.4 — verify real per-channel `sub`-stream codec against the DVR, and drop `_build_paths`'s H.265 transcode restriction to `_main`-only paths so a switch to `sub` in the grid doesn't ship an unplayable H.265 stream to Chrome. Design below. |
| 17.4 | ⬜ later | Grid cells switch from `main` to `sub` (the actual decode-cost fix — CSS scaling doesn't reduce browser decode work, so all 6 cells currently decode full-resolution for thumbnail-sized boxes), modal keeps `main`, via `hls.loadSource()` source-swap on open/close rather than a second instance or a full rebuild. Depends on 17.3. Design below. |

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
- **Event/alarm stream** (deferred, not attempted): real-device recon
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
makes this measurable per-service now) — done, see "Memory/CPU limit
re-check (Phase 6)" in `ARCHITECTURE.md` — and the path-loss gap itself
(Phase 16 design below), which is the sharper version of "surfacing a
log line if mediamtx ever exits unexpectedly": logging alone wouldn't
have prevented the 2026-08-10 incident, only reconciliation would.

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
5. **Third bug, reported by the user in real use (2026-08-10)**:
   background cells going visibly black during a modal session, still
   black for a moment after closing it. Root cause: the
   `Hls.Events.ERROR` handler's final escalation tier — a full rebuild
   (`hls.destroy()` + new `Hls()` + `attachMedia()`) — ran immediately
   even while backgrounded. Fix #1 above made sure this tier still
   *runs* while backgrounded (rather than leaving the stream stuck),
   but running it blanks the video to black immediately, since a fresh
   `MediaSource` starts with zero buffered data — and while
   backgrounded, that fresh instance only gets `BG_ON_MS` (2s) per duty
   cycle to load anything before being paused again, often not enough
   for even one frame, so the cell can stay visibly black for many
   cycles. Fixed by deferring the rebuild itself: a `pendingRebuild`
   flag is set instead of rebuilding immediately, and `restoreForeground()`
   performs the actual rebuild only once the cell is about to be looked
   at again — until then the stale (but never-destroyed) `hls` instance
   just sits there showing its last good frame, frozen rather than
   black. Applied the same treatment to the Safari-native fallback's
   `error` listener (reassigning `video.src` has the same blanking
   effect as a full rebuild). Verified against the real DVR by forcing
   a genuine, sustained fatal error on a backgrounded cell (an XHR
   interception redirecting that channel's requests to a closed port,
   not just reasoning about it) for 215+ seconds: the video stayed
   frozen on its last good frame throughout — same non-black pixel
   sample the entire time — with `status` correctly staying `live`
   throughout rather than flashing `reconnecting…`; closing the modal
   correctly triggered the deferred rebuild, visibly (and correctly)
   showing `reconnecting…` while the still-simulated failure persisted,
   then recovering cleanly once the simulated failure was lifted.

## Phase 15.1 design: classical stream enhancement (pipeline + shader)

Builds directly on Phase 14: once the focused camera's stream is the
one getting priority bandwidth/CPU while its modal is open, that same
single stream is also the only one where spending client GPU/CPU on
*enhancing* the picture (not just delivering it faster) is affordable.
Applying this to all 6 grid cells at once would compete with the exact
resource contention Phase 14 exists to relieve — so this is explicitly
scoped to the one `<video>` currently sitting in `#overlaySlot`, never
the grid.

**Architecture — one pipeline, pluggable processors, so "which method"
is a config choice, not a rewrite** (this shared plumbing is what makes
15.2/15.3 additive later rather than a rewrite):

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
  15.2/15.3 below don't require redoing the canvas/texture/lifecycle
  plumbing — only a new processor gets added each time.

**Method selector — user picks, not auto-decided:**

- A control in `.overlay-side` (alongside Snapshot/Playback/Fullscreen)
  cycling `Off` (default) / `Classical` — `AI` is added to this same
  list only once 15.3 actually ships, not built as a disabled
  placeholder now (a "coming soon" option that does nothing is worse
  than no option). Choice persists in `localStorage` (`enhanceMode`,
  same pattern `static/auth.js` already uses for the auth key) — one
  global preference applied to whichever camera is currently focused,
  not remembered per-channel; simplest thing that works, revisit only
  if real usage shows people want different modes per camera (e.g.
  always-classical on the noisy IR channels, off on the already-sharp
  ones).
- `off` is a true passthrough (canvas layer not even created) — zero
  overhead for anyone who doesn't want this, and the default for
  everyone until they opt in.

**Classical processor (this sub-phase's actual deliverable) — two
cheap single-pass shaders:**

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

**Verification plan** (per `AGENTS.md` — real DVR and real client
hardware, not just reasoned about):

- Confirm the classical pass visibly improves at least one genuinely
  dark/soft real feed from this DVR, not just that the shader compiles.
  Prefer measuring this objectively over eyeballing before/after
  screenshots: capture a raw frame from the stream (ffmpeg snapshot, or
  the app's own Snapshot button) and run the *same* GLSL math (auto-levels/
  gamma + unsharp mask, `static/enhance.js`'s `ENHANCE_FRAGMENT_SRC`) as a
  small Python/numpy script against it, rather than trying to align two
  separately-captured screenshots — this gives a byte-comparable "what the
  shader should produce" reference. Then compute before/after numbers:
  histogram black/white-point spread (did auto-levels actually widen the
  dynamic range), luminance std dev (contrast), and Laplacian variance
  (sharpness — confirms the unsharp mask added edge energy without
  tipping into halos). These metrics confirm the pixels moved in the
  intended direction but won't catch blown highlights or unnatural
  sharpening artifacts on their own — pair them with one quick visual
  check, not a substitute for it. Worth writing as a small reusable
  script (raw frame in, both sets of metrics + a diff image out) rather
  than a one-off check, since future dark-feed regressions (15.2/15.3)
  will want the same comparison.
- Confirm zero measurable impact on the grid's other 5 cells or
  Phase 14's throttling behavior while enhancement runs in the modal.
- Confirm the selector persists across a page reload and correctly
  falls back to plain `<video>` when WebGL2/`requestVideoFrameCallback`
  is unavailable (test via a real capability gap, not just reading the
  code).
- Confirm Prev/Next (`showAdjacent`) correctly re-targets the canvas to
  the newly-focused channel's video with no stale frame or leaked
  WebGL context from the previous one.

**Status (2026-08-11): implemented, three of four checklist items now
confirmed, one real bug found and fixed, one item still blocked.**

- **Bug found and fixed**: retargeting the enhancement pipeline to a
  different video (mode toggle, or opening/closing/Prev-Next between
  channels) could briefly show the *previous* channel's last rendered
  frame under the newly-focused channel's name — `applyEnhancement()`
  used to flip `canvas.style.display = 'block'` as soon as
  `pipeline.start()` returned, but the canvas is a single long-lived
  element (by design, see its class comment) that still holds whatever
  the previous video last drew into it until a fresh frame actually
  arrives. Confirmed for real via a scripted repro (primed the canvas
  with IPCamera 02's content, then opened Garasi and read the canvas
  pixels in the very next tick, before any new frame could have
  rendered — identical to the stale IPCamera 02 frame). Fixed in
  `static/enhance.js`: `EnhancementPipeline` now takes an `onFirstFrame`
  callback, fired only once `_renderFrame()` has actually drawn a real
  frame from the *currently targeted* video; `applyEnhancement()` defers
  the video/canvas visibility swap to that callback instead of doing it
  eagerly, so the plain `<video>` (always correct, just unenhanced)
  stays visible until the enhanced picture is verifiably ready. Re-ran
  the same scripted repro against the fix: canvas never shows nor holds
  stale content anymore. This is the mechanism the "no stale frame ...
  from the previous one" checklist item below was worried about — it
  turned out to apply to every retarget, not just Prev/Next.
- **Confirmed**: selector persists across a page reload (`enhanceMode`
  in `localStorage`, survives navigation as expected).
- **Confirmed**: WebGL2-unavailable fallback, tested via a real
  capability gap (temporarily made `canvas.getContext('webgl2')` return
  `null`, matching what `enhanceCapable()`'s own probe checks) — 
  enhancement silently stays off, plain `<video>` stays visible, no
  console errors. (First attempt at this test gave a false failure
  because the modal was left open from a prior check — closing it first
  before re-testing gave the correct, clean result on retest.)
- **Confirmed**: zero measurable impact on the grid's other 5 cells or
  Phase 14's throttling while enhancement runs in the modal — verified
  with a single atomic, precisely-timestamped `XMLHttpRequest`
  instrumentation script (not the separate-tool-call polling used in an
  earlier, misleading attempt — see note below) spanning 26 real seconds:
  opened Teras (ch1) via the real `openOverlay()` flow with Classical
  enhancement active, and simultaneously logged every request to both
  ch1 and a background cell (ch2/Car Port). Result: ch1 sustained a
  steady, undisturbed ~4 req/s the entire window (enhancement adds no
  network activity of its own — expected, since it's pure client-side
  WebGL2 rendering); ch2 correctly duty-cycled — silent for ~10s, a
  ~2s burst of requests, repeating on schedule twice in the window,
  matching `BG_OFF_MS`/`BG_ON_MS` exactly. An earlier attempt at this
  same check (using separate `read_network_requests` tool calls with
  `clear`/wait/`check` as distinct steps) gave a false "throttling isn't
  working, cells fetch continuously" result — traced to the tool calls
  themselves having unknown, uncontrolled latency between them, not a
  real bug; the atomic in-page script above is the trustworthy version
  and fully vindicates Phase 14's mechanism.
- **Still blocked**, this time by the verification environment rather
  than the app: confirming the classical pass visibly improves a
  dark/soft real feed needs an actual decoded video frame to sample, and
  this session's browser-automation tab cannot produce one under any
  approach tried — `requestVideoFrameCallback` never fires
  (`document.visibilityState` stuck `"hidden"`; a polled
  `video.currentTime` sat frozen for 20+ continuous seconds), and even a
  direct one-shot `texImage2D` snapshot (which shouldn't need rVFC,
  just a decoded frame) found every grid-cell video stuck at
  `readyState: 0` with zero dimensions — confirmed this isn't
  session/resource degradation by testing in a brand-new tab, which
  showed the identical `readyState: 0`. (One channel did briefly show a
  real decoded frame — `readyState: 4`, 1280×720 — right at the very
  start of this verification session; that appears to have been a
  one-time fluke, not a reproducible state, since nothing since has
  matched it.) Different blocker than the 2026-08-10 attempt (that one
  was host resource contention), same outcome: needs a real,
  foregrounded browser tab, not automation, to finish. `IPCamera 02`
  (channel 10) showed the clearest IR-tinting in earlier screenshots but
  is deliberately deferred as the dark-feed test candidate — it has the
  worst latency and is physically farthest away of the online channels
  (`MEMORY.md`), not representative of a typical feed; a nighttime pass
  on one of the analog channels (or `IPCamera 01`) is the better
  real-world candidate — conveniently, it's now dusk locally, so this is
  a good time to check. A well-lit daytime scene (`Garasi`) was checked
  as best-effort earlier in the session and showed only a subtle
  difference, consistent with the classical pass targeting genuinely
  dark/soft footage rather than daytime footage.

## Phase 15.2 design: ML groundwork for stream enhancement (dev-only, not yet user-facing)

Staged separately from 15.3 so nothing downstream (a public `AI`
option) gets built before the model/runtime choice is actually proven
against this DVR's real footage — this sub-phase is pure groundwork,
with no visible change for a normal user.

- Pick a lightweight browser-capable model (a small super-resolution
  net like ESPCN/FSRCNN, or a denoise-focused one — final pick needs
  benchmarking against real footage from this DVR, not assumed) and a
  runtime (TensorFlow.js or ONNX Runtime Web, WebGL/WebGPU backend).
- Whatever is chosen must be **vendored locally** (`static/vendor/`,
  gitignored-model-weights-or-not decided at that time) per `AGENTS.md`'s
  existing no-CDN-dependency rule for frontend deps — the model weights
  themselves only fetched lazily when a user actually selects `AI`
  mode later (once 15.3 ships), never on page load, so nobody pays
  that download for a feature they never turn on.
- Lands the `ml` processor (same `EnhancementPipeline` interface 15.1
  already defined) behind a dev-only flag, not in the public selector
  yet, so real FPS/quality can be measured against this DVR's actual
  streams before committing to ship it.
- Depends on 15.1's pipeline/canvas/lifecycle plumbing already being in
  place — no new plumbing of its own.

**Verification plan**: confirm the vendored model/runtime loads and
runs the `ml` processor (behind its dev-only flag) with no network
calls beyond this LAN/the local service, and record real FPS/quality
measurements against actual footage from this DVR — the data this
stage exists to produce, and what 15.3's go/no-go decision rests on.

## Phase 15.3 design: `AI` becomes a real selector option

- `AI` is added to the `Off`/`Classical`/`AI` selector (15.1) as a
  real, user-facing choice — gated by a runtime capability/performance
  check (e.g. a brief benchmark pass on pipeline init) that
  auto-falls-back to `Classical` if the device can't sustain acceptable
  frame rate. Same "always have an accepted fallback" policy as
  WebGL2-unavailable in 15.1, applied to "WebGL2 exists but this
  device's GPU is too weak for the ML pass specifically."
- Depends on 15.2 having already benchmarked and settled on a model/
  runtime — this sub-phase is the productionization of that choice,
  not a new one.

**Verification plan**: real-device verification (per `AGENTS.md`) on
more than one client, not just the dev machine — ML inference cost
varies far more by hardware than the classical shader pass does.
Confirm the perf-fallback genuinely engages on a deliberately
underpowered test client, not just that it compiles.

## Phase 16 design: self-heal mediamtx live-view paths after an independent restart

**Problem**, already named as a known gap when mediamtx was split into
its own Swarm service (`ARCHITECTURE.md`, "Known gap: mediamtx
restarting alone loses live-view paths"): `MediaBridge.start()` pushes
every `ch{id}_main/sub` path into mediamtx exactly once, at
`dvr-window`'s *own* startup. If `mediamtx` restarts on its own —
OOM-kill, crash, a redeploy of just that service — the fresh instance
comes up with zero paths registered, and stays that way (every HLS/
WebRTC request for a channel fails) until `dvr-window` itself also
restarts. Deferred at the time as out of scope for that round.

**Confirmed as a real, not theoretical, production gap (2026-08-10)**:
`mediamtx` on `sm-qohelet`/`sw-david01` was OOM-killed (exit 137) under
a stale `384M`/`1.0` CPU limit that had drifted out of sync with the
repo's already-updated `768M`/`1.5` CPU recommendation (a separate
config-drift issue, fixed by redeploying the corrected limits — see
"Memory/CPU limit re-check (Phase 6)" in `ARCHITECTURE.md`). That
alone reduces how *often* mediamtx gets OOM-killed, but does nothing
for what happens the next time it restarts for any reason: live view
read as an endless client-side reconnect loop until a manual `docker
service update --force dvr-window_dvr-window` re-pushed the paths.

**Approach**: extend the existing playback-path GC sweep
(`MediaBridge`, Phase 6 — already polls mediamtx's own `GET /v3/paths/
list` every 30s to garbage-collect abandoned playback paths) to also
verify the live-view paths are present, rather than adding a second
polling loop:

- On each 30s sweep, check that every expected `ch{id}_main/sub` name
  appears in the same `paths/list` response already being fetched for
  GC.
- Any missing → re-push via the same idempotent `POST /v3/config/
  paths/replace/{name}` `_add_path` already uses at startup (confirmed
  safe to call repeatedly — that's the whole reason `replace` was
  chosen over `add` originally, see "Path registration is idempotent"
  in `ARCHITECTURE.md`).
- Bounds recovery time to one GC cycle (≤30s) after mediamtx comes
  back, instead of indefinitely until someone notices and manually
  restarts `dvr-window` — closing the gap without reimplementing
  mediamtx's own crash-restart (Swarm's `restart_policy` already
  handles that part fine, per the Phase 6 design above).
- Log a line when a re-push actually happens (missing paths found and
  restored) — the original "surfacing a log line" idea from the Phase
  6 design, but attached to the moment that matters (paths were
  actually gone) rather than to mediamtx's exit event itself, which
  `dvr-window` has no direct visibility into anyway (they're separate
  containers).

**Resolved without special-casing**: the worried-about race — "mediamtx
restarted and lost paths" vs. "mediamtx is still starting up and hasn't
been pushed to yet" (e.g. right after a fresh `docker stack deploy` of
both services together) — turns out not to need distinguishing.
`MediaBridge.start()` already pushes every path and waits for mediamtx
to be ready before returning, and the reconciliation loop's first sweep
can't fire until 30s after *this process's own* startup — by
construction, the initial push always happens first. A path still
missing by the first sweep is always a real gap, never a startup race.

**Implemented**: `gc_playback_paths()` renamed `reconcile_paths()`
(`app/mediabridge.py`) — same 30s loop in `app/main.py`
(`_reconcile_paths_loop`), one shared `GET /v3/paths/list` call now
drives both the live-view re-push (network mode only — self-managed
mode's mediamtx is a direct child subprocess with no equivalent gap)
and the original playback-path GC. `MediaBridge.start()` now also keeps
the pushed `{name: path_config}` dict (`self._live_paths`) so a missing
path can be reconstructed exactly, not just detected.

**Verified (2026-08-10)**, real incident recreated locally rather than
just reasoned about: brought up `docker-compose.yml`'s network-mode
split (`docker compose up -d`, the same `MEDIAMTX_HOST=mediamtx` path
production uses), confirmed all 12 paths registered, then `docker rm -f`
+ recreate on just the `mediamtx` container (matching a Swarm task
replacement more closely than `docker kill`+`start`, which turned out to
reuse the same container and not actually reproduce the gap) while
`dvr-window` kept running throughout. Confirmed via mediamtx's own
loaded config (`paths: {}`) that the fresh container genuinely started
with nothing registered, then confirmed all 12 paths back within one
sweep with **no `dvr-window` restart** — `docker logs` (via `rtk proxy`,
since the default filtered view was summarizing away the plain `print()`
lines) showed a `[reconcile] re-registered missing live-view path: ...`
line for exactly the paths that were actually missing, once per genuine
recovery — it fired twice total across the session, matching the two
real disruptions caused during testing, and stayed silent on every other
30s sweep in between where nothing needed fixing. Confirmed end-to-end
with an authenticated `curl` against the recovered `ch1_main` HLS
playlist returning `200` afterward, not just that the path existed.

## Phase 17 design: browser-side FPS for the live grid

**Problem**: the live grid (`static/index.js`) runs one independent
hls.js instance per channel (currently 6: analog 1-4 + IP-proxy 9-10),
all decoding continuously and simultaneously all the time — Phase 14's
throttling only kicks in for non-focused cells while the modal is open.
Every cell requests each channel's `main` (full-resolution) stream, even
though grid cells are laid out as small ~320×180px CSS Grid tiles — the
full-res decode is thrown away by CSS scaling; the browser doesn't
decode less because the element is drawn smaller. hls.js is constructed
with only `{ lowLatencyMode: true, xhrSetup }`, no buffer/back-buffer
tuning. There is currently **zero FPS/performance instrumentation**
anywhere in the codebase — Phase 15.1's own verification checklist
includes "confirm zero measurable impact on the grid's other 5 cells,"
which was never completed because there was no tool to measure it with.

Four sub-phases, ordered so each is validated with real numbers before
the next relies on it, per this project's real-DVR/real-client
verification standard (`AGENTS.md`):

### 17.1: FPS/perf instrumentation (foundational)

Nothing else in Phase 17 can be honestly verified without this — it
closes the exact gap that left Phase 15.1's own checklist unfinished.

- New `static/debugfps.js`, gated behind
  `localStorage.getItem('debugFps') === '1'` (settable via
  `?debugFps=1`, same persistence pattern as `enhance.js`'s
  `enhanceMode`) — zero extra work on a normal page load when disabled.
- **Per-grid-cell decode stats**: poll `video.getVideoPlaybackQuality()`
  (delta `totalVideoFrames`/`droppedVideoFrames` per ~1s, one
  `setInterval` per cell) — a cheap counter read, not a per-frame
  callback, so measuring 6 cells doesn't itself add decode contention.
- **Modal/focused video**: reuse the `requestVideoFrameCallback` pattern
  already established in `enhance.js` (only one stream; the callback may
  already be firing there if enhancement is active).
- **Page-level jank**: one single global `requestAnimationFrame` loop
  (not per-video) to separate "video decode is slow" from "main thread
  is janky for unrelated reasons" (e.g. the status/ping
  `MutationObserver` mirroring in `index.js:11-37`).
- Small on-screen badge per cell when the flag is on (e.g.
  `24fps / 2 drop`). One new `<script>` tag in `index.html` near the
  existing `enhance.js` tag.

**Verification plan**: real DVR, all 6 real channels live — counters
read sane numbers (cross-check against each channel's actual encoder
frame rate, not an assumed 25/30fps); confirm negligible CPU overhead
with the flag on vs. off (Chrome Task Manager, real client hardware);
confirm zero DOM/console difference with the flag off.

### 17.2: low-risk grid tuning (buffer config + connect stagger + CSS containment)

Bundled into one phase/PR — each item is individually small, low-risk,
and validated by the same 17.1 before/after run.

- **hls.js buffer config** (`static/index.js:465`, the `new Hls({...})`
  in `start()`): add `maxBufferLength: 10`, `maxMaxBufferLength: 20`,
  `backBufferLength: 10` (down from library defaults) to reduce
  buffering/memory overhead across 6 concurrently-open `MediaSource`
  buffers. No real ABR to tune here — each stream is a single-rendition
  HLS path, not a multi-bitrate variant playlist, so hls.js's ABR
  machinery has nothing to select between.
  **Real regression risk**: channels 9/10's wireless link has
  documented jitter (existing `dup=`/`drop=` comments, generous
  watchdog thresholds already tuned around it) — shrinking buffers
  could increase false-positive "lagging…"/rebuild triggers specifically
  on those channels. Needs an overnight real-DVR soak test watching 9/10
  for status flapping, not a smoke test.
- **Stagger initial connection storm** (`main()`,
  `static/index.js:628-657`): defer each cell's `setupHlsPlayer(...)`
  call by `index * ~250ms` via `setTimeout`, keeping DOM/skeleton
  creation synchronous. Smooths the initial burst of up to 6
  simultaneous manifest fetches + mediamtx on-demand ffmpeg cold-starts;
  doesn't reduce steady-state per-frame CPU cost once all 6 are
  decoding — real but lower-priority than the buffer tuning above.
- **CSS containment** (`static/index.css`, `.cell` rule): add
  `contain: layout paint style` (not `contain: size` — would fight the
  existing `minmax(320px, 1fr)` + `aspect-ratio: 16/9` intrinsic
  sizing). Scopes each cell's layout/paint boundary so per-cell
  status/ping DOM churn doesn't force sibling-cell recalculation.
  Explicitly not adding `content-visibility: auto` (no off-screen cells
  to skip in a normally-all-visible 6-cell grid) or `will-change`
  (forces a compositor layer per cell for no real benefit here — cells
  aren't animated).

**Verification plan**: 17.1's instrumentation before/after for
connect-burst network waterfall + page-level jank; overnight real-DVR
soak specifically on channels 9/10 for buffer-tuning regressions;
visual check that `contain: paint` doesn't break the existing
`overflow: hidden`/border-radius clipping on `.cell`.

### 17.3: backend prerequisite — sub-stream codec verification + transcode-scope fix

Hard prerequisite for 17.4 — not parallelizable with it. The backend
already exposes a `sub` HLS path for every channel via `/api/streams`
(`kind: "sub"`); the frontend just never requests it.

- **Verify real codec** of each channel's `sub` stream against the
  actual DVR (ffprobe, or the DVR's own ISAPI capability response — same
  source `_build_paths` already reads `stream["codec"]` from) for all 6
  channels. Cheap and read-only; determines how much of 17.3b/17.4 this
  deployment actually needs. `MEMORY.md` currently flags analog
  channels' sub-streams as possibly still H.265 (unverified).
- **Fix `_build_paths`'s transcode scope** (`app/mediabridge.py:95`):
  currently `if stream["codec"] == "H.265" and name.endswith("_main")`
  — drop the `_main`-only restriction (`if stream["codec"] == "H.265":`)
  so any H.265 sub-stream also gets transcoded once the frontend starts
  requesting it; otherwise it would ship an unplayable stream to Chrome
  (no native HEVC/MSE support). Update the adjacent comment and
  `ARCHITECTURE.md`'s matching "H.265→H.264 transcode for main streams"
  section.

**Real risk — the biggest in this whole phase**: `ARCHITECTURE.md`
already documents the existing single (channel 10 main) transcode
running at only ~1.0-1.05x real-time with "little CPU headroom." If
17.3a finds analog sub-streams are also H.265, going from 1 to
potentially 5-6 concurrent transcodes could overwhelm the production
host. This may mean 17.4 ships scoped down (only channels with
natively-H.264 subs switch; H.265-sub channels stay on `main` in the
grid) rather than a blanket switch.

**Verification plan**: codec confirmed per channel, documented; for any
newly-transcoded sub-stream, confirm output actually plays and matches
source (same `ffmpeg -frames:v 1` comparison method already used for
the main-stream transcode); confirm sustained CPU headroom on the
actual production host over a realistic period, not just a dev-box
smoke test.

### 17.4: frontend — grid uses `sub`, modal uses `main`

The actual decode-cost win. Gated on 17.3 landing and being verified for
this deployment's real channel set.

**Mechanism**: same hls.js instance, `hls.loadSource(newUrl)` swap on
modal open/close — not a second instance, not a full rebuild.
- Two instances (separate always-alive grid player + on-demand modal
  player) would break the single-`<video>`-node-relocation pattern that
  Phase 14 (`throttleBackground`/`restoreForeground`) and Phase 15.1
  (`applyEnhancement` targeting "whichever video is in `#overlaySlot`")
  both depend on, for marginal benefit.
- A full instance rebuild (`hls.destroy()` + new `Hls()`) reuses the
  existing `start()` path, but that path is documented (above) as
  blanking the video to black until rebuffered — already a known
  complaint for background-cell rebuilds; making it the standard cost
  of every modal open/close would likely reproduce that complaint
  constantly.
- `hls.loadSource()` on the existing attached instance is hls.js's
  documented mechanism for switching content on a live player without a
  full rebuild.

**Concrete changes** (`static/index.js`):
- `setupHlsPlayer(video, status, subUrl, mainUrl)` — track a mutable
  `currentUrl` instead of the current single closed-over `hlsUrl`.
- New `switchSource(newUrl)`: no-op if already current; otherwise reset
  watchdog state the same way a fresh `MANIFEST_PARSED` would
  (`retryMs`, `consecutiveErrors`, `driftSinceMs`, `clearLagTimers()`),
  then `hls.loadSource(newUrl)` (or `video.src = newUrl` on the
  native-Safari fallback path).
- `video._player = { throttleBackground, restoreForeground, switchSource }`.
- `main()`: resolve both `sub` and `main` stream URLs per channel; grid
  cells build against `sub` (store `main` on `cell.dataset.mainUrl`);
  defensive fallback to `main` (with `console.warn`) if a channel has
  no `sub` entry.
- `openOverlay()` (`index.js:61-77`): call
  `video._player?.switchSource(mainUrl)` before
  `video._player?.restoreForeground()` — ordering matters, since a
  queued `pendingRebuild` reads the shared `currentUrl` when it fires.
- `closeOverlay()`/`showAdjacent()` (`index.js:126-135, 165-183`): call
  `video._player?.switchSource(subUrl)` on the outgoing video,
  symmetric placement.
- No changes needed to `syncOverlayStatus`/`syncOverlayPing` or
  `enhance.js` — both already operate independent of which URL is
  loaded.

**Verification plan**: using 17.1's instrumentation, measure aggregate
grid decode-FPS/CPU with `sub` vs. today's `main`-everywhere baseline
on the real DVR (record the result even if the win turns out marginal
— that's useful information either way); repeated open/close/Prev/Next
soak test (dozens of cycles) on real client hardware with Chrome's
memory profiler, confirming no leak from repeated `loadSource()` calls
and an acceptably brief switch-induced rebuffer; re-run Phase 14's and
Phase 15.1's existing verification checklists against this changed code
path, since both now depend on `switchSource` sequencing; confirm
production-host CPU/network stays healthy with the real
transcoded/passthrough mix from 17.3.

**Recommended sequencing**: ship 17.1+17.2 first as their own PR, get
real before/after numbers, then decide 17.3/17.4's scope (possibly
per-channel, depending on what 17.3a's codec survey finds) as a
follow-up — 17.3/17.4 carry materially more risk than 17.1/17.2.

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.
- Bare-metal packaging (systemd unit, install script). Docker is the
  only supported deployment path — it's already built, working, and
  running in production (`docker-compose.yml` standalone,
  `docker-compose.swarm.yml` for the mediamtx-split Swarm setup).
- Event/alarm stream — deferred, not a code gap: the ISAPI account
  gets `403 lowPrivilege` on both the push and poll mechanisms, motion
  detection is disabled, and no alarm inputs are configured on the
  DVR. Revisit only if the DVR account/config changes.
- Per-camera default enhancement modes, auto-switching heuristics (e.g.
  auto-`Classical` on known-dark channels), or any server-side
  involvement in stream enhancement (Phase 15.4) — explicit non-goal
  until 15.1-15.3 are proven; premature ahead of real usage data from
  those earlier stages.

## Next step

Phase 6 is fully done. Phases 14 (focused-stream throttling) and 16
(mediamtx live-view path self-heal) are both done and deployed to
production (2026-08-10). Phase 15.1 (classical stream enhancement —
design above) is next up for implementation; 15.2 (ML groundwork) and
15.3 (`AI` as a real selector option) follow in order, each depending
on the one before it proving out. Phase 17 (browser-side FPS for the
live grid — design above) is queued behind 15.1: 17.1 (FPS
instrumentation) and 17.2 (low-risk grid tuning) can start any time;
17.3 (backend sub-stream prerequisite) and 17.4 (grid switches to
`sub`) are gated on 17.1/17.2 landing first and on 17.3's real-DVR
codec/CPU-headroom findings.
