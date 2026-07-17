# AGENTS.md

Instructions for AI coding agents (and human contributors) working in
this repo.

## What this is

A Linux port of Hikvision's Windows-only "Local Service Component"
DVR web plugin — a local Python backend + static web UI providing live
view, playback/search, and (soon) snapshot/download, talking to the
DVR over ISAPI (HTTP) and RTSP instead of any closed-source SDK. Full
design in `ARCHITECTURE.md`; current phase/status in `PLAN.md` and
`MEMORY.md`.

## Setup

This box has no system `pip`/`ensurepip` — bootstrap it manually:

```
python3 -m venv .venv --without-pip
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in HIKVISION_PASSWORD — never commit the real value
```

mediamtx (the RTSP-to-browser media bridge) must be present at
`mediamtx/mediamtx` — download the `linux_amd64` release binary from
https://github.com/bluenviron/mediamtx/releases if it isn't already
there (it's gitignored, not vendored in the repo).

## Run

```
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8896
```

This also spawns mediamtx as a child process. Live view:
`http://127.0.0.1:8896/static/index.html`; playback:
`http://127.0.0.1:8896/static/playback.html`.

## No automated test suite

There isn't one — this project's real dependency is a physical DVR,
and most bugs so far have been DVR-firmware behaviors (see "Read this
before touching..." below) that only show up against the real device.
**Verify changes by actually running the backend against the real DVR
and exercising the feature** (curl the endpoint, or drive the page in
a browser), not just by reading the code. When a fix depends on what
the DVR actually did (e.g. which recording segment played), don't
trust the ISAPI/RTSP response alone — extract a frame
(`ffmpeg -frames:v 1 ...`) and read the DVR's own on-screen timestamp
overlay, which is how the two playback bugs in `ARCHITECTURE.md` were
actually confirmed.

## Read this before touching ISAPI timestamps or playback

The DVR's ISAPI timestamps are labeled UTC (`Z` suffix) but are
actually its own local wall-clock digits, unconverted. Never round-trip
them through `Date`/`toISOString()`/timezone-aware datetime math —
read and write them as literal digit strings. Full explanation in
`ARCHITECTURE.md` under "Known device quirks and bugs" — that section
also covers the `CMSearch` exact-boundary bug and `playbackURI`
staleness. Skipping this before touching search/playback code is the
single most likely way to reintroduce a bug that's already been fixed
once.

## Secrets

Never commit the DVR password, host, or username, or any file
containing them. `.env` is gitignored and holds all three
(`HIKVISION_HOST`/`HIKVISION_USERNAME`/`HIKVISION_PASSWORD`, see
`.env.example`); `config.yaml` is tracked and holds only non-identifying
protocol config (ports). See `.gitignore` before adding any new
local/generated file that might embed credentials (mediamtx's
generated runtime config does, for example).

## Conventions

- Commit granularly — one commit per logical change/finding, not one
  giant commit per session. This has been the working style throughout
  and made the history genuinely useful for tracing why a fix exists.
- Keep `PLAN.md` (roadmap/status), `ARCHITECTURE.md` (technical
  reference), and `MEMORY.md` (device state snapshot) up to date as
  you go — don't let them drift from what the code actually does.
- No build step for the frontend (`static/`) — plain HTML/JS, vendor
  dependencies locally (see `static/vendor/hls.min.js`) rather than
  pulling from a CDN, so the local service stays self-contained.
