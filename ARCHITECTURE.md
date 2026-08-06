# Architecture

Technical reference for how this system is built and why. Stable
reference material — unlike `PLAN.md` (roadmap/status) and `MEMORY.md`
(current device state), this doesn't change as phases complete, only as
the design itself changes.

## Why ISAPI + RTSP, not HCNetSDK

Rejected Hikvision's official Linux SDK (HCNetSDK): closed-source blob,
its own licensing, limited architecture support, and a Windows-shaped C
API that doesn't map cleanly to "run a local web service." ISAPI (HTTP)
+ RTSP are documented, open protocols that cover everything needed
(live view, playback, search, snapshot, download) with a normal
HTTP/RTSP client stack — portable, no licensing entanglement.

Also explicitly not attempting a reverse-engineered clone of the
Windows `LocalServiceControl.exe` local-service plugin: closed-source,
undocumented wire protocol, no Windows box available to sniff it, no
installer at hand.

## Components

Two separate paths — control-plane (ISAPI, via the backend) and
media-plane (RTSP, via the media bridge, not proxied through the
backend at all):

```
CONTROL PLANE (channels, search, snapshot, download)
-----------------------------------------------------

  Browser  -- HTTP request -->  Backend (FastAPI)  -- HTTP digest -->  DVR :80 (ISAPI)
  Browser  <-- JSON response --  app/main.py            <-- XML --    DVR :80 (ISAPI)
                                 app/isapi.py (client)


MEDIA PLANE (live view + playback)
-----------------------------------------------------

  DVR :554 (RTSP)  -- RTSP -->  Media bridge: mediamtx  -- HLS / WebRTC -->  Browser <video>
                                app/mediabridge.py
                                (subprocess, bare metal / dev — its own
                                 Swarm service in production, see
                                 "mediamtx as a separate Swarm service")
```

`<video>` in the browser talks to mediamtx directly on its own ports
(`:8888` HLS, `:8889` WebRTC) — the FastAPI backend only tells the
browser *which* mediamtx path to use (via `/api/streams` and
`/api/playback/start`), it doesn't proxy the media itself. Browsers
can't play raw RTSP/H.264 elementary streams directly, so this bridge
is required; using **mediamtx** (single Go binary, RTSP-in /
HLS+WebRTC-out, has a runtime control API for dynamic paths) instead
of writing our own RTSP-to-browser transcoder.

- **Backend** (`app/`): config loading (`config.py`), ISAPI client
  (`isapi.py`), mediamtx process/service + dynamic path management
  (`mediabridge.py` — self-managed subprocess or a separate Swarm
  service depending on deployment, see below), FastAPI routes
  (`main.py`).
- **Frontend** (`static/`): plain HTML/JS, no build step. `index.html`
  (live grid), `playback.html` (search + play recordings). hls.js
  vendored locally (no CDN dependency, keeps the local service
  self-contained).
