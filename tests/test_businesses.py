import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from phone_agent import businesses as admin
from phone_agent import server
from phone_agent.order import Order
from phone_agent.payments import handle_stripe_event
from phone_agent.businesses import BusinessDirectory
from phone_agent.storage import Store

from .conftest import BELLA_PHONE, FIXTURE_CATALOG, seed_business
from .fakes import FakeClient, message, text_block, tool_block

TACO_PHONE = "+15550008888"


def taco_catalog():
    catalog = json.loads(FIXTURE_CATALOG.read_text())
    catalog["settings"]["name"] = "Taco Town"
    return catalog


def two_businesses(store):
    return seed_business(store), seed_business(store, "taco-town", TACO_PHONE, taco_catalog())


class FakeSms:
    def __init__(self):
        self.sent = []

    def send(self, to, body):
        self.sent.append((to, body))


def place(store, catalog, name="Ana"):
    order = Order(catalog=catalog)
    order.add_item("tiramisu")
    order.set_customer(name)
    order.mark_placed()
    return store.save(order)


def test_directory_routes_by_dialed_number_and_caches_catalogs():
    store = Store(":memory:")
    bella, taco = two_businesses(store)
    d = BusinessDirectory(store)
    assert d.for_number("(555) 000-9999").settings.get("name") == "Bella Cucina"
    assert d.for_number(TACO_PHONE).business_id == taco
    assert d.for_number("+15550007777") is None and d.for_number(None) is None
    assert d.for_number(TACO_PHONE) is d.for_number(TACO_PHONE)  # parsed once

    new = taco_catalog()
    new["settings"]["name"] = "Taco Town Express"
    store.set_catalog(taco, new)
    assert d.for_number(TACO_PHONE).settings["name"] == "Taco Town Express"  # next call sees it

    store.update_business(taco, active=False)
    assert d.for_number(TACO_PHONE) is None
    assert d.get(taco).slug == "taco-town"  # still found for old orders' webhooks
    assert d.get(taco, active_only=True) is None


def test_order_numbers_count_per_business():
    store = Store(":memory:")
    bella, taco = two_businesses(store)
    d = BusinessDirectory(store)
    assert place(store, d.get(bella)).number == 1
    assert place(store, d.get(bella)).number == 2
    first_taco = place(store, d.get(taco), "Lee")
    assert first_taco.number == 1 and first_taco.id == 3
    assert [o["order_number"] for o in store.recent(business_id=taco)] == [1]
    assert [o["order_number"] for o in store.recent(business_id=bella)] == [2, 1]
    assert len(store.recent()) == 3


def test_payment_webhook_texts_in_the_orders_businesses_name():
    store = Store(":memory:")
    _, taco = two_businesses(store)
    d = BusinessDirectory(store)
    order = Order(catalog=d.get(taco))
    order.add_item("tiramisu")
    order.set_customer("Lee")
    order.customer_phone = "+15025550123"
    saved = store.save(order, payment_status="unpaid")
    store.set_payment_link(saved.id, "cs_1", "https://pay.example/1")
    sms = FakeSms()
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_1", "payment_status": "paid"}}}
    assert handle_stripe_event(event, store, d.get, sms) == "paid"
    assert sms.sent[0][1].startswith("Taco Town: payment received for order #1")


def test_admin_rejects_bad_input(tmp_path):
    store = Store(":memory:")
    seed_business(store)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"settings": {"name": "X"}, "items": [{"id": "a"}]}))
    with pytest.raises(SystemExit, match="catalog not saved"):
        admin.main(["add", "x", "--catalog", str(bad)], store=store)
    with pytest.raises(SystemExit, match="already belongs"):
        admin.main(["add", "x", "--catalog", str(FIXTURE_CATALOG), "--phone", BELLA_PHONE], store=store)
    with pytest.raises(SystemExit, match="slug must be"):
        admin.main(["add", "Bad Slug", "--catalog", str(FIXTURE_CATALOG)], store=store)
    with pytest.raises(SystemExit, match="already exists"):
        admin.main(["add", "bella-cucina", "--catalog", str(FIXTURE_CATALOG)], store=store)
    assert len(store.businesses()) == 1


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "orders.db"))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "ADMIN_API_TOKEN", "tok")
    monkeypatch.setattr(server, "WORDS_PER_SECOND", 1e9)
    monkeypatch.setattr(server, "NOT_SET_UP_SECONDS", 0)
    fake = FakeClient([])
    server.app.state.client = fake
    server.app.state.payments = None
    with TestClient(server.app) as c:
        ids = two_businesses(server.app.state.store)
        yield c, fake, ids
    del server.app.state.client
    del server.app.state.payments


def test_voice_greets_as_the_dialed_business(app):
    c, _, (bella, taco) = app
    r = c.post("/voice", data={"To": TACO_PHONE})
    assert "Thanks for calling Taco Town" in r.text and f"/ws?business={taco}" in r.text
    assert "Thanks for calling Bella Cucina" in c.post("/voice", data={"To": BELLA_PHONE}).text
    r = c.post("/voice", data={"To": "+15550007777"})
    assert "<Hangup/>" in r.text and "ConversationRelay" not in r.text


def test_call_order_and_transcript_belong_to_the_business(app):
    c, fake, (bella, taco) = app
    fake.script = [
        message(tool_block("add_item", {"item_id": "tiramisu"}, id="t1"),
                tool_block("set_customer_name", {"name": "Lee"}, id="t2"),
                tool_block("place_order", {"caller_confirmed": True}, id="t3"), stop_reason="tool_use"),
        message(text_block("Order number 1. Bye!"), tool_block("end_call", {}, id="t4"), stop_reason="tool_use"),
    ]
    with c.websocket_connect(f"/ws?business={taco}") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CAt", "from": "+15550001111", "to": TACO_PHONE}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "one tiramisu for Lee, place it", "last": True}))
        while json.loads(ws.receive_text())["type"] != "end":
            pass

    auth = {"Authorization": "Bearer tok"}
    assert c.get(f"/orders?business={bella}", headers=auth).json() == []
    taco_orders = c.get(f"/orders?business={taco}", headers=auth).json()
    assert [(o["order_number"], o["customer_name"], o["business_id"]) for o in taco_orders] == [(1, "Lee", taco)]
    assert c.get(f"/calls?business={bella}", headers=auth).json() == []
    assert c.get(f"/calls?business={taco}", headers=auth).json()[0]["call_id"] == "CAt"
    listing = c.get("/businesses", headers=auth).json()
    assert [(r["slug"], r["name"]) for r in listing] == [("bella-cucina", "Bella Cucina"), ("taco-town", "Taco Town")]
    assert c.get("/businesses").status_code == 401


def test_call_for_unknown_business_is_ended(app):
    c, _, _ = app
    with c.websocket_connect("/ws?business=99") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CAx", "to": "+15550007777"}))
        assert "isn't taking orders" in json.loads(ws.receive_text())["token"]
        assert json.loads(ws.receive_text())["type"] == "end"
