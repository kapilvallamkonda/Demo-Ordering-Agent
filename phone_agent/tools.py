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

SWITCH_LANGUAGE = tool(
    "switch_language",
    "Switch the call to another language when the caller speaks it or asks for it, before you "
    "reply in that language. Use en-US to switch back to English.",
    {"language": {"type": "string", "description": "Language code, e.g. te-IN for Telugu, en-US for English."}},
    ["language"],
)

# Every built tool, modules first (in module order) then the common ones.
TOOLS: list[dict] = [t for m in MODULES.values() for t in m.tools] + COMMON_TOOLS


def tools_for(module_names, languages=()) -> list[dict]:
    """Tool schemas for a business: its modules' tools plus the common ones, and
    switch_language when it offers languages besides English (languages.py)."""
    return ([t for m in modules_for(module_names) for t in m.tools] + COMMON_TOOLS
            + ([SWITCH_LANGUAGE] if languages else []))


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
        elif name == "switch_language":
            from .languages import ENGLISH, for_business

            code = str(args.get("language", "")).strip()
            offered = {lang.code for lang in for_business(session.catalog.settings)} | {ENGLISH}
            if code not in offered:
                return f"This line only offers: {', '.join(sorted(offered))}.", True
            session.language = code
            result = {"ok": True, "language": code}
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
