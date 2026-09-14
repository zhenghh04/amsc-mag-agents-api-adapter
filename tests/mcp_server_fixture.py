#!/usr/bin/env python3
"""
Minimal stdio MCP server used as a test fixture (newline-delimited JSON-RPC 2.0).

Implements just enough of MCP for the adapter tests:
  initialize -> notifications/initialized -> tools/list -> tools/call
Tools:
  add(a, b)            -> returns the sum as text
  write_file(path, text) -> writes a file (proves the server runs in the sandbox
                            and its side effects land on the sandbox filesystem)
"""
import json
import os
import sys


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


TOOLS = [
    {"name": "add", "description": "Add two numbers a and b.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "write_file", "description": "Write text to a file at path.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["path", "text"]}},
]


def _call(name, args):
    if name == "add":
        return {"content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]}
    if name == "write_file":
        path = args.get("path", "")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(args.get("text", ""))
        return {"content": [{"type": "text", "text": f"wrote {path}"}]}
    return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture-mcp", "version": "0.1"}}})
        elif method == "notifications/initialized":
            pass  # notification, no reply
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": _call(params.get("name", ""), params.get("arguments", {}) or {})})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
