#!/bin/bash
# Run the "full codex subagents on MAG" example (examples/codex_subagents.py).
#
# Boots OpenAI's OSS `codex exec-server` + the MAG shim, points the stock OpenAI
# SDK at the shim, and streams the multi-agent codex run.
#
# Prereqs:
#   - codex-cli on PATH (`codex --version`)
#   - python3 with `openai>=3.13` and `uvicorn`, `httpx`, `websockets` (see requirements.txt)
#   - a .env at repo root: `cp env.example .env` then set MAG_API_KEY (and MAG_BASE_URL)
#
# Env knobs:
#   OPENAI_AGENTS_MODEL   codex-capable Responses model (default openai/gpt-5.3-codex)
#   AGENTS_RAW=1          dump raw JSON events instead of the pretty view
#   SHIM_PORT             shim port (default 8811)
#   SANDBOX_URI           exec-server ws URI (default ws://127.0.0.1:8790)
#   EXAMPLE_PYTHON        python with openai>=3.13 for the client (default: python3,
#                         or ./.venv/bin/python if present)
set +e
cd "$(dirname "$0")/.." || exit 1

[ -f .env ] && set -a && . ./.env && set +a
: "${MAG_BASE_URL:?set MAG_BASE_URL in .env}"
: "${MAG_API_KEY:?set MAG_API_KEY in .env}"
export MAG_BASE_URL MAG_API_KEY
export SANDBOX_URI="${SANDBOX_URI:-ws://127.0.0.1:8790}"
SHIM_PORT="${SHIM_PORT:-8811}"

cleanup(){ [ -n "$ES_PID" ] && kill "$ES_PID" 2>/dev/null; [ -n "$SHIM_PID" ] && kill "$SHIM_PID" 2>/dev/null; }
trap cleanup EXIT

echo "=== start codex exec-server + shim ==="
codex exec-server --listen "$SANDBOX_URI" >/tmp/execserver.log 2>&1 & ES_PID=$!
sleep 3
python3 -m uvicorn mag_agents_shim:app --port "$SHIM_PORT" --log-level warning >/tmp/shim.log 2>&1 & SHIM_PID=$!
for i in $(seq 1 30); do curl -sS "http://127.0.0.1:${SHIM_PORT}/healthz" >/tmp/h 2>/dev/null && break; sleep 0.5; done
echo "healthz: $(cat /tmp/h)"

# Point the stock OpenAI SDK at the shim.
export OPENAI_BASE_URL="http://127.0.0.1:${SHIM_PORT}/v1"
export OPENAI_API_KEY="$MAG_API_KEY"
export OPENAI_AGENTS_MODEL="${OPENAI_AGENTS_MODEL:-openai/gpt-5.3-codex}"

# Client python needs openai>=3.13 (Agents API surface). Prefer an explicit
# EXAMPLE_PYTHON, then a repo-local .venv, else python3.
PY="${EXAMPLE_PYTHON:-}"
[ -z "$PY" ] && [ -x ./.venv/bin/python ] && PY=./.venv/bin/python
[ -z "$PY" ] && PY=python3

echo; echo "########## full codex subagents on MAG ##########"
( cd examples && "$PY" codex_subagents.py )
echo "[exit: $?]"

echo; echo "=== shim log tail ==="; tail -n 8 /tmp/shim.log
