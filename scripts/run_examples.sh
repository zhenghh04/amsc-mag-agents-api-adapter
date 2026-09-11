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

cleanup(){ [ -n "$ES_PID" ] && kill "$ES_PID" 2>/dev/null; [ -n "$SHIM_PID" ] && kill "$SHIM_PID" 2>/dev/null; }
trap cleanup EXIT

echo "=== start codex exec-server + shim ==="
codex exec-server --listen "$SANDBOX_URI" >/tmp/execserver.log 2>&1 & ES_PID=$!
sleep 3
python3 -m uvicorn mag_agents_shim:app --port 8811 --log-level warning >/tmp/shim.log 2>&1 & SHIM_PID=$!
for i in $(seq 1 30); do curl -sS http://127.0.0.1:8811/healthz >/tmp/h 2>/dev/null && break; sleep 0.5; done
echo "healthz: $(cat /tmp/h)"

# Point the OpenAI SDK at the shim.
export OPENAI_BASE_URL="http://127.0.0.1:8811/v1"
export OPENAI_API_KEY="$MAG_API_KEY"
export OPENAI_AGENTS_MODEL="${OPENAI_AGENTS_MODEL:-gpt-5.3-codex}"

echo; echo "########## EXAMPLE 01: basic session (SDK, non-stream) ##########"
( cd "$EX_DIR/examples" && "$VENV/bin/python" 01_basic_session.py ) 2>&1 | head -40
echo "[example 01 exit: ${PIPESTATUS[0]}]"

echo; echo "########## EXAMPLE 03: multi-agent review (SDK, streaming) ##########"
( cd "$EX_DIR/examples" && "$VENV/bin/python" 03_multi_agent_review.py ) 2>&1 | head -60
echo "[example 03 exit: ${PIPESTATUS[0]}]"

echo; echo "########## EXAMPLE 05: raw curl/SSE (URL swapped to shim) ##########"
curl -sS --no-buffer --fail-with-body http://127.0.0.1:8811/v1/agents/sessions \
  -H 'OpenAI-Beta: agents=v1' -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  -H 'Content-Type: application/json' \
  --json "$(jq -n --arg model "$OPENAI_AGENTS_MODEL" '{
    agent:{model:$model,instructions:"Answer clearly and do not invent unavailable facts."},
    environment:{type:"none"},
    input:"In two bullets, explain what a durable agent session provides.",
    stream:true}')" 2>&1 | head -30
echo "[example 05 exit: ${PIPESTATUS[0]}]"

echo; echo "=== shim log tail ==="; tail -n 6 /tmp/shim.log
