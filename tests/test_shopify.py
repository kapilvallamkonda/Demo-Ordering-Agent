"""Shopify (pos_shopify.py): connecting, importing products, and phone orders as draft
orders paid at Shopify's checkout."""

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from phone_agent import pos as pos_mod
from phone_agent.businesses import BusinessDirectory
from phone_agent.order import Order
from phone_agent.pos import PosError, PosHub, connect_url, merge_import
from phone_agent.pos_shopify import ShopifyProvider, normalize_shop, product_item
from phone_agent.storage import Store
from phone_agent.tools import CallSession, run_tool

from .conftest import seed_business

SHOP = "manu-wedding.myshopify.com"
SECRET = "shpss_test"


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("POS_SECRET_KEY", Fernet.generate_key().decode())


class FakeShopify:
    """Answers GraphQL by which operation the query names, and the OAuth token exchange."""

    def __init__(self, answers):
        self.answers = answers  # operation keyword -> data dict (or callable(variables))
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.url.host, request.url.path, request.headers.get("X-Shopify-Access-Token"), body))
        if request.url.path.endswith("/admin/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "shpat_1", "scope": "read_products,write_draft_orders"})
        for key, answer in self.answers.items():
            if key in body.get("query", ""):
                data = answer(body.get("variables") or {}) if callable(answer) else answer
                return httpx.Response(200, json=data if "errors" in data else {"data": data})
        return httpx.Response(400, json={"errors": "unexpected query"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def variables(self, key):
        return [b.get("variables") for _, _, _, b in self.calls if key in b.get("query", "")]


def signed(query: dict) -> dict:
    msg = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
    return {**query, "hmac": hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()}


def test_store_addresses():
    assert normalize_shop("manu-wedding") == SHOP
    assert normalize_shop("https://Manu-Wedding.myshopify.com/admin") == SHOP
    for bad in ("manuweddingaccessoriesusa.com", "", "evil.com/x.myshopify.com", "a b"):
        with pytest.raises(PosError):
            normalize_shop(bad)


def test_connect_link_and_callback_signature():
    prov = ShopifyProvider("client1", SECRET, http=FakeShopify({"shop {": {"shop": {"name": "Manu", "currencyCode": "USD"}}}).client())
    url = connect_url("shopify", 7, "https://agent.example", {"shopify": prov}, shop="manu-wedding")
    u = urlparse(url)
    q = parse_qs(u.query)
    assert u.netloc == SHOP and u.path == "/admin/oauth/authorize"
    assert q["redirect_uri"] == ["https://agent.example/pos/shopify/callback"]
    assert q["scope"] == ["read_products,write_draft_orders,read_orders"]
    assert pos_mod.check_state(q["state"][0], "shopify") == 7
    with pytest.raises(PosError, match="Shopify address"):
        connect_url("shopify", 7, "https://agent.example", {"shopify": prov}, shop="")
    query = signed({"code": "c1", "shop": SHOP, "state": q["state"][0], "timestamp": "1700000000"})
    tokens, merchant = prov.exchange_code("c1", query, "https://agent.example/pos/shopify/callback")
    assert tokens["access_token"] == "shpat_1" and merchant == SHOP
    with pytest.raises(PosError, match="signature"):
        prov.exchange_code("c1", {**query, "shop": "other.myshopify.com"}, "")
    with pytest.raises(PosError, match="signature"):
        prov.exchange_code("c1", {k: v for k, v in query.items() if k != "hmac"}, "")


def variant(vid, title, price, options, available=True):
    return {"id": f"gid://shopify/ProductVariant/{vid}", "title": title, "price": price,
            "availableForSale": available, "selectedOptions": [{"name": n, "value": v} for n, v in options]}


GARLAND = {"id": "gid://shopify/Product/1", "title": "Rose Garland Pair", "description": "Fresh  roses.\nMade to order.",
           "productType": "Wedding Garlands", "variants": {"nodes": [
               variant(12, "5 feet", "150.00", [("Length", "5 feet")]),
               variant(11, "4 feet", "120.00", [("Length", "4 feet")])]}}
COCONUT = {"id": "gid://shopify/Product/2", "title": "Decorated Coconut", "description": "", "productType": "",
           "variants": {"nodes": [variant(21, "Default Title", "25.00", [("Title", "Default Title")])]}}
SET = {"id": "gid://shopify/Product/3", "title": "Kanyadanam Set", "description": "", "productType": "Sets",
       "variants": {"nodes": [variant(31, "Red / Small", "60.00", [("Color", "Red"), ("Size", "Small")]),
                              variant(32, "Red / Large", "80.00", [("Color", "Red"), ("Size", "Large")], False)]}}


def test_products_become_catalog_items():
    g = product_item(GARLAND)
    assert g["price"] == 120.0 and g["category"] == "Wedding Garlands" and g["description"] == "Fresh roses. Made to order."
    assert g["options"] == {"length": {"choices": {"4 feet": 0.0, "5 feet": 30.0}, "required": True}}
    assert g["pos"] == {"item": "gid://shopify/ProductVariant/11", "choices": {"length": {
        "4 feet": {"variation": "gid://shopify/ProductVariant/11"}, "5 feet": {"variation": "gid://shopify/ProductVariant/12"}}}}
    c = product_item(COCONUT)
    assert "options" not in c and c["category"] == "Products" and c["pos"] == {"item": "gid://shopify/ProductVariant/21"}
    s = product_item(SET)  # two option names: one combined choice
    assert s["options"]["choice"]["choices"] == {"Red / Small": 0.0, "Red / Large": 20.0}
    assert product_item({"id": "x", "title": "No price", "variants": {"nodes": [{"id": "v", "price": None}]}}) is None


def shop_answers(draft_state):
    return {
        "shop {": {"shop": {"name": "Manu Wedding", "currencyCode": "USD"}},
        "products(": {"products": {"pageInfo": {"hasNextPage": False}, "nodes": [GARLAND, COCONUT]}},
        "draftOrderCreate": {"draftOrderCreate": {"draftOrder": {"id": "gid://shopify/DraftOrder/9",
                                                                 "invoiceUrl": "https://manu.example/checkout/9"},
                                                  "userErrors": []}},
        "draftOrder(id": lambda v: {"draftOrder": draft_state["draft"]},
        "draftOrderDelete": {"draftOrderDelete": {"deletedId": "gid://shopify/DraftOrder/9", "userErrors": []}},
    }


class Texts:
    def __init__(self):
        self.sent = []

    def send(self, to, body):
        self.sent.append((to, body))


def shopify_business(draft_state):
    store = Store(":memory:")
    api = FakeShopify(shop_answers(draft_state))
    prov = ShopifyProvider("client1", SECRET, http=api.client())
    rid = seed_business(store)
    directory = BusinessDirectory(store)
    texts = Texts()
    hub = PosHub(store, directory, {"shopify": prov}, run_inline=True, sms=texts)
    conn = hub.save_connection(rid, "shopify", "production", {"access_token": "shpat_1"}, SHOP)
    assert conn.settings == {"shop_name": "Manu Wedding", "currency": "USD"}
    # Import the products, as the owner's "Import catalog" button does, and publish.
    catalog, notes = merge_import(store.catalog_payload(store.business(rid).catalog_id), prov.catalog(conn), "shopify")
    store.set_catalog(rid, catalog)
    return store, directory, rid, hub, api, texts


def test_phone_order_becomes_a_draft_paid_at_shopify_checkout():
    draft_state = {"draft": {"status": "OPEN", "order": None}}
    store, directory, rid, hub, api, texts = shopify_business(draft_state)
    payments, catalog = pos_mod.payments_for(hub, store, directory.get(rid), default=None)
    assert catalog.payment_required  # always, with Shopify
    session = CallSession(order=Order(catalog=catalog), store=store, call_id="CA3", pos=hub, payments=payments)
    session.order.customer_phone = "+14695550199"
    garland = next(i for i in catalog.items.values() if i.name == "Rose Garland Pair")
    coconut = next(i for i in catalog.items.values() if i.name == "Decorated Coconut")
    assert not run_tool(session, "add_item", {"item_id": garland.id, "options": {"length": "5 feet"}, "notes": "extra jasmine"})[1]
    assert not run_tool(session, "add_item", {"item_id": coconut.id, "quantity": 2})[1]
    run_tool(session, "set_customer_name", {"name": "Lakshmi"})
    text, err = run_tool(session, "place_order", {"caller_confirmed": True})
    assert not err, text
    [variables] = api.variables("draftOrderCreate")
    draft = variables["input"]
    assert draft["lineItems"] == [{"variantId": "gid://shopify/ProductVariant/12", "quantity": 1},
                                  {"variantId": "gid://shopify/ProductVariant/21", "quantity": 2}]
    assert "Phone order #1 for Lakshmi" in draft["note"] and "extra jasmine" in draft["note"]
    assert {"key": "Pickup", "value": "As soon as possible"} in draft["customAttributes"]
    assert all(c[0] == SHOP and c[2] == "shpat_1" for c in api.calls if "graphql" in c[1])
    assert "/admin/api/2026-10/graphql.json" in api.calls[-1][1]
    rec = store.order_for_pos(session.order_id)
    assert rec["status"] == "awaiting_payment" and rec["pos_order_id"] == "gid://shopify/DraftOrder/9"
    assert texts.sent[0][0] == "+14695550199" and "https://manu.example/checkout/9" in texts.sent[0][1]
    assert hub.check_payment(session.order_id) == "waiting"
    draft_state["draft"] = {"status": "COMPLETED", "order": {"id": "gid://shopify/Order/77"}}
    assert hub.check_payment(session.order_id) == "paid"
    rec = store.order_for_pos(session.order_id)
    assert rec["status"] == "confirmed" and rec["payment_status"] == "paid" and rec["pos_status"] == "sent"


def test_unpaid_draft_is_deleted_after_the_window():
    draft_state = {"draft": {"status": "OPEN", "order": None}}
    store, directory, rid, hub, api, texts = shopify_business(draft_state)
    payments, catalog = pos_mod.payments_for(hub, store, directory.get(rid), default=None)
    session = CallSession(order=Order(catalog=catalog), store=store, call_id="CA4", pos=hub, payments=payments)
    session.order.customer_phone = "+14695550199"
    item = next(iter(catalog.items.values()))
    opts = {name: next(iter(g.choices)) for name, g in item.options.items()}
    run_tool(session, "add_item", {"item_id": item.id, "options": opts})
    run_tool(session, "set_customer_name", {"name": "Ravi"})
    assert not run_tool(session, "place_order", {"caller_confirmed": True})[1]
    created = store.order_for_pos(session.order_id)["created_at"]
    assert hub.check_payment(session.order_id, now=created + catalog.pay_within_minutes * 60 + 60) == "expired"
    assert api.variables("draftOrderDelete") == [{"input": {"id": "gid://shopify/DraftOrder/9"}}]
    assert store.order_for_pos(session.order_id)["status"] == "cancelled"


def test_custom_priced_lines_and_graphql_errors():
    from phone_agent.pos import PosLine
    from phone_agent.pos_shopify import _line

    line = PosLine("x", "Garland", 1, 140.0, 120.0, {"length": "4 feet"}, {"length": 0.0}, {"jasmine": 20.0}, "",
                   ids={"item": "gid://shopify/ProductVariant/11"})
    assert _line(line) == {"title": "Garland (4 feet, jasmine)", "originalUnitPrice": "140.00", "quantity": 1,
                           "taxable": True, "requiresShipping": False}
    api = FakeShopify({"shop {": {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}})
    prov = ShopifyProvider("c", SECRET, http=api.client())
    conn = pos_mod.Connection(1, "shopify", "production", SHOP, None, {"access_token": "t"}, {})
    with pytest.raises(PosError) as e:
        prov.setup(conn)
    assert e.value.retry is True


def test_owner_imports_the_shopify_catalog_into_the_draft(tmp_path, monkeypatch):
    from phone_agent import server

    from .test_onboarding import W, new_business, sign_in
    from .test_onboarding import app as onboarding_app

    gen = onboarding_app.__wrapped__(tmp_path, monkeypatch)
    c, store, _, _ = next(gen)
    try:
        sign_in(c, store)
        b = new_business(c, "Manu Wedding", template="events")["id"]
        assert c.post(f"/api/manage/businesses/{b}/pos/import", headers=W).status_code == 400  # not connected
        api = FakeShopify(shop_answers({"draft": None}))
        hub = PosHub(store, server.app.state.businesses, {"shopify": ShopifyProvider("c", SECRET, http=api.client())},
                     run_inline=True)
        monkeypatch.setattr(server.app.state, "pos", hub)
        hub.save_connection(b, "shopify", "production", {"access_token": "shpat_1"}, SHOP)
        r = c.post(f"/api/manage/businesses/{b}/pos/import", headers=W)
        assert r.status_code == 200, r.text
        assert r.json()["items"] == 2
        ed = c.get(f"/api/manage/businesses/{b}/editor").json()
        catalog = ed["catalog"]
        assert ed["draft"]["revision"] == r.json()["revision"] and ed["pos_provider"] == "shopify"
        assert catalog["template"] == "events" and catalog["settings"]["name"] == "Manu Wedding"
        assert {i["name"] for i in catalog["items"]} == {"Rose Garland Pair", "Decorated Coconut"}
        sign_in(c, store, "other@example.com")  # someone else can't import into it
        assert c.post(f"/api/manage/businesses/{b}/pos/import", headers=W).status_code in (403, 404)
    finally:
        next(gen, None)
