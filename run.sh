#!/usr/bin/env bash
# Idempotent setup + run: does first-time setup (venv, deps, mediamtx binary)
# only when it's actually missing, then always starts the backend. Safe to
# run every time — a "next run" just skips straight to starting.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

first_run=false

# --- .env must already exist and be filled in (see .env.example) ---
if [[ ! -f .env ]]; then
  echo "[run.sh] .env not found. Copy .env.example to .env and fill in" >&2
  echo "[run.sh] HIKVISION_HOST/HIKVISION_USERNAME/HIKVISION_PASSWORD/AUTH_KEY first." >&2
  exit 1
fi

missing=()
for key in HIKVISION_HOST HIKVISION_USERNAME HIKVISION_PASSWORD AUTH_KEY; do
  if ! grep -qE "^${key}=.+" .env; then
    missing+=("$key")
  fi
done
if (( ${#missing[@]} > 0 )); then
  echo "[run.sh] .env is missing a value for: ${missing[*]}" >&2
  exit 1
fi

# --- Python virtualenv (this box has no system pip, see AGENTS.md) ---
if [[ ! -x .venv/bin/python ]]; then
  first_run=true
  echo "[run.sh] First run: creating virtualenv (.venv)..."
  python3 -m venv .venv --without-pip
  curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
fi

if ! .venv/bin/python -c "import fastapi" >/dev/null 2>&1; then
  first_run=true
  echo "[run.sh] First run: installing Python dependencies..."
  .venv/bin/pip install -r requirements.txt
fi

# --- mediamtx binary (gitignored, not vendored — see AGENTS.md) ---
if [[ ! -x mediamtx/mediamtx ]]; then
  first_run=true
  echo "[run.sh] First run: downloading mediamtx..."

  arch="$(uname -m)"
  case "$arch" in
    x86_64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *)
      echo "[run.sh] Unsupported architecture: $arch" >&2
      echo "[run.sh] Download mediamtx manually from https://github.com/bluenviron/mediamtx/releases" >&2
      echo "[run.sh] and place the binary at mediamtx/mediamtx" >&2
      exit 1
      ;;
  esac

  asset_url="$(curl -sS https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
    | grep -o "\"browser_download_url\": *\"[^\"]*linux_${arch}\.tar\.gz\"" \
    | head -n1 \
    | sed -E 's/.*"(https[^"]+)"/\1/')"

  if [[ -z "$asset_url" ]]; then
    echo "[run.sh] Could not find a mediamtx linux_${arch} release asset." >&2
    echo "[run.sh] Download it manually from https://github.com/bluenviron/mediamtx/releases" >&2
    echo "[run.sh] and place the binary at mediamtx/mediamtx" >&2
    exit 1
  fi

  mkdir -p mediamtx
  tmp_tar="mediamtx/mediamtx.tar.gz"
  curl -sSL "$asset_url" -o "$tmp_tar"
  tar -xzf "$tmp_tar" -C mediamtx
  chmod +x mediamtx/mediamtx
  rm -f "$tmp_tar"
fi

if [[ "$first_run" == true ]]; then
  echo "[run.sh] First-run setup complete."
else
  echo "[run.sh] Environment already set up."
fi

# SERVER_HOST/SERVER_PORT are optional in .env (defaults match app/config.py).
server_host="$(grep -E '^SERVER_HOST=' .env | cut -d '=' -f2- || true)"
server_port="$(grep -E '^SERVER_PORT=' .env | cut -d '=' -f2- || true)"
server_host="${server_host:-127.0.0.1}"
server_port="${server_port:-8896}"

echo "[run.sh] Starting dvr-window on http://${server_host}:${server_port}/"
exec .venv/bin/python -m uvicorn app.main:app --host "$server_host" --port "$server_port"
