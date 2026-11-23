"""Analytics: what happened on calls and in setup, and where things go wrong.

Three parts:
  1. Measurements. Each call's row gets an outcome and metrics (call_metrics) when it
     ends; everything else worth counting is an event (Store.track): signed in, menu
     read, published, order placed, POS send failed, call turned away ...
  2. The AI review. After a call with a caller in it ends, a small, cheap model reads the
     transcript and records a structured review: outcome and reason, friction moments,
     questions the agent couldn't answer, requests we couldn't meet, agent mistakes, mood
     and a one-line summary (CallReviewer). One review at a time per business, a few at
     once overall, and a daily cap per business, so one busy business can't hold up or
     run up costs for others.
  3. Reports (report): funnels, call outcomes and drop-off, speed, failures, cost and
     the review roll-ups, for the operator (all businesses) or one business.

Privacy: events carry no transcripts; phone numbers are only ever hashed here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import Counter

log = logging.getLogger("phone_agent.analytics")

REVIEW_MODEL = os.environ.get("CALL_REVIEW_MODEL", "claude-haiku-5-5")
REVIEWS_ON = os.environ.get("CALL_REVIEWS", "on").lower() not in ("off", "0", "false", "no")
REVIEWS_PER_BUSINESS_PER_DAY = int(os.environ.get("CALL_REVIEWS_PER_BUSINESS_PER_DAY", "300"))
REVIEW_CONCURRENCY = 3

# Dollars per million tokens, for the cost estimate (override with env if prices change).
PRICES = {
    "agent": {"input": float(os.environ.get("PRICE_AGENT_INPUT", 4.0)),
              "output": float(os.environ.get("PRICE_AGENT_OUTPUT", 20.0)),
              "cache_read": float(os.environ.get("PRICE_AGENT_CACHE_READ", 0.20)),
              "cache_write": float(os.environ.get("PRICE_AGENT_CACHE_WRITE", 5.0))},
    "review": {"input": float(os.environ.get("PRICE_REVIEW_INPUT", 0.10)),
               "output": float(os.environ.get("PRICE_REVIEW_OUTPUT", 0.50))},
}


def hash_phone(phone: str | None) -> str | None:
    """A stable, non-reversible id for a caller (repeat callers), never the number."""
    if not phone:
        return None
    salt = os.environ.get("ANALYTICS_SALT") or os.environ.get("POS_SECRET_KEY") or "ringcart"
    return hashlib.sha256(f"{salt}:{phone}".encode()).hexdigest()[:16]


def agent_cost(stats: dict) -> float:
    p = PRICES["agent"]
    return round((stats.get("input_tokens", 0) * p["input"] + stats.get("output_tokens", 0) * p["output"]
                  + stats.get("cache_read_tokens", 0) * p["cache_read"]
                  + stats.get("cache_write_tokens", 0) * p["cache_write"]) / 1e6, 5)


# ---- 1. per-call measurements -----------------------------------------------------------


def call_metrics(agent, interruptions: int, ended_by: str, caller_phone: str | None) -> tuple[str, dict]:
    """(outcome, metrics) for a finished call. outcome: order, no_order (the caller talked
    but didn't order) or no_speech (hung up or silent before saying anything)."""
    st = agent.stats
    session = agent.session
    order = session.order
    ordered = bool(session.order_number)
    outcome = "order" if ordered else ("no_order" if st["turns"] else "no_speech")
    lat = sorted(st["first_word_ms"])
    metrics = {
        "duration_s": round(time.time() - st["started"], 1),
        "turns": st["turns"],
        "interruptions": interruptions,
        "first_word_ms": lat,
        "first_item_s": st["first_item_s"],
        "tool_calls": st["tool_calls"],
        "tool_errors": st["tool_errors"],
        "refusals": st["refusals"],
        "not_found": st["not_found"],
        "api_errors": st["api_errors"],
        "order_total": float(order.total) if ordered and order is not None else None,
        "cart_left": round(st["max_cart"], 2) if not ordered and st["max_cart"] else None,
        "ended_by": ended_by,
        "caller": hash_phone(caller_phone),
        "tokens": {k: st[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")},
        "cost": agent_cost(st),
    }
    return outcome, metrics


# ---- 2. the AI review of each call ------------------------------------------------------

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["order_placed", "no_order", "hung_up_early", "info_only",
                                               "wrong_number_or_spam"]},
        "no_order_reason": {"type": "string", "enum": [
            "none", "just_asking", "price", "item_unavailable", "not_on_menu", "wanted_delivery",
            "wanted_reservation", "wanted_a_person", "agent_confusion", "too_slow", "payment",
            "pickup_time", "hung_up_unknown", "other"]},
        "reason_detail": {"type": "string"},
        "caller_intent": {"type": "string"},
        "friction": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": [
                "misheard", "repeated_themselves", "confusing_answer", "too_long", "slow_reply",
                "refusal", "price_surprise", "had_to_wait", "other"]},
                "what": {"type": "string"}},
            "required": ["kind", "what"], "additionalProperties": False}},
        "unanswered_questions": {"type": "array", "items": {"type": "string"}},
        "unmet_requests": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": [
                "not_on_menu", "delivery", "reservation", "speak_to_person", "catering", "other"]},
                "what": {"type": "string"}},
            "required": ["kind", "what"], "additionalProperties": False}},
        "agent_mistakes": {"type": "array", "items": {"type": "string"}},
        "sentiment_start": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "sentiment_end": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "summary": {"type": "string"},
    },
    "required": ["outcome", "no_order_reason", "reason_detail", "caller_intent", "friction",
                 "unanswered_questions", "unmet_requests", "agent_mistakes", "sentiment_start",
                 "sentiment_end", "summary"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """You review one phone call between a caller and the AI ordering assistant of \
{business}. The transcript is below: "caller" is the customer, "agent" is the assistant, "tool" \
lines are what the system did (errors there are refusals the caller heard about), "event" lines \
are things like interruptions.

Fill in the review for the business owner:
- outcome and no_order_reason (use "none" when an order was placed); reason_detail: one short \
sentence on why, in plain words.
- caller_intent: what the caller wanted, in a few words.
- friction: moments the caller struggled (misheard words, repeating themselves, confusing or \
too-long answers, refusals, a surprising price). Only real moments; empty if it went smoothly.
- unanswered_questions: questions the agent couldn't answer or deflected, as the caller asked \
them (short). Only ones that went unanswered.
- unmet_requests: things the caller wanted that weren't possible.
- agent_mistakes: concrete errors by the assistant (wrong item or price, missed a required \
choice, made up information, rude or confusing). Empty if none.
- sentiment at the start and the end, and a one-line summary (under 20 words) an owner can scan.
Be factual and brief; don't invent anything that isn't in the transcript."""


def transcript_for_review(lines: list[tuple[str, str]], max_chars: int = 40000) -> str:
    text = "\n".join(f"{role}: {line}" for role, line in lines)
    return text[-max_chars:]


class CallReviewer:
    """Reviews finished calls in the background. Isolation: one review at a time per
    business, REVIEW_CONCURRENCY overall, and a daily cap per business (Store.take_usage)."""

    def __init__(self, store, client, model: str = REVIEW_MODEL):
        self.store = store
        self.client = client
        self.model = model
        self._all = asyncio.Semaphore(REVIEW_CONCURRENCY)
        self._per_business: dict[int | None, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    def schedule(self, call_id: str, business_id: int | None, business_name: str) -> None:
        if not REVIEWS_ON or self.client is None:
            return
        task = asyncio.get_running_loop().create_task(self.review(call_id, business_id, business_name))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def review(self, call_id: str, business_id: int | None, business_name: str) -> dict | None:
        lock = self._per_business.setdefault(business_id, asyncio.Lock())
        try:
            async with lock, self._all:
                lines = self.store.call_transcript_text(call_id)
                if not any(role == "caller" for role, _ in lines):
                    return None
                day = time.strftime("%Y-%m-%d", time.gmtime())
                if not self.store.take_usage(f"business:{business_id}", "call_review", day,
                                             REVIEWS_PER_BUSINESS_PER_DAY):
                    log.info("business %s: daily call-review cap reached; skipping %s", business_id, call_id)
                    return None
                async with self.client.beta.messages.stream(
                    model=self.model, max_tokens=4000,
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
                    system=REVIEW_PROMPT.format(business=business_name or "the business"),
                    messages=[{"role": "user", "content": "<transcript>\n" + transcript_for_review(lines)
                               + "\n</transcript>"}],
                ) as stream:
                    message = await stream.get_final_message()
                if message.stop_reason in ("refusal", "max_tokens"):
                    log.warning("call %s review stopped: %s", call_id, message.stop_reason)
                    return None
                text = next((b.text for b in message.content if b.type == "text"), "")
                review = json.loads(text)
                usage = getattr(message, "usage", None)
                p = PRICES["review"]
                review["_cost"] = round(((getattr(usage, "input_tokens", 0) or 0) * p["input"]
                                         + (getattr(usage, "output_tokens", 0) or 0) * p["output"]) / 1e6, 6)
                self.store.save_call_review(call_id, business_id, review, self.model)
                self.store.track("call_reviewed", business_id, call_id=call_id, outcome=review.get("outcome"),
                                 reason=review.get("no_order_reason"))
                return review
        except Exception:
            log.exception("call %s: review failed", call_id)
            self.store.track("call_review_failed", business_id, call_id=call_id)
            return None


# ---- 3. reports -------------------------------------------------------------------------


def _pct(n: float, d: float) -> float | None:
    return round(100 * n / d, 1) if d else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo))


