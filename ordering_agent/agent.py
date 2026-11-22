"""The ordering agent: a streaming Claude tool-use loop over one call's order.

`OrderingAgent.respond()` is an async generator of text chunks, so a transport
(phone, terminal, web) can start speaking/printing before the reply is done.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable

import anthropic

from .menu import Menu
from .order import Order, OrderError, normalize_phone
from .payments import PaymentService
from .prompts import build_greeting, build_system_prompt
from .storage import OrderStore
from .tools import CallSession, run_tool, tools_for

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
        payments: PaymentService | None = None,
        pos=None,
    ):
        self.client = client or anthropic.AsyncAnthropic()
        self.menu = menu
        self.greeting = build_greeting(menu)
        self.tools = tools_for(menu.template.modules)  # what this kind of business can do
        self.session = CallSession(order=Order(menu=menu), store=store, call_id=call_id,
                                   payments=payments, pos=pos)
        if caller_phone:
            try:
                self.session.order.customer_phone = normalize_phone(caller_phone)
            except OrderError:
                pass  # hidden or odd caller ID; the agent asks for a number if it needs one
        # The system prompt and tools are identical across calls, so they get cached.
        self._system = [
            {
                "type": "text",
                "text": build_system_prompt(menu, payment_links=payments is not None)
                + f'\nThe call opened with you saying: "{self.greeting}"',
            }
        ]
        self.messages: list = []
        self._pending_note: str | None = None
        # Called as on_tool(name, input, result_text, is_error) after each tool runs (transcripts).
        self.on_tool: Callable[[str, dict, str, bool], None] | None = None
        # True from the moment the model starts a tool call until its result is in the
        # history. Cancelling then would lose the call (e.g. place_order) or leave a
        # tool_use without a tool_result, so an interruption mutes the reply instead.
        self._tool_turn = False
        self._muted = False

    @property
    def end_requested(self) -> bool:
        return self.session.end_requested

    def stop_speaking(self) -> bool:
        """The caller interrupted. Returns True if the current reply has a tool call in
        flight: it then finishes silently (the tool runs, its result is recorded, nothing
        more is said) and the caller must await it rather than cancel it. Returns False
        when the reply can simply be cancelled."""
        if self._tool_turn:
            self._muted = True
            return True
        return False

    def note_interrupted(self, heard: str | None) -> None:
        """The caller talked over the agent. Record what they actually heard."""
        if heard:
            note = f'(You were interrupted. The caller only heard you say: "{heard}"'
        else:
            note = "(You were interrupted before the caller heard your reply"
        self._pending_note = (note + ". Anything after that, including tool results you "
                              "haven't told them about, they have not heard.)")

    def _request_params(self) -> dict:
        params: dict = {
            "model": MODEL,
            "max_tokens": 16000,
            "system": self._system,
            "tools": self.tools,
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

    def _add_caller_text(self, text: str) -> None:
        # After an interrupted reply the history can already end on a user turn (a
        # tool_result, or a turn that never got an answer). Extend it instead of
        # sending two user turns in a row.
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "user":
            blocks = last["content"]
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            last["content"] = [*blocks, {"type": "text", "text": text}]
        else:
            self.messages.append({"role": "user", "content": text})

    async def respond(self, caller_text: str) -> AsyncIterator[str]:
        """Handle one caller utterance, yielding reply text as it streams."""
        self._tool_turn = self._muted = False
        try:
            async for chunk in self._respond(caller_text):
                if not self._muted:
                    yield chunk
        finally:
            self._tool_turn = self._muted = False

    async def _respond(self, caller_text: str) -> AsyncIterator[str]:
        content = caller_text
        if self._pending_note:
            content = f"{self._pending_note}\n{caller_text}"
            self._pending_note = None
        self._add_caller_text(content)

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
                        elif (event.type == "content_block_start"
                              and getattr(event.content_block, "type", None) == "tool_use"):
                            self._tool_turn = True
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

            # From here until the tool results are appended, interruptions mute rather than
            # cancel (see stop_speaking), so a tool_use always gets its tool_result.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                yield REFUSAL_LINE
                return
            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return  # end_turn, or a text-only answer
            self._tool_turn = True
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
                    # Tools can call out to Stripe/Twilio; keep the event loop free for other calls.
                    text, is_error = await asyncio.to_thread(
                        run_tool, self.session, block.name, block.input)
                    log.info("tool %s(%s) -> %s", block.name, block.input, text)
                    if self.on_tool:
                        try:
                            self.on_tool(block.name, block.input, text, is_error)
                        except Exception:
                            log.exception("on_tool hook failed")
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": text,
                         "is_error": is_error}
                    )
            # All results for one assistant turn go back in a single user message.
            self.messages.append({"role": "user", "content": results})
            self._tool_turn = False

            if self._muted:
                return  # interrupted: the tools ran; the next turn tells the caller
            if self.session.end_requested:
                return  # the goodbye has been said; no need for another model turn

        log.warning("hit MAX_TOOL_ROUNDS for one turn")
        yield ERROR_LINE
