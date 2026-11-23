"""Analytics: call measurements, the AI call review, and the reports."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from phone_agent import analytics, manage, server
from phone_agent.accounts import Accounts, LoginLimiter
from phone_agent.storage import Store

from .conftest import BELLA_PHONE, seed_business
from .fakes import FakeClient, message, text_block, tool_block



REVIEW = {"outcome": "no_order", "no_order_reason": "wanted_delivery", "reason_detail": "Wanted it delivered.",
          "caller_intent": "order dinner", "friction": [{"kind": "refusal", "what": "told no delivery"}],
          "unanswered_questions": ["Is there parking?"],
          "unmet_requests": [{"kind": "delivery", "what": "delivery to home"},
                             {"kind": "not_on_menu", "what": "garlic knots"}],
          "agent_mistakes": [], "sentiment_start": "neutral", "sentiment_end": "negative",
          "summary": "Wanted delivery; left without ordering."}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.com")
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(manage, "limiter", LoginLimiter())
    fake = FakeClient([])
    server.app.state.client, server.app.state.payments = fake, None
    with TestClient(server.app, base_url="https://agent.example") as c:
        yield c, server.app.state.store, fake
    for attr in ("client", "payments"):
        if hasattr(server.app.state, attr):
            delattr(server.app.state, attr)


def sign_in(c, store, email):
    user = store.user_by_email(email)
    uid = user["id"] if user else store.add_user(email, email.split("@")[0])
    c.cookies.set(manage.COOKIE, Accounts(store).start_session(uid))
    return uid


def read_until_last(ws):
    out = []
    while True:
        m = json.loads(ws.receive_text())
        out.append(m.get("token", ""))
        if m.get("last"):
            return "".join(out)


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_a_call_is_measured_reviewed_and_reported(app):
    c, store, fake = app
    bid = seed_business(store)
    fake.script = [
        message(text_block("Sorry, we only do pickup. "), tool_block("add_item", {"item_id": "nope"}, id="t1"),
                stop_reason="tool_use"),
        message(text_block("We don't have that one. Anything else?")),
        message(text_block(json.dumps(REVIEW))),  # the review, after the call ends
    ]
    with c.websocket_connect(f"/ws?business={bid}") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA1", "from": "+15550001111", "to": BELLA_PHONE}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "can you deliver garlic knots?", "last": True}))
        assert "Anything else?" in read_until_last(ws)
        ws.send_text(json.dumps({"type": "interrupt", "utteranceUntilInterrupt": "We don't"}))

    row = wait_for(lambda: store.read("SELECT outcome, metrics FROM calls WHERE call_id = 'CA1' AND outcome IS NOT NULL"))
    outcome, metrics = row[0][0], json.loads(row[0][1])
    assert outcome == "no_order"
    assert metrics["turns"] == 1 and metrics["interruptions"] == 1 and metrics["not_found"] == 1
    assert metrics["ended_by"] == "caller" and len(metrics["first_word_ms"]) == 1
    assert metrics["caller"] and "+1555" not in json.dumps(metrics)  # hashed, never the number
    review = wait_for(lambda: store.recent_calls(business_id=bid)[0]["review"])
    assert review["no_order_reason"] == "wanted_delivery"
    review_req = fake.requests[-1]
    assert review_req["model"] == analytics.REVIEW_MODEL
    assert "can you deliver garlic knots?" in review_req["messages"][0]["content"]
    assert review_req["output_config"]["format"]["type"] == "json_schema"

    # The business sees its own insights; only the operator sees everything.
    owner = sign_in(c, store, "ana@example.com")
    store.grant(owner, bid, role="owner")
    ins = c.get(f"/api/manage/businesses/{bid}/insights").json()
    assert ins["calls"]["talked"] == 1 and ins["calls"]["conversion_pct"] == 0.0
    assert ins["reviews"]["no_order_reasons"] == [["wanted_delivery", 1]]
    assert ins["reviews"]["unanswered_questions"][0]["text"] == "Is there parking?"
    assert ins["reviews"]["unanswered_questions"][0]["count"] == 1
    assert ins["reviews"]["not_on_menu"][0]["text"] == "garlic knots"
    assert ins["problems"]["items_not_found"] == 1
    assert "setup_funnel" not in ins
    assert c.get("/api/manage/admin/analytics").status_code == 403
    sign_in(c, store, "boss@example.com")
    op = c.get("/api/manage/admin/analytics").json()
    assert op["calls"]["total"] == 1 and op["businesses"][0]["calls"] == 1
    assert op["speed"]["first_word_p50_ms"] is not None


def test_report_counts_orders_money_drop_off_and_declines():
    store = Store(":memory:")
    bid = seed_business(store)
    now = time.time()
    rows = [("A", "order", {"turns": 4, "first_item_s": 20, "order_total": 30.0, "first_word_ms": [800, 1200],
                            "duration_s": 90, "cost": 0.02, "caller": "x1"}),
            ("B", "no_order", {"turns": 3, "first_item_s": 15, "cart_left": 12.5, "first_word_ms": [900],
                               "duration_s": 60, "cost": 0.01, "caller": "x1", "refusals": ["days"]}),
            ("C", "no_order", {"turns": 2, "first_item_s": None, "first_word_ms": [3000], "duration_s": 30,
                               "cost": 0.01, "caller": "x2"}),
            ("D", "no_speech", {"turns": 0, "duration_s": 5, "cost": 0.0})]
    for cid, outcome, m in rows:
        store.start_call(cid, None, bid)
        store.set_call_metrics(cid, outcome, m)
    store.track("call_declined", bid, reason="lines_busy")
    store.track("menu_import_done", bid, ok=True, seconds=40)
    store.track("menu_import_failed", bid, ok=False, seconds=5)
    r = analytics.report(store, 7, bid, now=now + 1)
    calls = r["calls"]
    assert calls["total"] == 4 and calls["talked"] == 3 and calls["orders"] == 1 and calls["no_speech"] == 1
    assert calls["conversion_pct"] == 33.3 and calls["declined"] == {"lines_busy": 1}
    assert calls["drop_off"] == {"before_adding_anything": 1, "with_items_in_cart": 1}
    assert calls["repeat_callers"] == 1
    assert r["money"]["left_in_carts"] == 12.5
    assert r["problems"]["refusals"] == {"days": 1}
    assert r["problems"]["menu_reads"] == 1 and r["problems"]["menu_reads_failed"] == 1
    assert r["speed"]["first_word_p50_ms"] == 1050 and r["cost"]["per_order"] == 0.04
    # Another business's calls never show up in this one's report.
    other = seed_business(store, slug="taco-town", phone="+15025550111")
    store.start_call("Z", None, other)
    store.set_call_metrics("Z", "order", {"turns": 1})
    assert analytics.report(store, 7, bid, now=now + 1)["calls"]["total"] == 4


def test_setup_funnel_follows_businesses_through_the_steps():
    store = Store(":memory:")
    store.track("landing_view", visitor="v1")
    store.track("landing_view", visitor="v1")
    store.track("landing_view", visitor="v2")
    store.track("signin", user=1, new=True)
    bid = seed_business(store)  # published and live with a number
    store.track("test_chat_started", bid)
    funnel = {s["step"]: s["count"] for s in analytics.setup_funnel(store, time.time() - 3600)["steps"]}
    assert funnel["Visited the site"] == 2 and funnel["Signed in"] == 1
    assert funnel["Created a business"] == 1 and funnel["Published"] == 1 and funnel["Went live"] == 1
    assert funnel["First order"] == 0


def test_outside_tools_are_off_until_configured_and_mask_business_pages(app, monkeypatch):
    c, store, _ = app
    assert c.get("/web-analytics.js").text.startswith("/* web analytics off")
    assert "posthog" not in c.get("/manage").headers["content-security-policy"]
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    js = c.get("/web-analytics.js?page=manage").text
    assert '"phc_test"' in js and '"maskTextSelector": "*"' in js and "us-assets.i.posthog.com/static/array.js" in js
    assert "maskTextSelector" not in c.get("/web-analytics.js?page=landing").text
    csp = c.get("/manage").headers["content-security-policy"]
    assert "script-src https://us-assets.i.posthog.com" in csp and "connect-src 'self' https://us.i.posthog.com" in csp
    from phone_agent import web_analytics
    assert web_analytics.init_sentry() is False  # no SENTRY_DSN


class FakePostHog:
    def __init__(self):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None):
        self.events.append((event, distinct_id, properties or {}))


def test_posthog_ai_events_never_change_claude_requests_or_send_content(monkeypatch):
    """Regression: PostHog's Claude wrapper doesn't cover beta.messages.stream, so PostHog
    settings in our requests made every call fail. We report AI events ourselves instead."""
    import asyncio

    from phone_agent import web_analytics
    from phone_agent.agent import PhoneAgent
    from phone_agent.catalog import load_catalog

    from .conftest import FIXTURE_CATALOG

    ph = FakePostHog()
    monkeypatch.setattr(web_analytics, "_posthog", ph)
    monkeypatch.setattr(web_analytics, "_posthog_ready", True)
    usage = type("U", (), {"input_tokens": 1200, "output_tokens": 40, "cache_read_input_tokens": 900,
                           "cache_creation_input_tokens": 0})()
    reply = message(text_block("We have tiramisu tonight."))
    reply.usage, reply.model = usage, "claude-opus-5-5"
    fake = FakeClient([reply])
    agent = PhoneAgent(load_catalog(FIXTURE_CATALOG), client=fake, call_id="CA9", caller_phone="+15550001111")

    async def talk():
        return "".join([c async for c in agent.respond("my number is 555 000 1111, any desserts?")])

    assert "tiramisu" in asyncio.run(talk())
    assert not [k for k in fake.requests[0] if "posthog" in k]  # the Claude request is untouched
    gen = [(e, d, p) for e, d, p in ph.events if e == "$ai_generation"]
    assert len(gen) == 1
    _, distinct, props = gen[0]
    assert props["$ai_input_tokens"] == 1200 and props["$ai_output_tokens"] == 40
    assert props["$ai_model"] == "claude-opus-5-5" and props["$ai_session_id"] == "call-CA9"
    assert distinct == "system:phone_agent" and props["$process_person_profile"] is False
    blob = json.dumps(ph.events)
    assert "555" not in blob and "desserts" not in blob and "tiramisu" not in blob  # no content
