#!/bin/bash
# Run the OpenAI Agents API example scripts UNCHANGED against the MAG shim,
# by pointing the OpenAI SDK's base_url at the shim.
#
# Prereqs:
#   - a .env at repo root (cp env.example .env, fill in MAG_API_KEY)
#   - the examples checked out (default: /tmp/openai-agents-api-examples) with a
#     .venv that has `openai==3.13.0`:
#       git clone https://github.com/rick-stevens-ai/openai-agents-api-examples /tmp/openai-agents-api-examples
#       python3 -m venv /tmp/openai-agents-api-examples/.venv
#       /tmp/openai-agents-api-examples/.venv/bin/pip install 'openai==3.13.0'
set +e
cd "$(dirname "$0")/.." || exit 1
SHIM_DIR="$(pwd)"
EX_DIR="${EXAMPLES_DIR:-/tmp/openai-agents-api-examples}"
VENV="$EX_DIR/.venv"

[ -f .env ] && set -a && . ./.env && set +a
: "${MAG_BASE_URL:?set MAG_BASE_URL in .env}"
: "${MAG_API_KEY:?set MAG_API_KEY in .env}"
export SANDBOX_URI="${SANDBOX_URI:-ws://127.0.0.1:8790}"

# Kill by PID *and* by port. uvicorn/codex outlive a bare `kill "$PID"` when the
# launcher wrapped them in a subshell, and a leaked shim keeps :8811 bound — the next
# run then silently tests the *stale* process instead of the code under test.
cleanup(){
  [ -n "$ES_PID" ] && kill "$ES_PID" 2>/dev/null
  [ -n "$SHIM_PID" ] && kill "$SHIM_PID" 2>/dev/null
  sleep 0.5
  for p in 8811 8790; do
    pid="$(ss -lptnH "sport = :$p" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  return 0
}
trap cleanup EXIT

# Refuse to start on an occupied port rather than test someone else's process.
for p in 8811 8790; do
  if ss -lntH "sport = :$p" 2>/dev/null | grep -q .; then
    echo "FATAL: port $p already in use; kill the stale process first"; exit 1
  fi
done

echo "=== start codex exec-server + shim ==="
codex exec-server --listen "$SANDBOX_URI" >/tmp/execserver.log 2>&1 & ES_PID=$!
sleep 3
python3 -m uvicorn mag_agents_shim:app --port 8811 --log-level warning >/tmp/shim.log 2>&1 & SHIM_PID=$!
for i in $(seq 1 30); do curl -sS http://127.0.0.1:8811/healthz >/tmp/h 2>/dev/null && break; sleep 0.5; done
echo "healthz: $(cat /tmp/h)"
# has_key:false means we reached some other shim, or .env never loaded.
grep -q '"has_key": *true' /tmp/h || { echo "FATAL: shim has no MAG key"; exit 1; }

# Point the OpenAI SDK at the shim.
export OPENAI_BASE_URL="http://127.0.0.1:8811/v1"
export OPENAI_API_KEY="$MAG_API_KEY"
export OPENAI_AGENTS_MODEL="${OPENAI_AGENTS_MODEL:-gpt-6-astra@openai}"

echo "model: $OPENAI_AGENTS_MODEL"

RC_ALL=0
# Capture to a file, then head the FILE. Piping the example straight into `head`
# makes any output longer than N lines exit 120 — CPython's "couldn't flush stdout
# at shutdown" after head closes the pipe — so a passing example reports a failure.
run_example(){ # $1=script  $2=head lines  $3=description
  echo; echo "########## ${1}: ${3} ##########"
  local n="${2:-40}" log="/tmp/ex_${1%.py}.log"
  ( cd "$EX_DIR/examples" && "$VENV/bin/python" "$1" ) >"$log" 2>&1
  rc=$?
  head -"$n" "$log"
  total="$(wc -l <"$log")"
  [ "$total" -gt "$n" ] && echo "... [${total} lines total; full output: $log]"
  echo "[${1} exit: $rc]"
  [ "$rc" -ne 0 ] && RC_ALL=1
  return 0
}

run_example 01_basic_session.py       40 "basic session (SDK, non-stream)"
run_example 02_hosted_coding_stream.py 50 "hosted coding sandbox, streaming"
run_example 03_multi_agent_review.py  60 "multi-agent review (SDK, streaming)"
run_example 04_docs_mcp_research.py   60 "hosted mcp + web_search + programmatic tools"

echo; echo "########## EXAMPLE 05: raw curl/SSE (URL swapped to shim) ##########"
# Same file-then-head treatment: a closed pipe makes curl exit 23 on a good stream.
curl -sS --no-buffer --fail-with-body http://127.0.0.1:8811/v1/agents/sessions \
  -H 'OpenAI-Beta: agents=v1' -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  -H 'Content-Type: application/json' \
  --json "$(jq -n --arg model "$OPENAI_AGENTS_MODEL" '{
    agent:{model:$model,instructions:"Answer clearly and do not invent unavailable facts."},
    environment:{type:"none"},
    input:"In two bullets, explain what a durable agent session provides.",
    stream:true}')" >/tmp/ex_05.log 2>&1
rc=$?
head -30 /tmp/ex_05.log
echo "[example 05 exit: $rc]"; [ "$rc" -ne 0 ] && RC_ALL=1

echo; echo "=== shim log tail ==="; tail -n 6 /tmp/shim.log
echo; echo "RESULT: $([ "$RC_ALL" -eq 0 ] && echo 'ALL PASS ✅' || echo 'SOME FAILED ❌')"
exit "$RC_ALL"
