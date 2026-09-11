"""
Builders for OpenAI Agents API wire objects (SDK-faithful).

Field schemas were reverse-engineered from the real `openai==3.13.0` SDK models
(`openai.types.beta.*`) so that the official SDK client (and raw HTTP) accept our
shim's responses unchanged. Validated offline by `validate_models.py` against those
same pydantic models. Pure dicts — no SDK dependency at runtime.
"""
from __future__ import annotations

import time
import uuid


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def build_agent(req: dict, agent_id: str | None = None) -> dict:
    ma = req.get("multi_agent") or {}
    return {
        "id": agent_id or _id("agent"),
        "object": "agent",
        "created_at": _now(),
        "updated_at": _now(),
        "model": req.get("model"),
        "instructions": req.get("instructions"),
        "name": req.get("name"),
        "metadata": req.get("metadata") or {},
        "multi_agent": {
            "enabled": bool(ma.get("enabled", False)),
            "max_concurrent_subagents": ma.get("max_concurrent_subagents"),
        },
        "reasoning": {},  # effort / summary both optional
        "service_tier": "auto",
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tools": req.get("tools") or [],
    }


def build_environment(env_req: dict | None) -> dict:
    t = (env_req or {}).get("type", "none")
    if t == "self_hosted":
        return {
            "type": "self_hosted",
            "id": _id("env"),
            "remote_url": (env_req or {}).get("remote_url", ""),
            "workspace_directory": (env_req or {}).get("workspace_directory", "/workspace"),
            "capability_directories": (env_req or {}).get("capability_directories", []),
        }
    # The shim treats openai_hosted as unavailable; everything else -> none.
    return {"type": "none"}


def build_session(agent: dict, environment: dict, status: str = "in_progress",
                  session_id: str | None = None, error: dict | None = None) -> dict:
    return {
        "id": session_id or _id("sess"),
        "object": "agent.session",
        "agent": agent,
        "environment": environment,
        "created_at": _now(),
        "last_active_at": _now(),
        "metadata": {},
        "required_actions": [],
        "vault_ids": [],
        "status": status,
        "error": error,
        "usage": None,
    }


def build_turn(session_id: str, agent_id: str, status: str = "in_progress",
               turn_id: str | None = None) -> dict:
    return {
        "id": turn_id or _id("turn"),
        "object": "agent.session.turn",
        "agent_id": agent_id,
        "session_id": session_id,
        "created_at": _now(),
        "started_at": _now(),
        "completed_at": _now() if status in ("completed", "failed", "cancelled") else None,
        "status": status,
        "error": None,
        "subagent_id": None,
        "usage": None,
    }


def build_subagent(session_id: str, parent_agent_id: str, name: str | None = None,
                   instructions: str | None = None, closed: bool = False,
                   sub_id: str | None = None) -> dict:
    return {
        "id": sub_id or _id("subagent"),
        "object": "agent.session.subagent",
        "session_id": session_id,
        "parent_agent_id": parent_agent_id,
        "opened_at": _now(),
        "closed_at": _now() if closed else None,
        "name": name,
        # SDK: Subagent.instructions is an optional list of content items, not a string.
        "instructions": instructions if isinstance(instructions, list) else None,
        "status": "closed" if closed else "active",
    }


# --------------------------------------------------------------------------- #
# Streaming events (discriminated on `type`)
# --------------------------------------------------------------------------- #
def _ev(type_: str, **payload) -> dict:
    return {"type": type_, "event_id": _id("event"), **payload}


def ev_session_created(session):      return _ev("agent.session.created", session=session)
def ev_session_in_progress(session):  return _ev("agent.session.in_progress", session=session)
def ev_session_idle(session):         return _ev("agent.session.idle", session=session)


def ev_turn_created(session_id, turn):
    return _ev("agent.session.turn.created", session_id=session_id, turn=turn, turn_id=turn["id"])


def ev_turn_completed(session_id, turn):
    return _ev("agent.session.turn.completed", session_id=session_id, turn=turn,
               turn_id=turn["id"], usage=None)


def ev_output_text_delta(session_id, turn_id, item_id, delta, output_index=0, content_index=0):
    return _ev("agent.session.turn.output_text.delta", session_id=session_id, turn_id=turn_id,
               item_id=item_id, output_index=output_index, content_index=content_index, delta=delta)


def ev_output_text_done(session_id, turn_id, item_id, text, output_index=0, content_index=0):
    return _ev("agent.session.turn.output_text.done", session_id=session_id, turn_id=turn_id,
               item_id=item_id, output_index=output_index, content_index=content_index, text=text)


def ev_error(session_id, message, type_="server_error", code=None, param=None):
    return _ev("error", session_id=session_id,
               error={"message": message, "type": type_, "code": code, "param": param})


def ev_subagent_created(subagent):  return _ev("agent.session.subagent.created", subagent=subagent)
def ev_subagent_closed(subagent):   return _ev("agent.session.subagent.closed", subagent=subagent)
