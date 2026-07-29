#!/bin/sh
# Docker/Swarm secrets are mounted as files under /run/secrets/<name>,
# not injected as env vars — app/config.py reads plain env vars, so
# translate any present secret files into env vars before exec'ing the
# real command. Falls through untouched if a secret isn't mounted (e.g.
# plain `docker run` with HIKVISION_PASSWORD/AUTH_KEY passed directly).
set -eu

[ -f /run/secrets/hikvision_password ] && export HIKVISION_PASSWORD="$(cat /run/secrets/hikvision_password)"
[ -f /run/secrets/auth_key ] && export AUTH_KEY="$(cat /run/secrets/auth_key)"

exec "$@"
