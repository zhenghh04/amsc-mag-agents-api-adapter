# amsc-mag-agents-api-adapter

**A translation layer that makes the [AMSC Model Access Gateway (MAG)](https://amsc-docs-d762d2.gitlab.io/model-access-gateway/) speak the [OpenAI Agents API](https://openai.com/index/introducing-the-agents-api/) contract.**

**Status: working, tested and passing (2026-09-11).** Real-sandbox + concurrent-subagent fidelity step complete; the official `openai==3.13.0` SDK's Agents API example scripts run **unchanged** against it.

It runs the agent orchestration loop itself on top of MAG's OpenAI-compatible
**Responses API**, exposing `POST /v1/agents/sessions` (header `OpenAI-Beta: agents=v1`),
with tools executed in OpenAI's open-source **`codex exec-server`** sandbox.

> **Why this exists.** OpenAI's *managed* Agents API is model-hosted — it has no
> `base_url` hook, so you cannot point it at MAG. But MAG exposes every *primitive*
> the Agents API is built on. This adapter closes the gap: point the OpenAI Agents
> API SDK's `base_url` at this shim and it drives MAG.

---

## Why this is a thin adapter, not a harness rewrite

MAG already exposes every primitive the Agents API is built on (all verified live):

| Agents API needs | MAG endpoint | Verified |
|---|---|---|
| Model inference (Responses wire API) | `POST /v1/responses` | ✅ 200 |
| Model-emitted tool calls | `output:[{type:"function_call"}]` | ✅ |
| Stateful sessions | `previous_response_id`, `/v1/responses/{id}` | ✅ |
| Streaming | `stream:true` → SSE | ✅ |
| Context compaction | `POST /v1/responses/compact` | ✅ (endpoint live) |
| Cancel / inspect | `/responses/{id}/cancel`, `/input_items` | ✅ (in OpenAPI) |

So the adapter only has to own: (1) the Agents API HTTP/SSE contract, (2) the
`function_call` → execute → `function_call_output` → re-call loop, (3) a
tool-execution environment.

## Quick start

```bash
# 0. deps
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
npm install -g @openai/codex        # provides `codex exec-server` (real sandbox)

# 1. configure — paste your AMSC bearer token as ONE unbroken line
cp env.example .env
$EDITOR .env                        # set MAG_API_KEY

# 2. run everything (starts exec-server + shim, runs both test suites)
bash scripts/run_all.sh
```

Or start the pieces by hand:

```bash
codex exec-server --listen ws://127.0.0.1:8790
#   harden with e.g.  -c 'sandbox_permissions=["disk-write-cwd"]'

set -a; . ./.env; set +a
python3 -m uvicorn mag_agents_shim:app --port 8811

python3 tests/test_shim.py     http://127.0.0.1:8811
python3 tests/test_fidelity.py http://127.0.0.1:8811
```

### Configuration (all via environment / `.env`)

| Var | Meaning |
|---|---|
| `MAG_BASE_URL` | MAG endpoint (default AMSC i2 gateway) |
| `MAG_API_KEY`  | your AMSC bearer token — **one unbroken line** |
| `SANDBOX_URI`  | `codex exec-server` WS URI; omit to disable the shell tool |
| `OPENAI_AGENTS_MODEL` | model the shim asks MAG to run (e.g. `gpt-5.3-codex`) |
| `SHIM_MAX_TURNS` | agent-loop turn cap (default 10) |

## What's proven (tested, all passing)

**`tests/test_shim.py`** — Agents API request with one function tool → full loop
through MAG (6/6 assertions).

**`tests/test_fidelity.py`** — two harder scenarios:

*A) Real sandbox (`codex exec-server`):*
```
→ [root] shell: printf "1..5" > /tmp/mag_sbx_marker.txt && awk '{s+=$1} END{print s}' ...
← shell => {"stdout":"15\n","exit_code":0}
═ FINAL: ...the sum is 15.
PASS: shell invoked · answer=15 · file created ON DISK · contents == [1,2,3,4,5]
```

*B) Parallel subagents:*
```
→ [root] run_subagent(kernel) + run_subagent(cpus)   # both spawned in one turn
   [kernel] shell: uname -s  → Linux
   [cpus]   shell: nproc     → 20
═ FINAL: kernel: Linux · cpus: 20
PASS: 2 subagents started+completed · both results in final answer
```

