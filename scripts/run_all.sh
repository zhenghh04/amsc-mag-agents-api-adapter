#!/bin/bash
# Start codex exec-server + the shim, then run both test suites.
# Requires a .env at repo root (cp env.example .env and fill in MAG_API_KEY).
set +e
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

[ -f .env ] && set -a && . ./.env && set +a
: "${MAG_BASE_URL:?set MAG_BASE_URL in .env}"
: "${MAG_API_KEY:?set MAG_API_KEY in .env}"
export SANDBOX_URI="${SANDBOX_URI:-ws://127.0.0.1:8790}"

cleanup() { [ -n "$ES_PID" ] && kill "$ES_PID" 2>/dev/null; [ -n "$SHIM_PID" ] && kill "$SHIM_PID" 2>/dev/null; }
trap cleanup EXIT

echo "=== start codex exec-server (hardened sandbox) ==="
# Hardening: writes restricted to each session's cwd/workspace; reads allowed;
# NOT the permissive sandboxType:none of the original demo.
codex exec-server --listen "$SANDBOX_URI" \
  -c 'sandbox_permissions=["disk-write-cwd","disk-full-read-access"]' \
  >/tmp/execserver.log 2>&1 &
ES_PID=$!; sleep 3
if ! kill -0 $ES_PID 2>/dev/null; then echo "exec-server DIED:"; cat /tmp/execserver.log; exit 1; fi
echo "exec-server up (pid $ES_PID)"

echo "=== start shim ==="
python3 -m uvicorn mag_agents_shim:app --port 8811 --log-level warning >/tmp/shim.log 2>&1 &
SHIM_PID=$!
for i in $(seq 1 30); do curl -sS http://127.0.0.1:8811/healthz >/tmp/h 2>/dev/null && break; sleep 0.5; done
echo "healthz: $(cat /tmp/h 2>/dev/null)"

echo; echo "########## TEST 1: original spike (function tool) ##########"
python3 tests/test_shim.py http://127.0.0.1:8811; RC1=$?

echo; echo "########## TEST 2: fidelity (real sandbox + subagents) ##########"
python3 tests/test_fidelity.py http://127.0.0.1:8811; RC2=$?

echo; echo "########## TEST 3: next-steps (streaming/apply_patch/auth/persist/cancel) ##########"
python3 tests/test_next_steps.py http://127.0.0.1:8811; RC3=$?

echo; echo "########## TEST 4: context compaction (POST /v1/responses/compact) ##########"
# Primitive-level: talks to MAG directly (uses MAG_* from .env); no shim server needed.
python3 tests/test_compaction.py; RC4=$?

echo; echo "=== shim log (tail) ==="; tail -n 8 /tmp/shim.log
echo; echo "EXIT CODES: spike=$RC1 fidelity=$RC2 next_steps=$RC3 compaction=$RC4"
if [ $RC1 -eq 0 ] && [ $RC2 -eq 0 ] && [ $RC3 -eq 0 ] && [ $RC4 -eq 0 ]; then echo "ALL GREEN"; else echo "SOME FAILED"; fi
