#!/usr/bin/env bash
# Runs the full lab: success -> failure (injected delay) -> recovery.
# Assumes both services are already running (see README).
set -u
CHECKOUT=${CHECKOUT:-http://localhost:8081}
PAYMENT=${PAYMENT:-http://localhost:8082}

section() { printf '\n==================== %s ====================\n' "$1"; }
checkout() {  # $1 = request id, $2 = amount
  curl -s -w '  [HTTP %{http_code}, %{time_total}s]\n' -X POST "$CHECKOUT/checkout" \
    -H "Content-Type: application/json" -H "X-Request-ID: $1" \
    -d "{\"amount\": $2}"
}

section "PHASE 1: SUCCESS (no delay)"
curl -s -X POST "$PAYMENT/admin/delay" -d '{"seconds": 0}'; echo
for i in 1 2 3 4 5; do checkout "success-$i" 49.99; done
echo "--- checkout /metrics"; curl -s "$CHECKOUT/metrics"; echo

section "PHASE 2: FAILURE (Payment delay 5s > Checkout timeout 2s)"
curl -s -X POST "$PAYMENT/admin/delay" -d '{"seconds": 5}'; echo
for i in 1 2 3 4 5; do checkout "timeout-$i" 49.99; done
echo "--- checkout still alive?"; curl -s "$CHECKOUT/health"; echo
echo "--- checkout /metrics"; curl -s "$CHECKOUT/metrics"; echo

section "PHASE 3: RECOVERY (delay removed)"
curl -s -X POST "$PAYMENT/admin/delay" -d '{"seconds": 0}'; echo
for i in $(seq 1 10); do checkout "recovery-$i" 49.99; done
echo "--- checkout /metrics"; curl -s "$CHECKOUT/metrics"; echo
sleep 4  # let the delayed payment threads finish before reading payment metrics
echo "--- payment /metrics"; curl -s "$PAYMENT/metrics"; echo
