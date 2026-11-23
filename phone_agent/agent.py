"""The phone agent: a streaming Claude tool-use loop over one call (its order, for the orders module).

`PhoneAgent.respond()` is an async generator of text chunks, so a transport
(phone, terminal, web) can start speaking/printing before the reply is done.
"""

from __future__ import annotations

import asyncio
import time
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable

import anthropic

from .catalog import Catalog

from .payments import PaymentService
from . import languages, schedule, web_analytics
from .prompts import build_greeting, build_system_prompt
from .storage import Store
from .tools import CallSession, run_tool, start_session, tools_for

log = logging.getLogger(__name__)

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
# Phone replies are short, and callers notice every bit of latency. "low" effort keeps
# turns quick. Raise to "medium" if you see mistakes on complicated orders.
EFFORT = os.environ.get("AGENT_EFFORT", "low")
FAST_MODE = os.environ.get("AGENT_FAST_MODE", "").lower() in {"1", "true", "yes"}
MAX_TOOL_ROUNDS = 8  # guard against runaway loops within a single caller turn
MAX_JSON_RETRIES = 2

REFUSAL_LINE = "Sorry, I can't help with that. Is there anything from the catalog I can get for you?"
ERROR_LINE = "Sorry, I'm having trouble on my end. Could you say that again?"


def refusal_kind(message: str) -> str | None:
    """What kind of "no" a tool error was, for analytics (None: some other error)."""
    m = message.lower()
    if "unknown item_id" in m:
        return "not_found"
    if "in person only" in m:
        return "in_person_only"
    if "ahead; the earliest pickup day" in m:
        return "notice"
    if "weekends only" in m or "weekdays only" in m or "only on " in m:
        return "days"
    if "sold out" in m or "out of stock" in m or "not available right now" in m:
        return "unavailable"
    if "needs a '" in m:
        return "missing_choice"
    if "too soon" in m or "already passed" in m:
        return "pickup_time"
    return None


class PhoneAgent:
    def __init__(
        self,
        catalog: Catalog,
        client: anthropic.AsyncAnthropic | None = None,
        store: Store | None = None,
        call_id: str | None = None,
        caller_phone: str | None = None,
        payments: PaymentService | None = None,
        pos=None,
        posthog_distinct_id: str | None = None,
        sms=None,
    ):
        self.client = client or anthropic.AsyncAnthropic()
        # PostHog AI events (web_analytics): the owner testing in the browser, or none for a call.
        self._posthog_distinct_id = posthog_distinct_id
        raw_session_id = str(call_id or uuid.uuid4())
        if not all(char.isalnum() or char in "-_~.@()!':|" for char in raw_session_id):
            raw_session_id = str(uuid.uuid4())
        self._ai_session_id = f"call-{raw_session_id}"
        self.catalog = catalog
        self.greeting = build_greeting(catalog)
        # what this kind of business can do, plus switch_language if it offers other languages
        self.tools = tools_for(catalog.template.modules,
                               [lang.code for lang in languages.for_business(catalog.settings)])
        self.session = start_session(CallSession(
            catalog=catalog, store=store, call_id=call_id, caller_phone=caller_phone,
            payments=payments, pos=pos, sms=sms))
        # The system prompt and tools are identical across calls, so they get cached.
        # The date line comes last so the cached prefix stays the same across calls.
        now = schedule.now_in(catalog.tz)
        self._system = [
            {
                "type": "text",
                "text": build_system_prompt(catalog, payment_links=payments is not None)
                + f'\nThe call opened with you saying: "{self.greeting}"',
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"Right now it's {schedule.say_time(now)}, {now.year}, where the "
                        f"{catalog.template.words['business']} is. Use this for \"today\", \"tomorrow\", "
                        "days of the week, and the year of a date the caller gives without one (the next "
                        "time that date comes round).",
            },
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
        # Measurements for analytics (analytics.call_metrics turns these into the call's row).
        self.stats: dict = {"started": time.time(), "turns": 0, "first_word_ms": [], "input_tokens": 0,
                            "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                            "tool_calls": 0, "tool_errors": 0, "refusals": [], "not_found": 0,
                            "api_errors": 0, "first_item_s": None, "max_cart": 0.0}

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

    def _count_usage(self, usage) -> None:
        if usage is None:
            return
        for key, attr in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                          ("cache_read_tokens", "cache_read_input_tokens"),
                          ("cache_write_tokens", "cache_creation_input_tokens")):
            value = getattr(usage, attr, 0)
            if isinstance(value, int):
                self.stats[key] += value

    def _count_tool(self, name: str, result: str, is_error: bool) -> None:
        st = self.stats
        st["tool_calls"] += 1
        if is_error:
            st["tool_errors"] += 1
            kind = refusal_kind(result)
            if kind == "not_found":
                st["not_found"] += 1
            elif kind:
                st["refusals"].append(kind)
        order = self.session.order
        if name == "add_item" and not is_error:
            if st["first_item_s"] is None:
                st["first_item_s"] = round(time.time() - st["started"], 1)
        if order is not None:
            try:
                st["max_cart"] = max(st["max_cart"], float(order.total))
            except Exception:
                pass

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

    def _capture_tool_span(self, name: str, latency: float, trace_id: str) -> None:
        """A tool run as a PostHog $ai_span: its name and how long it took, nothing else."""
        props = {"$ai_trace_id": trace_id, "$ai_session_id": self._ai_session_id,
                 "$ai_span_id": str(uuid.uuid4()), "$ai_span_name": name, "$ai_latency": round(latency, 3)}
        if not self._posthog_distinct_id:
            props["$process_person_profile"] = False
        web_analytics.capture("$ai_span", props, self._posthog_distinct_id or "system:phone_agent")

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
        self.stats["turns"] += 1
        trace_id = str(uuid.uuid4())
        t0, first = time.monotonic(), True
        try:
            async for chunk in self._respond(caller_text, trace_id):
                if first and chunk.strip():
                    self.stats["first_word_ms"].append(round((time.monotonic() - t0) * 1000))
                    first = False
                if not self._muted:
                    yield chunk
        finally:
            self._tool_turn = self._muted = False

    async def _respond(self, caller_text: str, trace_id: str) -> AsyncIterator[str]:
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
                t_call = time.monotonic()
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
                self._count_usage(getattr(response, "usage", None))
                web_analytics.capture_ai_generation(
                    model=getattr(response, "model", None) or MODEL, usage=getattr(response, "usage", None),
                    latency_s=time.monotonic() - t_call, trace_id=trace_id, session_id=self._ai_session_id,
                    span_name="phone_agent", distinct_id=self._posthog_distinct_id)
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
                self.stats["api_errors"] += 1
                web_analytics.capture_ai_generation(
                    model=MODEL, usage=None, latency_s=time.monotonic() - t_call, trace_id=trace_id,
                    session_id=self._ai_session_id, span_name="phone_agent",
                    distinct_id=self._posthog_distinct_id, error=True)
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
                    tool_started = time.monotonic()
                    text, is_error = await asyncio.to_thread(
                        run_tool, self.session, block.name, block.input)
                    self._capture_tool_span(block.name, time.monotonic() - tool_started, trace_id)
                    log.info("tool %s(%s) -> %s", block.name, block.input, text)
                    self._count_tool(block.name, text, is_error)
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
            if self.session.waiting:
                self.session.waiting = False
                return  # side talk or a TV: stay silent until the caller speaks to us

        log.warning("hit MAX_TOOL_ROUNDS for one turn")
        yield ERROR_LINE
