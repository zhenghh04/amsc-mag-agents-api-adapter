"""
Bounded-next-steps test: exercises the fidelity items added on top of the core loop.

  1) Native streaming passthrough - real token deltas (>=2) + content_part + item.done(message)
  2) apply_patch tool            - model applies an OpenAI patch envelope; assert on-disk file
  3) Auth passthrough            - a bad caller bearer is forwarded to MAG -> session fails
  4) Persistence                 - GET /v1/agents/sessions/{id} returns the stored session
  5) Cancel                      - mid-flight cancel aborts a long tool -> turn.cancelled;
                                   unknown session -> 404

Usage:  python test_next_steps.py [base_url]
"""
import json
import os
import sys
import threading
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"
H = {"OpenAI-Beta": "agents=v1"}

ok = True
def check(c, l):
    global ok; ok = ok and bool(c); print(f"  [{'PASS' if c else 'FAIL'}] {l}")


def stream(req, headers=None, sink=None, on_event=None):
    events = []
    with httpx.Client(timeout=300.0) as client:
        with client.stream("POST", f"{BASE}/v1/agents/sessions",
                           headers={**H, **(headers or {})}, json=req) as r:
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.read()[:200]!r}"); return events
            cur = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    cur = line[7:].strip()
                elif line.startswith("data: "):
                    ev = (cur, json.loads(line[6:]))
                    events.append(ev)
                    if sink is not None:
                        sink.append(ev)
                    if on_event is not None:
                        on_event(ev)
    return events


def types(events):
    return [t for t, _ in events]


# ---- 1) native streaming passthrough ---------------------------------------
print("=== 1) NATIVE STREAMING PASSTHROUGH ===")
ev1 = stream({
    "agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex"),
              "instructions": "Answer in one or two sentences."},
    "environment": {"type": "none"},
    "input": [{"role": "user", "content": "In one sentence, what is the Argonne Leadership Computing Facility?"}],
    "stream": True,
})
deltas = [d.get("delta", "") for t, d in ev1 if t == "agent.session.turn.output_text.delta"]
tset = types(ev1)
check(len(deltas) >= 2, f">=2 real output_text deltas streamed (got {len(deltas)})")
check("agent.session.turn.in_progress" in tset, "turn.in_progress emitted")
check("agent.session.turn.content_part.added" in tset, "content_part.added emitted")
check("agent.session.turn.content_part.done" in tset, "content_part.done emitted")
check(any(t == "agent.session.turn.item.done" and d.get("item", {}).get("type") == "message"
          for t, d in ev1), "turn.item.done(message) emitted")

# ---- 2) apply_patch ---------------------------------------------------------
print("\n=== 2) apply_patch TOOL (real sandbox) ===")
ev2 = stream({
    "agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex"),
              "instructions": "Use the apply_patch tool to make file changes, then confirm briefly."},
    "environment": {"type": "self_hosted"},
    "input": [{"role": "user", "content":
               "Use the apply_patch tool to add a new file named notes.txt whose only line is "
               "HELLO_MAG. Then tell me it is done."}],
    "stream": True,
})
ws2 = next((d.get("session", {}).get("environment", {}).get("workspace_directory")
            for t, d in ev2 if t == "agent.session.created"), None)
notes = os.path.join(ws2, "notes.txt") if ws2 else None
cmd_items = [d for t, d in ev2 if t == "agent.session.turn.item.done"
             and d.get("item", {}).get("type") == "command_execution"]
check(bool(notes) and os.path.exists(notes), f"apply_patch created {notes} on disk")
if notes and os.path.exists(notes):
    check("HELLO_MAG" in open(notes).read(), "file content correct (HELLO_MAG)")
check(len(cmd_items) >= 1, f"command_execution item.done emitted (got {len(cmd_items)})")

# ---- 3) auth passthrough ----------------------------------------------------
print("\n=== 3) AUTH PASSTHROUGH (caller bearer forwarded to MAG) ===")
r3 = httpx.post(f"{BASE}/v1/agents/sessions",
                headers={**H, "Authorization": "Bearer invalid-token-xyz"},
                json={"agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex")},
                      "environment": {"type": "none"},
                      "input": [{"role": "user", "content": "hi"}]},
                timeout=120.0)
sess3 = r3.json() if r3.status_code == 200 else {}
check(r3.status_code == 200 and sess3.get("status") == "failed",
      f"bad caller token -> session failed (status={sess3.get('status')}) => header was forwarded")

# ---- 4) persistence ---------------------------------------------------------
print("\n=== 4) PERSISTENCE (GET session) ===")
r4 = httpx.post(f"{BASE}/v1/agents/sessions", headers=H,
                json={"agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex"), "instructions": "Answer in one word."},
                      "environment": {"type": "none"},
                      "input": [{"role": "user", "content": "Say hello."}]},
                timeout=120.0)
sid = r4.json().get("id") if r4.status_code == 200 else None
check(bool(sid), f"non-stream run returned a session id ({sid})")
if sid:
    g = httpx.get(f"{BASE}/v1/agents/sessions/{sid}", timeout=30.0)
    check(g.status_code == 200 and g.json().get("id") == sid, "GET returns the same session")
    check(g.json().get("status") == "idle", f"stored status is idle ({g.json().get('status')})")

# ---- 5) cancel --------------------------------------------------------------
print("\n=== 5) CANCEL ===")
c404 = httpx.post(f"{BASE}/v1/agents/sessions/sess_does_not_exist/cancel", timeout=30.0)
check(c404.status_code == 404, "cancel unknown session -> 404")

state = {"sid": None, "events": []}
def run_cancel_stream():
    stream({
        "agent": {"model": os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex"),
                  "instructions": "Use the shell tool exactly as asked."},
        "environment": {"type": "self_hosted"},
        "input": [{"role": "user", "content":
                   "Run the shell command `sleep 8 && echo done`, then reply 'finished'."}],
        "stream": True,
    }, sink=state["events"],
       on_event=lambda ev: state.update(sid=ev[1].get("session", {}).get("id"))
                 if ev[0] == "agent.session.created" else None)

th = threading.Thread(target=run_cancel_stream, daemon=True)
th.start()
# wait for the session to exist, then cancel mid-flight
for _ in range(100):
    if state["sid"]:
        break
    time.sleep(0.1)
cancelled_ok = False
if state["sid"]:
    time.sleep(2.0)  # let the model start the sleep tool
    cc = httpx.post(f"{BASE}/v1/agents/sessions/{state['sid']}/cancel", timeout=30.0)
    check(cc.status_code == 200, "cancel in-flight session -> 200")
    th.join(timeout=20.0)
    cancelled_ok = "agent.session.turn.cancelled" in types(state["events"])
check(cancelled_ok, "mid-flight cancel produced turn.cancelled")

print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
