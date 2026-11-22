"""Running tools on a call: the tools every call has (end_call), the tool list for a
business (its template's modules), and the dispatcher.

The dispatcher never raises for bad input: errors go back to the model as the tool's
result so it can ask the caller. Module tools are defined in phone_agent/modules/.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .modules import MODULES, modules_for
from .session import BadInput, CallSession, ToolError, tool

log = logging.getLogger("phone_agent.tools")

__all__ = ["CallSession", "COMMON_TOOLS", "TOOLS", "run_tool", "start_session", "tools_for"]

COMMON_TOOLS: list[dict] = [
    tool(
        "end_call",
        "Hang up after you have said goodbye. Use when the caller is done or asks to end the call.",
        {},
        [],
    ),
]

# Every built tool, modules first (in module order) then the common ones.
TOOLS: list[dict] = [t for m in MODULES.values() for t in m.tools] + COMMON_TOOLS


def tools_for(module_names) -> list[dict]:
    """Tool schemas for a business: its modules' tools plus the common ones."""
    return [t for m in modules_for(module_names) for t in m.tools] + COMMON_TOOLS


def start_session(session: CallSession) -> CallSession:
    """Let each of the business's modules set up its per-call state."""
    for m in modules_for(session.catalog.template.modules):
        if m.start:
            m.start(session)
    return session


def run_tool(session: CallSession, name: str, args: Any) -> tuple[str, bool]:
    """Execute one tool call. Returns (result_text, is_error). Never raises for bad input."""
    if not isinstance(args, dict):
        return json.dumps({"INVALID_JSON": json.dumps(args)}), True
    try:
        if name == "end_call":
            session.end_requested = True
            result = {"ok": True}
        else:
            module = next((m for m in modules_for(session.catalog.template.modules)
                           if name in m.tool_names), None)
            if module is None:
                return f"Unknown tool '{name}'.", True
            result = module.handle(session, name, args)
    except BadInput as e:
        return f"Invalid input: {e}.", True
    except ToolError as e:
        return str(e), True

    session.log.append({"tool": name, "args": args, "result": result})
    return json.dumps(result), False
