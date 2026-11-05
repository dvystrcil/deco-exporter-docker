#!/usr/bin/env bash
# Run the BUILT IMAGE under the pod's security constraints and assert on real
# /metrics output. A green `docker build` says nothing about whether the
# entrypoint imports, the hashed deps resolved, or the exporter reports a dead
# router honestly -- so this checks the shipped artifact, not the source tree.
#
# The router address is a closed local port, on purpose: the image must come
# up, fail its poll, and say so (deco_scrape_success 0, no rate series). CI
# never talks to the real Deco.
#
# Usage: bin/smoke-test.sh <image>
set -euo pipefail
IMAGE=${1:?usage: smoke-test.sh <image>}
NAME=deco-smoke-$$
PORT=${SMOKE_PORT:-19850}
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail() { echo "::error::$*"; docker logs "$NAME" 2>&1 | tail -30 || true; exit 1; }

# 1. No password -> refuse to start (exit 2), rather than poll with an empty one.
set +e
docker run --rm --read-only --user 1000:1000 --cap-drop ALL "$IMAGE" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = 2 ] || fail "expected exit 2 with DECO_PASSWORD unset, got $rc"

# 2. The pinned library is what got installed, and pip is gone.
docker run --rm --entrypoint python3 "$IMAGE" -c '
import importlib.metadata as m, importlib.util as u, sys
v = m.version("tplinkrouterc6u")
assert v == "5.38.0", v
assert u.find_spec("pip") is None, "pip still installed"
print("tplinkrouterc6u", v, "ok; pip absent")'

# 3. Serve with an unreachable router. `-e DECO_PASSWORD` (no `=value`) passes
#    it from this shell's environment, so it never appears in docker's argv.
export DECO_PASSWORD=smoke-test-not-a-real-password
docker run -d --name "$NAME" --read-only --user 1000:1000 --cap-drop ALL \
  -e DECO_PASSWORD -e DECO_HOST=http://127.0.0.1:9 -e DECO_TIMEOUT_SECONDS=2 \
  -p "127.0.0.1:${PORT}:9850" "$IMAGE" >/dev/null

body=""
for _ in $(seq 1 30); do
  body=$(curl -sf "http://127.0.0.1:${PORT}/metrics" || true)
  # Wait for the first poll to have completed (duration is set by a poll).
  if grep -q '^deco_scrape_duration_seconds' <<<"$body"; then break; fi
  sleep 1
done
grep -q '^deco_scrape_duration_seconds' <<<"$body" || fail "no completed poll within 30s"
grep -qx 'deco_scrape_success 0.0' <<<"$body" || fail "dead router must read deco_scrape_success 0"
grep -qx 'deco_mesh_stats_enabled 0.0' <<<"$body" || fail "mesh stats must be off by default"
if grep -qE '^deco_(client_|last_success|cpu|memory|mesh_node)' <<<"$body"; then
  fail "a failed scrape exported data series"
fi
[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz")" = 200 ] \
  || fail "/healthz not 200"
if docker logs "$NAME" 2>&1 | grep -qF "$DECO_PASSWORD"; then
  fail "password appeared in container logs"
fi
echo "SMOKE-COMPLETE"
