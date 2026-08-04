FROM python:3.12-slim

# mediamtx no longer runs inside this image — it's its own Swarm
# service now (bluenviron/mediamtx image, see docker-compose.swarm.yml
# and PLAN.md "Chosen fix: split mediamtx into its own Swarm service").
# ffmpeg stays: capture_clip/capture_frame (app/mediabridge.py) still
# spawn it directly from this container for downloads/snapshots. Only
# bare-metal installs (run.sh) still self-manage a local mediamtx
# subprocess/binary — unaffected by this image.

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

RUN mkdir -p tmp/downloads tmp/snapshots; \
    chmod +x /usr/local/bin/docker-entrypoint.sh; \
    chown -R appuser:appuser tmp

USER appuser

EXPOSE 8896

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8896/healthz || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8896"]
