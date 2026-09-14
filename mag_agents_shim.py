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
                     .turn.created / .turn.in_progress /
                     .turn.item.added|done (assistant message, command_execution,
                     function_call) / .turn.content_part.added|done /
                     .turn.output_text.delta|done /
                     agent.output.command_execution_output.delta /
                     .turn.completed|cancelled / .idle,
                     plus agent.session.subagent.created|active|closed
  * agent loop over MAG /v1/responses with previous_response_id chaining
  * NATIVE streaming passthrough: MAG /v1/responses is called with stream:true and
    its token deltas are forwarded live (not chunked after the fact)
  * context compaction via POST /v1/responses/compact once a turn chain grows
  * real sandbox tools via OpenAI's OSS `codex exec-server` (env self_hosted):
    shell, fs_read, fs_write, apply_patch
  * local (stdio) MCP servers: agent tools of type:mcp with a stdio transport are
    launched as processes IN the exec-server sandbox; their tools are discovered
    (tools/list) and exposed to the model, and calls route back over MCP JSON-RPC
    (tools/call), surfaced as Agents-API mcp_call turn items
  * subagents (agent.multi_agent.enabled) run concurrently under max_concurrent_subagents
  * auth passthrough: the caller's Authorization bearer is forwarded to MAG
  * session persistence + cancel: GET /v1/agents/sessions/{id},
    POST /v1/agents/sessions/{id}/cancel

Run:
    codex exec-server --listen ws://127.0.0.1:8790 \
      -c 'sandbox_permissions=["disk-write-cwd"]'          # hardened sandbox
    export MAG_BASE_URL=https://i2-api.genesis.american-science-cloud.org
    export MAG_API_KEY=<bearer token>
    export SANDBOX_URI=ws://127.0.0.1:8790
    uvicorn mag_agents_shim:app --port 8811
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import agents_api_models as M
from apply_patch import PatchError, apply_hunks, parse_patch
from codex_sandbox import SandboxClient
from mcp_stdio import McpStdioSession

MAG_BASE = os.environ.get("MAG_BASE_URL", "").rstrip("/")
MAG_KEY = os.environ.get("MAG_API_KEY", "")
SANDBOX_URI = os.environ.get("SANDBOX_URI", "")
MAX_TURNS = int(os.environ.get("SHIM_MAX_TURNS", "10"))
# Compact the running response chain every N tool turns (0 = never). Compaction
# keeps a long agentic session's server-side context bounded.
COMPACT_EVERY = int(os.environ.get("SHIM_COMPACT_EVERY", "0"))

app = FastAPI(title="MAG Agents API translation layer")

# In-memory session store (persistence) + per-session cancel signals.
SESSIONS: dict[str, dict] = {}
CANCELS: dict[str, asyncio.Event] = {}
COMPACTIONS = 0  # count of successful /v1/responses/compact calls (observability)


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #
SHELL_TOOL = {
    "type": "function", "name": "shell",
    "description": "Run a bash command in the sandboxed execution environment. Returns "
                   "JSON {stdout, stderr, exit_code}.",
    "parameters": {"type": "object",
                   "properties": {"command": {"type": "string"}}, "required": ["command"]},
}
FS_READ_TOOL = {
    "type": "function", "name": "fs_read",
    "description": "Read a text file from the sandbox workspace. Returns JSON {exists, content}.",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
FS_WRITE_TOOL = {
    "type": "function", "name": "fs_write",
    "description": "Write a text file in the sandbox workspace (creates parent dirs). "
                   "Returns JSON {ok, path}.",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]},
}
APPLY_PATCH_TOOL = {
    "type": "function", "name": "apply_patch",
    "description": "Apply an OpenAI apply_patch envelope (*** Begin Patch ... *** End Patch) "
                   "to the sandbox workspace: Add/Update/Delete File. Returns JSON {ok, applied}.",
    "parameters": {"type": "object",
                   "properties": {"patch": {"type": "string"}}, "required": ["patch"]},
}
SUBAGENT_TOOL = {
    "type": "function", "name": "run_subagent",
    "description": "Delegate a self-contained subtask to a fresh subagent. Returns the "
                   "subagent's final text. Call multiple times in one turn to run in parallel.",
    "parameters": {"type": "object",
                   "properties": {"name": {"type": "string"}, "task": {"type": "string"}},
                   "required": ["task"]},
}

