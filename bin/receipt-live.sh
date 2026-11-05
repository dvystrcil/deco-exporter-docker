#!/usr/bin/env bash
# Live receipt (homelab#1931 AC4): prove the SHIPPED image, as deployed, reads
# the real BE85. Not run in CI -- CI never talks to the router.
#
# It reads the running pod rather than starting a new one, so it adds no load
# on the Deco beyond the exporter's own poll, and what it checks is exactly the
# image Argo deployed (its digest is printed as the receipt's subject).
#
# Usage: bin/receipt-live.sh [namespace]   (needs kubectl access to the cluster)
set -euo pipefail
NS=${1:-deco-exporter}
LPORT=${RECEIPT_PORT:-19851}
PF_PID=""
cleanup() { [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true; }
trap cleanup EXIT
fail() { echo "RECEIPT-FAIL: $*"; exit 1; }

pod=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=deco-exporter \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
[ -n "$pod" ] || fail "no running deco-exporter pod in $NS"
image=$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].image}')
digest=$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].imageID}')
echo "pod:    $pod"
echo "image:  $image"
echo "digest: $digest"
case "$image" in harbor.sirddail.net/ai/deco-exporter:*) ;; *) fail "unexpected image $image";; esac

kubectl -n "$NS" port-forward "pod/$pod" "$LPORT:9850" >/dev/null 2>&1 &
PF_PID=$!
body=""
for _ in $(seq 1 20); do
  body=$(curl -sf "http://127.0.0.1:$LPORT/metrics" || true)
  [ -n "$body" ] && break
  sleep 1
done
[ -n "$body" ] || fail "could not read /metrics through port-forward"

metric() { awk -v m="$1" '$1==m {print $2}' <<<"$body"; }
[ "$(metric deco_scrape_success)" = "1.0" ] || fail "deco_scrape_success is $(metric deco_scrape_success), want 1"
[ "$(metric deco_mesh_stats_enabled)" = "0.0" ] || echo "NOTE: mesh stats are ENABLED (AC8) -- AC2-style measurement must exist"

online=$(metric 'deco_clients{state="online"}')
awk -v n="$online" 'BEGIN{exit !(n+0 > 0)}' || fail "no online clients reported ($online)"

last=$(metric deco_last_success_timestamp_seconds)
age=$(awk -v t="$last" -v now="$(date +%s)" 'BEGIN{printf "%d", now - t}')
[ "$age" -lt 300 ] || fail "last success is ${age}s old"

named=$(grep -c '^deco_client_down_speed_raw{.*name="[^"]' <<<"$body" || true)
[ "$named" -gt 0 ] || fail "no client series carries a name"
cpu=$(metric deco_cpu_usage); mem=$(metric deco_memory_usage)
[ -n "$cpu" ] && [ -n "$mem" ] || fail "router cpu/memory missing"

echo "online clients: $online (named series: $named), cpu=$cpu mem=$mem, last success ${age}s ago"
echo "top 5 by down_speed (raw units -- calibration pending):"
grep '^deco_client_down_speed_raw{' <<<"$body" | sort -t' ' -k2 -gr | head -5 \
  | sed -E 's/.*ip="([^"]*)".*name="([^"]*)".* ([0-9.e+]+)$/  \3\t\1\t\2/'
echo "RECEIPT-OK"
