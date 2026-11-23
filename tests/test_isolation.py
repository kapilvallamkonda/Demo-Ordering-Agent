"""Businesses can't affect each other: data, calls, failures and background work."""

import json
import threading
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from phone_agent import server
from phone_agent.businesses import BusinessDirectory
from phone_agent.catalog import parse_catalog
from phone_agent.modules import BUILT, MODULES
from phone_agent.order import Order
from phone_agent.pos import Connection, PosHub
from phone_agent.session import CallSession
from phone_agent.storage import Store
from phone_agent.templates import TEMPLATES, Template
from phone_agent.tools import run_tool, start_session

from .conftest import BELLA_PHONE, FIXTURE_CATALOG, seed_business
from .fakes import FakeClient, message, text_block

TACO_PHONE = "+15550008888"


def fixture(name):
    raw = json.loads(FIXTURE_CATALOG.read_text())
    raw["settings"]["name"] = name
    return raw


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "ADMIN_API_TOKEN", "tok")
    monkeypatch.setattr(server, "WORDS_PER_SECOND", 1e9)
    monkeypatch.setattr(server, "NOT_SET_UP_SECONDS", 0)
    server.app.state.client = FakeClient([])
    server.app.state.payments = None
    with TestClient(server.app) as c:
        store = server.app.state.store
        bella = seed_business(store, catalog=fixture("Bella Cucina"))
        taco = seed_business(store, "taco-town", TACO_PHONE, fixture("Taco Town"))
        yield c, store, bella, taco
    del server.app.state.client, server.app.state.payments


def setup_msg(call_sid, to):
    return json.dumps({"type": "setup", "callSid": call_sid, "from": "+15550001111", "to": to})


def first_reply(ws) -> dict:
    return json.loads(ws.receive_text())


# ---- a broken business doesn't take others down -------------------------------------------


def test_broken_catalog_only_takes_its_own_business_offline(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    seed_business(store, catalog=fixture("Bella Cucina"))
    # A catalog that can't be loaded any more (e.g. its template was removed from the code).
    store.add_business("ghost", "+15550006666", {**fixture("Ghost"), "template": "spaceship"})
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "ADMIN_API_TOKEN", "tok")
    monkeypatch.setenv("ADMIN_API_TOKEN", "tok")  # the manager reads it from the environment
    server.app.state.client = FakeClient([])
    server.app.state.payments = None
    try:
        with TestClient(server.app) as c:  # starts despite the broken catalog
            assert "Thanks for calling Bella Cucina" in c.post("/voice", data={"To": BELLA_PHONE}).text
            ghost = c.post("/voice", data={"To": "+15550006666"}).text
            assert "isn't taking orders" in ghost and "ConversationRelay" not in ghost
            listing = {r["slug"]: r for r in c.get("/businesses", headers={"Authorization": "Bearer tok"}).json()}
            assert listing["bella-cucina"]["status"] == "ok"
            assert listing["ghost"]["status"] == "broken" and "unknown template" in listing["ghost"]["problem"]
            me = c.get("/api/manage/me", headers={"Authorization": "Bearer tok"})
            assert me.status_code == 200 and len(me.json()["businesses"]) == 2
    finally:
        del server.app.state.client, server.app.state.payments


def test_old_catalog_versions_are_looked_up_within_the_business(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    a = seed_business(store, catalog=fixture("A"))
    b = seed_business(store, "b", TACO_PHONE, fixture("B"))
    b_version = store.business(b).catalog_id
    directory = BusinessDirectory(store)
    # An order of business A naming B's catalog version must not get B's catalog.
    assert directory.catalog_version(a, b_version).settings["name"] == "A"


# ---- a call is bound to the business it was made to ----------------------------------------


def test_websocket_needs_the_token_from_voice(app, monkeypatch):
    c, store, bella, taco = app
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "test-auth-token")
    # No token: someone opening the WebSocket directly can't start a call.
    with c.websocket_connect(f"/ws?business={taco}") as ws:
        ws.send_text(setup_msg("CA1", TACO_PHONE))
        assert "isn't taking orders" in first_reply(ws)["token"]
    # A token issued for another business, or another call, doesn't work either.
    for business, call in ((bella, "CA2"), (taco, "CAother")):
        token = server.call_token(business, call)
        with c.websocket_connect(f"/ws?business={taco}&token={token}") as ws:
            ws.send_text(setup_msg("CA2", TACO_PHONE))
            assert "isn't taking orders" in first_reply(ws)["token"]
    # Expired.
    old = server.call_token(taco, "CA3", now=time.time() - 3600)
    assert not server.check_call_token(old, taco, "CA3")
    # The real one.
    token = server.call_token(taco, "CA4")
    assert server.check_call_token(token, taco, "CA4")
    server.app.state.client.script = [message(text_block("Hi from Taco Town."))]
    with c.websocket_connect(f"/ws?business={taco}&token={token}") as ws:
        ws.send_text(setup_msg("CA4", TACO_PHONE))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hello", "last": True}))
        assert first_reply(ws)["token"] == "Hi from Taco Town."