SANDBOX_TOOLS = [SHELL_TOOL, FS_READ_TOOL, FS_WRITE_TOOL, APPLY_PATCH_TOOL]
# Tools whose execution surfaces as an Agents-API `command_execution` item.
_COMMAND_TOOLS = {"shell", "fs_read", "fs_write", "apply_patch"}


def _tool_get_weather(city: str = "") -> str:
    return json.dumps({"city": city, "temp_f": 72, "conditions": "clear skies"})
LOCAL_FUNCS = {"get_weather": _tool_get_weather}

Emit = Callable[[dict], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Local (stdio) MCP servers via exec-server
# --------------------------------------------------------------------------- #
def _sanitize_tool_name(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in s)


def _parse_mcp_specs(tools: list[dict]) -> list[dict]:
    """Extract Agents-API `type:mcp` tool declarations with a `stdio` transport
    (McpTransportResourceStdio) — a local MCP server launched in the execution
    environment. HTTP-transport / `connection_origin:service` (hosted) MCP servers
    are out of this adapter's scope and are skipped."""
    specs: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict) or t.get("type") != "mcp":
            continue
        transport = t.get("transport") or {}
        if transport.get("type") != "stdio":
            continue
        specs.append({
            "server_label": t.get("server_label") or "mcp",
            "command": transport.get("command", ""),
            "args": transport.get("args") or [],
            "cwd": transport.get("cwd") or os.environ.get("SANDBOX_CWD", "/tmp"),
            "env_vars": transport.get("env_vars") or [],
            "allowed_tools": t.get("allowed_tools"),
        })
    return specs


# --------------------------------------------------------------------------- #
# MAG Responses API (native streaming)
# --------------------------------------------------------------------------- #
def _mag_headers(auth: str) -> dict:
    return {"Authorization": auth, "Content-Type": "application/json"}


async def _mag_stream(client: httpx.AsyncClient, auth: str, body: dict,
                      on_text_delta: Callable[[str], Awaitable[None]] | None) -> dict:
    """POST /v1/responses with stream:true; forward text deltas live; return the
    final `response` object (from response.completed) so the loop can inspect
    output items and previous_response_id."""
    body = {**body, "stream": True}
    final: dict = {}
    async with client.stream("POST", f"{MAG_BASE}/v1/responses",
                             headers=_mag_headers(auth), json=body) as r:
        if r.status_code != 200:
            detail = (await r.aread()).decode(errors="replace")[:400]
            raise RuntimeError(f"MAG /v1/responses {r.status_code}: {detail}")
        async for line in r.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            et = ev.get("type", "")
            if et == "response.output_text.delta" and on_text_delta is not None:
                await on_text_delta(ev.get("delta", ""))
            elif et in ("response.completed", "response.incomplete", "response.failed"):
                final = ev.get("response", {}) or final
    if not final:
        raise RuntimeError("MAG stream ended without response.completed")
    return final


async def _mag_compact(client: httpx.AsyncClient, auth: str, model: str,
                       input_items: list[dict]) -> list[dict] | None:
    """Compact a conversation's input list via POST /v1/responses/compact.

    MAG's compact route takes {model, input:[...]} and returns
    object=response.compaction with a compacted `output` list usable as the next
    request's input. (It rejects previous_response_id chaining — MAG's own
    response ids exceed the upstream 64-char limit.) Best-effort: returns None on
    any failure so the agent loop never breaks on compaction."""
    global COMPACTIONS
    try:
        r = await client.post(f"{MAG_BASE}/v1/responses/compact",
                              headers=_mag_headers(auth),
                              json={"model": model, "input": input_items})
        if r.status_code == 200:
            out = r.json().get("output")
            if isinstance(out, list) and out:
                cleaned = _compaction_to_input(out)
                if cleaned:
                    COMPACTIONS += 1
                    return cleaned
    except Exception:
        pass
    return None


