#!/usr/bin/env bash
# Build once, test once, then prove the compressor works on a real PR.
# Non-zero exit on any failure (the repo verify gate).
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_VERSION="${ARGUS_PYTHON:-3.13}"
uv venv --clear --python "$PYTHON_VERSION" .venv
# shellcheck disable=SC1091
. .venv/bin/activate
uv pip install -q -e ".[dev]"

echo "== unit tests =="
pytest -q

echo "== E2E: compress a real merged PR =="
REPO="${ARGUS_TEST_REPO:-phj6688/argus-review}"
PR="$(gh pr list -R "$REPO" --state merged --limit 1 --json number -q '.[0].number')"
if [ -z "$PR" ]; then
  echo "no merged PR found in $REPO" >&2
  exit 1
fi
echo "compressing $REPO#$PR"
argus compress "$REPO" "$PR" --budget 8000

echo "verify OK"
