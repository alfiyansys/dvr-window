FROM python:3.12-slim

# Pinned, not "latest" — reproducible builds. Bump deliberately via
# --build-arg when upgrading. TARGETARCH is set automatically by
# BuildKit to the build's target platform (amd64/arm64), matching the
# two architectures run.sh supports for bare-metal installs.
ARG MEDIAMTX_VERSION=v1.19.3
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY static/ static/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64|arm64) mediamtx_arch="${TARGETARCH}" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p mediamtx; \
    curl -sSL \
      "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${mediamtx_arch}.tar.gz" \
      -o /tmp/mediamtx.tar.gz; \
    tar -xzf /tmp/mediamtx.tar.gz -C mediamtx; \
    chmod +x mediamtx/mediamtx; \
    rm /tmp/mediamtx.tar.gz; \
    chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root: app writes tmp/downloads, tmp/snapshots, and mediamtx
# generates mediamtx/runtime.yml at startup — all need to be writable
# by the runtime user, not just readable.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p tmp/downloads tmp/snapshots \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8896 8888 8889

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8896/healthz || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8896"]
