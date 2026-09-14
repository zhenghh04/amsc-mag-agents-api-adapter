"""
Local (stdio) MCP servers run *inside* the sandbox via `codex exec-server`.

The OpenAI Agents API lets an agent attach tools from a remote MCP server declared
as an agent tool of `type:"mcp"`. When its `transport` is `stdio`
(`McpTransportResourceStdio`: command/args/cwd/env_vars) and its `connection_origin`
is `"environment"`, the server is a *local process started in the execution
environment* — i.e. inside the codex sandbox, not a hosted service.

This module implements exactly that: it launches the MCP server as a long-lived
process through the exec-server (`process/start` with `pipeStdin:true`), and speaks
the MCP stdio wire protocol (newline-delimited JSON-RPC 2.0) to it by writing to the
process's stdin (`process/write {processId, writeId, chunk}`) and reading its stdout
(`process/output` notifications) — both verified live against codex-cli 0.154.0.

Each MCP server gets its own dedicated exec-server WebSocket connection with a
background reader, so it never contends with the serialized shell/fs SandboxClient.

    initialize (MCP) -> notifications/initialized -> tools/list -> tools/call
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import uuid

import websockets


class McpError(Exception):
    pass


class McpStdioSession:
    """One local stdio MCP server, launched and driven through the exec-server."""

    def __init__(self, exec_uri: str, server_label: str, command: str, args: list[str],
                 cwd: str, env_vars: list[str] | None = None):
        self.exec_uri = exec_uri
        self.server_label = server_label
        self.command = command
        self.args = list(args or [])
        self.cwd = cwd
        # env_vars: names inherited from the shim's environment (per McpTransportResourceStdio).
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": os.environ.get("HOME", "/tmp")}
        for name in (env_vars or []):
            if name in os.environ:
                env[name] = os.environ[name]
        self.env = env

        self.ws: websockets.WebSocketClientProtocol | None = None
        self.pid = f"mcp_{uuid.uuid4().hex[:10]}"
        self.tools: list[dict] = []
        self.server_info: dict = {}

        self._exec_ids = itertools.count(1)      # exec-server RPC ids
        self._mcp_ids = itertools.count(1)       # MCP JSON-RPC ids
        self._exec_waiters: dict[int, asyncio.Future] = {}
        self._mcp_waiters: dict[int, asyncio.Future] = {}
        self._stdout_buf = ""
        self._stderr: list[str] = []
        self._dead = asyncio.Event()
        self._reader: asyncio.Task | None = None

    # -- exec-server bridge ------------------------------------------------- #
    async def _exec_rpc(self, method: str, params: dict, timeout: float = 20.0) -> dict:
        rid = next(self._exec_ids)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._exec_waiters[rid] = fut
        await self.ws.send(json.dumps({"id": rid, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._exec_waiters.pop(rid, None)

    async def _reader_loop(self) -> None:
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid is not None and mid in self._exec_waiters:
                    fut = self._exec_waiters.get(mid)
                    if fut and not fut.done():
                        if "error" in m:
                            fut.set_exception(McpError(f"exec-server: {m['error']}"))
                        else:
                            fut.set_result(m.get("result", {}))
                    continue
                meth = m.get("method")
                p = m.get("params", {}) or {}
                if p.get("processId") not in (None, self.pid):
                    continue
                if meth == "process/output":
                    chunk = base64.b64decode(p.get("chunk", "")).decode(errors="replace")
                    if p.get("stream") == "stderr":
                        self._stderr.append(chunk)
                    else:
                        self._feed_stdout(chunk)
                elif meth in ("process/exited", "process/closed"):
                    self._dead.set()
                    self._fail_all(McpError("MCP server process exited"))
        except Exception as exc:  # connection dropped
            self._dead.set()
            self._fail_all(McpError(f"exec-server connection lost: {exc}"))

    def _feed_stdout(self, chunk: str) -> None:
        """Accumulate stdout; MCP stdio frames are newline-delimited JSON."""
        self._stdout_buf += chunk
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON server chatter
            mid = msg.get("id")
            if mid is not None and mid in self._mcp_waiters:
                fut = self._mcp_waiters.get(mid)
                if fut and not fut.done():
                    fut.set_result(msg)
            # notifications (no id, or unknown id) are ignored

    def _fail_all(self, exc: Exception) -> None:
        for d in (self._exec_waiters, self._mcp_waiters):
            for fut in list(d.values()):
                if not fut.done():
                    fut.set_exception(exc)
            d.clear()

    # -- MCP JSON-RPC over the process's stdin/stdout ----------------------- #
    async def _mcp_send(self, obj: dict) -> None:
        line = (json.dumps(obj) + "\n").encode()
        await self._exec_rpc("process/write", {
            "processId": self.pid, "writeId": f"w{uuid.uuid4().hex[:8]}",
            "chunk": base64.b64encode(line).decode(),
        })

    async def _mcp_request(self, method: str, params: dict | None = None,
                           timeout: float = 30.0) -> dict:
        mid = next(self._mcp_ids)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._mcp_waiters[mid] = fut
        req = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            req["params"] = params
        await self._mcp_send(req)
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._mcp_waiters.pop(mid, None)
        if "error" in msg:
            raise McpError(f"{method}: {msg['error']}")
        return msg.get("result", {}) or {}

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> "McpStdioSession":
        self.ws = await websockets.connect(self.exec_uri, max_size=None)
        # exec-server handshake
        rid = next(self._exec_ids)
        await self.ws.send(json.dumps({"id": rid, "method": "initialize",
                                       "params": {"clientName": "mag-agents-shim-mcp"}}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == rid:
                if "error" in m:
                    raise McpError(f"exec-server initialize: {m['error']}")
                break
        await self.ws.send(json.dumps({"method": "initialized", "params": {}}))
        # launch the MCP server process (stdin piped so we can talk to it)
        self._reader = asyncio.ensure_future(self._reader_loop())
        await self._exec_rpc("process/start", {
            "processId": self.pid,
            "argv": [self.command, *self.args],
            "cwd": "file://" + self.cwd,
            "env": self.env,
            "tty": False,
            "pipeStdin": True,
        })
        # MCP handshake
        init = await self._mcp_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mag-agents-shim", "version": "0.1"},
        })
        self.server_info = init.get("serverInfo", {}) or {}
        await self._mcp_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # discover tools
        res = await self._mcp_request("tools/list")
        self.tools = res.get("tools", []) or []
        return self

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool. Returns {output:str, is_error:bool}."""
        res = await self._mcp_request("tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        for c in res.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append(json.dumps(c))
        return {"output": "\n".join(parts), "is_error": bool(res.get("isError", False))}

    async def close(self) -> None:
        try:
            if self.ws is not None and not self._dead.is_set():
                await self._exec_rpc("process/terminate", {"processId": self.pid}, timeout=5.0)
        except Exception:
            pass
        if self._reader is not None:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