def test_voice_puts_a_call_token_in_the_websocket_url(app, monkeypatch):
    c, store, bella, taco = app
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setattr(server.RequestValidator, "validate", lambda *a: True)
    twiml = c.post("/voice", data={"To": TACO_PHONE, "CallSid": "CA9"}).text
    url = twiml.split('url="')[1].split('"')[0]
    token = url.split("token=")[1]
    assert f"business={taco}&amp;token=" in twiml or f"business={taco}&token=" in url
    assert server.check_call_token(token, taco, "CA9")


def test_dialed_number_must_match_the_business(app):
    c, store, bella, taco = app
    with c.websocket_connect(f"/ws?business={taco}") as ws:
        ws.send_text(setup_msg("CA5", BELLA_PHONE))  # URL says Taco Town, Bella was dialed
        assert "isn't taking orders" in first_reply(ws)["token"]
    assert store.recent_calls() == []  # no call was recorded for either


# ---- one busy business can't use up everyone's capacity -------------------------------------


def test_call_limit_is_per_business(app, monkeypatch):
    c, store, bella, taco = app
    monkeypatch.setattr(server, "MAX_CALLS_PER_BUSINESS", 1)
    server.app.state.client.script = [message(text_block("Hi."))]
    with c.websocket_connect(f"/ws?business={taco}") as ws:
        ws.send_text(setup_msg("CA6", TACO_PHONE))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hi", "last": True}))
        first_reply(ws)
        assert server.app.state.active_calls[taco] == 1
        busy = c.post("/voice", data={"To": TACO_PHONE}).text
        assert "lines are busy" in busy and "ConversationRelay" not in busy
        assert "Thanks for calling Bella Cucina" in c.post("/voice", data={"To": BELLA_PHONE}).text
        with c.websocket_connect(f"/ws?business={taco}") as ws2:  # a second call that skipped /voice
            ws2.send_text(setup_msg("CA7", TACO_PHONE))
            assert "lines are busy" in first_reply(ws2)["token"]
    assert server.app.state.active_calls[taco] == 0  # freed when the call ended


# ---- POS: a slow or failing POS only delays its own business ---------------------------------


class SlowForOne:
    """A fake POS provider that hangs for one business until released."""

    name = "square"

    def __init__(self, slow_business):
        self.slow_business = slow_business
        self.release = threading.Event()
        self.sent = []
        self.fail_once = set()

    def setup(self, conn):
        return "LOC", {}

    def push(self, conn, order, existing_id):
        if conn.business_id == self.slow_business:
            self.release.wait(5)
        if order.order_id in self.fail_once:
            self.fail_once.discard(order.order_id)
            from phone_agent.pos import PosError
            raise PosError("temporarily down")
        self.sent.append((conn.business_id, order.order_id))
        return f"POS-{order.order_id}"

    def mark_paid(self, *a):
        pass


def place(store, directory, business_id):
    session = start_session(CallSession(catalog=directory.get(business_id), store=store))
    run_tool(session, "add_item", {"item_id": "tiramisu"})
    run_tool(session, "set_customer_name", {"name": "Ana"})
    return session.order


def wait_for(predicate, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_slow_pos_doesnt_hold_up_other_businesses(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "db.sqlite")
    slow = seed_business(store, catalog=fixture("Slow"))
    fast = seed_business(store, "fast", TACO_PHONE, fixture("Fast"))
    directory = BusinessDirectory(store)
    provider = SlowForOne(slow)
    hub = PosHub(store, directory, {"square": provider}, retry_delays=(0.05,), workers=2, per_business=1)
    for b in (slow, fast):
        hub.save_connection(b, "square", "sandbox", {"access_token": "t"}, "M")

    def placed(business_id):
        order = place(store, directory, business_id)
        saved = store.save(order)
        hub.order_placed(saved.id, business_id)
        return saved.id

    slow_ids = [placed(slow) for _ in range(3)]  # more jobs than workers, all stuck on a hung POS
    fast_id = placed(fast)
    assert wait_for(lambda: (fast, fast_id) in provider.sent), "the other business's order was held up"
    assert not any(b == slow for b, _ in provider.sent)
    provider.release.set()
    assert wait_for(lambda: sum(b == slow for b, _ in provider.sent) == 3, seconds=8)
    assert all(store.order_for_pos(i)["pos_status"] == "sent" for i in slow_ids + [fast_id])


def test_pos_retry_waits_on_a_timer_not_a_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "db.sqlite")
    b = seed_business(store, catalog=fixture("B"))
    directory = BusinessDirectory(store)
    provider = SlowForOne(slow_business=None)
    hub = PosHub(store, directory, {"square": provider}, retry_delays=(0.1,), workers=1)
    hub.save_connection(b, "square", "sandbox", {"access_token": "t"}, "M")
    first = store.save(place(store, directory, b))
    provider.fail_once.add(first.id)
    hub.order_placed(first.id, b)
    second = store.save(place(store, directory, b))
    hub.order_placed(second.id, b)
    # The one worker isn't blocked by the first order's retry delay.
    assert wait_for(lambda: (b, second.id) in provider.sent, seconds=1)
    assert wait_for(lambda: store.order_for_pos(first.id)["pos_status"] == "sent")