def _compaction_to_input(output: list[dict]) -> list[dict]:
    """Turn a response.compaction `output` into valid `input` items to continue
    from. The compaction packs the prior conversation into role-bearing `message`
    items; a non-input `compaction` marker item is dropped, and content parts are
    re-typed by role (input_text for user, output_text for assistant)."""
    items: list[dict] = []
    for o in output:
        if not isinstance(o, dict) or o.get("type") != "message":
            continue
        role = o.get("role", "assistant")
        parts = []
        for c in o.get("content", []):
            if isinstance(c, dict) and c.get("text"):
                parts.append({"type": "input_text" if role == "user" else "output_text",
                              "text": c["text"]})
        if parts:
            items.append({"role": role, "content": parts})
    return items


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


def _last_user_index(history: list[dict]) -> int:
    """Index of the last user message (start of the active task tail); 0 if none."""
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], dict) and history[i].get("role") == "user":
            return i
    return 0


class SessionCtx:
    """Per-session state shared across the root loop and its subagents."""
    def __init__(self, agent_obj: dict, sandbox_enabled: bool, subagents_enabled: bool,
                 max_subagents: int, auth: str, mcp_specs: list[dict] | None = None):
        self.agent = agent_obj
        self.agent_id = agent_obj["id"]
        self.session_id = M.build_session(agent_obj, {"type": "none"})["id"]
        self.sandbox_enabled = sandbox_enabled
        self.subagents_enabled = subagents_enabled
        self.sem = asyncio.Semaphore(max(1, max_subagents))
        self.auth = auth
        # Local MCP servers (stdio, launched via exec-server). Populated at parse
        # time; live sessions + the exposed-name -> (session, tool, label) routing
        # map are filled when the root turn boots them.
        self.mcp_specs = mcp_specs or []
        self.mcp_sessions: list[McpStdioSession] = []
        self.mcp_router: dict[str, tuple] = {}
        # Each session writes into its own workspace dir (hardening: the sandbox is
        # allowed to write here, not the whole filesystem).
        self.workspace = os.path.join(os.environ.get("SANDBOX_CWD", "/tmp"),
                                      f"mag_ws_{self.session_id[-8:]}")
        self.cancel = asyncio.Event()


# --------------------------------------------------------------------------- #
# Sandbox tool execution (with command_execution item events at depth 0)
# --------------------------------------------------------------------------- #
async def _exec_sandbox_tool(sandbox: SandboxClient, name: str, args: dict) -> tuple[str, dict]:
    """Run a sandbox tool; return (json_output, meta) where meta carries exit_code
    and combined output for the command_execution item."""
    if name == "shell":
        res = await sandbox.run(args.get("command", ""))
        out = json.dumps(res)
        combined = (res.get("stdout", "") + res.get("stderr", "")).strip()
        return out, {"exit_code": res.get("exit_code"), "output": combined,
                     "command": args.get("command", "")}
    if name == "fs_read":
        res = await sandbox.fs_read(args.get("path", ""))
        return json.dumps(res), {"exit_code": 0 if res.get("exists") else 1,
                                 "output": res.get("content", "")[:2000],
                                 "command": f"fs_read {args.get('path','')}"}
    if name == "fs_write":
        res = await sandbox.fs_write(args.get("path", ""), args.get("content", ""))
        return json.dumps(res), {"exit_code": 0 if res.get("ok") else 1,
                                 "output": res.get("path", ""),
                                 "command": f"fs_write {args.get('path','')}"}
    if name == "apply_patch":
        res = await _apply_patch_in_sandbox(sandbox, args.get("patch", ""))
        return json.dumps(res), {"exit_code": 0 if res.get("ok") else 1,
                                 "output": ", ".join(res.get("applied", [])) or res.get("error", ""),
                                 "command": "apply_patch"}
    return json.dumps({"error": f"unknown sandbox tool '{name}'"}), {"exit_code": 1, "output": "", "command": name}


