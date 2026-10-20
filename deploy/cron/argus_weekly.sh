#!/usr/bin/env bash
# Set these paths before installing the cron entry.
{
EVAL_DIR="${ARGUS_EVAL_DIR:?Set ARGUS_EVAL_DIR}"
STATE_DIR="${ARGUS_EVAL_STATE:?Set ARGUS_EVAL_STATE}"
OUTPUT_ROOT="${ARGUS_EVAL_OUTPUT:?Set ARGUS_EVAL_OUTPUT}"
MIRROR_ROOT="${ARGUS_MIRROR_ROOT:?Set ARGUS_MIRROR_ROOT}"
JUDGE_MODEL="${ARGUS_EVAL_JUDGE:?Set ARGUS_EVAL_JUDGE}"
NETWORK="${ARGUS_EVAL_NETWORK:-bridge}"
IMAGE="argus-eval:latest"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${ARGUS_CRON_LOG_DIR:-$HOME/.local/state/argus}/argus_weekly.log"
JOB=weekly
OUT="$OUTPUT_ROOT/$(date -u +%Y-%m-%dT%H%M%S)"
source "$DIR/common.sh"

mkdir -p "$STATE_DIR" "$OUTPUT_ROOT"
exec 9>"$STATE_DIR/weekly.lock"
flock -n 9 || exit 0
WEEKLY_CONTAINER=argus-weekly
docker rm -f argus-weekly >/dev/null 2>&1 || true
mkdir -p "$OUT"
docker build -q -t "$IMAGE" -f "$DIR/Dockerfile.eval" "$DIR"
build_environment
MODEL=$(docker exec "$API_CONTAINER" python -m argus.evalkit.cron model)
[ -n "$MODEL" ] || { echo 'Review model is missing'; exit 1; }
IMAGE_ID=$(docker image inspect --format '{{.Id}}' argus-api:latest)
timeout --signal=TERM --kill-after=300 11h docker run --rm --no-healthcheck --name argus-weekly \
    --user 1000:1000 --network "$NETWORK" \
    --env-file "$ENV_FILE" -e HOME=/tmp -e ARGUS_PUBLISH=0 -e ARGUS_JUDGE_LIVE=0 \
    -e ARGUS_EVAL_IMAGE_ID="$IMAGE_ID" \
    -e ARGUS_LLM_MAX_CONCURRENCY=1 -e ARGUS_WATCH_ENABLED=0 \
    -v "$EVAL_DIR:/evaluation:ro" -v "$STATE_DIR:/state:rw" \
    -v "$OUT:/output:rw" -v "$MIRROR_ROOT:/mirrors:ro" \
    "$IMAGE" python -m argus.evalkit.weekly run \
    --eval-dir /evaluation --state-dir /state --out-dir /output \
    --mirror-root /mirrors --model "$MODEL" --judge-model "$JUDGE_MODEL" --concurrency 1 \
    --replay-timeout-s 21600 --deadline-s 36000
exit
}
