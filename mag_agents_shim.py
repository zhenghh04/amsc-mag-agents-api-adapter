"""
MAG <-> OpenAI Agents API translation layer.

Exposes the OpenAI Agents API surface (`POST /v1/agents/sessions`, header
`OpenAI-Beta: agents=v1`) and drives it on top of MAG's OpenAI-compatible Responses
API. Emits SDK-faithful objects (validated against openai==3.13.0 models by
`validate_models.py`) so the official `openai` SDK and raw HTTP accept our responses
unchanged.

Capabilities:
  * stream=false  -> returns an AgentSession object (status idle)
  * stream=true   -> Agents-API SSE: agent.session.created / .in_progress /
                     .turn.created / .turn.output_text.delta|done / .turn.completed /
                     .idle, plus agent.session.subagent.created|closed
  * agent loop over MAG /v1/responses with previous_response_id chaining
  * real sandbox `shell` tool via OpenAI's OSS `codex exec-server` (env self_hosted)
  * subagents (agent.multi_agent.enabled) run concurrently under max_concurrent_subagents

Run:
    codex exec-server --listen ws://127.0.0.1:8790            # optional sandbox
    export MAG_BASE_URL=https://i2-api.genesis.american-science-cloud.org
    export MAG_API_KEY=<bearer token>
    export SANDBOX_URI=ws://127.0.0.1:8790
    uvicorn mag_agents_shim:app --port 8811
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncGenerator, Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import agents_api_models as M
from codex_sandbox import SandboxClient

MAG_BASE = os.environ.get("MAG_BASE_URL", "").rstrip("/")
MAG_KEY = os.environ.get("MAG_API_KEY", "")
SANDBOX_URI = os.environ.get("SANDBOX_URI", "")
MAX_TURNS = int(os.environ.get("SHIM_MAX_TURNS", "10"))

app = FastAPI(title="MAG Agents API translation layer")

SHELL_TOOL = {
    "type": "function", "name": "shell",
    "description": "Run a bash command in the sandboxed execution environment. Returns "
                   "JSON {stdout, stderr, exit_code}.",
    "parameters": {"type": "object",
                   "properties": {"command": {"type": "string"}}, "required": ["command"]},
}
SUBAGENT_TOOL = {
    "type": "function", "name": "run_subagent",
    "description": "Delegate a self-contained subtask to a fresh subagent. Returns the "
                   "subagent's final text. Call multiple times in one turn to run in parallel.",
    "parameters": {"type": "object",
                   "properties": {"name": {"type": "string"}, "task": {"type": "string"}},
                   "required": ["task"]},
}

def _tool_get_weather(city: str = "") -> str:
    return json.dumps({"city": city, "temp_f": 72, "conditions": "clear skies"})
LOCAL_FUNCS = {"get_weather": _tool_get_weather}

Emit = Callable[[dict], Awaitable[None]]


# --------------------------------------------------------------------------- #
# MAG Responses API
# --------------------------------------------------------------------------- #
async def _mag_responses(client: httpx.AsyncClient, body: dict) -> dict:
    r = await client.post(
        f"{MAG_BASE}/v1/responses",
        headers={"Authorization": f"Bearer {MAG_KEY}", "Content-Type": "application/json"},
        json=body,
    )
    if r.status_code != 200:
        raise RuntimeError(f"MAG /v1/responses {r.status_code}: {r.text[:400]}")
    return r.json()


def _extract_text(output: list[dict]) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content", []):
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    parts.append(c.get("text", ""))
        elif item.get("type") in ("output_text", "text"):
            parts.append(item.get("text", ""))
    return "".join(parts)


def _function_calls(output: list[dict]) -> list[dict]:
    return [o for o in output if isinstance(o, dict) and o.get("type") == "function_call"]


def _chunk(s: str, n: int):
    for i in range(0, len(s), max(1, n)):
        yield s[i : i + n]


class SessionCtx:
    """Per-session state shared across the root loop and its subagents."""
    def __init__(self, agent_obj: dict, sandbox_enabled: bool, subagents_enabled: bool,
                 max_subagents: int):
        self.agent = agent_obj
        self.agent_id = agent_obj["id"]
        self.session_id = M.build_session(agent_obj, {"type": "none"})["id"]
        self.sandbox_enabled = sandbox_enabled
        self.subagents_enabled = subagents_enabled
        self.sem = asyncio.Semaphore(max(1, max_subagents))


# --------------------------------------------------------------------------- #
# Agent loop (root turn + subagents)
# --------------------------------------------------------------------------- #
async def _run_loop(ctx: SessionCtx, agent_cfg: dict, input_items: list[dict],
                    emit: Emit, turn_id: str, depth: int) -> str:
    model = agent_cfg.get("model") or ctx.agent["model"]
    instructions = agent_cfg.get("instructions")
    tools = list(agent_cfg.get("tools") or [])
    if ctx.sandbox_enabled:
        tools.append(SHELL_TOOL)
    if depth == 0 and ctx.subagents_enabled:
        tools.append(SUBAGENT_TOOL)

    sandbox: SandboxClient | None = None
    if ctx.sandbox_enabled:
        sandbox = await SandboxClient(SANDBOX_URI).connect()

    item_id = M._id("item")
    prev_id: str | None = None
    turn_input = list(input_items)
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            for _turn in range(MAX_TURNS):
                body: dict[str, Any] = {"model": model, "input": turn_input,
                                        "store": True, "max_output_tokens": 2000}
                if tools:
                    body["tools"] = tools
                if instructions and prev_id is None:
                    body["instructions"] = instructions
                if prev_id:
                    body["previous_response_id"] = prev_id

                resp = await _mag_responses(client, body)
                prev_id = resp.get("id")
                output = resp.get("output", [])
                calls = _function_calls(output)

                if not calls:
                    text = _extract_text(output)
                    if depth == 0:  # stream the assistant text as Agents-API deltas
                        for ch in _chunk(text, 80):
                            await emit(M.ev_output_text_delta(ctx.session_id, turn_id, item_id, ch))
                        await emit(M.ev_output_text_done(ctx.session_id, turn_id, item_id, text))
                    return text

                async def _do_call(fc: dict) -> dict:
                    name = fc.get("name", "")
                    try:
                        args = json.loads(fc.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if name == "shell" and sandbox is not None:
                        out = json.dumps(await sandbox.run(args.get("command", "")))
                    elif name == "run_subagent" and ctx.subagents_enabled:
                        sub = M.build_subagent(ctx.session_id, ctx.agent_id,
                                               name=args.get("name"))
                        await emit(M.ev_subagent_created(sub))
                        async with ctx.sem:
                            sub_cfg = {"model": model,
                                       "instructions": "You are a focused subagent. Complete the "
                                                       "task and report the result concisely.",
                                       "tools": []}
                            res = await _run_loop(ctx, sub_cfg,
                                                  [{"role": "user", "content": args.get("task", "")}],
                                                  emit, turn_id, depth + 1)
                        closed = M.build_subagent(ctx.session_id, ctx.agent_id,
                                                  name=args.get("name"), closed=True,
                                                  sub_id=sub["id"])
                        await emit(M.ev_subagent_closed(closed))
                        out = json.dumps({"result": res})
                    elif name in LOCAL_FUNCS:
                        out = LOCAL_FUNCS[name](**args)
                    else:
                        out = json.dumps({"error": f"unknown tool '{name}'"})
                    return {"type": "function_call_output",
                            "call_id": fc.get("call_id"), "output": out}

                results = await asyncio.gather(*[_do_call(fc) for fc in calls])
                turn_input = list(results)
            return "(max turns reached)"
    finally:
        if sandbox is not None:
            await sandbox.close()


# --------------------------------------------------------------------------- #
# Request parsing + session assembly
# --------------------------------------------------------------------------- #
def _parse(payload: dict) -> tuple[SessionCtx, dict, list[dict]]:
    agent_req = payload.get("agent") or {}
    if not agent_req.get("model"):
        raise HTTPException(status_code=422, detail="agent.model is required")

    env_req = payload.get("environment")
    env_type = (env_req or {}).get("type", "none")
    sandbox_enabled = bool(SANDBOX_URI) and env_type != "none"

    ma = agent_req.get("multi_agent") or {}
    subagents_enabled = bool(ma.get("enabled", False))
    max_sub = ma.get("max_concurrent_subagents") or 3

    agent_obj = M.build_agent(agent_req)
    ctx = SessionCtx(agent_obj, sandbox_enabled, subagents_enabled, max_sub)

    raw_input = payload.get("input", [])
    input_items = [{"role": "user", "content": raw_input}] if isinstance(raw_input, str) else raw_input
    return ctx, M.build_environment(env_req), input_items


# --------------------------------------------------------------------------- #
# Streaming + non-streaming endpoints
# --------------------------------------------------------------------------- #
def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def _stream(ctx: SessionCtx, environment: dict, input_items: list[dict]) -> AsyncGenerator[str, None]:
    q: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await q.put(event)

    async def driver():
        session = M.build_session(ctx.agent, environment, status="in_progress",
                                  session_id=ctx.session_id)
        turn = M.build_turn(ctx.session_id, ctx.agent_id, status="in_progress")
        try:
            await emit(M.ev_session_created(session))
            await emit(M.ev_session_in_progress(session))
            await emit(M.ev_turn_created(ctx.session_id, turn))
            await _run_loop(ctx, ctx.agent, input_items, emit, turn["id"], depth=0)
            done_turn = M.build_turn(ctx.session_id, ctx.agent_id, status="completed",
                                     turn_id=turn["id"])
            await emit(M.ev_turn_completed(ctx.session_id, done_turn))
            idle = M.build_session(ctx.agent, environment, status="idle",
                                   session_id=ctx.session_id)
            await emit(M.ev_session_idle(idle))
        except Exception as exc:
            await emit(M.ev_error(ctx.session_id, str(exc)))
        finally:
            await q.put(None)

    task = asyncio.create_task(driver())
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield _sse(item)
    finally:
        await task


async def _nonstream(ctx: SessionCtx, environment: dict, input_items: list[dict]) -> JSONResponse:
    async def _noop(_event: dict) -> None:
        return None
    turn = M.build_turn(ctx.session_id, ctx.agent_id, status="in_progress")
    try:
        await _run_loop(ctx, ctx.agent, input_items, _noop, turn["id"], depth=0)
        session = M.build_session(ctx.agent, environment, status="idle",
                                  session_id=ctx.session_id)
    except Exception as exc:
        session = M.build_session(ctx.agent, environment, status="failed",
                                  session_id=ctx.session_id,
                                  error={"message": str(exc), "type": "server_error",
                                         "code": None, "param": None})
    return JSONResponse(session)


@app.post("/v1/agents/sessions")
async def create_session(request: Request,
                         openai_beta: str | None = Header(default=None, alias="OpenAI-Beta")):
    if not MAG_BASE or not MAG_KEY:
        raise HTTPException(status_code=500, detail="MAG_BASE_URL / MAG_API_KEY not configured")
    if openai_beta != "agents=v1":
        raise HTTPException(status_code=400, detail="missing header 'OpenAI-Beta: agents=v1'")

    payload = await request.json()
    ctx, environment, input_items = _parse(payload)

    if payload.get("stream"):
        return StreamingResponse(_stream(ctx, environment, input_items),
                                 media_type="text/event-stream")
    return await _nonstream(ctx, environment, input_items)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mag_base": MAG_BASE, "has_key": bool(MAG_KEY),
            "sandbox_uri": SANDBOX_URI or None}
