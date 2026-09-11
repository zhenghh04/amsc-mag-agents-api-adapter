"""
Client for OpenAI's open-source `codex exec-server` — the real self-hosted sandbox.

In the managed Agents API, OpenAI's harness connects to `codex exec-server` (a
standalone JSON-RPC/WebSocket executor) to run shell commands and file ops inside
your environment. In this translation layer WE are the harness, so this module speaks
the *client* side of the `codex-exec-server-protocol` to drive that same executor.

Wire protocol (verified live against codex-cli 0.154.0):
  initialize -> {sessionId, environmentInfo}
  initialized (notification)
  process/start {processId, argv, cwd(file://), env, tty, pipeStdin} -> {processId, sandboxType}
  process/output (notification) {processId, seq, stream:"stdout"|"stderr", chunk(base64)}
  process/exited (notification) {processId, exitCode, sandboxDenied}
  process/terminate {processId}

Start the executor with:
    codex exec-server --listen ws://127.0.0.1:8790
Harden the sandbox with, e.g.:
    codex exec-server --listen ws://127.0.0.1:8790 \
      -c 'sandbox_permissions=["disk-write-cwd"]'
(The executor reports `sandboxDenied` when a policy blocks an operation.)
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import uuid

import websockets


class SandboxClient:
    def __init__(self, uri: str, cwd_uri: str | None = None, env: dict | None = None):
        self.uri = uri
        self.cwd_uri = cwd_uri or ("file://" + os.environ.get("SANDBOX_CWD", "/tmp"))
        self.env = env or {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.info: dict = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()  # concurrent-requests default is 1; serialize per connection

    async def connect(self) -> "SandboxClient":
        self.ws = await websockets.connect(self.uri, max_size=None)
        rid = next(self._ids)
        await self._send({"id": rid, "method": "initialize", "params": {"clientName": "mag-agents-shim"}})
        while True:
            m = await self._recv()
            if m.get("id") == rid:
                if "error" in m:
                    raise RuntimeError(f"exec-server initialize failed: {m['error']}")
                self.info = m.get("result", {})
                break
        await self._send({"method": "initialized", "params": {}})
        return self

    async def _send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    async def _recv(self) -> dict:
        return json.loads(await self.ws.recv())

    async def run(self, command: str, timeout: float = 120.0) -> dict:
        """Run `bash -lc <command>` in the sandbox; return {stdout, stderr, exit_code}."""
        async with self._lock:
            pid = f"p{uuid.uuid4().hex[:10]}"
            rid = next(self._ids)
            await self._send({
                "id": rid,
                "method": "process/start",
                "params": {
                    "processId": pid,
                    "argv": ["bash", "-lc", command],
                    "cwd": self.cwd_uri,
                    "env": self.env,
                    "tty": False,
                    "pipeStdin": False,
                },
            })
            out: list[str] = []
            err: list[str] = []
            exit_code: int | None = None
            sandbox_denied = False

            # Drain until `process/closed` (the definitive end), NOT `process/exited`.
            # For instantaneous commands under load, exec-server can deliver the final
            # `process/output` frame *after* `process/exited`; stopping at `exited` would
            # drop that output. `process/closed` is emitted only after output is closed.
            async def pump():
                nonlocal exit_code, sandbox_denied
                while True:
                    m = await self._recv()
                    if m.get("id") == rid and "error" in m:
                        raise RuntimeError(f"process/start error: {m['error']}")
                    meth = m.get("method")
                    p = m.get("params", {}) or {}
                    if meth in ("process/output", "process/exited", "process/closed") \
                            and p.get("processId") != pid:
                        continue
                    if meth == "process/output":
                        chunk = base64.b64decode(p.get("chunk", "")).decode(errors="replace")
                        (out if p.get("stream") == "stdout" else err).append(chunk)
                    elif meth == "process/exited":
                        exit_code = p.get("exitCode")
                        sandbox_denied = p.get("sandboxDenied", False)
                    elif meth == "process/closed":
                        return

            try:
                await asyncio.wait_for(pump(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._send({"id": next(self._ids), "method": "process/terminate",
                                  "params": {"processId": pid}})
                return {"stdout": "".join(out), "stderr": "".join(err),
                        "exit_code": 124, "timed_out": True}

            return {
                "stdout": "".join(out),
                "stderr": "".join(err),
                "exit_code": exit_code,
                "sandbox_denied": sandbox_denied,
            }

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
