#!/usr/bin/env bash
# Set these paths before installing the cron entry.
{
WEEKLY_ROOT="${ARGUS_EVAL_OUTPUT:?Set ARGUS_EVAL_OUTPUT}"
MONTHLY_ROOT="${ARGUS_MONTHLY_OUTPUT:?Set ARGUS_MONTHLY_OUTPUT}"
NETWORK="${ARGUS_EVAL_NETWORK:-bridge}"
IMAGE="argus-api:latest"
LOG="${ARGUS_CRON_LOG_DIR:-$HOME/.local/state/argus}/argus_monthly.log"
JOB=monthly
MONTH="${ARGUS_MONTH:-$(date -u -d "$(date -u +%Y-%m-01) -1 month" +%Y-%m)}"
[[ "$MONTH" =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]] || { echo 'ARGUS_MONTH must be YYYY-MM'; exit 1; }
SINCE="$MONTH-01"
UNTIL=$(date -u -d "$SINCE +1 month" +%Y-%m-01)
OUT="$MONTHLY_ROOT/$MONTH"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

mkdir -p "$MONTHLY_ROOT"
chmod 700 "$MONTHLY_ROOT"
exec 9>"$MONTHLY_ROOT/monthly.lock"
flock -n 9 || exit 0
mkdir -p "$OUT"
chmod 700 "$OUT"
build_environment --reference-only
SNAPSHOT_DIR=$(mktemp -d /tmp/argus-monthly.XXXXXX)
SNAPSHOT_REMOTE="/tmp/argus-monthly-$(date -u +%s)-$$.db"
docker exec "$API_CONTAINER" python -m argus.evalkit.cron snapshot --out "$SNAPSHOT_REMOTE"
docker exec "$API_CONTAINER" python -m argus.evalkit.cron repos \
    --db "$SNAPSHOT_REMOTE" --out "$SNAPSHOT_REMOTE.repos.json"
docker cp "$API_CONTAINER:$SNAPSHOT_REMOTE" "$SNAPSHOT_DIR/copy.db"
docker cp "$API_CONTAINER:$SNAPSHOT_REMOTE.repos.json" "$OUT/repos.json"
chmod 600 "$SNAPSHOT_DIR/copy.db"
WEEKLY=$(find "$WEEKLY_ROOT" -regextype posix-extended -mindepth 2 -maxdepth 2 -type f \
    -regex '.*/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}/report.json' -print | sort | tail -n 1)
[ -n "$WEEKLY" ] || { echo 'No weekly report available'; exit 1; }

docker run --rm --no-healthcheck --user 1000:1000 --network "$NETWORK" \
    --env-file "$ENV_FILE" -e ARGUS_GITHUB_MIN_REMAINING=2500 \
    -v "$OUT:/output:rw" "$IMAGE" python -m argus.evalkit.reference \
    --repos /output/repos.json --since "$SINCE" --until "$UNTIL" \
    --out-rows /output/reference_rows.jsonl --out-summary /output/reference_summary.json \
    --out-prs /output/reference_prs.jsonl

run_module() {
    docker run --rm --no-healthcheck --user 1000:1000 --network none \
        -e ARGUS_PUBLISH=0 -e ARGUS_JUDGE_LIVE=0 \
        -e ARGUS_LLM_MAX_CONCURRENCY=1 -e ARGUS_WATCH_ENABLED=0 \
        -v "$OUT:/output:rw" -v "$SNAPSHOT_DIR/copy.db:/copy.db:ro" \
        -v "$WEEKLY:/weekly.json:ro" "$IMAGE" python -m "argus.evalkit.$1" "${@:2}"
}
run_module labels --db /copy.db --out /output/labels.jsonl --since "$SINCE" --until "$UNTIL"
run_module monthly --db /copy.db --weekly /weekly.json --reference /output/reference_summary.json \
    --reference-prs /output/reference_prs.jsonl \
    --month "$MONTH" --out-dir /output
SUMMARY=$(docker run --rm --no-healthcheck --user 1000:1000 --network none -v "$OUT:/output:ro" "$IMAGE" \
    python -m argus.evalkit.cron gate-summary --report /output/report.json)
notify 'Argus monthly outcomes' "$MONTH: $SUMMARY" low
exit
}