async def _apply_patch_in_sandbox(sandbox: SandboxClient, patch_text: str) -> dict:
    try:
        ops = parse_patch(patch_text)
    except PatchError as exc:
        return {"ok": False, "error": f"parse: {exc}", "applied": []}
    applied: list[str] = []
    for op in ops:
        if op.kind == "add":
            w = await sandbox.fs_write(op.path, op.content or "")
            if not w.get("ok"):
                return {"ok": False, "error": f"write {op.path}: {w.get('stderr') or w}", "applied": applied}
            applied.append(f"add {op.path}")
        elif op.kind == "delete":
            d = await sandbox.fs_delete(op.path)
            if not d.get("ok"):
                return {"ok": False, "error": f"delete {op.path}", "applied": applied}
            applied.append(f"delete {op.path}")
        elif op.kind == "update":
            cur = await sandbox.fs_read(op.path)
            if not cur.get("exists"):
                return {"ok": False, "error": f"update {op.path}: not found", "applied": applied}
            try:
                new_text = apply_hunks(cur.get("content", ""), op.hunks)
            except PatchError as exc:
                return {"ok": False, "error": f"update {op.path}: {exc}", "applied": applied}
            w = await sandbox.fs_write(op.path, new_text)
            if not w.get("ok"):
                return {"ok": False, "error": f"write {op.path}: {w.get('stderr') or w}", "applied": applied}
            applied.append(f"update {op.path}")
    return {"ok": True, "applied": applied}


async def _boot_mcp(ctx: SessionCtx) -> list[dict]:
    """Launch each declared local MCP server via exec-server, discover its tools,
    and return them as MAG function-tool schemas (namespaced). Populates
    ctx.mcp_router so calls route back to the right server/tool. Best-effort per
    server: a server that fails to start is skipped, not fatal."""
    exposed: list[dict] = []
    for spec in ctx.mcp_specs:
        try:
            sess = await McpStdioSession(
                SANDBOX_URI, spec["server_label"], spec["command"], spec["args"],
                spec["cwd"], spec["env_vars"]).start()
        except Exception:
            continue
        ctx.mcp_sessions.append(sess)
        allowed = spec.get("allowed_tools")
        for tool in sess.tools:
            tname = tool.get("name")
            if not tname or (allowed and tname not in allowed):
                continue
            exposed_name = _sanitize_tool_name(f"mcp_{spec['server_label']}_{tname}")
            ctx.mcp_router[exposed_name] = (sess, tname, spec["server_label"])
            exposed.append({
                "type": "function", "name": exposed_name,
                "description": tool.get("description")
                or f"MCP tool '{tname}' on server '{spec['server_label']}'.",
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            })
    return exposed


# --------------------------------------------------------------------------- #
# Agent loop (root turn + subagents)
# --------------------------------------------------------------------------- #
async def _guard(ctx: SessionCtx, coro):
    """Await `coro`, but abort promptly (raising Cancelled) if the session is
    cancelled mid-flight — even during a long inference call or tool run."""
    work = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(ctx.cancel.wait())
    try:
        done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done and work not in done:
            work.cancel()
            raise Cancelled()
        return work.result()
    finally:
        waiter.cancel()