# ---- modules ---------------------------------------------------------------------------------


def test_module_registry():
    assert "orders" in BUILT and "bookings" not in BUILT
    assert MODULES["orders"].tool_names >= {"add_item", "place_order"}
    assert TEMPLATES["salon"].status == "coming_soon"


def test_tools_of_modules_a_template_doesnt_use_are_refused(monkeypatch):
    info = Template(id="info", label="Info line", description="", modules=(), words=TEMPLATES["retail"].words,
                    role="You answer questions for {name}.")
    monkeypatch.setitem(TEMPLATES, "info", info)
    catalog = parse_catalog({**fixture("Info"), "template": "info"})
    session = start_session(CallSession(catalog=catalog))
    assert session.order is None  # the orders module didn't start
    text, err = run_tool(session, "add_item", {"item_id": "tiramisu"})
    assert err and "Unknown tool" in text
    assert run_tool(session, "end_call", {}) == ('{"ok": true}', False)


def test_each_call_has_its_own_order():
    catalog = parse_catalog(fixture("Bella"))
    a = start_session(CallSession(catalog=catalog, caller_phone="+15025550123"))
    b = start_session(CallSession(catalog=catalog))
    run_tool(a, "add_item", {"item_id": "tiramisu"})
    assert len(a.order.lines) == 1 and b.order.lines == []
    assert a.order.customer_phone == "+15025550123" and b.order.customer_phone == ""
    assert isinstance(a.order, Order) and a.order is not b.order


def test_import_jobs_are_per_business(tmp_path, monkeypatch):
    """Menu readings run as in-memory jobs shared by the server: an owner can't see
    another business's reading, even with its job id."""
    import json

    from .fakes import message, text_block
    from .test_onboarding import EXTRACTED, new_business, sign_in, start_import, wait_import
    from .test_onboarding import app as onboarding_app

    gen = onboarding_app.__wrapped__(tmp_path, monkeypatch)
    c, store, fake, _ = next(gen)
    try:
        sign_in(c, store, "ana@example.com")
        a = new_business(c, "Ana's")
        fake.script = [message(text_block(json.dumps(EXTRACTED)))]
        r = start_import(c, a["id"], a["draft"]["revision"], files=[("files", ("m.jpg", b"jpg", "image/jpeg"))])
        job = r.json()["job"]
        assert wait_import(c, a["id"], r)["state"] == "done"
        sign_in(c, store, "bo@example.com")
        b = new_business(c, "Bo's")
        assert c.get(f"/api/manage/businesses/{b['id']}/import/{job}").status_code == 404
        assert c.get(f"/api/manage/businesses/{b['id']}/import").json() == {"job": None}
        assert c.get(f"/api/manage/businesses/{a['id']}/import/{job}").status_code == 404  # not their business
    finally:
        next(gen, None)


def test_a_square_outage_only_delays_that_business_payment_check(monkeypatch):
    """Watching payment links shares the POS job pool: an unreachable Square for one
    business makes its check wait and retry, never raise into the pool or cancel early."""
    from cryptography.fernet import Fernet

    from .test_pos import square_link_setup

    monkeypatch.setenv("POS_SECRET_KEY", Fernet.generate_key().decode())
    order_state = {"order": {"id": "SQ-LINK-ORDER", "state": "DRAFT", "net_amount_due_money": {"amount": 1}}}
    store, hub, api, texts, session, _ = square_link_setup(order_state)
    api.routes[("GET", "/v2/orders/SQ-LINK-ORDER")] = (503, {"errors": [{"code": "SERVICE_UNAVAILABLE"}]})
    created = store.order_for_pos(session.order_id)["created_at"]
    assert hub.check_payment(session.order_id, now=created + 60) == "waiting"
    assert hub.check_payment(session.order_id, now=created + 31 * 60) == "waiting"  # past the window, still trying
    assert store.order_for_pos(session.order_id)["status"] == "awaiting_payment"
    # Long after, it gives up locally so the order doesn't hang forever.
    assert hub.check_payment(session.order_id, now=created + 31 * 60 + 3601) == "expired"
    assert store.order_for_pos(session.order_id)["status"] == "cancelled"


def test_inquiries_are_kept_per_business():
    """Custom requests (modules/inquiries.py) are numbered, listed and updated per business."""
    from phone_agent.storage import Store

    store = Store(":memory:")
    a, b = seed_business(store), seed_business(store, "taco-town", "+15550008888")
    assert store.save_inquiry(a, call_id="CA1", summary="catering for 80") == 1
    assert store.save_inquiry(b, call_id="CA2", summary="custom cake") == 1  # own numbering
    assert [q["summary"] for q in store.inquiries(b)] == ["custom cake"]
    assert store.set_inquiry_status(b, 1, "closed")
    assert store.inquiries(a)[0]["status"] == "new"
    assert not store.set_inquiry_status(a, 2, "closed")
