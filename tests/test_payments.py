import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ordering_agent import server
from ordering_agent.menu import load_menu
from ordering_agent.order import Order, OrderError, normalize_phone
from ordering_agent.payments import PaymentLink, PaymentService, handle_stripe_event, order_text
from ordering_agent.storage import OrderStore
from ordering_agent.tools import CallSession, run_tool

from .conftest import FIXTURE_MENU, seed_restaurant
from .fakes import FakeClient


class FakeLinks:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def create(self, order, order_number, expires_minutes):
        if self.fail:
            raise RuntimeError("stripe down")
        self.calls.append((order_number, order.total, expires_minutes))
        return PaymentLink(id=f"cs_test_{order_number}", url=f"https://pay.example/{order_number}")


class FakeSms:
    def __init__(self):
        self.sent = []

    def send(self, to, body):
        self.sent.append((to, body))


def menu_with(required: bool):
    menu = load_menu(FIXTURE_MENU)
    restaurant = {**menu.restaurant, "payment": {"required_before_cooking": required,
                                                 "pay_within_minutes": 10}}
    return replace(menu, restaurant=restaurant)


def placed_session(menu, payments, phone="+15025550123"):
    store = OrderStore(":memory:")
    session = CallSession(order=Order(menu=menu), store=store, call_id="CA1", payments=payments)
    if phone:
        session.order.customer_phone = phone
    run_tool(session, "add_item", {"item_id": "wings", "options": {"sauce": "bbq"}})
    run_tool(session, "set_customer_name", {"name": "Ana"})
    text, err = run_tool(session, "place_order", {"caller_confirmed": True})
    return session, store, json.loads(text) if not err else text, err