def report(store, days: int = 7, business_id: int | None = None, now: float | None = None,
           tz_offset_min: int = 0) -> dict:
    """Everything the Analytics pages show. business_id None: all businesses (operator)."""
    now = now or time.time()
    since = now - days * 86400
    scope, args = ("AND business_id = ?", (business_id,)) if business_id is not None else ("", ())

    # calls
    rows = store.read("SELECT call_id, business_id, started_at, ended_at, outcome, metrics, order_number"
                      f" FROM calls WHERE started_at >= ? {scope}", (since, *args))
    calls = []
    for call_id, bid, started, ended, outcome, metrics, order_number in rows:
        m = json.loads(metrics) if metrics else {}
        calls.append({"call_id": call_id, "business_id": bid, "started": started, "ended": ended,
                      "outcome": outcome or ("order" if order_number else None), "m": m})
    finished = [c for c in calls if c["outcome"]]
    talked = [c for c in finished if c["outcome"] in ("order", "no_order")]
    ordered = [c for c in finished if c["outcome"] == "order"]
    latencies = [ms for c in finished for ms in c["m"].get("first_word_ms", [])]
    durations = [c["m"]["duration_s"] for c in finished if c["m"].get("duration_s") is not None]
    first_item = [c["m"]["first_item_s"] for c in finished if c["m"].get("first_item_s") is not None]
    refusals = Counter(k for c in finished for k in c["m"].get("refusals", []))
    callers = Counter(c["m"].get("caller") for c in finished if c["m"].get("caller"))

    # orders and money
    orows = store.read(f"SELECT total, status, payment_status, pos_status, created_at FROM orders"
                       f" WHERE created_at >= ? {scope}", (since, *args))
    revenue = sum(r[0] for r in orows if r[1] != "cancelled")
    by_day: dict[str, dict] = {}
    for c in calls:
        day = time.strftime("%Y-%m-%d", time.gmtime(c["started"] - tz_offset_min * 60))
        d = by_day.setdefault(day, {"calls": 0, "orders": 0})
        d["calls"] += 1
        d["orders"] += c["outcome"] == "order"
    hours = Counter(int(time.strftime("%H", time.gmtime(c["started"] - tz_offset_min * 60))) for c in calls)

    # turned-away calls, failures and other events
    erows = store.read(f"SELECT kind, props, business_id FROM events WHERE at >= ? {scope}", (since, *args))
    kinds = Counter(r[0] for r in erows)
    declined = Counter(json.loads(r[1]).get("reason", "other") for r in erows if r[0] == "call_declined")
    imports = [json.loads(r[1]) for r in erows if r[0] in ("menu_import_done", "menu_import_failed")]
    import_secs = [i["seconds"] for i in imports if i.get("seconds") is not None and i.get("ok")]

    # reviews
    rrows = store.read("SELECT r.call_id, r.review, r.business_id, c.started_at FROM call_reviews r"
                       " JOIN calls c ON c.call_id = r.call_id"
                       f" WHERE c.started_at >= ? {scope.replace('business_id', 'r.business_id')}", (since, *args))
    reviews = [(cid, json.loads(rv), bid, at) for cid, rv, bid, at in rrows]
    reasons = Counter(rv["no_order_reason"] for _, rv, _, _ in reviews if rv.get("no_order_reason") not in (None, "none"))
    friction = Counter(f["kind"] for _, rv, _, _ in reviews for f in rv.get("friction", []))
    unmet = Counter(u["kind"] for _, rv, _, _ in reviews for u in rv.get("unmet_requests", []))
    names = _names(store)

    def examples(pick, limit=25):
        """The same thing said on several calls is one entry with a count, most frequent first."""
        groups: dict = {}
        for cid, rv, bid, at in sorted(reviews, key=lambda r: -r[3]):
            for text in pick(rv):
                key = " ".join(str(text).lower().split()).rstrip("?.! ")
                g = groups.get(key)
                if g is None:
                    groups[key] = {"text": text, "count": 1, "call_id": cid, "business": names.get(bid, "?"),
                                   "businesses": {names.get(bid, "?")}, "at": at}
                else:
                    g["count"] += 1
                    g["businesses"].add(names.get(bid, "?"))
        out = sorted(groups.values(), key=lambda g: (-g["count"], -g["at"]))[:limit]
        for g in out:
            g["businesses"] = sorted(g["businesses"])
        return out

    sentiment_end = Counter(rv.get("sentiment_end") for _, rv, _, _ in reviews)
    agent_cost_total = sum(c["m"].get("cost", 0) for c in finished)
    review_cost_total = sum(rv.get("_cost", 0) for _, rv, _, _ in reviews)

    out = {
        "days": days, "since": since, "business_id": business_id,
        "calls": {
            "total": len(calls), "finished": len(finished), "talked": len(talked), "orders": len(ordered),
            "no_order": sum(c["outcome"] == "no_order" for c in finished),
            "no_speech": sum(c["outcome"] == "no_speech" for c in finished),
            "conversion_pct": _pct(len(ordered), len(talked)),
            "declined": dict(declined), "declined_total": sum(declined.values()),
            "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
            "avg_turns": round(sum(c["m"].get("turns", 0) for c in talked) / len(talked), 1) if talked else None,
            "avg_first_item_s": round(sum(first_item) / len(first_item), 1) if first_item else None,
            "interruptions_per_call": round(sum(c["m"].get("interruptions", 0) for c in talked) / len(talked), 2)
            if talked else None,
            "repeat_callers": sum(1 for n in callers.values() if n > 1),
            "by_day": [{"day": d, **v} for d, v in sorted(by_day.items())],
            "by_hour": [hours.get(h, 0) for h in range(24)],
            "drop_off": {  # where talking callers left without ordering
                "before_adding_anything": sum(1 for c in talked if c["outcome"] == "no_order"
                                              and c["m"].get("first_item_s") is None),
                "with_items_in_cart": sum(1 for c in talked if c["outcome"] == "no_order"
                                          and c["m"].get("first_item_s") is not None),
            },
        },
        "money": {
            "orders": len([r for r in orows if r[1] != "cancelled"]), "revenue": round(revenue, 2),
            "avg_order": round(revenue / max(1, len([r for r in orows if r[1] != "cancelled"])), 2) if orows else None,
            "left_in_carts": round(sum(c["m"].get("cart_left") or 0 for c in finished), 2),
        },
        "speed": {
            "first_word_p50_ms": _percentile(latencies, 0.5), "first_word_p95_ms": _percentile(latencies, 0.95),
            "turns_measured": len(latencies),
            "menu_read_avg_s": round(sum(import_secs) / len(import_secs), 1) if import_secs else None,
        },
        "problems": {
            "refusals": dict(refusals.most_common()), "items_not_found": sum(c["m"].get("not_found", 0) for c in finished),
            "tool_errors": sum(c["m"].get("tool_errors", 0) for c in finished),
            "agent_api_errors": sum(c["m"].get("api_errors", 0) for c in finished),
            "pos_sent": sum(1 for r in orows if r[3] == "sent"),
            "pos_failed": sum(1 for r in orows if r[3] == "failed"),
            "links_sent": sum(1 for r in orows if r[2] in ("unpaid", "paid", "expired")),
            "links_paid": sum(1 for r in orows if r[2] == "paid"),
            "links_expired": sum(1 for r in orows if r[2] == "expired"),
            "texts_failed": kinds.get("sms_failed", 0),
            "menu_reads": sum(1 for i in imports if i.get("ok")),
            "menu_reads_failed": sum(1 for i in imports if not i.get("ok")),
            "reviews_failed": kinds.get("call_review_failed", 0),
        },
        "reviews": {
            "reviewed": len(reviews),
            "no_order_reasons": reasons.most_common(),
            "friction": friction.most_common(),
            "unmet_requests": unmet.most_common(),
            "sentiment_end": {k: v for k, v in sentiment_end.items() if k},
            "unanswered_questions": examples(lambda rv: rv.get("unanswered_questions", [])),
            "agent_mistakes": examples(lambda rv: rv.get("agent_mistakes", [])),
            "not_on_menu": examples(lambda rv: [u["what"] for u in rv.get("unmet_requests", [])
                                                if u.get("kind") == "not_on_menu"]),
        },
        "cost": {"agent": round(agent_cost_total, 4), "reviews": round(review_cost_total, 4),
                 "per_call": round((agent_cost_total + review_cost_total) / len(finished), 4) if finished else None,
                 "per_order": round((agent_cost_total + review_cost_total) / len(ordered), 4) if ordered else None},
    }
    if business_id is None:
        out["setup_funnel"] = setup_funnel(store, since)
        out["businesses"] = _per_business(store, since, calls, orows_by_business(store, since), names)
    return out


