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
API_CONTAINER="${ARGUS_API_CONTAINER:-argus-api}"
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
