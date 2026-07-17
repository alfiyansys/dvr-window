# DVR Window

A Linux-native replacement for Hikvision's Windows-only "Local Service
Component" DVR web plugin. A small Python backend + static web UI that
talks to the DVR directly over its documented **ISAPI** (HTTP) and
**RTSP** protocols — no closed-source SDK, no Windows box required.

## Features

- **Live view** — grid of all channels (analog + IP-proxy cameras),
  streamed to the browser via HLS.
- **Playback & search** — browse recordings by day, jump to a time,
  click a segment, or scrub a day timeline; continuous playback
  auto-advances across segment boundaries and skips real recording
  gaps instead of freezing.
- **Snapshot & download** — grab a still frame or export an MP4 clip
  for an arbitrary time range.
- **PTZ control** — pan/tilt/zoom for PTZ-capable channels via an
  on-screen D-pad.
- **Shared-key auth** — a single key gates both the API and the video
  streams themselves (live view/playback bypass the backend entirely,
  so both layers need protecting).

## How it works

Two independent paths, since video never flows through the backend:

```
CONTROL PLANE (channels, search, snapshot, download)
  Browser <--HTTP/JSON--> Backend (FastAPI) <--HTTP digest/XML--> DVR :80 (ISAPI)

MEDIA PLANE (live view + playback)
  DVR :554 (RTSP) --> mediamtx (sidecar) --> HLS/WebRTC --> Browser <video>
```

The backend (`app/`) speaks ISAPI to the DVR for control operations
and manages [mediamtx](https://github.com/bluenviron/mediamtx) as a
sidecar process that bridges RTSP to HLS/WebRTC for the browser. The
frontend (`static/`) is plain HTML/JS with no build step or CDN
dependencies.

See `ARCHITECTURE.md` for the full technical reference, including
DVR firmware quirks this project works around.

## Requirements

- Linux, Python 3
- A Hikvision DVR/NVR reachable on the LAN with ISAPI + RTSP enabled
- `mediamtx` binary (downloaded automatically by `run.sh` on first run)

## Setup

```bash
python3 -m venv .venv --without-pip
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your DVR host/credentials and a generated AUTH_KEY
```

Generate an `AUTH_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

## Run

```bash
./run.sh
```

Idempotent — does first-run setup automatically (venv, dependencies,
mediamtx download) if it hasn't run before, otherwise starts straight
away. Equivalent to:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8896
```

- Live view: `http://127.0.0.1:8896/`
- Playback: `http://127.0.0.1:8896/playback`

## Configuration

All configuration lives in `.env` (see `.env.example`) — never
committed. Required: `HIKVISION_HOST`, `HIKVISION_USERNAME`,
`HIKVISION_PASSWORD`, `AUTH_KEY`. Everything else (DVR/mediamtx ports,
local bind address) has sane defaults and is optional.

## Status

Live view, PTZ, playback/search, continuous playback, snapshot/download,
and auth are all implemented. Packaging (systemd service, install
script) is next — see `PLAN.md` for the full roadmap.

## Project docs

- `AGENTS.md` — contributor/agent instructions, conventions
- `ARCHITECTURE.md` — technical design, protocol reference, known DVR quirks
- `PLAN.md` — roadmap and phase status