def _names(store) -> dict:
    out = {}
    for (bid,) in store.read("SELECT id FROM businesses"):
        out[bid] = store.business_name(bid)
    return out


def orows_by_business(store, since: float) -> dict:
    out: dict = {}
    for bid, total, status in store.read("SELECT business_id, total, status FROM orders WHERE created_at >= ?", (since,)):
        if status != "cancelled":
            d = out.setdefault(bid, [0, 0.0])
            d[0] += 1
            d[1] += total
    return out


def _per_business(store, since, calls, orders_by_business, names) -> list[dict]:
    per: dict = {}
    for c in calls:
        d = per.setdefault(c["business_id"], {"calls": 0, "talked": 0, "orders": 0})
        d["calls"] += 1
        d["talked"] += c["outcome"] in ("order", "no_order")
        d["orders"] += c["outcome"] == "order"
    unanswered = Counter()
    for bid, rv in store.read("SELECT business_id, review FROM call_reviews WHERE at >= ?", (since,)):
        unanswered[bid] += len(json.loads(rv).get("unanswered_questions", []))
    out = []
    for bid in set(per) | set(orders_by_business):
        d = per.get(bid, {"calls": 0, "talked": 0, "orders": 0})
        n, rev = orders_by_business.get(bid, (0, 0.0))
        out.append({"business_id": bid, "name": names.get(bid, "?"), **d,
                    "conversion_pct": _pct(d["orders"], d["talked"]), "revenue": round(rev, 2),
                    "unanswered": unanswered.get(bid, 0)})
    return sorted(out, key=lambda r: -r["calls"])