- **Packaging**: Docker only, by decision — `Dockerfile` +
  `docker-compose.yml` (standalone) / `docker-compose.swarm.yml`
  (mediamtx split into its own Swarm service, see "mediamtx as a
  separate Swarm service"), already deployed to production. No
  bare-metal systemd unit / install script planned.

## ISAPI reference

- HTTP (not HTTPS-only on this firmware), **digest auth**, **XML-only**
  responses (`Accept: application/json` is ignored) — confirmed on
  `deviceInfo`, channel list, `CMSearch`.
- No digest-auth quirks — handles 10+ concurrent requests fine,
  standard RFC handshake, no special `qop`/`nc` workarounds needed. Any
  standard HTTP digest-auth client library works.
- Channels: `GET /ISAPI/System/Video/inputs/channels` (analog inputs),
  `GET /ISAPI/Streaming/channels` (per-stream codec/resolution/audio),
  `GET /ISAPI/PTZCtrl/channels/<id>/capabilities` (PTZ).
- `CMSearch`: `POST /ISAPI/ContentMgmt/search`, XML body (`searchID`,
  `trackList`, `timeSpanList`, `maxResults`, `searchResultPostion`).
  Paginated: re-issuing the identical request with the **same
  `searchID`** auto-advances to the next page (server tracks position
  per searchID, client doesn't manage offsets). Each match includes a
  ready-to-use `playbackURI` under `/Streaming/tracks/<trackID>/`.
- Account privilege: `PUT` requests (e.g. changing a channel's codec)
  can return `403 lowPrivilege` depending on the account — read-only
  accounts can't make config changes. See `MEMORY.md` for which account
  this project currently uses and its privilege level.
- Snapshot: `GET /ISAPI/Streaming/channels/<streamID>/picture` returns
  a JPEG directly — no wrapping, just proxy the bytes.
- `CMSearch` matches return a segment's **own full start/end** in their
  `playbackURI`, not whatever time window was searched for — a search
  scoped to a 20-second window still returned a segment spanning 27
  minutes, with `starttime` at the segment's own beginning. Both
  `/api/playback/start` and `/api/download` correct for this the same
  way (`_rewrite_playback_window` in `app/main.py`): the match's
  `name`/`size` tokens must be kept (they identify which stored file to
  read) while `starttime` is overwritten with what was actually
  requested — otherwise playback always starts from the segment's own
  beginning regardless of the caller's requested time (this is what
  makes "jump to time" and "click a segment mid-way through" actually
  seek there instead of replaying from the start).
- `ContentMgmt/download` (`POST /ISAPI/ContentMgmt/download`, tried
  during Phase 5) **ignores `starttime`/`endtime` entirely** and
  streams the whole segment file (~1GB for a ~1.5 hour segment on this
  DVR) — not usable for "download a clip." Downloads are built on RTSP
  playback instead (see Media bridge design below), which does respect
  a custom `starttime`.

## RTSP reference

- Live: `rtsp://user:pass@host:554/Streaming/Channels/<streamID>`
  (capital `Channels`), Digest auth, same realm as ISAPI HTTP.
- Playback: `rtsp://host/Streaming/tracks/<trackID>/?starttime=...&endtime=...&name=...&size=...`
  — this exact URI comes from a `CMSearch` match's `playbackURI`, not
  hand-constructed.

## Media bridge design

`MediaBridge` (`app/mediabridge.py`) manages mediamtx in one of two
modes, chosen by whether `MEDIAMTX_HOST` is set:

- **Self-managed** (`MEDIAMTX_HOST` unset, defaults to `127.0.0.1`) —
  bare-metal (`run.sh`) and local dev. mediamtx is spawned as a child
  process (`subprocess.Popen`); its YAML config, including the
  per-channel `paths`, is generated at startup from the live channel
  list and written to a gitignored runtime path (it embeds DVR
  credentials in RTSP source URLs — must never be committed).
- **Network mode** (`MEDIAMTX_HOST` set, e.g. `mediamtx` in
  production) — mediamtx runs as its own Swarm service instead of a
  subprocess. `MediaBridge` doesn't spawn or configure it at all; it
  waits for the already-running service to become reachable, then
  pushes the per-channel `paths` onto it via mediamtx's own config
  API. See "mediamtx as a separate Swarm service" below for why and
  how.

The four ports below are this bridge's own local listeners, not DVR
config — defaults shown, overridable via `MEDIAMTX_HLS_PORT`/
`MEDIAMTX_WEBRTC_PORT`/`MEDIAMTX_API_PORT`/`MEDIAMTX_RTSP_PORT` (see
`.env.example`) in case one conflicts with something else on the host.
`MEDIAMTX_HOST` itself defaults to `127.0.0.1`, matching self-managed
mode; every network call in `mediabridge.py` (readiness check,
control-API calls, the RTSP re-serve URLs `capture_clip`/
`capture_frame` build) goes through this one host variable, so the two
modes share almost all of the same code — only *how mediamtx gets
started and configured* differs.

- **Live channels**: one static mediamtx path per stream
  (`ch{id}_main`/`ch{id}_sub`), `sourceOnDemand: true` so the DVR isn't
  connected to until a client actually watches — avoids holding N
  permanent RTSP connections to the DVR.
- **Playback**: mediamtx's **control API** (`127.0.0.1:9997`, `api:
  true` in the generated config) registers/deregisters paths at
  runtime — `POST /v3/config/paths/add/{name}` (JSON: `source`,
  `sourceOnDemand`), `DELETE /v3/config/paths/delete/{name}`. Each
  playback session gets a throwaway path created on
  `/api/playback/start` and removed on `/api/playback/stop`, since
  playback URLs are per-time-range and can't be pre-declared like live
  channels.
- **Output**: HLS on `:8888`, WebRTC on `:8889`. Frontend currently
  uses HLS via hls.js (works cross-browser without WebRTC signaling
  complexity); WebRTC paths are exposed by the API but not yet used.
- **RTSP re-serve** (`127.0.0.1:8554`, loopback-only) exists purely so
  the backend can capture download clips with ffmpeg — see "Clip
  download design" below. Not for LAN/browser use.
- Known gap: no TTL/GC for playback paths if a client abandons playback
  without calling stop (e.g. tab closed). Fine for single-user local
  use; revisit before packaging (Phase 6).

### H.265→H.264 transcode for main streams

Chrome can't play H.265 (see "Known device quirks and bugs"). Channel
10 (IPCamera 02) is the concrete case on this DVR — it has no H.264
option to switch to at all, see "IP-proxy channels (9/10)" above — but
the detection itself is generic: `_build_paths` (`app/mediabridge.py`)
checks each stream's own reported codec (`stream["codec"] ==
"H.265"`), not a hardcoded channel name, so this applies to whatever
channel(s) happen to need it on whatever DVR is configured, not just
channel 10 on this one.

mediamtx can't transcode on its own, so a stream needing this has no
direct `source`; instead its path uses mediamtx's `runOnDemand` hook
(`source: publisher`, runs only while a client is actually reading the
path) to spawn `ffmpeg -i <DVR RTSP H.265 source> -c:v libx264 ... -f
rtsp rtsp://127.0.0.1:8554/<name>` — pulling the real source and
pushing the re-encoded stream back into the same path over mediamtx's
own loopback RTSP re-serve (`_transcode_path` in `app/mediabridge.py`).

Limited to `main` streams only (`name.endswith("_main")`), not every
H.265 stream: the live-view frontend only ever requests each channel's
`main` stream, so H.265 sub-streams (e.g. the analog channels', or
channel 10's own sub-stream) never need transcoding since nothing ever
requests them.

Verified by extracting frames from both the raw source and the
transcoded HLS output with `ffmpeg -frames:v 1` and comparing — same
content, correct colors, no corruption from the re-encode. Sustained
transcode speed measured at **~1.0-1.05x real-time** on the dev
machine (`veryfast` libx264 preset) — keeps up, but with little CPU
headroom; revisit the preset/resolution if this box is resource
constrained or multiple viewers watch channel 10 concurrently.

### mediamtx as a separate Swarm service

Production (Swarm) runs mediamtx as its own service, not a
`subprocess.Popen` child of the FastAPI process — `dvr-window` and
`mediamtx` are two separate services in `docker-compose.swarm.yml`,
both on the `traefik-public` overlay network.

**Why**: triggered by a real incident (2026-08-03, `sm-qohelet`/
`sw-david01`) — mediamtx was OOM-killed inside the single shared
container and sat as an unreaped zombie for ~15 hours, completely
undetected. HLS/WebRTC/mediamtx's own control API all stopped
accepting connections while `/healthz` kept returning `{"status":
"ok"}` (it only ever proved the FastAPI process itself was alive) and
Swarm kept reporting the task `1/1 Running` (PID 1 was FastAPI, which
never exits just because its mediamtx sidecar did, so `restart_policy:
condition: on-failure` never triggered). An OOM-killed *container*, by
contrast, is something Swarm already detects and acts on — splitting
gets automatic restart-on-crash from the platform for free instead of
reimplementing subprocess supervision in Python.

**Image**: the upstream `bluenviron/mediamtx:<version>-ffmpeg` tag
directly, no custom-built image. The `-ffmpeg` variant (Alpine-based)
turned out to be required regardless of any config-delivery question:
mediamtx's own `runOnDemand` transcode (`_transcode_path` above) spawns
`ffmpeg` *inside whichever container mediamtx itself runs in* — the
default tag is `FROM scratch` with no `ffmpeg` at all, so the
transcode would simply fail to run post-split. Confirmed directly
(`docker run --rm --entrypoint sh bluenviron/mediamtx:1.19.2-ffmpeg -c
'which ffmpeg'`): Alpine 3.24, `ffmpeg` at `/usr/bin/ffmpeg`, plus
`/bin/sh`/`/bin/sed`/`/bin/cat` — which also happens to be exactly what
the entrypoint below needs.

**Config delivery**: `mediamtx/base.yml` (mediamtx's base config —
RTSP/HLS/WebRTC addresses, `authMethod`, `authInternalUsers`; no
`paths:`, those get pushed later via the API) and `mediamtx/
entrypoint.sh` are mounted into the container via Swarm `configs:`
entries, not baked into a custom image. The entrypoint substitutes
`AUTH_KEY` from the `auth_key` Swarm secret into `base.yml`'s
`__AUTH_KEY__` placeholders (sed, escaping `&`/the delimiter/backslash
in case the key contains them) before exec'ing mediamtx against the
substituted copy — Swarm secrets mount as files
(`/run/secrets/auth_key`), and mediamtx has no `_FILE`-suffix env var
convention to read one directly.

A full YAML file, not `MTX_*` env vars, and this wasn't the original
plan — reversed after testing the real binary (`mediamtx/mediamtx`,
already present locally per `AGENTS.md` setup) with actual
`MTX_AUTHINTERNALUSERS_*` overrides and reading back `/v3/config/
global/get`, not just reasoned about:

- Env-var overrides for `authInternalUsers` **merge onto mediamtx's
  compiled-in default list, per index** — they don't start blank.
  Setting `MTX_AUTHINTERNALUSERS_1_USER`/`_PASS`/
  `_PERMISSIONS_0_ACTION` left that index's stock-default `ips:
  [127.0.0.1/32, ::1/128]` restriction in place, unremovable — setting
  `_IPS=""` doesn't clear the list, it crashes mediamtx (`unable to
  parse IP/CIDR ''`). That directly blocked the one thing this split
  needed: a `backend` user reachable from a different container's
  overlay-network IP, not just loopback.
- Indices beyond the default list's length are **silently dropped** —
  a `backend` user at index 3 never appeared in the resolved config at
  all. Testing this from `127.0.0.1` is also methodologically
  unreliable on its own, since the untouched default index already
  grants passwordless `api` access from loopback regardless of whether
  a custom override actually took effect.

A full config file doesn't have either problem: `authInternalUsers`
set via a file **replaces** mediamtx's default list rather than
merging (already relied on for the `viewer` entry even before the
split — see "Auth" below), so an unrestricted-IP `backend` user is
just a normal list entry.

**Auth model** (full detail in "Auth" below): a third
`authInternalUsers` entry, `backend` (api action, `AUTH_KEY` password,
unrestricted by IP), was added alongside `viewer` — the previous
loopback-IP exemption for the `api` action can't work once
`add_playback_path`/`remove_playback_path`/the live-view path push
originate from `dvr-window`'s own overlay-network IP rather than
mediamtx's own loopback. The `any`/`publish` loopback exemption is
unaffected: the transcode ffmpeg publishing back into mediamtx's RTSP
re-serve is still spawned by mediamtx itself, same container, still
genuinely loopback.

**Path registration is idempotent**: `MediaBridge._add_path` (used by
`add_playback_path` and, in network mode, by `start()`'s live-view
path push) calls mediamtx's `POST /v3/config/paths/replace/{name}`,
not `/add/{name}`. `add` 400s if the path already exists — which
happens on every ordinary `dvr-window` restart that doesn't also
restart `mediamtx` (a rolling redeploy of just that service, or a
crash-restart under `restart_policy`), and crashed app startup every
time until this was caught by actually restarting the split standalone
against a real DVR. `replace` is a true upsert — 200 whether the path
existed already or not.

**Placement**: both services carry a `node.role == worker` placement
constraint in `docker-compose.swarm.yml` — nothing was otherwise
pinning either off the Swarm manager (`sm-qohelet`), which also runs
the cluster's Raft consensus/control plane and shouldn't compete with
application workloads for CPU/memory.

**Verified in production** (2026-08-04): deployed via `docker stack
deploy` to the real cluster (`sm-qohelet` manager, `sw-david01`/
`daya-regia.invis` workers). Both services converged healthy —
`mediamtx`'s new `HEALTHCHECK` (`nc -z` against the HLS/API ports; the
image has no `curl`, and `wget` treats mediamtx's 404 on `/` as a
failure even when mediamtx itself is fine) passing, `dvr-window`'s
logs confirming network mode (no local mediamtx subprocess spawned).
Real LAN traffic observed flowing through the split (an actual client
reading the H.265→H.264-transcoded `ch10_main` HLS stream), and a
browser check against the real domain showed all 6 channels reaching
`live`, matching a standalone pre-deploy test against the real DVR
(isolated Docker network, separate from the running production
service) that caught the `add`-vs-`replace` bug above.

## Clip download design

`/api/download` doesn't use `ContentMgmt/download` (see ISAPI
reference above — it ignores the requested time range) or the DVR's
playback RTSP `endtime` (found unreliable, see below). Instead:

1. Fresh, nudged `CMSearch` to get a valid `name`/`size` token for the
   segment covering the requested window (same staleness/boundary
   handling as playback).
2. Rewrite that match's `playbackURI` (`_rewrite_playback_window` in
   `app/main.py`) to use the **caller's own** `starttime`, keeping the
   fresh `name`/`size`.
3. Register that as a dynamic mediamtx path (same mechanism as
   playback), then run `ffmpeg` against mediamtx's **RTSP re-serve**
   (not its HLS output) with `-t <duration>` to enforce the exact
   cutoff, `-c:v copy -an` (video-only, see below), writing an MP4.
4. Return the file (`FileResponse` with a `BackgroundTask` that
   deletes the temp file after it's sent), tear down the dynamic path.

Two dead ends hit along the way, kept here so they aren't retried:

- **Pulling directly from the DVR's playback RTSP with ffmpeg hung**
  (connected fine, got valid SDP, then zero packets until timeout) —
  even though mediamtx's RTSP client pulled the *identical* source
  URL without issue. Specific to how ffmpeg's RTSP client negotiates
  with this DVR's playback endpoint; not investigated further since
  routing through mediamtx works. So capture always goes DVR → mediamtx
  → (RTSP re-serve) → ffmpeg, never DVR → ffmpeg directly.
- **Capturing from mediamtx's HLS output** (instead of its RTSP
  re-serve) hit segment-pruning 404s mid-capture — LL-HLS prunes old
  segments faster than a capture can reliably keep up. RTSP re-serve
  has no such windowing.
- **Transcoding this DVR's G.711 audio to AAC for the mp4 container
  reliably hung ffmpeg** (packets stopped flowing entirely, even
  though video-only capture from the same source worked immediately).
  Not root-caused; downloads are video-only for now. Most channels
  here don't have audio enabled on their main stream anyway.
- The DVR **doesn't reliably stop at the RTSP source's `endtime`**
  once mediamtx is in the loop — a 30-second requested window kept
  streaming for 39+ seconds until the client's own timeout killed it.
  The real cutoff is enforced by ffmpeg's `-t`, not by trusting the
  DVR/mediamtx to stop on their own.

## Snapshot design

`/api/snapshot` doesn't use ISAPI's own `/ISAPI/Streaming/channels/<id>/picture`
endpoint. Confirmed empirically: on this firmware it always returns a
fixed 704x576 (4:3) JPEG regardless of the channel's actual configured
main-stream resolution (e.g. 1920x1080 or 1280x720, 16:9) — squashing
the real 16:9 sensor image into that frame produces a visibly
distorted snapshot. It also flatly `400`s for the IP-proxy channels
(9/10) rather than returning anything.

Instead, `MediaBridge.capture_frame` grabs a real frame from the same
mediamtx path the live view itself plays, via the RTSP re-serve (same
approach as clip downloads, see below) — `ffmpeg -frames:v 1` against
`rtsp://viewer:{AUTH_KEY}@127.0.0.1:{RTSP_PORT}/{name}`. This matches
the live view's actual proportions and works for every channel,
including 9/10 where the ISAPI endpoint didn't work at all.

One IP-proxy channel (confirmed on channel 9 specifically, not every
source) only sends H.264 parameter sets every few seconds — too
infrequent for the instant single-frame grab, which fails fast with
"Could not find codec parameters" instead of waiting (empirically,
`-t 2` of connection time isn't enough, `-t 3`+ reliably is).
`capture_frame` tries the instant grab first (works immediately for
every other channel) and only falls back to a short stream-copy
capture + frame-extraction from that local file when the fast path
fails — keeping the common case fast while still working reliably for
this one channel's slower parameter-set cadence.

## Continuous playback across recording segments

Playback of a single segment used to just silently freeze once it
reached the end of its requested time range — confirmed by
instrumenting the `<video>` element through a real boundary: `ended`
**never fires**. Instead `waiting` → `pause` fires and `currentTime`
freezes permanently, with no error surfaced anywhere. mediamtx's own
log explains why: `[RTSP source] stopped: an error occurred` →
`muxer destroyed: terminated` — the DVR closing the playback RTSP
session at the end of the requested range is treated as a transport
**error**, not a clean end-of-stream signal (a deliberate
`/api/playback/stop` instead logs the different `stopped: path is
closing`). There is no reliable event-based way to detect "segment
finished" on this DVR.

The fix (`static/playback.html`) is a client-side timer, not event
detection: `scheduleAdvance` computes the current segment's known
duration and fires ~4.5s before its expected natural end, calling
`advanceToNext` to start the next segment proactively — before the
freeze/error state is ever reached. A bounded drift-check
(`armAdvanceCheck`) compares `video.currentTime` against expected
elapsed time first and re-arms up to 3 times if the video is still
rebuffering, rather than cutting a stalled segment short.

Chaining reuses `/api/playback/start` unmodified (no new endpoint) —
calling it again with `startTime` = the previous segment's end just
finds and plays whatever comes next. This needed one real backend fix
first, caught by a design-review pass before implementation: naively
trusting the caller's `startTime` would ask the DVR to stream from a
timestamp inside a real recording gap (if one exists) rather than the
next segment's actual start, reproducing the exact freeze bug instead
of skipping over the gap. `start_playback` now clamps server-side —
`effective_start = max(requested_start, match["startTime"])` — and
returns the resolved `segmentStartTime`/`segmentEndTime` in its
response (previously just `{name, hlsPath, hlsPort}`) so the frontend
knows the *real* boundaries, not just what it optimistically requested
(needed for both accurate gap-vs-contiguous labeling and scheduling the
*next* advance correctly).

Verified against the real DVR: jumped to 10s before a known segment
end, confirmed via the DVR's own on-screen timestamp overlay
(extracted with `ffmpeg -frames:v 1` directly against the HLS URL,
bypassing the browser entirely) that the stream kept flowing correctly
more than 3 minutes past the transition point — not just that the API
returned success. (A `<video>` element in the browser tab used for
testing appeared to "pause" after the transition; that turned out to
be Chrome's background-tab power-saving policy pausing video-only
media in a non-focused automation tab — `AbortError: ... paused to
save power` — not a bug in the app. Confirmed the actual stream was
fine independent of that via the direct ffmpeg frame extraction above.)
Also verified: `stopCurrent()` clears any pending advance timer (so
manually navigating away — clicking a different segment, pressing
Stop — cancels a scheduled auto-advance), and no orphaned mediamtx
paths accumulate across a chain of transitions (each transition's
`stopCurrent()` cleanly tears down the previous path before the next
one is created — matched started/stopped counts in mediamtx's log
across a full test session).

Explicit non-goals: cross-midnight/cross-day continuation (the search
upper bound used when chaining is same-day only; running off the end
of a day's recordings just stops cleanly with the existing "No
recording found" error rather than attempting date-rollover logic); a
true seamless crossfade via a second hidden `<video>` element (rejected
as excess complexity for a single-user local tool — a brief ~1-2s gap
during the source swap is accepted); an opt-out toggle (always-on by
design).

## Day timeline scrubber

A horizontal bar (`static/playback.html`, `.timeline`) below the
`<video>` element, showing the currently-loaded day's recorded
segments as blocks and gaps as empty space, click-to-seek. Complements
the segment list and jump-to-time rather than replacing either.

**Seeking reuses `startPlaybackAt` directly** — a click computes a
wall-clock time and calls the exact same function segment-clicks/
jump-to-time/continuous-auto-advance already share (`seekTimeline`).
Clicking a gap region gets the existing backend clamp-to-next-real-
segment + "Melompati jeda rekaman" label for free, since that logic
lives in `start_playback` (`app/main.py`) and applies to any caller —
no new backend code for this feature at all.

**Time math is pure fraction-of-day arithmetic — never a `Date`
object** (`secondsSinceMidnight`, `seekTimeline`): click X → fraction
of width → seconds since midnight → zero-padded H/M/S concatenated
into the literal-digit ISO string with the loaded date. This sidesteps
the fake-UTC subtlety entirely (nothing to reason about, since no
`Date` is ever constructed) — stricter than the `new Date(...)`
duration-arithmetic already used elsewhere in this file for gap
detection and download defaults (safe there only because both operands
share the same fake-Z offset and it cancels out in the subtraction).
Zero-padding matters: an unpadded `5` instead of `05` would build a
malformed timestamp string the backend can't parse correctly.

A segment can start the previous calendar day and end within the
loaded day (the DVR's segments are contiguous — see the
22:33→00:02 example elsewhere in this doc) — `secondsSinceMidnight`
clips both ends to `[0, 86400]` for whichever date doesn't match
`loadedDate`, using plain string comparison on the date portion
(works fine for `"YYYY-MM-DD"`, lexicographic order matches
chronological order) rather than any date parsing.

**Position marker**: one `timeupdate` listener attached once at page
load, not re-attached per `startPlaybackAt` call — the `<video>`
element is never recreated, only `hls.attachMedia`/`destroy()` cycles
around it (`stopCurrent`/`startPlaybackAt`), so attaching per-call
would stack duplicate listeners across a long continuous-playback
session. Also explicitly redrawn (`updateMarker()`) at both points
`currentSeg` gets reassigned inside `startPlaybackAt` — not solely
relying on the next `timeupdate`, which would leave a visible window
showing the *previous* segment's stale marker position — and inside
`stopCurrent()`, where `currentSeg` becomes `null`; without that call
the marker would freeze in place after Stop instead of disappearing.
Hidden whenever nothing is playing or the playing content isn't from
`loadedDate` (e.g. a jump-to-time to a different day than what's
displayed).

**Rendering**: segment blocks get a CSS `min-width` (2px) so short
motion-triggered clips don't render as invisible/unclickable slivers —
minor visual overlap on a busy day is an accepted tradeoff. Verified
against the real DVR: a ~89-second segment (22:33:16→00:02:27,
clipped to 0→147s of the loaded day) rendered at exactly the expected
~2.4px, right at the floor.

**Click-to-seek only**, no drag-with-live-preview — a deliberate
simplicity tradeoff, not a limitation to fix later.

Verified against the real DVR: clicking near the "12" (noon) tick
landed on the actual segment covering that time, both the DVR's
on-screen timestamp overlay and the marker position matched (`~50%`
for a click near noon); the marker moved correctly in lockstep with
`video.currentTime` when nudged past the same Chrome background-tab
pause described in the continuous-playback section above (a testing-
environment artifact, not a bug — confirmed by checking `video.paused`
and `document.hidden` directly); Stop correctly clears the marker;
segment-click and jump-to-time both regression-checked as unaffected.

## IP-proxy channels (9/10)

Channels 9/10 on this DVR are ONVIF cameras proxied through it, not
analog inputs — discovered via a completely separate ISAPI list,
`/ISAPI/ContentMgmt/InputProxy/channels` (`InputProxyChannel` entries),
which `/ISAPI/System/Video/inputs/channels` (analog-only) never
returns. Their streaming-channel entries (`/ISAPI/Streaming/channels`)
also key back to the input differently — `dynVideoInputChannelID`
instead of `videoInputChannelID` — confirmed once these channels came
online (`app/main.py`: `_build_ip_channel`/`_streams_for_channel` keys
off whichever field is present).

Once discovered, these channels reuse the same channel-ID numbering
scheme as everything else (`_main_track_id` = `channel_id * 100 + 1`,
so channel 9 → track/stream `901`) and the same RTSP live path
(`rtsp://host:554/Streaming/Channels/901`) — live view, search
(`CMSearch`), playback, and download all work against them unmodified.

Two endpoints don't, though, both firmware-side on this DVR (not
account-privilege related — same read-only account works fine for
channels 1-4 on these same endpoints):

- **PTZ capabilities** (`/ISAPI/PTZCtrl/channels/9/capabilities`) →
  `400 badXmlContent`, not `404`. `get_ptz_capabilities` treats this
  the same as "no PTZ" rather than raising — but PTZ *control* for
  these channels works via a different endpoint anyway, see "PTZ for
  IP-proxy channels" below.
- **Snapshot** (`/ISAPI/Streaming/channels/901/picture`) → same `400
  badXmlContent`. Not fixed — `/api/snapshot?channelId=9` currently
  surfaces this as a raw 500. Revisit if snapshot support for these
  channels is needed; no workaround found yet.

Channel 10's video-encoding type (H.265) can't be switched to H.264
from the DVR's web UI even with a full admin account — confirmed the
field is disabled there regardless of privilege. Root cause: this
proxy channel's `InputProxyChannel` entry has `streamType: auto`,
meaning the DVR mirrors whatever encoding the camera itself is set to
rather than encoding the stream on its own hardware, unlike analog
channels where the DVR *is* the encoder. Not fixable on the camera
side either — no encoding menu in its companion app (Yoosee). Instead,
`ch10_main` is transcoded server-side (see "H.265→H.264 transcode for
main streams" in the media bridge design below).

## PTZ for IP-proxy channels

Channels 9/10 got PTZ-capable cameras. The DVR's PTZ subsystem doesn't
model them at all, though — `/ISAPI/PTZCtrl/channels` (the full list,
no ID filter) only ever returns entries for the 4 analog channels.
Querying channel 9 directly is inconsistent: `capabilities` and
`status` both fail (`400`/`403`), but `presets` succeeds (returns an
empty list) — and, surprisingly, actually issuing a **continuous move**
command (`PUT /ISAPI/PTZCtrl/channels/9/continuous`) returns `200 OK`
*and* really moves the camera. This was confirmed by direct physical
observation (not just trusting the DVR's response, per this project's
usual verification standard) — an initial frame-comparison check
looked identical before/after, but that was almost certainly a false
negative from a low-texture scene, not a real absence of movement.

So: don't trust `/capabilities` or `/status` for these channels — they
don't reflect what `/continuous` can actually do. `_build_ip_channel`
(`app/main.py`) hardcodes `ptz.enabled = true` for any enabled
IP-proxy channel rather than relying on the broken capabilities check.

Sign convention: **positive `pan` turns the camera right**, confirmed
by observation for channel 9. Tilt/zoom sign follow the same
documented ISAPI convention (positive tilt up, positive zoom in) but
haven't been independently verified the same way.

Separately, both cameras were probed directly over ONVIF
(`GetCapabilities` against `http://<camera-ip>:5000/onvif/device_service`,
no auth needed for this call) — they do advertise a PTZ service, at
`.../onvif/deviceio_service`. Not used by this project (the DVR
relay above works and needs no camera credentials, which we don't
have), but noted here since it confirms these are genuinely
PTZ-capable devices, not just a DVR fluke. That same probe also
turned up a device quirk worth flagging: the `DeviceIO` extension in
the capabilities response advertises a stale internal XAddr
(`http://192.168.1.33:5000/...`) instead of the camera's real
reachable IP — leftover config from before the camera moved onto this
subnet. Not relevant to PTZ, but a trap for any ONVIF client that
blindly follows advertised XAddrs instead of the known camera IP.

**API**: `PUT /api/ptz/{channelId}/continuous` (body: `pan`/`tilt`/`zoom`,
each -100..100) and `PUT /api/ptz/{channelId}/stop` — both just proxy
straight to the DVR's own endpoint (`ISAPIClient.ptz_continuous_move`/
`ptz_stop` in `app/isapi.py`), no special handling needed since the DVR
already does the right thing once you skip the broken capability
checks. The live-view overlay (`static/index.html`) shows a D-pad +
zoom buttons for any channel with `ptzEnabled`, using pointerdown/up to
start/stop continuous movement (hold to move, release to stop).

## Auth

One shared secret (`AUTH_KEY` in `.env`) gates both layers this app
exposes on the LAN — there's no per-user login, just a single key.
Two layers exist because video never flows through FastAPI (see
"Components" above): it goes DVR → mediamtx → browser directly, with
mediamtx's HLS (`:8888`)/WebRTC (`:8889`) listeners bound on all
interfaces. Protecting only the API would leave the actual footage
open.

- **`/api/*` (`app/main.py`)**: a single `@app.middleware("http")`
  checks `X-Auth-Key` against `AUTH_KEY` for any path under `/api/`,
  `401`s otherwise. Everything else (`/`, `/playback`, `/static/*`,
  `/healthz`) stays open — no session/cookie/redirect machinery, just
  a header check on the endpoints that actually touch the DVR.
- **mediamtx (`app/mediabridge.py`, or `mediamtx/base.yml` in
  production — see "mediamtx as a separate Swarm service" above)**:
  `authInternalUsers` defines three entries, identical in shape in
  both self-managed and network mode so bare-metal/local-dev and
  production behave the same way:
  - `viewer` — password `AUTH_KEY`, `read` only, unrestricted by IP.
    Real LAN browsers authenticate as this (`static/auth.js`).
  - `backend` — password `AUTH_KEY`, `api` only, unrestricted by IP.
    `add_playback_path`/`remove_playback_path` and (network mode only)
    the live-view path push in `MediaBridge.start()` authenticate as
    this. Unrestricted by IP because in network mode these calls
    originate from a different container's overlay-network IP, not
    loopback — an IP-based exemption the way self-managed mode used to
    have can't work there.
  - `any` — no password, `publish` only, restricted to
    `127.0.0.1`/`::1`. The H.265 transcode ffmpeg
    (`_transcode_path` above) publishing back into the RTSP re-serve —
    always spawned by mediamtx itself, same container/process, so
    still genuinely loopback in both modes.

  `authInternalUsers` **replaces** mediamtx's default user list rather
  than merging with it (confirmed directly against the binary — env-var
  overrides merge onto the compiled-in defaults per-index instead, see
  "mediamtx as a separate Swarm service" above) — dropping any of the
  three above breaks the corresponding calls with `401`s.
  `capture_clip`'s ffmpeg (which *reads* from the RTSP re-serve to
  build a download clip) isn't covered by any of the three, so it
  authenticates as `viewer:{AUTH_KEY}` explicitly.
- **Frontend (`static/auth.js`)**: shared by both pages.
  `ensureAuthKey()` shows a small login overlay if no key is cached in
  `localStorage`, validating it immediately against `/api/device`
  before accepting it. `authFetch()` wraps `fetch` with the
  `X-Auth-Key` header and clears/re-prompts on a `401` — guarded so
  concurrent requests share one re-prompt instead of each popping the
  form independently. `hlsXhrSetup()` sets Basic Auth
  (`viewer:{key}`) on hls.js's XHR loader.
- **Known limitation**: the Safari-native-HLS fallback path
  (`video.src = hlsUrl`, used when `Hls.isSupported()` is false) can't
  attach custom headers — no XHR is involved, so that path can't carry
  the `viewer` credential. It's already a secondary fallback, not the
  primary playback path, so this is accepted rather than fixed.

## Known device quirks and bugs

These are DVR-firmware behaviors (V4.30.300), not bugs in this
codebase — documented here so they don't get "fixed" again or
accidentally reintroduced.

- **Chrome/Chromium has no HEVC-via-MSE support.** H.265 live channels
  show a black frame in the browser (confirmed via
  `MediaSource.isTypeSupported('video/mp4; codecs=hvc1...')` →
  `false`); mediamtx correctly serves the HEVC HLS stream regardless
  (verified with `ffprobe`), so this isn't fixable in our pipeline —
  the channel's codec has to be H.264 for browser playback. See
  `MEMORY.md` for which channels are currently set to which codec.
- **`playbackURI` goes stale.** An hour-old one from an earlier
  `CMSearch` got `400 Bad Request` from the DVR's RTSP server.
  `/api/playback/start` always re-searches immediately before handing
  the URI to mediamtx rather than reusing a URI a user looked at
  minutes earlier.
- **`CMSearch` exact-boundary bug.** A search `startTime` that exactly
  equals a segment's start returns the *previous* segment instead of
  the requested one. Found by extracting an HLS frame with `ffmpeg
  -frames:v 1` and reading the DVR's own on-screen timestamp overlay —
  the only reliable way to verify what the DVR actually played, since
  ISAPI/RTSP responses alone don't prove it. Fixed by nudging the
  search start 2 seconds forward (`_nudge_time` in `app/main.py`)
  before re-searching, landing inside the segment instead of on its edge.
- **ISAPI timestamps aren't real UTC despite the "Z" suffix.** They're
  the DVR's own local wall-clock digits (WIB, UTC+7), unconverted —
  confirmed against `/ISAPI/System/time` (correctly NTP-synced) and a
  playback frame's on-screen overlay. Any code touching these
  timestamps (search results, playback requests, future snapshot/event
  timestamps) must read/write them as **literal digits** — regex
  extraction, plain string formatting — never through `Date`/
  `toISOString()`/timezone-aware datetime math, which silently applies
  the browser's or server's real timezone on top and only looks
  correct by accident when that timezone happens to be UTC.
