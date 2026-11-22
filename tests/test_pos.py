import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ordering_agent import pos as pos_mod
from ordering_agent import server
from ordering_agent.menu import parse_menu
from ordering_agent.order import Order
from ordering_agent.payments import handle_stripe_event
from ordering_agent.pos import PosError, PosHub, check_state, make_state, merge_import, seal, unseal
from ordering_agent.pos_clover import CloverProvider
from ordering_agent.pos_square import SquareProvider
from ordering_agent.restaurants import RestaurantDirectory
from ordering_agent.storage import OrderStore
from ordering_agent.tools import CallSession, run_tool

from .conftest import FIXTURE_MENU, seed_restaurant
from .fakes import FakeClient


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("POS_SECRET_KEY", Fernet.generate_key().decode())


class FakeApi:
    """Records requests and answers from a route table: (method, path suffix) -> response."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body))
        for (method, suffix), answer in self.routes.items():
            if request.method == method and request.url.path.endswith(suffix):
                status, payload = answer(body) if callable(answer) else answer
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": "no route"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def bodies(self, method, suffix):
        return [b for m, p, _, b in self.requests if m == method and p.endswith(suffix)]


def setup_restaurant(menu_raw=None):
    store = OrderStore(":memory:")
    rid = seed_restaurant(store, menu=menu_raw)
    directory = RestaurantDirectory(store)
    return store, directory, rid


def place(store, directory, rid, hub, items=(("wings", {"sauce": "bbq"}, []),), payment_status="not_requested"):
    menu = directory.get(rid)
    session = CallSession(order=Order(menu=menu), store=store, call_id="CA1", pos=hub)
    session.order.customer_phone = "+15025550123"
    for item_id, options, extras in items:
        _, err = run_tool(session, "add_item", {"item_id": item_id, "options": options, "extras": extras,
                                                "quantity": 2, "notes": "extra crispy"})
        assert not err
    run_tool(session, "set_customer_name", {"name": "Ana"})
    text, err = run_tool(session, "place_order", {"caller_confirmed": True})
    assert not err, text
    return session


# ---- secrets and connect links ------------------------------------------------------------


def test_tokens_are_encrypted_at_rest():
    sealed = seal({"access_token": "EAAA-secret"})
    assert "EAAA-secret" not in sealed
    assert unseal(sealed) == {"access_token": "EAAA-secret"}


def test_connect_state_is_signed_and_expires():
    state = make_state("square", 7, now=1000)
    assert check_state(state, "square", now=1500) == 7
    with pytest.raises(PosError):
        check_state(state, "clover", now=1500)  # for another provider
    with pytest.raises(PosError):
        check_state(state.replace(".7.", ".8."), "square", now=1500)  # tampered restaurant
    with pytest.raises(PosError, match="expired"):
        check_state(state, "square", now=1000 + 3601)


# ---- Square ----------------------------------------------------------------------------------


def square_api(fail_times=0):
    state = {"fails": fail_times}

    def create(body):
        if state["fails"]:
            state["fails"] -= 1
            return 503, {"errors": [{"code": "SERVICE_UNAVAILABLE"}]}
        return 200, {"order": {"id": "SQ-ORDER-1", "total_money": {"amount": 2808, "currency": "USD"}}}

    return FakeApi({
        ("GET", "/v2/locations"): (200, {"locations": [{"id": "LOC1", "name": "Main", "status": "ACTIVE",
                                                       "currency": "USD"}]}),
        ("POST", "/v2/orders"): create,
        ("GET", "/v2/orders/SQ-ORDER-1"): (200, {"order": {"id": "SQ-ORDER-1",
                                                           "total_money": {"amount": 2808, "currency": "USD"}}}),
        ("POST", "/v2/payments"): (200, {"payment": {"id": "PAY1"}}),
    })


def square_hub(store, directory, api):
    hub = PosHub(store, directory, {"square": SquareProvider("app", "secret", "sandbox", http=api.client())},
                 retry_delays=(0, 0), run_inline=True)
    return hub


def test_square_order_is_sent_as_pickup_with_tax_and_notes():
    store, directory, rid = setup_restaurant()
    api = square_api()
    hub = square_hub(store, directory, api)
    conn = hub.save_connection(rid, "square", "sandbox", {"access_token": "tok"}, "M1")
    assert conn.location_id == "LOC1"  # the only location was picked

    session = place(store, directory, rid, hub)
    [body] = api.bodies("POST", "/v2/orders")
    order = body["order"]
    assert body["idempotency_key"] == f"phone-order-{rid}-{session.order_id}"
    assert order["location_id"] == "LOC1"
    assert order["reference_id"] == f"phone-{session.order_number}"
    [line] = order["line_items"]  # the fixture menu has no Square ids: an ad hoc line
    assert line["quantity"] == "2" and line["note"] == "extra crispy"
    assert line["name"].startswith("Chicken Wings") and "bbq" in line["name"]
    assert line["base_price_money"]["currency"] == "USD"
    assert order["taxes"] == [{"uid": "sales-tax", "name": "Sales tax", "scope": "ORDER", "percentage": "8"}]
    pickup = order["fulfillments"][0]
    assert pickup["type"] == "PICKUP" and pickup["pickup_details"]["schedule_type"] == "ASAP"
    assert pickup["pickup_details"]["recipient"] == {"display_name": "Ana", "phone_number": "+15025550123"}
    assert pickup["pickup_details"]["prep_time_duration"] == "PT20M"

    rec = store.order_for_pos(session.order_id)
    assert rec["pos_status"] == "sent" and rec["pos_order_id"] == "SQ-ORDER-1"
    assert [o["pos_status"] for o in store.recent()] == ["sent"]
    assert "items" not in store.recent()[0]  # the dashboard payload stays the same


def test_square_payment_is_recorded_as_external_after_stripe_paid():
    store, directory, rid = setup_restaurant()
    api = square_api()
    hub = square_hub(store, directory, api)
    hub.save_connection(rid, "square", "sandbox", {"access_token": "tok"}, "M1")
    session = place(store, directory, rid, hub)
    store.set_payment_link(session.order_id, "cs_1", "https://pay/1")
    store.update_status(session.order_id, "confirmed")
    with store._lock:  # simulate "payment link sent"
        store._conn.execute("UPDATE orders SET payment_status = 'unpaid' WHERE id = ?", (session.order_id,))
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_1", "payment_status": "paid"}}}
    assert handle_stripe_event(event, store, directory.get, None, hub.order_paid) == "paid"
    [payment] = api.bodies("POST", "/v2/payments")
    assert payment["source_id"] == "EXTERNAL"
    assert payment["amount_money"] == {"amount": 2808, "currency": "USD"}  # Square's own total
    assert payment["order_id"] == "SQ-ORDER-1" and payment["external_details"]["source_id"] == "cs_1"
    assert store.order_for_pos(session.order_id)["pos_paid"] is True
    hub.order_paid(session.order_id)  # a retried webhook doesn't pay twice
    assert len(api.bodies("POST", "/v2/payments")) == 1


def test_square_outage_is_retried_with_the_same_idempotency_key():
    store, directory, rid = setup_restaurant()
    api = square_api(fail_times=2)
    hub = square_hub(store, directory, api)
    hub.save_connection(rid, "square", "sandbox", {"access_token": "tok"}, "M1")
    session = place(store, directory, rid, hub)
    keys = {b["idempotency_key"] for b in api.bodies("POST", "/v2/orders")}
    assert len(api.bodies("POST", "/v2/orders")) == 3 and len(keys) == 1
    rec = store.order_for_pos(session.order_id)
    assert rec["pos_status"] == "sent" and rec["pos_attempts"] == 3


def test_square_rejection_is_not_retried_and_shows_on_dashboard():
    store, directory, rid = setup_restaurant()
    api = square_api()
    api.routes[("POST", "/v2/orders")] = (400, {"errors": [{"code": "INVALID_VALUE"}]})
    hub = square_hub(store, directory, api)
    hub.save_connection(rid, "square", "sandbox", {"access_token": "tok"}, "M1")
    place(store, directory, rid, hub)
    assert len(api.bodies("POST", "/v2/orders")) == 1
    [o] = store.recent()
    assert o["pos_status"] == "failed" and "400" in o["pos_error"]


def test_square_catalog_import_maps_variations_and_modifiers():
    api = FakeApi({("GET", "/v2/catalog/list"): (200, {"objects": [
        {"type": "CATEGORY", "id": "CAT1", "category_data": {"name": "Biryanis"}},
        {"type": "MODIFIER_LIST", "id": "ML1", "modifier_list_data": {
            "name": "Spice", "selection_type": "SINGLE", "modifiers": [
                {"id": "MOD-MILD", "modifier_data": {"name": "mild"}},
                {"id": "MOD-HOT", "modifier_data": {"name": "hot", "price_money": {"amount": 0}}}]}},
        {"type": "MODIFIER_LIST", "id": "ML2", "modifier_list_data": {
            "name": "Add-ons", "selection_type": "MULTIPLE", "modifiers": [
                {"id": "MOD-RAITA", "modifier_data": {"name": "raita", "price_money": {"amount": 150}}}]}},
        {"type": "ITEM", "id": "ITEM1", "item_data": {
            "name": "Dum Biryani", "categories": [{"id": "CAT1"}], "description_plaintext": "Non-veg.",
            "variations": [
                {"id": "VAR-GOAT", "item_variation_data": {"name": "Goat", "pricing_type": "FIXED_PRICING",
                                                           "price_money": {"amount": 1900}}},
                {"id": "VAR-CHK", "item_variation_data": {"name": "Chicken", "pricing_type": "FIXED_PRICING",
                                                          "price_money": {"amount": 1500}}}],
            "modifier_list_info": [{"modifier_list_id": "ML1", "min_selected_modifiers": 1},
                                   {"modifier_list_id": "ML2"}]}},
        {"type": "ITEM", "id": "ITEM2", "item_data": {"name": "Catering Tray", "variations": [
            {"id": "VAR-X", "item_variation_data": {"name": "Regular", "pricing_type": "VARIABLE_PRICING"}}]}},
    ]})})
    sq = SquareProvider("app", "secret", http=api.client())
    conn = pos_mod.Connection(1, "square", "sandbox", "M1", "LOC1", {"access_token": "t"}, {})
    [item] = sq.catalog(conn)  # the variable-price tray is left out
    assert item["name"] == "Dum Biryani" and item["category"] == "Biryanis" and item["price"] == 15.0
    assert item["options"]["type"] == {"choices": {"Chicken": 0.0, "Goat": 4.0}, "required": True}
    assert item["options"]["spice"] == {"choices": {"mild": 0.0, "hot": 0.0}, "required": True}
    assert item["extras"] == {"raita": 1.5}
    assert item["pos"] == {"item": "VAR-CHK",
                           "choices": {"type": {"Chicken": {"variation": "VAR-CHK"}, "Goat": {"variation": "VAR-GOAT"}},
                                       "spice": {"mild": {"modifier": "MOD-MILD"}, "hot": {"modifier": "MOD-HOT"}}},
                           "extras": {"raita": "MOD-RAITA"}}

    # The imported menu works end to end: the order goes to Square as catalog items.
    raw, _ = merge_import(None, [item], "square", base_restaurant=json.loads(FIXTURE_MENU.read_text())["restaurant"])
    store, directory, rid = setup_restaurant(raw)
    api2 = square_api()
    hub = square_hub(store, directory, api2)
    hub.save_connection(rid, "square", "sandbox", {"access_token": "tok"}, "M1")
    place(store, directory, rid, hub, items=(("dum_biryani", {"type": "Goat", "spice": "hot"}, ["raita"]),))
    [line] = api2.bodies("POST", "/v2/orders")[0]["order"]["line_items"]
    assert line["catalog_object_id"] == "VAR-GOAT"
    assert line["modifiers"] == [{"catalog_object_id": "MOD-HOT", "quantity": "1"},
                                 {"catalog_object_id": "MOD-RAITA", "quantity": "1"}]
    assert "base_price_money" not in line


def test_reimport_keeps_item_ids():
    base = json.loads(FIXTURE_MENU.read_text())["restaurant"]
    first, _ = merge_import(None, [{"name": "Dum Biryani", "category": "B", "price": 15.0, "pos_key": "A"}],
                            "square", base_restaurant=base)
    first["items"][0]["id"] = "hyderabadi_dum_biryani"  # renamed by hand after the first import
    again, notes = merge_import(first, [
        {"name": "Dum Biryani (new name)", "category": "B", "price": 16.0, "pos_key": "A"},
        {"name": "Dum Biryani", "category": "B", "price": 9.0, "pos_key": "B"},
    ], "square")
    assert [i["id"] for i in again["items"]] == ["hyderabadi_dum_biryani", "dum_biryani"]
    assert again["restaurant"] == base and again["pos"]["provider"] == "square"
    parse_menu(again)  # valid menu


# ---- Clover ----------------------------------------------------------------------------------


def clover_api():
    return FakeApi({
        ("GET", "/order_types"): (200, {"elements": [{"id": "OT-DINE", "label": "Dine In"},
                                                     {"id": "OT-TOGO", "label": "To Go"}]}),
        ("GET", "/tax_rates"): (200, {"elements": [{"id": "TAX1", "name": "Sales", "rate": 800000, "isDefault": True}]}),
        ("POST", "/orders"): (200, {"id": "CL-ORDER-1"}),
        ("POST", "/bulk_line_items"): lambda body: (200, [{"id": f"LI{i}"} for i, _ in enumerate(body["items"])]),
        ("POST", "/modifications"): (200, {}),
        ("POST", "/orders/CL-ORDER-1"): (200, {}),
        ("DELETE", "/orders/CL-ORDER-1"): (200, {}),
    })


def test_clover_order_is_created_open_with_custom_lines():
    store, directory, rid = setup_restaurant()
    api = clover_api()
    hub = PosHub(store, directory, {"clover": CloverProvider("app", "secret", http=api.client())},
                 retry_delays=(0,), run_inline=True)
    conn = hub.save_connection(rid, "clover", "sandbox", {"access_token": "tok"}, "MERCH1")
    assert conn.settings["order_type_id"] == "OT-TOGO"
    session = place(store, directory, rid, hub)
    [order] = api.bodies("POST", "/v3/merchants/MERCH1/orders")
    assert order["state"] == "open" and order["orderType"] == {"id": "OT-TOGO"}
    assert order["title"] == f"Phone #{session.order_number} Ana"
    assert order["total"] == round(session.order.total * 100)
    [bulk] = api.bodies("POST", "/bulk_line_items")
    assert len(bulk["items"]) == 2  # one Clover line per unit
    first = bulk["items"][0]
    assert "bbq" in first["name"] and first["note"] == "extra crispy"
    assert first["price"] == round(session.order.lines[0].unit_price * 100)
    assert first["taxRates"] == [{"id": "TAX1", "name": "Sales", "rate": 800000}]
    assert store.order_for_pos(session.order_id)["pos_order_id"] == "CL-ORDER-1"


def test_clover_partial_order_is_deleted_before_retrying():
    store, directory, rid = setup_restaurant()
    api = clover_api()
    calls = {"n": 0}

    def flaky(body):
        calls["n"] += 1
        return (500, {"message": "oops"}) if calls["n"] == 1 else (200, [{"id": "LI1"}, {"id": "LI2"}])

    api.routes[("POST", "/bulk_line_items")] = flaky
    hub = PosHub(store, directory, {"clover": CloverProvider("app", "secret", http=api.client())},
                 retry_delays=(0,), run_inline=True)
    hub.save_connection(rid, "clover", "sandbox", {"access_token": "tok"}, "MERCH1")
    session = place(store, directory, rid, hub)
    assert [m for m, p, _, _ in api.requests if p.endswith("/orders/CL-ORDER-1")] == ["DELETE"]
    assert store.order_for_pos(session.order_id)["pos_status"] == "sent"


def test_clover_import_and_modifiers_on_inventory_items():
    api = clover_api()
    api.routes[("GET", "/modifier_groups")] = (200, {"elements": [
        {"id": "G1", "name": "Protein", "minRequired": 1, "maxAllowed": 1, "modifiers": {"elements": [
            {"id": "MOD-CHK", "name": "chicken", "price": 0}, {"id": "MOD-GOAT", "name": "goat", "price": 400}]}}]})
    api.routes[("GET", "/items")] = (200, {"elements": [
        {"id": "IT1", "name": "Fried Rice", "price": 1300, "priceType": "FIXED",
         "categories": {"elements": [{"name": "Indo-Chinese"}]}, "modifierGroups": {"elements": [{"id": "G1"}]}},
        {"id": "IT2", "name": "Open Item", "price": 0, "priceType": "VARIABLE"},
        {"id": "IT3", "name": "Hidden", "price": 100, "hidden": True}]})
    provider = CloverProvider("app", "secret", http=api.client())
    conn = pos_mod.Connection(1, "clover", "sandbox", "MERCH1", None, {"access_token": "t"}, {})
    [item] = provider.catalog(conn)
    assert item["options"]["protein"] == {"choices": {"chicken": 0.0, "goat": 4.0}, "required": True}
    assert item["pos"]["item"] == "IT1"

    raw, _ = merge_import(None, [item], "clover", base_restaurant=json.loads(FIXTURE_MENU.read_text())["restaurant"])
    store, directory, rid = setup_restaurant(raw)
    hub = PosHub(store, directory, {"clover": provider}, retry_delays=(0,), run_inline=True)
    hub.save_connection(rid, "clover", "sandbox", {"access_token": "tok"}, "MERCH1")
    place(store, directory, rid, hub, items=(("fried_rice", {"protein": "goat"}, []),))
    bulk = api.bodies("POST", "/bulk_line_items")[-1]
    assert bulk["items"][0]["item"] == {"id": "IT1"} and "price" not in bulk["items"][0]
    mods = api.bodies("POST", "/modifications")
    assert mods == [{"modifier": {"id": "MOD-GOAT"}}] * 2


def test_clover_token_is_refreshed_when_about_to_expire():
    store, directory, rid = setup_restaurant()
    api = clover_api()
    api.routes[("POST", "/oauth/v2/refresh")] = (200, {"access_token": "new", "refresh_token": "r2",
                                                       "access_token_expiration": 9_999_999_999})
    hub = PosHub(store, directory, {"clover": CloverProvider("app", "secret", http=api.client())},
                 run_inline=True)
    hub.save_connection(rid, "clover", "sandbox",
                        {"access_token": "old", "refresh_token": "r1", "expires_at": 1}, "MERCH1")
    assert hub.connection(rid).access_token == "new"
    assert hub.connection(rid).tokens["refresh_token"] == "r2"  # saved, not refreshed again
    assert len(api.bodies("POST", "/oauth/v2/refresh")) == 1


# ---- no POS, and the connect callback ---------------------------------------------------------


def test_restaurant_without_pos_is_untouched():
    store, directory, rid = setup_restaurant()
    hub = PosHub(store, directory, {}, run_inline=True)
    place(store, directory, rid, hub)
    assert store.recent()[0]["pos_status"] is None


def test_pos_failure_never_breaks_placing_the_order():
    store, directory, rid = setup_restaurant()

    class Broken:
        def order_placed(self, *a):
            raise RuntimeError("boom")

    session = place(store, directory, rid, Broken())
    assert session.order_number == 1


def test_connect_callback_stores_the_connection():
    store, directory, rid = setup_restaurant()
    api = square_api()
    api.routes[("POST", "/oauth2/token")] = (200, {"access_token": "EAAA", "refresh_token": "R",
                                                   "expires_at": "2026-12-22T00:00:00Z", "merchant_id": "M9"})
    hub = PosHub(store, directory, {"square": SquareProvider("app", "secret", http=api.client())}, run_inline=True)
    server.app.state.pos, server.app.state.client = hub, FakeClient([])
    with TestClient(server.app) as client:
        server.app.state.store, server.app.state.restaurants = store, directory
        r = client.get("/pos/square/callback", params={"code": "c1", "state": make_state("square", rid)})
        assert r.status_code == 200 and "is connected" in r.text
        bad = client.get("/pos/square/callback", params={"code": "c1", "state": "square.1.1.x"})
        assert bad.status_code == 400
    conn = hub.connection(rid)
    assert conn.merchant_id == "M9" and conn.location_id == "LOC1" and conn.access_token == "EAAA"
    assert "EAAA" not in store.pos_connection(rid)["secret"]
    [token_req] = api.bodies("POST", "/oauth2/token")
    assert token_req["grant_type"] == "authorization_code" and token_req["code"] == "c1"
    del server.app.state.pos, server.app.state.client


def test_admin_commands(monkeypatch, capsys):
    store, directory, rid = setup_restaurant()
    api = square_api()
    api.routes[("GET", "/v2/catalog/list")] = (200, {"objects": [
        {"type": "ITEM", "id": "I1", "item_data": {"name": "Samosa", "variations": [
            {"id": "V1", "item_variation_data": {"pricing_type": "FIXED_PRICING", "price_money": {"amount": 600}}}]}}]})
    hub = square_hub(store, directory, api)
    monkeypatch.setenv("POS_ACCESS_TOKEN", "sandbox-token")
    pos_mod.main(["connect-token", "bella-cucina", "square"], store=store, hub=hub)
    assert "location LOC1" in capsys.readouterr().out
    pos_mod.main(["status"], store=store, hub=hub)
    assert "square (sandbox)" in capsys.readouterr().out
    pos_mod.main(["import-menu", "bella-cucina"], store=store, hub=hub)
    out = capsys.readouterr()
    menu = json.loads(out.out)
    assert menu["items"][0]["id"] == "samosa" and menu["restaurant"]["name"] == "Bella Cucina"
    assert "1 items imported" in out.err
    with pytest.raises(SystemExit, match="POS_ACCESS_TOKEN"):
        monkeypatch.delenv("POS_ACCESS_TOKEN")
        pos_mod.main(["connect-token", "bella-cucina", "square"], store=store, hub=hub)
    pos_mod.main(["disconnect", "bella-cucina"], store=store, hub=hub)
    assert store.pos_connection(rid) is None


def test_pay_first_order_reaches_the_pos_only_once_paid():
    from dataclasses import replace

    store, directory, rid = setup_restaurant()
    api = clover_api()
    hub = PosHub(store, directory, {"clover": CloverProvider("app", "secret", http=api.client())},
                 retry_delays=(0,), run_inline=True)
    hub.save_connection(rid, "clover", "sandbox", {"access_token": "tok"}, "MERCH1")
    menu = directory.get(rid)
    menu = replace(menu, restaurant={**menu.restaurant, "payment": {"required_before_cooking": True}})

    class Links:
        def request_payment(self, order, number):
            from ordering_agent.payments import PaymentLink
            return PaymentLink("cs_9", "https://pay/9")

    session = CallSession(order=Order(menu=menu), store=store, call_id="CA9", pos=hub, payments=Links())
    session.order.customer_phone = "+15025550123"
    run_tool(session, "add_item", {"item_id": "tiramisu"})
    run_tool(session, "set_customer_name", {"name": "Ana"})
    run_tool(session, "place_order", {"caller_confirmed": True})
    assert store.order_for_pos(session.order_id)["pos_status"] == "held"
    assert not api.bodies("POST", "/v3/merchants/MERCH1/orders")
    assert hub.resume() == 0  # held orders aren't resent on restart

    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_9", "payment_status": "paid"}}}
    handle_stripe_event(event, store, directory.get, None, hub.order_paid)
    rec = store.order_for_pos(session.order_id)
    assert rec["pos_status"] == "sent" and rec["pos_paid"]
    assert "PAID ONLINE" in api.bodies("POST", "/orders/CL-ORDER-1")[-1]["note"]
