#!/usr/bin/env bash
# Argus web console: build + lint + headless UI screenshots. Non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> install deps"
npm install --no-audit --no-fund

echo "==> lint"
npm run lint

echo "==> build (also typechecks)"
npm run build

echo "==> e2e: render + screenshot every screen (desktop + mobile)"
npx playwright install chromium >/dev/null 2>&1 || true
npx playwright test

echo "verify OK — screenshots in web/e2e/screenshots/"
