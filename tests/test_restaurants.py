import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ordering_agent import restaurants as admin
from ordering_agent import server
from ordering_agent.order import Order
from ordering_agent.payments import handle_stripe_event
from ordering_agent.restaurants import RestaurantDirectory
from ordering_agent.storage import OrderStore

from .conftest import BELLA_PHONE, FIXTURE_MENU, seed_restaurant
from .fakes import FakeClient, message, text_block, tool_block

TACO_PHONE = "+15550008888"


def taco_menu():
    menu = json.loads(FIXTURE_MENU.read_text())
    menu["restaurant"]["name"] = "Taco Town"
    return menu


def two_restaurants(store):
    return seed_restaurant(store), seed_restaurant(store, "taco-town", TACO_PHONE, taco_menu())


class FakeSms:
    def __init__(self):
        self.sent = []

    def send(self, to, body):
        self.sent.append((to, body))


def place(store, menu, name="Ana"):
    order = Order(menu=menu)
    order.add_item("tiramisu")
    order.set_customer(name)
    order.mark_placed()
    return store.save(order)


def test_directory_routes_by_dialed_number_and_caches_menus():
    store = OrderStore(":memory:")
    bella, taco = two_restaurants(store)
    d = RestaurantDirectory(store)
    assert d.for_number("(555) 000-9999").restaurant.get("name") == "Bella Cucina"
    assert d.for_number(TACO_PHONE).restaurant_id == taco
    assert d.for_number("+15550007777") is None and d.for_number(None) is None
    assert d.for_number(TACO_PHONE) is d.for_number(TACO_PHONE)  # parsed once

    new = taco_menu()
    new["restaurant"]["name"] = "Taco Town Express"
    store.set_menu(taco, new)
    assert d.for_number(TACO_PHONE).restaurant["name"] == "Taco Town Express"  # next call sees it

    store.update_restaurant(taco, active=False)
    assert d.for_number(TACO_PHONE) is None
    assert d.get(taco).slug == "taco-town"  # still found for old orders' webhooks
    assert d.get(taco, active_only=True) is None


def test_order_numbers_count_per_restaurant():
    store = OrderStore(":memory:")
    bella, taco = two_restaurants(store)
    d = RestaurantDirectory(store)
    assert place(store, d.get(bella)).number == 1
    assert place(store, d.get(bella)).number == 2
    first_taco = place(store, d.get(taco), "Lee")
    assert first_taco.number == 1 and first_taco.id == 3
    assert [o["order_number"] for o in store.recent(restaurant_id=taco)] == [1]
    assert [o["order_number"] for o in store.recent(restaurant_id=bella)] == [2, 1]
    assert len(store.recent()) == 3


def test_payment_webhook_texts_in_the_orders_restaurants_name():
    store = OrderStore(":memory:")
    _, taco = two_restaurants(store)
    d = RestaurantDirectory(store)
    order = Order(menu=d.get(taco))
    order.add_item("tiramisu")
    order.set_customer("Lee")
    order.customer_phone = "+15025550123"
    saved = store.save(order, payment_status="unpaid")
    store.set_payment_link(saved.id, "cs_1", "https://pay.example/1")
    sms = FakeSms()
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_1", "payment_status": "paid"}}}
    assert handle_stripe_event(event, store, d.get, sms) == "paid"
    assert sms.sent[0][1].startswith("Taco Town: payment received for order #1")


def test_old_database_is_migrated_and_adopted(tmp_path, capsys):
    path = tmp_path / "orders.db"
    conn = sqlite3.connect(path)  # the schema before restaurants existed
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,"
                 " call_id TEXT, customer_name TEXT, customer_phone TEXT, total REAL NOT NULL, payload TEXT NOT NULL)")
    conn.execute("INSERT INTO orders (created_at, customer_name, total, payload) VALUES (1, 'Old', 5,"
                 " '{\"customer_name\": \"Old\", \"lines\": []}')")
    conn.execute("CREATE TABLE calls (call_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,"
                 " caller_phone TEXT, order_number INTEGER)")
    conn.execute("INSERT INTO calls (call_id, started_at) VALUES ('CAold', 1)")
    conn.commit()
    conn.close()

    store = OrderStore(path)
    assert store.recent()[0]["order_number"] == 1 and store.recent()[0]["restaurant_id"] is None
    admin.main(["add", "bella-cucina", "--phone", "555 000 9999", "--menu", str(FIXTURE_MENU),
                "--adopt-existing"], store=store)
    assert "assigned 1 existing orders and 1 calls" in capsys.readouterr().out
    rid = store.restaurant_by_slug("bella-cucina").id
    assert store.recent(restaurant_id=rid)[0]["customer_name"] == "Old"
    assert store.recent_calls(restaurant_id=rid)[0]["call_id"] == "CAold"
    # the next order continues that restaurant's numbering
    assert place(store, RestaurantDirectory(store).get(rid)).number == 2