def setup_funnel(store, since: float) -> dict:
    """Owners' setup journey. Steps after sign-in are counted over businesses created in
    the window; a business that reached a later step counts for the earlier ones too."""
    ev = store.read("SELECT kind, business_id, props FROM events WHERE at >= ?"
                    " AND kind IN ('landing_view', 'signin', 'test_chat_started')", (since,))
    visitors = {json.loads(p).get("visitor") for k, _, p in ev if k == "landing_view"} - {None}
    landing_views = sum(1 for k, _, _ in ev if k == "landing_view")
    signins = [json.loads(p) for k, _, p in ev if k == "signin"]
    tested = {b for k, b, _ in ev if k == "test_chat_started"}
    biz = store.read("SELECT id, catalog_id, phone_number FROM businesses WHERE created_at >= ?", (since,))
    created = [b[0] for b in biz]
    with_menu, published, live, called, ordered = set(), set(), set(), set(), set()
    for bid, catalog_id, phone in biz:
        draft = store.draft(bid)
        items = len((draft or {}).get("catalog", {}).get("items", [])) if draft else 0
        if items or catalog_id:
            with_menu.add(bid)
        if catalog_id:
            published.add(bid)
        if phone:
            live.add(bid)
    for (bid,) in store.read("SELECT DISTINCT business_id FROM calls"):
        called.add(bid)
    for (bid,) in store.read("SELECT DISTINCT business_id FROM orders"):
        ordered.add(bid)
    steps = [
        ("Visited the site", len(visitors) or landing_views),
        ("Signed in", len({s.get("user") for s in signins}) or len(signins)),
        ("Created a business", len(created)),
        ("Added a menu", len([b for b in created if b in with_menu])),
        ("Tried the test chat", len([b for b in created if b in tested or b in published])),
        ("Published", len([b for b in created if b in published])),
        ("Went live", len([b for b in created if b in live])),
        ("First call", len([b for b in created if b in called])),
        ("First order", len([b for b in created if b in ordered])),
    ]
    return {"steps": [{"step": s, "count": n} for s, n in steps],
            "new_accounts": sum(1 for s in signins if s.get("new"))}

