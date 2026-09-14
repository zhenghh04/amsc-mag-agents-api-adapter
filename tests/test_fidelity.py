"""
Fidelity test (SDK-faithful event schema):
  A) REAL sandbox   - environment self_hosted enables the shell tool (codex exec-server);
                      model writes a file + sums it; assert the on-disk side effect.
  B) Subagents      - multi_agent enabled; model spawns two parallel subagents that each
                      use the shell; assert subagent.created/closed events + final answer.

Usage:  python test_fidelity.py [base_url] [marker_path]
"""
import json
import os
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"
# Under the hardened sandbox (disk-write-cwd) the model may only write inside its
# per-session workspace; we write a RELATIVE filename and locate it via the
# workspace_directory advertised on the session.created event.
MARKER_NAME = "mag_sbx_marker.txt"


def stream(req):
    events = []
    with httpx.Client(timeout=300.0) as client:
        with client.stream("POST", f"{BASE}/v1/agents/sessions",
                           headers={"OpenAI-Beta": "agents=v1"}, json=req) as r:
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.read()[:300]!r}"); return events
            cur = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    cur = line[7:].strip()
                elif line.startswith("data: "):
                    events.append((cur, json.loads(line[6:])))
    return events


def final_text(events):
    return next((d.get("text", "") for t, d in events
                 if t == "agent.session.turn.output_text.done"), "")


def workspace_dir(events):
    for t, d in events:
        if t == "agent.session.created":
            return (d.get("session", {}).get("environment", {}) or {}).get("workspace_directory")
    return None


ok = True
def check(c, l):
    global ok; ok = ok and c; print(f"  [{'PASS' if c else 'FAIL'}] {l}")


# ---- A) real sandbox --------------------------------------------------------
print("=== A) REAL SANDBOX (environment=self_hosted -> codex exec-server) ===")
evA = stream({
    "agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex"),
              "instructions": "Use the shell tool to do exactly what the user asks, then answer briefly."},
    "environment": {"type": "self_hosted"},
    "input": [{"role": "user", "content":
               f"Using the shell tool: write numbers 1..5 (one per line) to a file named "
               f"{MARKER_NAME} in the current directory, then sum them with awk and tell me the sum."}],
    "stream": True,
})
fa = final_text(evA)
ws = workspace_dir(evA)
marker = os.path.join(ws, MARKER_NAME) if ws else None
check("15" in fa, f"final answer contains 15 -> {fa[:80]!r}")
check(bool(ws), f"session advertised workspace_directory -> {ws}")
check(bool(marker) and os.path.exists(marker), f"on-disk side effect in workspace: {marker}")
if marker and os.path.exists(marker):
    check(open(marker).read().split() == ["1", "2", "3", "4", "5"], "file contents correct")

# ---- B) parallel subagents --------------------------------------------------
print("\n=== B) PARALLEL SUBAGENTS (multi_agent enabled) ===")
evB = stream({
    "agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex"),
              "instructions": "Delegate via run_subagent. Spawn the requested subagents in one "
                              "turn (parallel), then summarize their results in one sentence.",
              "multi_agent": {"enabled": True, "max_concurrent_subagents": 2}},
    "environment": {"type": "self_hosted"},
    "input": [{"role": "user", "content":
               "Spawn two subagents in parallel: 'kernel' runs shell `uname -s`; 'cpus' runs "
               "shell `nproc`. Report both values."}],
    "stream": True,
})
fb = final_text(evB)
started = [d for t, d in evB if t == "agent.session.subagent.created"]
closed = [d for t, d in evB if t == "agent.session.subagent.closed"]
check(len(started) >= 2, f">=2 subagent.created (got {len(started)})")
check(len(closed) >= 2, f">=2 subagent.closed (got {len(closed)})")
check("Linux" in fb, f"final reflects kernel subagent -> {fb[:100]!r}")
check(any(c.isdigit() for c in fb), "final reflects cpu-count subagent")

print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
