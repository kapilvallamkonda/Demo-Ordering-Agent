"""The ordering agent: a streaming Claude tool-use loop over one call's order.

`OrderingAgent.respond()` is an async generator of text chunks, so a transport
(phone, terminal, web) can start speaking/printing before the reply is done.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator

import anthropic

from .menu import Menu
from .order import Order
from .prompts import build_greeting, build_system_prompt
from .storage import OrderStore
from .tools import TOOLS, CallSession, run_tool

log = logging.getLogger(__name__)

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
# Phone replies are short, and callers notice every bit of latency. "low" effort keeps
# turns quick. Raise to "medium" if you see mistakes on complicated orders.
EFFORT = os.environ.get("AGENT_EFFORT", "low")
FAST_MODE = os.environ.get("AGENT_FAST_MODE", "").lower() in {"1", "true", "yes"}
MAX_TOOL_ROUNDS = 8  # guard against runaway loops within a single caller turn
MAX_JSON_RETRIES = 2

REFUSAL_LINE = "Sorry, I can't help with that. Is there anything from the menu I can get for you?"
ERROR_LINE = "Sorry, I'm having trouble on my end. Could you say that again?"


class OrderingAgent:
    def __init__(
        self,
        menu: Menu,
        client: anthropic.AsyncAnthropic | None = None,
        store: OrderStore | None = None,
        call_id: str | None = None,
        caller_phone: str | None = None,
    ):
        self.client = client or anthropic.AsyncAnthropic()
        self.menu = menu
        self.greeting = build_greeting(menu)
        self.session = CallSession(order=Order(menu=menu), store=store, call_id=call_id)
        if caller_phone:
            self.session.order.customer_phone = caller_phone
        # The system prompt and tools are identical across calls, so they get cached.
        self._system = [
            {
                "type": "text",
                "text": build_system_prompt(menu)
                + f'\nThe call opened with you saying: "{self.greeting}"',
            }
        ]
        self.messages: list = []
        self._pending_note: str | None = None

    @property
    def end_requested(self) -> bool:
        return self.session.end_requested

    def note_interrupted(self, heard: str | None) -> None:
        """The caller talked over the agent. Record what they actually heard."""
        if heard:
            self._pending_note = f'(You were interrupted. The caller only heard you say: "{heard}")'
        else:
            self._pending_note = "(You were interrupted before the caller heard your reply.)"

    def _request_params(self) -> dict:
        params: dict = {
            "model": MODEL,
            "max_tokens": 16000,
            "system": self._system,
            "tools": TOOLS,
            "output_config": {"effort": EFFORT},
            "cache_control": {"type": "ephemeral"},
            # If a safety classifier declines a turn, the API re-runs it on a fallback model
            # instead of leaving the caller hanging.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if FAST_MODE:
            params["speed"] = "fast"
        return params

    async def respond(self, caller_text: str) -> AsyncIterator[str]:
        """Handle one caller utterance, yielding reply text as it streams."""
        content = caller_text
        if self._pending_note:
            content = f"{self._pending_note}\n{caller_text}"
            self._pending_note = None
        self.messages.append({"role": "user", "content": content})

        json_retries = 0
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            try:
                async with self.client.beta.messages.stream(
                    **self._request_params(), messages=self.messages
                ) as stream:
                    async for event in stream:
                        if event.type == "text":
                            yield event.text
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # The SDK couldn't parse a streamed tool input at all. There is no complete
                # tool_use to answer, so re-issue the turn (bounded).
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    log.exception("tool input JSON unparseable; giving up")
                    yield ERROR_LINE
                    return
                continue
            except anthropic.APIError:
                log.exception("Claude API error")
                yield ERROR_LINE
                return

            # Everything from here until the next await is synchronous, so a cancellation
            # (caller interrupts) can't leave a tool_use without its tool_result.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                yield REFUSAL_LINE
                return
            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return  # end_turn, or a text-only answer
            if response.stop_reason == "max_tokens":
                # A truncated tool input can parse as a valid-looking partial object; don't run it.
                results = [
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                     "content": "Tool input was cut off. Try again."}
                    for b in tool_uses
                ]
            else:
                results = []
                for block in tool_uses:
                    text, is_error = run_tool(self.session, block.name, block.input)
                    log.info("tool %s(%s) -> %s", block.name, block.input, text)
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": text,
                         "is_error": is_error}
                    )
            # All results for one assistant turn go back in a single user message.
            self.messages.append({"role": "user", "content": results})

            if self.session.end_requested:
                return  # the goodbye has been said; no need for another model turn

        log.warning("hit MAX_TOOL_ROUNDS for one turn")
        yield ERROR_LINE