async def _run_loop(ctx: SessionCtx, agent_cfg: dict, input_items: list[dict],
                    emit: Emit, turn_id: str, depth: int) -> str:
    model = agent_cfg.get("model") or ctx.agent["model"]
    instructions = agent_cfg.get("instructions")
    # Raw `type:mcp` declarations are not MAG tools — they're replaced below by the
    # function tools discovered from each MCP server (_boot_mcp).
    tools = [t for t in (agent_cfg.get("tools") or [])
             if not (isinstance(t, dict) and t.get("type") == "mcp")]
    if ctx.sandbox_enabled:
        tools.extend(SANDBOX_TOOLS)
    if depth == 0 and ctx.subagents_enabled:
        tools.append(SUBAGENT_TOOL)

    sandbox: SandboxClient | None = None
    if ctx.sandbox_enabled:
        # Create the workspace on the host first: under the hardened `disk-write-cwd`
        # policy the sandbox cannot create its own cwd, and the cwd must exist.
        os.makedirs(ctx.workspace, exist_ok=True)
        sandbox = await SandboxClient(SANDBOX_URI, workspace=ctx.workspace).connect()

    # Local MCP servers: boot once, at the root turn. Their discovered tools become
    # function tools the model can call; routing lives on ctx.mcp_router.
    if depth == 0 and ctx.mcp_specs and SANDBOX_URI:
        tools.extend(await _boot_mcp(ctx))

    msg_item_id = M._id("item")
    part_opened = False
    prev_id: str | None = None
    turn_input = list(input_items)
    # Compaction mode keeps the full conversation client-side so it can be
    # compacted as it grows (previous_response_id chaining can't be compacted on
    # MAG). Off by default (SHIM_COMPACT_EVERY=0) -> efficient server-side chaining.
    use_history = COMPACT_EVERY > 0
    history: list[dict] = list(input_items)
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            for turn_no in range(MAX_TURNS):
                if ctx.cancel.is_set():
                    raise Cancelled()

                body: dict[str, Any] = {"model": model, "max_output_tokens": 2000}
                if use_history:
                    body["input"] = history
                    body["store"] = False
                    if instructions:
                        body["instructions"] = instructions
                else:
                    body["input"] = turn_input
                    body["store"] = True
                    if instructions and prev_id is None:
                        body["instructions"] = instructions
                    if prev_id:
                        body["previous_response_id"] = prev_id
                if tools:
                    body["tools"] = tools

                # Native streaming: forward text deltas live (depth 0 only).
                async def _on_delta(d: str):
                    nonlocal part_opened
                    if depth != 0 or not d:
                        return
                    if not part_opened:
                        await emit(M.ev_content_part_added(
                            ctx.session_id, turn_id, msg_item_id, M.build_output_text_part("")))
                        part_opened = True
                    await emit(M.ev_output_text_delta(ctx.session_id, turn_id, msg_item_id, d))

                resp = await _guard(ctx, _mag_stream(client, ctx.auth, body,
                                                     _on_delta if depth == 0 else None))
                prev_id = resp.get("id")
                output = resp.get("output", [])
                calls = _function_calls(output)
                if use_history:
                    history = history + [o for o in output if isinstance(o, dict)]

                if not calls:
                    text = _extract_text(output)
                    if depth == 0:
                        if not part_opened and text:
                            await emit(M.ev_content_part_added(
                                ctx.session_id, turn_id, msg_item_id, M.build_output_text_part("")))
                        await emit(M.ev_output_text_done(ctx.session_id, turn_id, msg_item_id, text))
                        await emit(M.ev_content_part_done(
                            ctx.session_id, turn_id, msg_item_id, M.build_output_text_part(text)))
                        await emit(M.ev_turn_item_done(
                            ctx.session_id, turn_id,
                            M.build_assistant_message(turn_id, msg_item_id, text)))
                    return text

                results = await _guard(ctx, asyncio.gather(
                    *[_dispatch_call(ctx, sandbox, model, fc, emit, turn_id, depth) for fc in calls]))
                turn_input = list(results)

                if use_history:
                    history = history + list(results)
                    # Compact the running conversation once it has grown enough.
                    # Preserve the active task tail (from the last user message on)
                    # so an in-progress tool loop is never summarized away; only the
                    # settled older prefix is compacted.
                    if (turn_no + 1) % COMPACT_EVERY == 0:
                        cut = _last_user_index(history)
                        prefix, tail = history[:cut], history[cut:]
                        if len(prefix) >= 2:
                            compacted = await _mag_compact(client, ctx.auth, model, prefix)
                            if compacted:
                                history = compacted + tail
            return "(max turns reached)"
    finally:
        if sandbox is not None:
            await sandbox.close()
        if depth == 0 and ctx.mcp_sessions:
            for sess in ctx.mcp_sessions:
                try:
                    await sess.close()
                except Exception:
                    pass
            ctx.mcp_sessions = []
            ctx.mcp_router = {}


