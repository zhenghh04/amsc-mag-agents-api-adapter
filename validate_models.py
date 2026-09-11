"""
Offline validation: every dict our shim emits must parse against the real
openai==3.13.0 SDK models. Run inside the venv that has openai==3.13.0.
Exit 0 = all builders produce SDK-valid objects.
"""
import sys

from pydantic import TypeAdapter

from openai.types.beta.agent_session import AgentSession
from openai.types.beta.agent_session_event import AgentSessionEvent

import agents_api_models as M

EventAdapter = TypeAdapter(AgentSessionEvent)

ok = True
def check(label, fn):
    global ok
    try:
        fn(); print(f"  [PASS] {label}")
    except Exception as e:
        ok = False; print(f"  [FAIL] {label}\n         {str(e)[:300]}")

req = {"model": "gpt-5.3-codex", "instructions": "hi",
       "multi_agent": {"enabled": True, "max_concurrent_subagents": 2}}
agent = M.build_agent(req)
env = M.build_environment({"type": "none"})
sess_ip = M.build_session(agent, env, status="in_progress")
sess_idle = M.build_session(agent, env, status="idle", session_id=sess_ip["id"])
turn_ip = M.build_turn(sess_ip["id"], agent["id"], status="in_progress")
turn_done = M.build_turn(sess_ip["id"], agent["id"], status="completed", turn_id=turn_ip["id"])
sub = M.build_subagent(sess_ip["id"], agent["id"], name="kernel", instructions="x")
sub_closed = M.build_subagent(sess_ip["id"], agent["id"], name="kernel", closed=True, sub_id=sub["id"])

print("=== non-stream session object ===")
check("AgentSession (in_progress)", lambda: AgentSession.model_validate(sess_ip))
check("AgentSession (idle)", lambda: AgentSession.model_validate(sess_idle))
check("AgentSession self_hosted env",
      lambda: AgentSession.model_validate(M.build_session(agent, M.build_environment({"type": "self_hosted", "remote_url": "ws://x"}))))

print("=== streaming events ===")
events = {
    "session.created": M.ev_session_created(sess_ip),
    "session.in_progress": M.ev_session_in_progress(sess_ip),
    "session.idle": M.ev_session_idle(sess_idle),
    "turn.created": M.ev_turn_created(sess_ip["id"], turn_ip),
    "turn.completed": M.ev_turn_completed(sess_ip["id"], turn_done),
    "output_text.delta": M.ev_output_text_delta(sess_ip["id"], turn_ip["id"], "item_1", "hello"),
    "output_text.done": M.ev_output_text_done(sess_ip["id"], turn_ip["id"], "item_1", "hello world"),
    "error": M.ev_error(sess_ip["id"], "boom"),
    "subagent.created": M.ev_subagent_created(sub),
    "subagent.closed": M.ev_subagent_closed(sub_closed),
}
for label, e in events.items():
    check(label, lambda e=e: EventAdapter.validate_python(e))

print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
