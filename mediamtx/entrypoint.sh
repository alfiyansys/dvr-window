#!/bin/sh
# Entrypoint for the mediamtx Swarm service, delivered via a Swarm
# `configs:` mount (not baked into a custom image - see PLAN.md
# "Chosen fix: split mediamtx into its own Swarm service"). Swarm
# secrets mount as files under /run/secrets/<name>, and mediamtx has
# no _FILE-suffix env var convention to read one directly, so this
# substitutes AUTH_KEY into the mounted base config before exec'ing
# mediamtx.
set -eu

secret="$(cat /run/secrets/auth_key)"

# AUTH_KEY is user-supplied (see app/config.py's fail-closed load) and
# could contain sed-special characters - escape &, the | delimiter
# used below, and \ itself before using it as replacement text.
escaped_secret="$(printf '%s' "$secret" | sed -e 's/[&|\]/\\&/g')"

sed "s|__AUTH_KEY__|${escaped_secret}|g" /config/mediamtx.yml > /tmp/mediamtx.yml

exec /mediamtx /tmp/mediamtx.yml
