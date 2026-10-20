# Shared setup keeps credentials out of logs and alerts.
set -euo pipefail
umask 077
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
ENV_FILE=''
SNAPSHOT_REMOTE=''
SNAPSHOT_DIR=''
WEEKLY_CONTAINER=''

# Notification is optional and configured by env: a ntfy topic reached through
# a container (ARGUS_NTFY_CONTAINER + ARGUS_NTFY_TOPIC, optional ARGUS_NTFY_URL)
# and/or a command (ARGUS_NOTIFY_CMD) that receives "title: body" and a level
# (info|warn). With none set, notify is a no-op and the log is the only record.
#
# The API container is found in this order: ARGUS_API_CONTAINER (a name or id), then
# the argus-api service of the compose file in ARGUS_COMPOSE_FILE, then a container
# named argus-api (only when ARGUS_COMPOSE_FILE is unset: a compose file with no
# running API stops the run). A stock compose install names its containers after the project,
# so set ARGUS_COMPOSE_FILE to the docker-compose.yml you started it with.
# API_IMAGE is the image that container runs; ARGUS_API_IMAGE overrides it.
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
API_IMAGE="${ARGUS_API_IMAGE:-$(docker inspect --format '{{.Config.Image}}' "$API_CONTAINER" 2>/dev/null || true)}"
API_IMAGE="${API_IMAGE:-argus-api:latest}"
notify() {
    local title="$1" body="$2" priority="${3:-high}" level=warn
    [ "$priority" != low ] || level=info
    if [ -n "${ARGUS_NTFY_CONTAINER:-}" ] && [ -n "${ARGUS_NTFY_TOPIC:-}" ]; then
        docker exec "$ARGUS_NTFY_CONTAINER" wget -q -O- \
            --header="Title: $title" --header="Priority: $priority" \
            --post-data="$body" "${ARGUS_NTFY_URL:-http://localhost:80}/$ARGUS_NTFY_TOPIC" >/dev/null 2>&1 || true
    fi
    if [ -n "${ARGUS_NOTIFY_CMD:-}" ] && [ -x "$ARGUS_NOTIFY_CMD" ]; then
        (cd / && timeout 15 "$ARGUS_NOTIFY_CMD" "$title: $body" "$level") >/dev/null 2>&1 || true
    fi
}

cleanup() {
    local status=$?
    [ -z "$ENV_FILE" ] || rm -f "$ENV_FILE"
    [ -z "$SNAPSHOT_DIR" ] || rm -rf "$SNAPSHOT_DIR"
    if [ -n "$WEEKLY_CONTAINER" ]; then
        docker rm -f "$WEEKLY_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [ -n "$SNAPSHOT_REMOTE" ]; then
        docker exec "$API_CONTAINER" rm -f "$SNAPSHOT_REMOTE" "$SNAPSHOT_REMOTE-wal" "$SNAPSHOT_REMOTE-shm" \
            "$SNAPSHOT_REMOTE.repos.json" >/dev/null 2>&1 || true
    fi
    if [ "$status" -eq 3 ] && [ "$JOB" = weekly ]; then
        local alert
        if alert=$(docker run --rm --no-healthcheck --user 1000:1000 --network none \
            -v "$OUT:/output:ro" "$IMAGE" python -m argus.evalkit.cron alert-summary \
            --report /output/report.json); then
            notify 'Argus regression alert' "$alert. Report: $OUT/report.json"
        else
            status=1
        fi
    fi
    if [ "$status" -ne 0 ] && { [ "$status" -ne 3 ] || [ "$JOB" != weekly ]; }; then
        notify 'Argus evaluation failed' "$JOB failed with exit $status. See $LOG"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

build_environment() {
    ENV_FILE=$(mktemp)
    chmod 600 "$ENV_FILE"
    docker exec "$API_CONTAINER" python -m argus.evalkit.cron environment "$@" >"$ENV_FILE" 2>/dev/null
}