def test_admin_rejects_bad_input(tmp_path):
    store = OrderStore(":memory:")
    seed_restaurant(store)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"restaurant": {"name": "X"}, "items": [{"id": "a"}]}))
    with pytest.raises(SystemExit, match="menu not saved"):
        admin.main(["add", "x", "--menu", str(bad)], store=store)
    with pytest.raises(SystemExit, match="already belongs"):
        admin.main(["add", "x", "--menu", str(FIXTURE_MENU), "--phone", BELLA_PHONE], store=store)
    with pytest.raises(SystemExit, match="slug must be"):
        admin.main(["add", "Bad Slug", "--menu", str(FIXTURE_MENU)], store=store)
    with pytest.raises(SystemExit, match="already exists"):
        admin.main(["add", "bella-cucina", "--menu", str(FIXTURE_MENU)], store=store)
    assert len(store.restaurants()) == 1


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDERS_DB", str(tmp_path / "orders.db"))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "ORDERS_API_TOKEN", "tok")
    monkeypatch.setattr(server, "WORDS_PER_SECOND", 1e9)
    monkeypatch.setattr(server, "NOT_SET_UP_SECONDS", 0)
    fake = FakeClient([])
    server.app.state.client = fake
    server.app.state.payments = None
    with TestClient(server.app) as c:
        ids = two_restaurants(server.app.state.store)
        yield c, fake, ids
    del server.app.state.client
    del server.app.state.payments


def test_voice_greets_as_the_dialed_restaurant(app):
    c, _, (bella, taco) = app
    r = c.post("/voice", data={"To": TACO_PHONE})
    assert "Thanks for calling Taco Town" in r.text and f"/ws?restaurant={taco}" in r.text
    assert "Thanks for calling Bella Cucina" in c.post("/voice", data={"To": BELLA_PHONE}).text
    r = c.post("/voice", data={"To": "+15550007777"})
    assert "<Hangup/>" in r.text and "ConversationRelay" not in r.text


def test_call_order_and_transcript_belong_to_the_restaurant(app):
    c, fake, (bella, taco) = app
    fake.script = [
        message(tool_block("add_item", {"item_id": "tiramisu"}, id="t1"),
                tool_block("set_customer_name", {"name": "Lee"}, id="t2"),
                tool_block("place_order", {"caller_confirmed": True}, id="t3"), stop_reason="tool_use"),
        message(text_block("Order number 1. Bye!"), tool_block("end_call", {}, id="t4"), stop_reason="tool_use"),
    ]
    with c.websocket_connect(f"/ws?restaurant={taco}") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CAt", "from": "+15550001111", "to": TACO_PHONE}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "one tiramisu for Lee, place it", "last": True}))
        while json.loads(ws.receive_text())["type"] != "end":
            pass

    auth = {"Authorization": "Bearer tok"}
    assert c.get(f"/orders?restaurant={bella}", headers=auth).json() == []
    taco_orders = c.get(f"/orders?restaurant={taco}", headers=auth).json()
    assert [(o["order_number"], o["customer_name"], o["restaurant_id"]) for o in taco_orders] == [(1, "Lee", taco)]
    assert c.get(f"/calls?restaurant={bella}", headers=auth).json() == []
    assert c.get(f"/calls?restaurant={taco}", headers=auth).json()[0]["call_id"] == "CAt"
    listing = c.get("/restaurants", headers=auth).json()
    assert [(r["slug"], r["name"]) for r in listing] == [("bella-cucina", "Bella Cucina"), ("taco-town", "Taco Town")]
    assert c.get("/restaurants").status_code == 401


def test_call_for_unknown_restaurant_is_ended(app):
    c, _, _ = app
    with c.websocket_connect("/ws?restaurant=99") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CAx", "to": "+15550007777"}))
        assert "isn't taking orders" in json.loads(ws.receive_text())["token"]
        assert json.loads(ws.receive_text())["type"] == "end"
