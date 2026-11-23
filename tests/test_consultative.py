"""Consultative selling: the event & wedding supplies template, guides, custom requests
(inquiries) and how far ahead pickups can be scheduled."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from phone_agent.catalog import parse_catalog
from phone_agent.catalog_check import check_catalog
from phone_agent.modules.guides import find
from phone_agent.order import Order
from phone_agent.prompts import build_greeting, build_system_prompt
from phone_agent.settings_schema import validate_settings
from phone_agent.storage import Store
from phone_agent.templates import get_template
from phone_agent.tools import CallSession, run_tool, start_session, tools_for

from .conftest import FIXTURE_CATALOG, seed_business

STARTER = json.loads((Path(__file__).resolve().parent.parent / "data" / "templates" / "events.json").read_text())
MONDAY = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # 9am in Chicago


def events(**settings):
    return parse_catalog({**STARTER, "settings": {**STARTER["settings"], **settings}})


class FakeSms:
    def __init__(self):
        self.sent, self.done = [], threading.Event()

    def send(self, to, body):
        self.sent.append((to, body))
        self.done.set()


# ---- the template -------------------------------------------------------------------------


def test_events_template_starter_and_prompt():
    assert check_catalog(STARTER)["errors"] == []
    catalog = events()
    assert catalog.template.status == "available"
    prompt = build_system_prompt(catalog, payment_links=True)
    assert "supplies items for weddings and other events" in prompt
    assert "ask the event date and which ceremonies" in prompt
    # Guides: titles only, the text is read with the tool.
    assert "- What's needed for a Telugu wedding" in prompt and "Jeelakarra Bellam" not in prompt
    assert "call record_inquiry" in prompt and "custom designs, full setups, rentals" in prompt
    assert build_greeting(catalog).endswith("Are you shopping for a wedding or another event?")
    names = {t["name"] for t in tools_for(catalog.template.modules)}
    assert {"add_item", "read_guide", "record_inquiry", "end_call"} <= names


def test_no_guides_means_no_guides_section():
    prompt = build_system_prompt(events(guides=[]))
    assert "Guides:" not in prompt and "\n\n\n" not in prompt


def test_pickups_can_be_scheduled_as_far_ahead_as_the_business_allows():
    far = "2027-04-01"  # about four months out
    o = Order(catalog=events(), clock=lambda: MONDAY)
    o.set_pickup(far, "11:00")
    assert o.pickup_at.date().isoformat() == far
    retail = parse_catalog(json.loads(FIXTURE_CATALOG.read_text()))  # no setting: 30 days
    o = Order(catalog=retail, clock=lambda: MONDAY)
    try:
        o.set_pickup(far, "11:00")
        raise AssertionError("should be refused")
    except ValueError as e:
        assert "up to 30 days ahead" in str(e)
    assert events(max_advance_days=5000).max_advance_days == 365


def test_long_notice_items_are_refused_too_soon():
    o = Order(catalog=events(), clock=lambda: MONDAY)
    try:
        o.add_item("imported_garland_pair", options={"length": "4 feet"})  # pickup as soon as possible
        raise AssertionError("should be refused")
    except ValueError as e:
        assert "21 days ahead" in str(e) and "Dec 14" in str(e)
    o.set_pickup("2026-12-20", "11:00")
    o.add_item("imported_garland_pair", options={"length": "4 feet"})
    try:
        o.set_pickup("2026-12-01", "11:00")
        raise AssertionError("should be refused")
    except ValueError as e:
        assert "21 days ahead" in str(e)


# ---- guides -------------------------------------------------------------------------------


def test_read_guide_finds_by_title_and_lists_the_rest():
    session = start_session(CallSession(catalog=events()))
    text, err = run_tool(session, "read_guide", {"title": "what's needed for a telugu wedding"})
    assert not err and "Kanyadanam set" in json.loads(text)["text"]
    text, err = run_tool(session, "read_guide", {"title": "garlands"})
    assert not err and json.loads(text)["title"] == "Choosing garlands"
    text, err = run_tool(session, "read_guide", {"title": "parking"})
    assert err and "Choosing garlands" in text
    session = start_session(CallSession(catalog=events(guides=[])))
    text, err = run_tool(session, "read_guide", {"title": "anything"})
    assert err and "no guides" in text


def test_find_prefers_the_clearest_match():
    guides = [{"title": "Garland sizes", "text": "a"}, {"title": "Garland care", "text": "b"}]
    assert find(guides, "garland") is None  # ambiguous
    assert find(guides, "how big are garland sizes")["text"] == "a"


def test_guide_settings_are_validated():
    t = get_template("events")
    base = {"name": "X", "pickup_minutes": 20, "tax_rate": 0.08}
    assert validate_settings({**base, "guides": [{"title": "A", "text": "B"}]}, t) == []
    assert any("Guides" in e for e in validate_settings({**base, "guides": "nope"}, t))
    assert any("too long" in e for e in validate_settings({**base, "guides": [{"title": "A", "text": "x" * 7000}]}, t))
    assert any("Text new requests to" in e for e in validate_settings({**base, "notify_phone": "12"}, t))
    assert validate_settings({**base, "notify_phone": "214 555 0123"}, t) == []


# ---- custom requests ----------------------------------------------------------------------


def request_args(**kw):
    return {"summary": "Custom jasmine and rose garlands, 2 pairs", "details": "Red and white, 5 feet",
            "event_date": "2027-03-14", "budget": "about $400", "name": "Lakshmi", "caller_confirmed": True, **kw}


def test_record_inquiry_saves_numbers_and_texts_staff():
    store = Store(":memory:")
    raw = {**STARTER, "settings": {**STARTER["settings"], "notify_phone": "(214) 555-0123"}}
    rid = seed_business(store, "manu", "+15550001234", raw)
    from phone_agent.businesses import BusinessDirectory

    catalog = BusinessDirectory(store).get(rid)
    sms = FakeSms()
    session = start_session(CallSession(catalog=catalog, store=store, call_id="CA9",
                                        caller_phone="+14695550199", sms=sms))
    text, err = run_tool(session, "record_inquiry", request_args(caller_confirmed=False))
    assert err and "confirmation" in text
    text, err = run_tool(session, "record_inquiry", request_args())
    assert not err and json.loads(text)["request_number"] == 1
    saved = store.inquiries(rid)[0]
    assert saved["customer_phone"] == "+14695550199" and saved["event_date"] == "2027-03-14"
    assert saved["status"] == "new" and saved["budget"] == "about $400"
    assert sms.done.wait(2)
    to, body = sms.sent[0]
    assert to == "+12145550123" and "New request #1 for 2027-03-14 from Lakshmi" in body
    _, err = run_tool(session, "record_inquiry", request_args(phone="972 555 0100"))
    assert not err and store.inquiries(rid)[0]["number"] == 2
    assert store.inquiries(rid)[0]["customer_phone"] == "+19725550100"


def test_record_inquiry_checks_its_input():
    session = start_session(CallSession(catalog=events()))  # no caller ID, no store
    text, err = run_tool(session, "record_inquiry", request_args())
    assert err and "phone number" in text
    session = start_session(CallSession(catalog=events(), caller_phone="+14695550199"))
    text, err = run_tool(session, "record_inquiry", request_args(event_date="March 14"))
    assert err and "YYYY-MM-DD" in text
    text, err = run_tool(session, "record_inquiry", request_args(event_date="2020-01-01"))
    assert err and "already passed" in text
    text, err = run_tool(session, "record_inquiry", request_args(event_date=""))
    assert not err and json.loads(text)["recorded"] is True  # works without a store (terminal chat)


def test_requests_in_the_manager_are_per_business(tmp_path, monkeypatch):
    from .test_onboarding import W, new_business, sign_in
    from .test_onboarding import app as onboarding_app

    gen = onboarding_app.__wrapped__(tmp_path, monkeypatch)
    c, store, _, _ = next(gen)
    try:
        sign_in(c, store)
        mine = new_business(c, "Manu Wedding", template="events")["id"]
        sign_in(c, store, "other@example.com")
        theirs = new_business(c, "Other Shop")["id"]
        store.save_inquiry(mine, call_id="CA1", summary="Garlands", customer_name="A")
        store.save_inquiry(theirs, call_id="CA2", summary="Cake", customer_name="B")
        assert [q["summary"] for q in c.get(f"/api/manage/businesses/{theirs}/inquiries").json()] == ["Cake"]
        assert c.get(f"/api/manage/businesses/{mine}/inquiries").status_code in (403, 404)
        assert c.post(f"/api/manage/businesses/{mine}/inquiries/1", json={"status": "closed"},
                      headers=W).status_code in (403, 404)
        sign_in(c, store)
        r = c.post(f"/api/manage/businesses/{mine}/inquiries/1", json={"status": "contacted"}, headers=W)
        assert r.status_code == 200
        assert c.post(f"/api/manage/businesses/{mine}/inquiries/1", json={"status": "lost"}, headers=W).status_code == 400
        assert c.post(f"/api/manage/businesses/{mine}/inquiries/9", json={"status": "closed"}, headers=W).status_code == 404
        assert store.inquiries(mine)[0]["status"] == "contacted" and store.inquiries(theirs)[0]["status"] == "new"
    finally:
        next(gen, None)


def test_menu_reader_drafts_guides(tmp_path, monkeypatch):
    from .fakes import message, text_block
    from .test_onboarding import EXTRACTED, new_business, sign_in, start_import, wait_import
    from .test_onboarding import app as onboarding_app

    gen = onboarding_app.__wrapped__(tmp_path, monkeypatch)
    c, store, fake, _ = next(gen)
    try:
        sign_in(c, store)
        b = new_business(c, "Manu Wedding", template="events")
        extracted = {**EXTRACTED, "guides": [{"title": "Fresh garland guide", "text": "Keep them cool."},
                                             {"title": "", "text": "no title: dropped"}]}
        fake.script = [message(text_block(json.dumps(extracted)))]
        out = wait_import(c, b["id"], start_import(c, b["id"], b["draft"]["revision"],
                                                   files=[("files", ("p.jpg", b"\xff\xd8x", "image/jpeg"))]))
        assert out["state"] == "done", out
        assert out["catalog"]["settings"]["guides"] == [
            {"title": "Fresh garland guide", "text": "Keep them cool.", "from_menu": True}]
        assert "guides" in fake.requests[-1]["output_config"]["format"]["schema"]["required"]
    finally:
        next(gen, None)
