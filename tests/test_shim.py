"""
Smoke test for the translation layer using the SDK-faithful event schema:
sends an Agents API request with a function tool (streaming) and asserts the
session lifecycle events plus a final answer reflecting the tool result.

Usage:  python test_shim.py [base_url]
"""
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"

REQUEST = {
    "agent": {
        "model": "gpt-5.3-codex",
        "instructions": "When asked about weather you MUST call get_weather, then state the "
                        "result in one sentence.",
        "tools": [{"type": "function", "name": "get_weather",
                   "description": "Get current weather for a city.",
                   "parameters": {"type": "object",
                                  "properties": {"city": {"type": "string"}},
                                  "required": ["city"]}}],
    },
    "environment": {"type": "none"},
    "input": [{"role": "user", "content": "What is the weather in Chicago? Use the tool."}],
    "stream": True,
}


def main() -> int:
    events = []
    with httpx.Client(timeout=180.0) as client:
        with client.stream("POST", f"{BASE}/v1/agents/sessions",
                           headers={"OpenAI-Beta": "agents=v1"}, json=REQUEST) as r:
            if r.status_code != 200:
                print(f"FAIL HTTP {r.status_code}: {r.read()[:300]!r}"); return 1
            cur = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    cur = line[7:].strip()
                elif line.startswith("data: "):
                    events.append((cur, json.loads(line[6:])))

    types = [t for t, _ in events]
    final = next((d.get("text", "") for t, d in events
                  if t == "agent.session.turn.output_text.done"), "")
    ok = True
    def check(c, l):
        nonlocal ok; ok = ok and c; print(f"  [{'PASS' if c else 'FAIL'}] {l}")

    print("=== assertions ===")
    check("agent.session.created" in types, "agent.session.created")
    check("agent.session.turn.created" in types, "agent.session.turn.created")
    check("agent.session.turn.output_text.done" in types, "output_text.done")
    check("agent.session.idle" in types, "agent.session.idle (reached idle)")
    check(("72" in final) or ("clear" in final.lower()),
          f"final reflects tool result -> {final[:90]!r}")
    print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
