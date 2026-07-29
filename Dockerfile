FROM python:3.12-slim

# Pinned, not "latest" — reproducible builds. Bump deliberately via
# --build-arg when upgrading. TARGETARCH is set automatically by
# BuildKit to the build's target platform (amd64/arm64), matching the
# two architectures run.sh supports for bare-metal installs.
ARG MEDIAMTX_VERSION=v1.19.3
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chown up front, not a separate `chown -R` afterwards: on overlayfs,
# chowning files that already exist in a lower layer forces a full
# copy-up, silently doubling their size in the image (this cost a
# ~54MB duplicate layer here before). Owning them correctly at
# COPY/create time avoids that entirely.
COPY --chown=appuser:appuser app/ app/
COPY --chown=appuser:appuser static/ static/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64|arm64) mediamtx_arch="${TARGETARCH}" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p mediamtx tmp/downloads tmp/snapshots; \
    curl -sSL \
      "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${mediamtx_arch}.tar.gz" \
      -o /tmp/mediamtx.tar.gz; \
    tar -xzf /tmp/mediamtx.tar.gz -C mediamtx; \
    chmod +x mediamtx/mediamtx /usr/local/bin/docker-entrypoint.sh; \
    rm /tmp/mediamtx.tar.gz; \
    chown -R appuser:appuser mediamtx tmp

USER appuser

EXPOSE 8896 8888 8889

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8896/healthz || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8896"]