async def _dispatch_call(ctx: SessionCtx, sandbox: SandboxClient | None, model: str,
                         fc: dict, emit: Emit, turn_id: str, depth: int) -> dict:
    name = fc.get("name", "")
    call_id = fc.get("call_id")
    try:
        args = json.loads(fc.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}

    if name in ctx.mcp_router:
        sess, tool_name, server_label = ctx.mcp_router[name]
        item_id = M._id("item")
        if depth == 0:
            await emit(M.ev_turn_item_added(ctx.session_id, turn_id,
                M.build_mcp_call_item(turn_id, item_id, server_label, tool_name, args,
                                      status="in_progress")))
        try:
            res = await sess.call_tool(tool_name, args)
            out, err, status = res["output"], None, ("failed" if res["is_error"] else "completed")
        except Exception as exc:
            out, err, status = "", {"type": "tool", "message": str(exc)}, "failed"
        if depth == 0:
            await emit(M.ev_turn_item_done(ctx.session_id, turn_id,
                M.build_mcp_call_item(turn_id, item_id, server_label, tool_name, args,
                                      status=status, output=out or None, error=err)))
        return {"type": "function_call_output", "call_id": call_id,
                "output": out if not err else json.dumps({"error": err["message"]})}

    if name in _COMMAND_TOOLS and sandbox is not None:
        item_id = M._id("item")
        t0 = time.time()
        if depth == 0:
            cmd_preview = args.get("command") or args.get("path") or name
            await emit(M.ev_turn_item_added(ctx.session_id, turn_id,
                M.build_command_item(turn_id, item_id, str(cmd_preview), status="in_progress")))
        out, meta = await _exec_sandbox_tool(sandbox, name, args)
        if depth == 0:
            if meta.get("output"):
                await emit(M.ev_command_output_delta(ctx.session_id, turn_id, item_id, meta["output"]))
            await emit(M.ev_turn_item_done(ctx.session_id, turn_id,
                M.build_command_item(turn_id, item_id, meta.get("command", name),
                                     status="completed" if meta.get("exit_code") == 0 else "failed",
                                     exit_code=meta.get("exit_code"), output=meta.get("output"),
                                     cwd=ctx.workspace, duration_ms=int((time.time() - t0) * 1000))))
        return {"type": "function_call_output", "call_id": call_id, "output": out}

    if name == "run_subagent" and ctx.subagents_enabled:
        sub = M.build_subagent(ctx.session_id, ctx.agent_id, name=args.get("name"))
        await emit(M.ev_subagent_created(sub))
        await emit(M.ev_subagent_active(sub))
        async with ctx.sem:
            sub_cfg = {"model": model,
                       "instructions": "You are a focused subagent. Complete the task and "
                                       "report the result concisely.",
                       "tools": []}
            res = await _run_loop(ctx, sub_cfg,
                                  [{"role": "user", "content": args.get("task", "")}],
                                  emit, turn_id, depth + 1)
        closed = M.build_subagent(ctx.session_id, ctx.agent_id, name=args.get("name"),
                                  closed=True, sub_id=sub["id"])
        await emit(M.ev_subagent_closed(closed))
        return {"type": "function_call_output", "call_id": call_id,
                "output": json.dumps({"result": res})}

    if name in LOCAL_FUNCS:
        out = LOCAL_FUNCS[name](**args)
        return {"type": "function_call_output", "call_id": call_id, "output": out}

    return {"type": "function_call_output", "call_id": call_id,
            "output": json.dumps({"error": f"unknown tool '{name}'"})}


# --------------------------------------------------------------------------- #
# Request parsing + session assembly
# --------------------------------------------------------------------------- #
def _parse(payload: dict, auth: str) -> tuple[SessionCtx, dict, list[dict]]:
    agent_req = payload.get("agent") or {}
    if not agent_req.get("model"):
        raise HTTPException(status_code=422, detail="agent.model is required")

    env_req = payload.get("environment")
    env_type = (env_req or {}).get("type", "none")
    sandbox_enabled = bool(SANDBOX_URI) and env_type != "none"

    ma = agent_req.get("multi_agent") or {}
    subagents_enabled = bool(ma.get("enabled", False))
    max_sub = ma.get("max_concurrent_subagents") or 3

    # Local (stdio) MCP servers run via exec-server, regardless of environment.type.
    mcp_specs = _parse_mcp_specs(agent_req.get("tools") or [])

    agent_obj = M.build_agent(agent_req)
    ctx = SessionCtx(agent_obj, sandbox_enabled, subagents_enabled, max_sub, auth,
                     mcp_specs=mcp_specs)

    raw_input = payload.get("input", [])
    input_items = [{"role": "user", "content": raw_input}] if isinstance(raw_input, str) else raw_input
    environment = M.build_environment(env_req)
    if environment.get("type") == "self_hosted":
        # Advertise the real per-session workspace (the one dir the hardened
        # sandbox may write to) so callers know where files land.
        environment["workspace_directory"] = ctx.workspace
    return ctx, environment, input_items


