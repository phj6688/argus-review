#!/usr/bin/env bash
# Replay evaluation cases with the source of this checkout and its own venv.
# Usage: argus_replay.sh --cases FILE --out FILE --variant NAME [replay options...]
# ARGUS_MIRROR_ROOT and ARGUS_EVAL_CACHE pass through to replay.
# ARGUS_API_CONTAINER names the running API container (a name or id). Without it,
# ARGUS_COMPOSE_FILE selects the argus-api service of that compose file, and the run
# stops when that service is not running. With neither set, the default is a container
# named argus-api.
# ARGUS_EVAL_AUTH_TOKEN is the model credential when set. Without it, the production
# token is copied from the API container into this process environment only, never argv or logs.
{
# Tracing would print the copied token, so it stays off even under bash -x or SHELLOPTS.
set +x
set -euo pipefail
umask 077
CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PYTHON="${ARGUS_REPLAY_PY:-$CHECKOUT/.venv/bin/python}"
[ -x "$PYTHON" ] || { echo "Missing $PYTHON. Create the venv in $CHECKOUT first." >&2; exit 1; }
# A non-editable or stale venv would replay other code than this checkout holds.
export PYTHONPATH="$CHECKOUT/src${PYTHONPATH:+:$PYTHONPATH}"
LOADED=$("$PYTHON" -c 'import os, sys, argus; sys.stdout.write(os.path.realpath(argus.__file__))')
case "$LOADED" in
    "$CHECKOUT/src/argus/"*) ;;
    *) echo "$PYTHON loads argus from $LOADED, not from $CHECKOUT/src" >&2; exit 1 ;;
esac

API_CONTAINER="${ARGUS_API_CONTAINER:-}"
if [ -z "$API_CONTAINER" ] && [ -n "${ARGUS_COMPOSE_FILE:-}" ]; then
    API_CONTAINER=$(docker compose -f "$ARGUS_COMPOSE_FILE" ps -q argus-api 2>/dev/null | head -n 1 || true)
    # An explicit compose file that has no running API must stop the run. Falling
    # back to a container named argus-api could read another install's data.
    if [ -z "$API_CONTAINER" ]; then
        echo "No running argus-api service for ARGUS_COMPOSE_FILE=$ARGUS_COMPOSE_FILE" >&2
        exit 1
    fi
fi
API_CONTAINER="${API_CONTAINER:-argus-api}"
copy_from_api() {
    local value
    value=$(docker exec "$API_CONTAINER" printenv "$1") || { echo "Cannot read $1 from $API_CONTAINER" >&2; exit 1; }
    export "$1=$value"
}

if [ -n "${ARGUS_EVAL_AUTH_TOKEN:-}" ]; then
    echo 'Model credential: ARGUS_EVAL_AUTH_TOKEN' >&2
else
    copy_from_api ANTHROPIC_AUTH_TOKEN
    echo 'Model credential: ANTHROPIC_AUTH_TOKEN copied from '"$API_CONTAINER" >&2
fi
[ -n "${GITHUB_TOKEN:-}" ] || copy_from_api GITHUB_TOKEN
# Set ANTHROPIC_BASE_URL to route through a gateway; unset uses the SDK default.
[ -z "${ANTHROPIC_BASE_URL:-}" ] || export ANTHROPIC_BASE_URL
# Harness scratch writes stay out of the checkout and away from the production database.
# A shared, predictable /tmp path could be pre-created by another user (or be a
# symlink) and feed this run a seeded database. The default is a fresh private
# directory; a configured one must already be ours.
if [ -n "${ARGUS_REPLAY_SCRATCH:-}" ]; then
    SCRATCH="$ARGUS_REPLAY_SCRATCH"
    mkdir -p "$SCRATCH"
    if [ -L "$SCRATCH" ] || [ ! -d "$SCRATCH" ] || [ "$(stat -c %u "$SCRATCH")" != "$(id -u)" ]; then
        echo "ARGUS_REPLAY_SCRATCH $SCRATCH must be a real directory owned by $(id -un)" >&2
        exit 1
    fi
    chmod 700 "$SCRATCH"
else
    SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/argus-replay.XXXXXX")
fi
export ARGUS_DB_PATH="${ARGUS_DB_PATH:-$SCRATCH/replay.db}"
export ARGUS_PUBLISH=0 ARGUS_JUDGE_LIVE=0 ARGUS_WATCH_ENABLED=0
unset ARGUS_FALLBACK_MODEL
exec "$PYTHON" -m argus.evalkit.replay "$@"
}
