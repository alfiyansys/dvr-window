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
inside the container at ~2026-08-03 16:05 UTC and sat as an unreaped
zombie for ~15 hours, completely undetected. HLS (`:8888`), WebRTC
(`:8889`), and mediamtx's own control API (`:9997`) all stopped
accepting connections — live view was fully dead — while `/healthz`
kept returning `{"status": "ok"}`, Docker Swarm kept reporting the
task `1/1 Running`, and Traefik/the FastAPI app/`/api/*` all worked
normally throughout. Confirmed root cause via `docker inspect`
(`State.OOMKilled: true`) and `/proc/<mediamtx-pid>/status` showing
`State: Z` inside the container. Only found by chance during an
unrelated manual check, not by any alerting.

Two independent gaps let this go unnoticed:

- **No supervision of the mediamtx child process.** `MediaBridge.start()`
  (`app/mediabridge.py`) does a single `subprocess.Popen(...)` at
  startup and never checks on it again — no polling, no restart on
  unexpected exit. Once mediamtx dies, nothing in the app notices or
  reacts.
- **`/healthz` doesn't reflect mediamtx at all.** It's a static
  `{"status": "ok"}` (`app/main.py`) that only proves the FastAPI
  process itself is alive. Swarm's `restart_policy: condition:
  on-failure` (`docker-compose.swarm.yml`) can only fire on a
  container-level exit — but PID 1 (FastAPI) never exits just because
  its mediamtx sidecar did, so the restart policy never triggers.

Likely OOM trigger: the container's memory limit is `384M`
(`docker-compose.swarm.yml`, reservation `128M`) — tight for mediamtx
plus concurrent per-channel `ffmpeg` H.265→H.264 transcodes
(`_transcode_path` in `mediabridge.py`) under real load with multiple
channels active at once. Not confirmed with actual peak-usage
measurements, just the closest match to what's known.

Fix, in priority order:

1. **Make `/healthz` (or a separate check) reflect mediamtx's real
   state** — check `self._proc.poll() is None`, and/or hit mediamtx's
   own API on `127.0.0.1:{API_PORT}`. Wire a Docker `HEALTHCHECK`
   (currently none in `Dockerfile`) to it so Swarm can actually detect
   this and restart the task automatically instead of serving a dead
   stream indefinitely.
2. ~~**Supervise the mediamtx subprocess from inside the app**~~ —
   superseded by the container split below, which gets this from Swarm
   for free instead of reinventing it in Python. (Original idea: a
   background watcher (thread or asyncio task) that notices
   `Popen.poll()` return non-`None` unexpectedly and either restarts
   mediamtx in place or exits the app process so Swarm's
   `restart_policy` takes over.)
3. **Re-check the `384M` memory limit** against actual peak usage with
   all configured channels' transcodes running concurrently, and size
   it with real headroom instead of the current guess.
4. **Surface the failure** — at minimum a log line when mediamtx exits
   unexpectedly; this incident took a multi-node SSH investigation
   (checking Swarm task state, Traefik, port listeners on 3 different
   hosts, then `/proc/<pid>/status` inside the container) to even
   confirm what was wrong, entirely because nothing surfaced it up
   front.

### Chosen fix: split mediamtx into its own Swarm service

Considered as an alternative to fix item 2 above (in-app subprocess
watcher) and chosen over it: put `mediamtx` in its own
container/Swarm service instead of a `subprocess.Popen` child of the
FastAPI process. An OOM-killed *container* is something Swarm already
detects and acts on — `restart_policy: condition: on-failure`
(`docker-compose.swarm.yml`) fires on container exit, no new
supervision code needed. That's strictly closer to the actual gap
this incident exposed than teaching the FastAPI app to poll
`Popen.poll()` itself: right now nothing *containerized* dies when
mediamtx does, so Swarm has nothing to react to. Splitting fixes that
at the platform level instead of re-implementing it in Python. Item 2
above is superseded by this; items 1 (mediamtx-aware `/healthz`/
`HEALTHCHECK`), 3 (memory-limit re-check), and 4 (log the failure)
still apply, just aimed at the new `mediamtx` service's own container
instead of a subprocess.

Two services, `dvr-window` (FastAPI) and `mediamtx`, both on the
`traefik-public` overlay network so FastAPI can reach mediamtx's
control API (`app/mediabridge.py`) by service DNS name instead of
`127.0.0.1`. mediamtx's HLS/WebRTC ports (8888/8889) keep being
published directly — browsers hit them straight, not through
Traefik/FastAPI — so that half of `docker-compose.swarm.yml` barely
changes, it's really just which container those `ports:` entries
belong to. Only the per-channel `ffmpeg` transcode processes stay
attached to mediamtx (it spawns them itself via `runOnDemand`, same
container) — this is a two-way split, not three; `capture_clip`/
`capture_frame`'s `ffmpeg` calls stay short-lived subprocesses of
FastAPI either way, no container needed for those.

What has to change:

- **Startup**: `MediaBridge.start()` currently writes `runtime.yml`
  and does one `subprocess.Popen([mediamtx_bin, config_path])` —
  mediamtx can't be started this way once it's a separately-deployed
  container. mediamtx's own base config (RTSP/HLS/WebRTC addresses,
  `authInternalUsers` built from `AUTH_KEY`) moves into that
  container's own entrypoint, rendered from env/secret at its boot,
  independent of FastAPI. The per-channel `paths` FastAPI currently
  bakes into that same startup config file instead get pushed after
  the fact via mediamtx's own config API — the same pattern
  `add_playback_path`/`remove_playback_path` already use for playback
  paths, just applied to the live-view paths too instead of being
  config-file-only.
- **Readiness**: `_wait_until_ready()` polls `127.0.0.1:8888` —
  becomes polling the `mediamtx` service's DNS name over the overlay
  network, tolerant of mediamtx not being up yet on a cold stack
  deploy (Swarm doesn't guarantee inter-service start order within one
  `docker stack deploy`).
- **Auth model**: the `authInternalUsers` loopback exemption
  (`ips: ["127.0.0.1", "::1"]`, granting passwordless `api`+`publish`)
  stops being valid for the `api` action once `add_playback_path`/
  `remove_playback_path` calls originate from the `dvr-window`
  container's overlay-network IP instead of mediamtx's own loopback —
  container IPs on an overlay network aren't loopback-trusted the way
  same-namespace `127.0.0.1` is. Needs a real credentialed internal
  user (distinct from the LAN-facing `viewer` user, which stays
  `read`-only) for control-API calls. The `publish` half of that
  exemption is unaffected — the transcode `ffmpeg` publishing back
  into mediamtx's RTSP re-serve is still spawned by mediamtx itself,
  same container, still genuinely loopback. `capture_clip`/
  `capture_frame` already authenticate as `viewer` for RTSP `read`
  (never relied on the loopback exemption to begin with, per the
  existing comment in `mediabridge.py`) — unaffected by the split
  either way.
- **Memory limits**: today's single `384M` limit
  (`docker-compose.swarm.yml`) covers both processes conflated
  together — exactly why fix item 3 above couldn't previously be sized
  from real data. Splitting gives each container its own
  limit/reservation, and `docker stats`/Swarm now reports each
  independently, giving fix item 3 the real per-process peak-usage
  numbers it was missing.
- **mediamtx's own `HEALTHCHECK`**: fix item 1 above still applies,
  now scoped to the `mediamtx` container's own image (hit its `:9997`
  API or `:8888` HLS root) rather than folded into `/healthz` on the
  FastAPI side.

**Decided (revised after testing against the real binary — see below):**
no custom wrapper `Dockerfile`, but not pure env-var config either.
Use the upstream `bluenviron/mediamtx:<version>-ffmpeg` image directly,
with mediamtx's *base* config (RTSP/HLS/WebRTC addresses, `authMethod`,
`authInternalUsers`) delivered as a real YAML file mounted via a Swarm
`configs:` entry, plus a small `entrypoint.sh` (also delivered via
`configs:`, not baked into an image) that substitutes `AUTH_KEY` from
the Swarm secret into that file before exec'ing mediamtx. Per-channel
`paths` are still not part of this file — those get added after boot
via mediamtx's own config API, per the Startup bullet above.

The `-ffmpeg` tag (Alpine-based, not the default `FROM scratch` tag)
turned out to be required anyway, independent of the config question:
mediamtx's own `runOnDemand` transcode (`_transcode_path` in
`app/mediabridge.py`) spawns `ffmpeg` *inside whichever container
mediamtx itself runs in* — the default scratch-based image has no
`ffmpeg` at all, so the transcode would simply fail to run post-split.
Confirmed directly (`docker run --rm --entrypoint sh
bluenviron/mediamtx:1.19.2-ffmpeg -c 'which ffmpeg'`): Alpine 3.24,
`ffmpeg` at `/usr/bin/ffmpeg`, plus `/bin/sh`/`/bin/sed`/`/bin/cat` —
which also happens to be exactly what `entrypoint.sh` needs to do the
secret substitution. One image solves both problems.

Two things reversed the original pure-env-var plan, both confirmed by
actually running `mediamtx/mediamtx` (the binary already present
locally per `AGENTS.md` setup) with real `MTX_AUTHINTERNALUSERS_*` env
vars and reading back `/v3/config/global/get`, not just reasoned about:

- **Env-var overrides merge onto mediamtx's *compiled-in default*
  `authInternalUsers` list, per index — they don't start from blank.**
  Setting `MTX_AUTHINTERNALUSERS_1_USER`/`_PASS`/`_PERMISSIONS_0_ACTION`
  left that index's stock-default `ips: [127.0.0.1/32, ::1/128]`
  restriction in place untouched in the resolved config. There's no way
  to clear it either — explicitly setting `_IPS=""` doesn't blank the
  list, it crashes mediamtx outright (`unable to parse IP/CIDR ''`).
  This directly blocks the one thing this split actually needs: a
  `backend`/`api` user reachable from the `dvr-window` container's
  overlay-network IP, not just loopback.
- **Indices beyond the default list's length are silently dropped.**
  Setting index 3 (`backend`, past the 3 stock entries) never appeared
  in `/v3/config/global/get` at all. Testing this from `127.0.0.1` is
  also methodologically unreliable on its own — the untouched default
  index 1 already grants passwordless `api` access from loopback, so a
  successful loopback-authenticated request doesn't prove a custom
  override actually took effect; it might just be the default entry
  letting it through regardless.

A full YAML file doesn't have either problem — full-file
`authInternalUsers` **replaces** mediamtx's default list rather than
merging (already relied on today, undisturbed, for the single-container
deployment's own `viewer` entry — see the existing comment in
`app/mediabridge.py`), so an unrestricted-IP `backend` user is just a
normal list entry, no index-merge surprises.

## Non-goals (for now)

- Two-way audio talk-back.
- Multi-DVR / multi-site management (single DVR target for v1).
- Mobile app — web UI only, works fine from a phone browser on LAN.
- Exact UI/UX parity with Hikvision's own web interface — functional parity, not a visual clone.

## Next step

Phase 6 — packaging: systemd service + install script, playback-path
garbage collection (see `ARCHITECTURE.md` known gaps), event/alarm
stream, basic auth on the local UI.