So the chain is: Agents API contract in → MAG `/v1/responses` inference →
model-chosen tool call → **real execution in the codex exec-server sandbox** (or a
**concurrent subagent** with its own sandbox) → `previous_response_id` chaining →
Agents-API SSE out.

## Tested against the official OpenAI Agents API SDK

The shim emits **SDK-faithful** objects — every session object and streaming event
is validated against the real `openai==3.13.0` models (`validate_models.py`, 13/13). We
ran [Rick Stevens' example scripts](https://github.com/rick-stevens-ai/openai-agents-api-examples)
**unchanged**, pointing only `OPENAI_BASE_URL` at the shim:

| Example | What it exercises | Result |
|---|---|---|
| `01_basic_session.py` | SDK non-stream `sessions.create()` → `AgentSession` | ✅ exit 0 (SDK parsed our session) |
| `03_multi_agent_review.py` | SDK streaming + `multi_agent` delegation | ✅ exit 0 — SDK iterated the full event stream; model spawned 2 parallel subagents (Release A/B) and synthesized |
| `05_raw_curl.sh` (URL→shim) | raw HTTP/SSE lifecycle | ✅ exit 0 — `created → in_progress → turn.created → output_text.delta*/done → turn.completed → idle` |

`OPENAI_BASE_URL=http://127.0.0.1:8811/v1` is the only change — the example code is
byte-for-byte the upstream repo. Reproduce with `bash scripts/run_examples.sh`
(see the header of that script for how to check out the examples repo first).

## Fidelity status

| Capability | Status |
|---|---|
| Agents API contract (`/v1/agents/sessions`, `OpenAI-Beta: agents=v1`) | ✅ |
| Agent loop over MAG `/v1/responses` + `previous_response_id` chaining | ✅ |
| Model `function_call` → tool exec → `function_call_output` | ✅ |
| **Real sandbox** via `codex exec-server` (shell + on-disk effects) | ✅ |
| **Subagents** run concurrently under `max_concurrent_subagents` | ✅ |
| Agents-API SSE (queue-based; interleaves subagent events cleanly) | ✅ |
| Native streaming passthrough (forward MAG SSE + streamed tool-arg deltas) | ⬜ |
| Context compaction via `/v1/responses/compact` when history grows | ⬜ |
| `fs/*` sandbox ops, `apply_patch`, local MCP servers via exec-server | ⬜ |
| Exact event-schema fidelity, auth passthrough, persistence, cancel | ⬜ |
| Sandbox policy hardening (executor ran `sandboxType:none` in the demo) | ⬜ |

### Implementation note (a real bug we hit + fixed)
`codex exec-server` can deliver the final `process/output` frame **after**
`process/exited` for instantaneous commands under load — so a client that stops
reading at `exited` silently drops output (we saw `uname`/`nproc` return empty while
`awk` worked). Fix: drain until `process/closed`, the definitive end-of-process
signal. See `codex_sandbox.py`.

## The strategic note

If the goal is just "agentic Codex on MAG at scale," you don't need this adapter — the
**open-source Codex harness already speaks this exact Responses API** (function_call +
stateful + streaming, all verified) and works against MAG today. This translation layer
matters only if you're committed to the **managed Agents API SDK** and want to swap
`base_url` to MAG. This adapter proves that swap is achievable.

## Files

```
mag_agents_shim.py      FastAPI translation service (agent loop, shell tool, subagents, stream+non-stream)
agents_api_models.py    SDK-faithful builders for session objects + streaming events
codex_sandbox.py        client for OpenAI's OSS `codex exec-server` (real sandbox)
validate_models.py      offline check that our builders parse against openai==3.13.0 models
tests/test_shim.py      smoke test (function tool, SDK event schema)
tests/test_fidelity.py  real-sandbox (on-disk) + parallel-subagent test
scripts/run_all.sh      starts exec-server + shim, runs both test suites
scripts/run_examples.sh runs the upstream OpenAI SDK example scripts against the shim
```

## Security

- Never commit your `MAG_API_KEY`. `.env` is gitignored; `env.example` carries no secret.
- The demo ran the sandbox with a permissive policy (`sandboxType:none`) — **harden the
  `codex exec-server` sandbox policy before any untrusted or production use.**
