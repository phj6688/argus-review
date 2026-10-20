#!/usr/bin/env bash
# Public-exposure gate check for the deployed Argus console.
#
# Asserts that ARGUS_HOST is gated: a PLAIN anonymous request (no spoofed
# headers) MUST be bounced by the reverse proxy's auth layer with 302/401/403. Anything
# else (200, 502, ...) means the gate is open or the route is broken -> non-zero
# exit. Complements verify.sh, which proves the app itself works; this proves the
# public edge is closed.
#
# The exit-gating probe is header-less on purpose: a spoofed CF-Connecting-IP
# only exercises the CDN/WAF layer and can mask an open origin gate, so the
# plain anonymous request is the canonical evidence. The spoofed variant runs
# afterwards as an optional, non-fatal WAF cross-check (set SPOOF_CHECK=0 to
# skip).
#
# Run AFTER putting the console behind your reverse proxy (see DEPLOY.md).
#   ARGUS_HOST=argus.example.com [ARGUS_API_URL=https://api.example.com] ./verify-gate.sh
# With ARGUS_HOST unset the public check is skipped and the script exits 0.
set -euo pipefail

HOST="${ARGUS_HOST:-}"
if [ -z "${HOST}" ]; then
  echo "SKIP: ARGUS_HOST is not set; no public host to check"
  exit 0
fi
URL="https://${HOST}"
SPOOF_IP="${SPOOF_IP:-8.8.8.8}"
SPOOF_CHECK="${SPOOF_CHECK:-1}"

# --max-time bounds a hung connection; `|| true` keeps a connection failure
# from aborting the script mid-substitution under `set -e`. On failure curl's
# own %{http_code} write-out is `000`, which is not in {302,401,403} so the
# FAIL branch below reports it -> fails closed, as intended.
echo "== gate check (plain anonymous): ${URL} =="
code="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "${URL}" || true)"
echo "root -> HTTP ${code}"
case "${code}" in
  302|401|403)
    echo "PASS: root is gated (${code})"
    ;;
  *)
    echo "FAIL: expected 302/401/403 on root, got ${code} -- gate is OPEN or route broken" >&2
    exit 1
    ;;
esac

# The root page alone proves little: a proxy can gate "/" and leave the data
# routes open. Probe two API routes that return review data or configuration.
API_URL="${ARGUS_API_URL:-${URL}}"
API_URL="${API_URL%/}"
for route in /reviews /config; do
  echo "== data route (plain anonymous): ${API_URL}${route} =="
  rcode="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "${API_URL}${route}" || true)"
  echo "${route} -> HTTP ${rcode}"
  case "${rcode}" in
    302|401|403)
      echo "PASS: ${route} is gated (${rcode})"
      ;;
    *)
      echo "FAIL: expected 302/401/403 on ${route}, got ${rcode} -- data route is OPEN or broken" >&2
      exit 1
      ;;
  esac
done

# /health must stay public (liveness carve-out), and must NOT redirect to auth.
echo "== liveness (plain anonymous): ${URL}/health =="
hcode="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "${URL}/health" || true)"
echo "/health -> HTTP ${hcode}"
case "${hcode}" in
  200)
    echo "PASS: /health is reachable without auth (${hcode})"
    ;;
  *)
    echo "WARN: /health returned ${hcode} (expected 200). Liveness carve-out may be missing." >&2
    # Non-fatal: the gate (the security-critical assertion) already passed.
    ;;
esac

# Optional, non-fatal: the spoofed-CF-Connecting-IP variant only exercises the
# CDN/WAF layer, not the origin gate, so it is a cross-check, never the verdict.
if [ "${SPOOF_CHECK}" = "1" ]; then
  echo "== WAF cross-check (spoofed CF-Connecting-IP: ${SPOOF_IP}; informational) =="
  scode="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "${URL}" -H "CF-Connecting-IP: ${SPOOF_IP}" || true)"
  echo "root (spoofed) -> HTTP ${scode} (not gating)"
fi

echo "gate OK"
