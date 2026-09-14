"""
Local (stdio) MCP servers via exec-server — end-to-end against MAG.

A tiny stdio MCP server (tests/mcp_server_fixture.py) is declared as an Agents-API
agent tool of type:"mcp" with a stdio transport. The shim launches it as a process
INSIDE the codex exec-server sandbox, discovers its tools (tools/list), exposes them
to MAG's model, and routes the model's calls back over MCP (tools/call).

  1) add(a,b)     - MAG's model discovers + calls the MCP tool; assert mcp_call item
                    with output "42" and the answer mentions 42
  2) write_file   - the MCP server (running in the sandbox) writes a file; assert the
                    file lands on disk in the sandbox's hardened write scope (cwd)

Usage:  python test_mcp.py [base_url]
"""
import json
import os
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"
H = {"OpenAI-Beta": "agents=v1"}
MODEL = os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex")
FIXTURE = os.path.abspath(os.path.join(os.path.dirname(__file__), "mcp_server_fixture.py"))
PYEXE = os.environ.get("MCP_FIXTURE_PYTHON", sys.executable)

ok = True
def check(c, l):
    global ok; ok = ok and bool(c); print(f"  [{'PASS' if c else 'FAIL'}] {l}")


def stream(req):
    events = []
    with httpx.Client(timeout=300.0) as client:
        with client.stream("POST", f"{BASE}/v1/agents/sessions", headers=H, json=req) as r:
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.read()[:200]!r}"); return events
            cur = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    cur = line[7:].strip()
                elif line.startswith("data: "):
                    events.append((cur, json.loads(line[6:])))
    return events


def mcp_tool(cwd):
    return {"type": "mcp", "server_label": "fixture",
            "connection_origin": "environment", "required": False, "request_metadata": {},
            "transport": {"type": "stdio", "command": PYEXE, "args": [FIXTURE],
                          "cwd": cwd, "env_vars": []}}


# ---- 1) discover + call an MCP tool (add) ----------------------------------
print("=== 1) MCP tool discovery + call (add) ===")
ev1 = stream({
    "agent": {"model": MODEL,
              "instructions": "You have MCP tools. Use the mcp_fixture_add tool to add numbers; "
                              "do not compute yourself. Then state the result.",
              "tools": [mcp_tool("/tmp")]},
    "environment": {"type": "self_hosted"},
    "input": [{"role": "user", "content": "Use your MCP add tool to add 2 and 40, then tell me the result."}],
    "stream": True,
})
mcp_items = [d.get("item", {}) for t, d in ev1
             if t == "agent.session.turn.item.done" and d.get("item", {}).get("type") == "mcp_call"]
final = "".join(d.get("text", "") for t, d in ev1 if t == "agent.session.turn.output_text.done")
check(len(mcp_items) >= 1, f"mcp_call turn item emitted (got {len(mcp_items)})")
if mcp_items:
    it = mcp_items[0]
    check(it.get("server_label") == "fixture", f"mcp_call server_label=fixture ({it.get('server_label')})")
    check(it.get("name") == "add", f"mcp_call name=add ({it.get('name')})")
    check(str(it.get("output", "")).strip() == "42", f"mcp_call output=42 ({it.get('output')!r})")
check("42" in final, f"final answer mentions 42 ({final[:80]!r})")

# ---- 2) MCP server writes a file inside the sandbox ------------------------
print("\n=== 2) MCP tool with a real sandbox side-effect (write_file) ===")
wsdir = "/tmp/mcp_test_ws"
os.makedirs(wsdir, exist_ok=True)
proof = os.path.join(wsdir, "proof.txt")
if os.path.exists(proof):
    os.remove(proof)
ev2 = stream({
    "agent": {"model": MODEL,
              "instructions": "You have MCP tools. Use the mcp_fixture_write_file tool to write files.",
              "tools": [mcp_tool(wsdir)]},
    "environment": {"type": "self_hosted"},
    "input": [{"role": "user", "content":
               "Use your MCP write_file tool with path exactly \"proof.txt\" and text exactly "
               "\"MCP_DISK_OK\". Then confirm you did it."}],
    "stream": True,
})
mcp_items2 = [d.get("item", {}) for t, d in ev2
              if t == "agent.session.turn.item.done" and d.get("item", {}).get("type") == "mcp_call"]
check(len(mcp_items2) >= 1, f"mcp_call turn item emitted for write_file (got {len(mcp_items2)})")
check(os.path.exists(proof), f"MCP server wrote {proof} on disk (in sandbox cwd write-scope)")
if os.path.exists(proof):
    check(open(proof).read().strip() == "MCP_DISK_OK", "file content correct (MCP_DISK_OK)")

print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