def _store(session: dict) -> dict:
    SESSIONS[session["id"]] = session
    return session


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
        session = _store(M.build_session(ctx.agent, environment, status="in_progress",
                                         session_id=ctx.session_id))
        turn = M.build_turn(ctx.session_id, ctx.agent_id, status="in_progress")
        try:
            await emit(M.ev_session_created(session))
            await emit(M.ev_session_in_progress(session))
            await emit(M.ev_turn_created(ctx.session_id, turn))
            await emit(M.ev_turn_in_progress(ctx.session_id, turn))
            await _run_loop(ctx, ctx.agent, input_items, emit, turn["id"], depth=0)
            done_turn = M.build_turn(ctx.session_id, ctx.agent_id, status="completed",
                                     turn_id=turn["id"])
            await emit(M.ev_turn_completed(ctx.session_id, done_turn))
            idle = _store(M.build_session(ctx.agent, environment, status="idle",
                                          session_id=ctx.session_id))
            await emit(M.ev_session_idle(idle))
        except Cancelled:
            cancelled_turn = M.build_turn(ctx.session_id, ctx.agent_id, status="cancelled",
                                          turn_id=turn["id"])
            await emit(M._ev("agent.session.turn.cancelled", session_id=ctx.session_id,
                             turn=cancelled_turn, turn_id=turn["id"]))
            sess = _store(M.build_session(ctx.agent, environment, status="cancelled",
                                          session_id=ctx.session_id))
            await emit(M._ev("agent.session.failed", session=sess))
        except Exception as exc:
            _store(M.build_session(ctx.agent, environment, status="failed",
                                   session_id=ctx.session_id,
                                   error={"message": str(exc), "type": "server_error",
                                          "code": None, "param": None}))
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
    _store(M.build_session(ctx.agent, environment, status="in_progress", session_id=ctx.session_id))
    try:
        await _run_loop(ctx, ctx.agent, input_items, _noop, turn["id"], depth=0)
        session = M.build_session(ctx.agent, environment, status="idle", session_id=ctx.session_id)
    except Cancelled:
        session = M.build_session(ctx.agent, environment, status="cancelled", session_id=ctx.session_id)
    except Exception as exc:
        session = M.build_session(ctx.agent, environment, status="failed",
                                  session_id=ctx.session_id,
                                  error={"message": str(exc), "type": "server_error",
                                         "code": None, "param": None})
    return JSONResponse(_store(session))


@app.post("/v1/agents/sessions")
async def create_session(request: Request,
                         openai_beta: str | None = Header(default=None, alias="OpenAI-Beta"),
                         authorization: str | None = Header(default=None)):
    if not MAG_BASE:
        raise HTTPException(status_code=500, detail="MAG_BASE_URL not configured")
    if openai_beta != "agents=v1":
        raise HTTPException(status_code=400, detail="missing header 'OpenAI-Beta: agents=v1'")

    # Auth passthrough: prefer the caller's bearer; fall back to the shim's env key.
    auth = authorization or (f"Bearer {MAG_KEY}" if MAG_KEY else "")
    if not auth:
        raise HTTPException(status_code=401, detail="no Authorization and no MAG_API_KEY configured")

    payload = await request.json()
    ctx, environment, input_items = _parse(payload, auth)
    CANCELS[ctx.session_id] = ctx.cancel

    if payload.get("stream"):
        return StreamingResponse(_stream(ctx, environment, input_items),
                                 media_type="text/event-stream")
    return await _nonstream(ctx, environment, input_items)


@app.get("/v1/agents/sessions/{session_id}")
async def get_session(session_id: str):
    sess = SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(sess)


@app.post("/v1/agents/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    ev = CANCELS.get(session_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="session not found")
    ev.set()
    sess = SESSIONS.get(session_id)
    if sess is not None:
        sess = {**sess, "status": "cancelled"}
        SESSIONS[session_id] = sess
    return JSONResponse(sess or {"id": session_id, "status": "cancelled"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mag_base": MAG_BASE, "has_key": bool(MAG_KEY),
            "sandbox_uri": SANDBOX_URI or None, "compact_every": COMPACT_EVERY,
            "sessions": len(SESSIONS), "compactions": COMPACTIONS}
