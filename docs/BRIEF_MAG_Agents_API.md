# MAG × OpenAI Agents API — one-page brief

**For:** Rick Stevens; the OpenAI conversation
**Prepared:** 2026-09-11
**Subject:** Can OpenAI's agentic Codex / Agents API run against the AMSC Model Access
Gateway (MAG) at scale — and what, precisely, to ask OpenAI for.
**Working proof:** https://github.com/zhenghh04/amsc-mag-agents-api-adapter

---

## Bottom line

- **Agentic Codex on MAG works today** — via the *self-hosted* Codex harness, which
  speaks MAG's OpenAI-compatible Responses API directly. This is the path for a
  50B-token run and needs nothing from OpenAI.
- **The *managed* OpenAI Agents API cannot point at MAG today** — it is
  model-hosted with no `base_url` / custom-provider hook. That is the one product
  ask to put to OpenAI.
- **We built a translation layer that closes the gap** — it makes MAG speak the
  Agents API contract, and OpenAI's own SDK example scripts run against it
  **unchanged**. This is the working proof that a `base_url` swap is achievable.

## What we tested against MAG (live, with a real token)

| Endpoint / capability | Result | Meaning |
|---|---|---|
| `GET /v1/models` | **200** | inference alive; 157 models incl. `gpt-5.3-codex` |
| `POST /v1/responses` (Codex default wire API) | **200** | Codex works out of the box |
| `POST /v1/chat/completions` | **200** | OpenAI-compatible fallback |
| model-emitted `function_call` | ✅ | tool-calling works |
| stateful sessions (`previous_response_id`) | ✅ | multi-turn agent loops |
| streaming SSE (`stream:true`) | ✅ | live token streaming |
| `POST /v1/responses/compact` | ✅ live | long-session context compaction |
| **`POST /v1/agents/sessions`** (managed Agents API) | **not implemented** | a 405 there is a name collision with MAG's own unrelated `/v1/agents` registry, not support |
| `/v1/conversations` (Agents API sessions) | **404** | not implemented |

**Why:** MAG is a *model gateway* (Kong → LiteLLM) that serves the *inference*
surface. The managed Agents API is OpenAI-hosted *orchestration* (session state,
tool-loop coordination, subagent fan-out) bound to OpenAI's own models — a gateway
cannot re-serve it.

## Two paths, one decision

The decision hinges on **where the 50B tokens live**:

| If the 50B tokens are… | Then… |
|---|---|
| an **OpenAI-platform** grant | the managed Agents API works natively — but on OpenAI's models; MAG is not in the loop (you may still self-host the *sandbox* near your data) |
| an **AMSC / MAG** allocation | use the **self-hosted Codex harness** (or our adapter) pointed at MAG; the managed Agents API cannot be used without the product change below |

## The specific ask to OpenAI

> **Add a custom-model-provider / `base_url` to the Agents API**, so the managed
> harness can route inference to an OpenAI-compatible endpoint (MAG) instead of
> OpenAI's hosted models.

This is the single capability that would let the *managed* Agents API run on MAG.
It does not exist today.

## Related: the Agents *SDK* already works on MAG (no product change needed)

Note the two OpenAI "agents" products are different. The **Agents *SDK***
(`openai-agents`: `Agent`, `Runner`, `ModelProvider`) is an open-source,
**client-side** framework with a built-in `base_url` hook — so it points at MAG
directly, today, with ~20 lines and no adapter. Andrew Schmeder (LBNL) shared the
minimal LiteLLM `ModelProvider` pattern; it's the same "self-hosted harness" path this
brief recommends for the 50B-token run.

The **product ask above is only for the *managed* Agents API** (`client.beta.agents` /
`POST /v1/agents/sessions`) — the cloud service that has no `base_url` hook. Code
written against *that* can't reach MAG without either the OpenAI change or our adapter.

| Need | Route |
|---|---|
| Agents on MAG, you control the client | **Agents SDK** + `ModelProvider(base_url=MAG)` — no infra |
| Existing **managed-Agents-API** code on MAG (`base_url` swap) | **this adapter**, until OpenAI ships the hook |

## The working proof (this repo)

We built a thin translation layer that runs the Agents API contract on top of MAG's
Responses API — the agent loop, real sandboxed tool execution (via OpenAI's OSS
`codex exec-server`), and concurrent subagents. **OpenAI's own Agents API SDK
example scripts run against it unmodified** (only `OPENAI_BASE_URL` changed):

| Example (`rick-stevens-ai/openai-agents-api-examples`) | Result |
|---|---|
| `01_basic_session.py` — SDK non-stream session | ✅ SDK parsed our `AgentSession` |
| `03_multi_agent_review.py` — SDK streaming + multi-agent | ✅ SDK iterated the full stream; 2 parallel subagents synthesized a report |
| `05_raw_curl.sh` — raw HTTP/SSE lifecycle | ✅ full `created → … → idle` |

Session objects and streaming events are validated against the real `openai==3.13.0`
models (13/13). This demonstrates the `base_url` swap OpenAI would need to support is
technically sound — we already emulate it end-to-end.

## Scale-up items to close with AMSC (MAG side)

MAG's docs currently list **no rate limits or quotas** ("usage info coming soon").
Before a 50B-token agentic run, get MAG to confirm:

1. **Per-key concurrency + TPM/RPM** — agentic Codex is bursty (parallel tool calls,
   subagent fan-out); one agent can open dozens of concurrent requests.
2. Whether **`/v1/responses` is rate-limited differently** from `/v1/chat/completions`.
3. **Sustained throughput headroom** for 50B tokens (tokens/sec; batch/priority lane?).
4. **Server-side usage accounting** to track burn against the allocation.

## Recommendation

- For the **50B-token run now**: self-host the Codex harness (or this adapter)
  against MAG — no OpenAI dependency.
- For **managed Agents API portability**: press OpenAI for the `base_url` hook; bring
  this repo as evidence that the integration is straightforward on the gateway side.