@pytest.mark.parametrize("raw,expected", [
    ("(502) 555-0123", "+15025550123"),
    ("1 502 555 0123", "+15025550123"),
    ("+44 20 7946 0958", "+442079460958"),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["555-0123", "anonymous", ""])
def test_normalize_phone_rejects_partial(raw):
    with pytest.raises(OrderError):
        normalize_phone(raw)


def test_optional_payment_texts_link_and_confirms_order():
    links, sms = FakeLinks(), FakeSms()
    session, store, result, err = placed_session(menu_with(False), PaymentService(links, sms))
    assert not err
    assert result["payment"]["link_sent"] is True
    assert result["payment"]["texted_to_number_ending"] == "0123"
    assert links.calls == [(1, session.order.total, 30)]  # Stripe minimum is 30 minutes
    to, body = sms.sent[0]
    assert to == "+15025550123"
    assert "order #1 for Ana" in body and "https://pay.example/1" in body and "pay at pickup" in body
    saved = store.recent()[0]
    assert saved["status"] == "confirmed" and saved["payment_status"] == "unpaid"
    assert saved["payment_url"] == "https://pay.example/1"


def test_required_payment_holds_order_until_paid():
    menu = menu_with(True)
    sms = FakeSms()
    _, store, result, _ = placed_session(menu, PaymentService(FakeLinks(), sms))
    assert result["payment"]["required_before_cooking"] is True
    assert "after payment" in str(result["ready_in_minutes"])
    assert "once it's paid" in sms.sent[0][1]
    assert store.recent()[0]["status"] == "awaiting_payment"

    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": "cs_test_1", "payment_status": "paid"}}}
    assert handle_stripe_event(event, store, lambda rid: menu, sms) == "paid"
    assert handle_stripe_event(event, store, lambda rid: menu, sms) == "duplicate"  # Stripe retries
    saved = store.recent()[0]
    assert saved["status"] == "confirmed" and saved["payment_status"] == "paid"
    assert len(sms.sent) == 2 and "payment received" in sms.sent[1][1]


def test_required_payment_expiry_cancels_order():
    menu = menu_with(True)
    sms = FakeSms()
    _, store, _, _ = placed_session(menu, PaymentService(FakeLinks(), sms))
    event = {"type": "checkout.session.expired", "data": {"object": {"id": "cs_test_1"}}}
    assert handle_stripe_event(event, store, lambda rid: menu, sms) == "expired"
    assert store.recent()[0]["status"] == "cancelled"
    assert "cancelled" in sms.sent[-1][1]


def test_optional_payment_expiry_keeps_order():
    menu = menu_with(False)
    sms = FakeSms()
    _, store, _, _ = placed_session(menu, PaymentService(FakeLinks(), sms))
    event = {"type": "checkout.session.expired", "data": {"object": {"id": "cs_test_1"}}}
    handle_stripe_event(event, store, lambda rid: menu, sms)
    saved = store.recent()[0]
    assert saved["status"] == "confirmed" and saved["payment_status"] == "expired"
    assert len(sms.sent) == 1  # no cancellation text


def test_place_order_needs_phone_when_payments_on():
    _, _, text, err = placed_session(menu_with(False), PaymentService(FakeLinks(), FakeSms()), phone=None)
    assert err and "set_customer_phone" in text


def test_set_customer_phone_tool():
    session = CallSession(order=Order(menu=load_menu(FIXTURE_MENU)))
    text, err = run_tool(session, "set_customer_phone", {"phone": "502 555 0123"})
    assert not err and session.order.customer_phone == "+15025550123"
    text, err = run_tool(session, "set_customer_phone", {"phone": "5550123"})
    assert err and "ten digits" in text


def test_link_failure_still_places_order():
    _, store, result, err = placed_session(menu_with(True), PaymentService(FakeLinks(fail=True), FakeSms()))
    assert not err and result["placed"] is True
    assert result["payment"]["link_sent"] is False
    assert "pay at the counter" in result["payment"]["tell_caller"]
    assert store.recent()[0]["status"] == "awaiting_payment"


def test_order_text_truncates_long_orders():
    order = Order(menu=load_menu(FIXTURE_MENU))
    for _ in range(10):
        order.add_item("tiramisu")
    order.set_customer("Ana")
    body = order_text(order, 7, "https://pay.example/7")
    assert "...and 2 more" in body


def test_stripe_webhook_route_checks_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDERS_DB", str(tmp_path / "orders.db"))
    monkeypatch.setenv("MENU_PATH", str(FIXTURE_MENU))
    server.app.state.client = FakeClient([])
    server.app.state.payments = None
    try:
        with TestClient(server.app) as c:
            monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
            assert c.post("/stripe/webhook", content=b"{}").status_code == 404
            monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
            r = c.post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "bad"})
            assert r.status_code == 400
            assert "received" in c.get("/paid?order=3").text
    finally:
        del server.app.state.client
        del server.app.state.payments


def test_stripe_webhook_route_accepts_signed_event(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import time

    monkeypatch.setenv("ORDERS_DB", str(tmp_path / "orders.db"))
    monkeypatch.setenv("MENU_PATH", str(FIXTURE_MENU))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    server.app.state.client = FakeClient([])
    server.app.state.payments = None
    try:
        with TestClient(server.app) as c:
            store = server.app.state.store
            rid = seed_restaurant(store)
            order = Order(menu=server.app.state.restaurants.get(rid))
            order.add_item("tiramisu")
            order.set_customer("Ana")
            saved = store.save(order, status="awaiting_payment", payment_status="unpaid")
            store.set_payment_link(saved.id, "cs_test_x", "https://pay.example/x")

            payload = json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                                  "data": {"object": {"id": "cs_test_x", "object": "checkout.session",
                                                      "payment_status": "paid"}}})
            ts = int(time.time())
            sig = hmac.new(b"whsec_test", f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
            r = c.post("/stripe/webhook", content=payload,
                       headers={"stripe-signature": f"t={ts},v1={sig}"})
            assert r.status_code == 200 and r.json()["result"] == "paid"
            assert store.recent()[0]["status"] == "confirmed"
    finally:
        del server.app.state.client
        del server.app.state.payments
