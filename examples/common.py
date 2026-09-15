"""Shared helpers for the MAG Agents API examples.

These examples use the stock OpenAI SDK *unchanged* — only OPENAI_BASE_URL is
pointed at the MAG shim (see scripts/run_codex_subagents.sh). Nothing here is
MAG-specific; it is a thin, readable event pretty-printer.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

DEFAULT_MODEL = "gpt-5.3-codex"


def require_api_key() -> None:
    """Fail early without printing or otherwise exposing the API key."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set. Export your MAG PAT as OPENAI_API_KEY "
            "(the run_*.sh scripts do this for you from .env)."
        )


def model() -> str:
    return os.environ.get("OPENAI_AGENTS_MODEL", DEFAULT_MODEL)


def event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if hasattr(event, "to_dict"):
        return event.to_dict()
    if isinstance(event, dict):
        return event
    raise TypeError(f"Unsupported event object: {type(event)!r}")


# --- pretty console output -------------------------------------------------- #
_C = {
    "dim": "\033[2m", "bold": "\033[1m", "reset": "\033[0m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "magenta": "\033[35m", "blue": "\033[34m",
}


# tiny bit of render state so streamed text isn't re-printed by the final item
_STATE = {"streamed": False, "cmd_output": False, "session": False}


def _c(name: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{_C[name]}{text}{_C['reset']}"


def print_event(event: Any, raw: bool = False) -> None:
    """Human-friendly renderer for the Agents-API event stream.

    Set raw=True (or env AGENTS_RAW=1) to dump the JSON events verbatim, which is
    what you want when validating the wire format.
    """
    data = event_dict(event)
    if raw or os.environ.get("AGENTS_RAW") == "1":
        print(json.dumps(data, ensure_ascii=False), flush=True)
        return

    etype = data.get("type", "")

    # session lifecycle (print the id once)
    if etype == "agent.session.created" or (data.get("session") or {}).get("id"):
        sid = (data.get("session") or {}).get("id") or data.get("session_id")
        if sid and not _STATE["session"]:
            _STATE["session"] = True
            print(_c("dim", f"· session {sid}"), flush=True)

    # subagent lifecycle — the heart of a multi-agent codex run
    if etype.startswith("agent.session.subagent."):
        sub = data.get("subagent") or data
        label = sub.get("name") or sub.get("id") or "subagent"
        phase = etype.rsplit(".", 1)[-1]
        icon = {"created": "◆", "active": "▶", "closed": "✓"}.get(phase, "·")
        print(_c("magenta", f"  {icon} subagent {phase}: {label}"), flush=True)
        return

    # command executions in the codex sandbox
    if etype in ("agent.session.turn.item.added", "agent.session.turn.item.done"):
        item = data.get("item") or {}
        itype = item.get("type", "")
        if itype in ("command_execution", "local_shell_call", "shell_call"):
            cmd = item.get("command") or item.get("action") or ""
            st = item.get("status", "")
            if etype.endswith("added"):
                print(_c("blue", f"    $ {cmd}"), flush=True)
            else:
                ok = item.get("exit_code")
                tag = _c("green", "ok") if ok in (0, None) else _c("yellow", f"exit {ok}")
                # Output already streamed live via command_execution_output.delta;
                # if no delta arrived (short commands), show it here as a fallback.
                if not _STATE["cmd_output"]:
                    out = (item.get("output") or "").rstrip()
                    for line in out.splitlines()[-6:]:
                        print(_c("dim", f"      {line}"), flush=True)
                _STATE["cmd_output"] = False
                print(_c("dim", f"    [{st} {tag}]"), flush=True)
            return
        if itype == "mcp_call":
            print(_c("cyan", f"    ⚙ mcp {item.get('name')} -> {item.get('server_label')}"), flush=True)
            return
        if itype in ("message", "assistant_message") and etype.endswith("done"):
            text = _text_of(item)
            if text and not _STATE["streamed"]:
                print(_c("bold", "\n=== final answer ===\n") + text + "\n", flush=True)
            elif text:
                print("", flush=True)  # close the streamed line
            return

    # streamed sandbox command output (from the coordinator's own commands)
    if etype == "agent.output.command_execution_output.delta":
        delta = (data.get("delta") or "").rstrip()
        if delta:
            _STATE["cmd_output"] = True
            for line in delta.splitlines():
                print(_c("dim", f"      {line}"), flush=True)
        return

    # streamed assistant text (the coordinator's live synthesis)
    if etype == "agent.session.turn.output_text.delta":
        d = data.get("delta", "")
        if d and not _STATE["streamed"]:
            print(_c("bold", "\n=== final answer (streaming) ===\n"), end="", flush=True)
            _STATE["streamed"] = True
        sys.stdout.write(d)
        sys.stdout.flush()
        return


def _text_of(item: dict) -> str:
    parts = item.get("content") or []
    if isinstance(parts, str):
        return parts
    out = []
    for p in parts:
        if isinstance(p, dict):
            out.append(p.get("text") or p.get("output_text") or "")
        else:
            out.append(str(p))
    return "".join(out).strip()
