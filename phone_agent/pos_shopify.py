"""Shopify: import the store's products, and take phone orders as Shopify draft orders.

With Shopify connected, callers pay through Shopify's own checkout (driven by
pos.LinkPayments, as for Square):
  - place_order creates a **draft order** (draftOrderCreate): real product variants, or a
    custom line with its own price for anything not linked to a Shopify product, with the
    phone order number, pickup time and notes in the order note and attributes.
  - The draft's checkout link (invoiceUrl) is texted to the caller. They pay on Shopify's
    checkout page, into the store's own Shopify account, and Shopify turns the draft into a
    real order in the store's admin, where staff handle it as usual.
  - We check the draft every LINK_POLL_SECONDS (pos.PosHub.check_payment): paid once the
    draft has an `order`. Past the business's payment window the draft is deleted
    (draftOrderDelete) and the order is cancelled.
Shopify works out tax at checkout from the store's own tax settings, so the total the
caller pays can differ slightly from what the agent read out.

Connecting is Shopify OAuth for one store: the owner enters their store's
.myshopify.com address and approves our app (scopes below). The access token is an
offline token (it doesn't expire). The callback's hmac is checked with the app secret.

Admin GraphQL API, version SHOPIFY_API_VERSION (default 2026-10).
Not yet tried against a real store: needs a Shopify app (client id and secret) and a
development store.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from urllib.parse import urlencode

from .pos import Connection, PosError, PosLine, PosOrder, http_call

API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")
SCOPES = ["read_products", "write_draft_orders", "read_orders"]
SHOP = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


def normalize_shop(raw: str) -> str:
    """'manu-wedding', 'https://manu-wedding.myshopify.com/admin' -> 'manu-wedding.myshopify.com'.
    Raises PosError for anything else (a custom domain like example.com can't be used)."""
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    if s and "." not in s:
        s += ".myshopify.com"
    if not SHOP.match(s):
        raise PosError("Enter your store's Shopify address, like your-store.myshopify.com (in Shopify: "
                       "Settings > Domains). Your own domain won't work here.", retry=False)
    return s


class ShopifyProvider:
    name = "shopify"
    environment = "production"  # Shopify has no sandbox; development stores are real stores
    pays_by_link = True

    def __init__(self, client_id: str, client_secret: str, http=None):
        import httpx

        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=20)

    @classmethod
    def from_env(cls) -> "ShopifyProvider":
        return cls(os.environ.get("SHOPIFY_CLIENT_ID", ""), os.environ.get("SHOPIFY_CLIENT_SECRET", ""))

    # ---- connecting

    def authorize_url(self, state: str, redirect_uri: str, shop: str = "") -> str:
        shop = normalize_shop(shop)
        params = {"client_id": self.client_id, "scope": ",".join(SCOPES), "redirect_uri": redirect_uri,
                  "state": state}
        return f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"

    def verify_callback(self, query: dict) -> bool:
        """Shopify signs the callback: hex HMAC-SHA256 of the other parameters, sorted, with
        the app secret."""
        given = str(query.get("hmac") or "")
        message = "&".join(f"{k}={v}" for k, v in sorted(query.items()) if k not in ("hmac", "signature"))
        want = hmac.new(self.client_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return bool(given) and bool(self.client_secret) and hmac.compare_digest(want, given)

    def exchange_code(self, code: str, query: dict, redirect_uri: str) -> tuple[dict, str | None]:
        if not self.verify_callback(query):
            raise PosError("the Shopify callback's signature didn't match", retry=False)
        shop = normalize_shop(str(query.get("shop") or ""))
        resp = http_call(self.http, "POST", f"https://{shop}/admin/oauth/access_token", json_body={
            "client_id": self.client_id, "client_secret": self.client_secret, "code": code}, what="Shopify")
        if not resp.get("access_token"):
            raise PosError("Shopify didn't return an access token", retry=False)
        return {"access_token": resp["access_token"], "scope": resp.get("scope"), "expires_at": None}, shop

    def refresh(self, conn: Connection) -> dict:
        return conn.tokens  # offline tokens don't expire

    def setup(self, conn: Connection) -> tuple[str | None, dict]:
        shop = self._gql(conn, "query { shop { name currencyCode } }")["shop"]
        return None, {"shop_name": shop.get("name"), "currency": shop.get("currencyCode", "USD")}

    # ---- GraphQL

    def _gql(self, conn: Connection, query: str, variables: dict | None = None) -> dict:
        shop = normalize_shop(conn.merchant_id or "")
        resp = http_call(self.http, "POST", f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
                         headers={"X-Shopify-Access-Token": conn.access_token},
                         json_body={"query": query, "variables": variables or {}}, what="Shopify")
        errors = resp.get("errors")
        if errors:
            text = str(errors)[:300]
            raise PosError(f"Shopify: {text}", retry="THROTTLED" in text.upper())
        return resp.get("data") or {}

    @staticmethod
    def _user_errors(payload: dict, what: str) -> None:
        errs = payload.get("userErrors") or []
        if errs:
            raise PosError(f"Shopify {what}: " + "; ".join(e.get("message", "") for e in errs)[:300], retry=False)

    # ---- orders

    def draft_input(self, conn: Connection, order: PosOrder) -> dict:
        when = order.pickup_words if order.pickup_at and order.pickup_words else "As soon as possible"
        notes = [f"{line.name}: {line.notes}" for line in order.lines if line.notes]
        note = order.note + (" Item notes: " + "; ".join(notes) if notes else "")
        return {
            "lineItems": [_line(line) for line in order.lines],
            "note": note[:5000],
            "tags": ["Ringcart", "Phone order"],
            "customAttributes": [
                {"key": "Phone order", "value": f"#{order.number}"},
                {"key": "Pickup", "value": when},
                {"key": "Customer phone", "value": order.customer_phone or "not given"},
            ],
        }

    def payment_link(self, conn: Connection, order: PosOrder) -> tuple[str, str, str]:
        """Create the draft order; returns (draft id, checkout url, draft id)."""
        data = self._gql(conn, """
            mutation($input: DraftOrderInput!) {
              draftOrderCreate(input: $input) { draftOrder { id invoiceUrl } userErrors { field message } }
            }""", {"input": self.draft_input(conn, order)})["draftOrderCreate"]
        self._user_errors(data, "draft order")
        draft = data.get("draftOrder") or {}
        if not draft.get("id") or not draft.get("invoiceUrl"):
            raise PosError("Shopify didn't return the draft order's checkout link")
        return draft["id"], draft["invoiceUrl"], draft["id"]

    def link_paid(self, conn: Connection, pos_order_id: str) -> bool | None:
        """True once the draft became an order (paid at checkout), False while it waits,
        None if the draft is gone (deleted in Shopify)."""
        node = self._gql(conn, "query($id: ID!) { draftOrder(id: $id) { status order { id } } }",
                         {"id": pos_order_id}).get("draftOrder")
        if node is None:
            return None
        return bool(node.get("order"))

    def cancel_link(self, conn: Connection, link_id: str) -> None:
        data = self._gql(conn, """
            mutation($input: DraftOrderDeleteInput!) {
              draftOrderDelete(input: $input) { deletedId userErrors { field message } }
            }""", {"input": {"id": link_id}})["draftOrderDelete"]
        errs = [e for e in data.get("userErrors") or [] if "not exist" not in e.get("message", "").lower()]
        if errs:
            raise PosError("Shopify draft delete: " + "; ".join(e.get("message", "") for e in errs)[:300])

    def push(self, conn: Connection, order: PosOrder, existing_id: str | None) -> str:
        """An order that isn't paid by the link (not the usual path: Shopify businesses pay
        first): a draft completed as payment pending, so it's an order in the admin."""
        draft_id = existing_id
        if not draft_id or "DraftOrder" not in draft_id:
            draft_id, _, _ = self.payment_link(conn, order)
        try:
            data = self._gql(conn, """
                mutation($id: ID!) {
                  draftOrderComplete(id: $id, paymentPending: true) {
                    draftOrder { order { id } } userErrors { field message }
                  }
                }""", {"id": draft_id})["draftOrderComplete"]
            self._user_errors(data, "completing the draft order")
        except PosError as e:
            raise PosError(str(e), retry=e.retry, partial_id=draft_id)
        return (((data.get("draftOrder") or {}).get("order")) or {}).get("id") or draft_id

    def mark_paid(self, conn: Connection, order: PosOrder, pos_order_id: str, payment_ref: str) -> None:
        if "/Order/" not in pos_order_id:
            return  # paid at Shopify's checkout: nothing to record
        data = self._gql(conn, """
            mutation($input: OrderMarkAsPaidInput!) {
              orderMarkAsPaid(input: $input) { order { id } userErrors { field message } }
            }""", {"input": {"id": pos_order_id}})["orderMarkAsPaid"]
        self._user_errors(data, "marking the order paid")

    # ---- catalog

    def catalog(self, conn: Connection) -> list[dict]:
        items, after = [], None
        for _ in range(100):  # 50 products a page; safety bound
            data = self._gql(conn, """
                query($after: String) {
                  products(first: 50, after: $after, query: "status:active") {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      id title description productType
                      variants(first: 100) { nodes { id title price availableForSale selectedOptions { name value } } }
                    }
                  }
                }""", {"after": after})["products"]
            for p in data.get("nodes") or []:
                item = product_item(p)
                if item:
                    items.append(item)
            page = data.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        return items


def _line(line: PosLine) -> dict:
    """A variant line when the item (and its chosen option) maps to a Shopify variant and
    nothing extra was added; otherwise a custom line at our price."""
    ids = line.ids
    variant = ids.get("item")
    for opt, choice in line.options.items():
        mapped = (ids.get("choices") or {}).get(opt, {}).get(choice) or {}
        if "variation" in mapped:
            variant = mapped["variation"]
        elif line.option_prices.get(opt):
            variant = None  # a priced choice Shopify doesn't know about
    if variant and not line.extras:
        return {"variantId": variant, "quantity": line.quantity}
    return {"title": line.label()[:255], "originalUnitPrice": f"{line.unit_price:.2f}",
            "quantity": line.quantity, "taxable": True, "requiresShipping": False}


def product_item(p: dict) -> dict | None:
    """A Shopify product as a catalog item: the cheapest variant's price, and the variants as
    one required option (named after the product's option when there's one, e.g. "size")."""
    variants = []
    for v in (p.get("variants") or {}).get("nodes") or []:
        try:
            price = float(v.get("price"))
        except (TypeError, ValueError):
            continue
        variants.append((v["id"], v.get("title") or "Default Title", price, v.get("availableForSale", True),
                         v.get("selectedOptions") or []))
    if not variants:
        return None
    variants.sort(key=lambda v: v[2])
    base = variants[0][2]
    item: dict = {
        "name": p.get("title") or "Product",
        "category": (p.get("productType") or "").strip() or "Products",
        "price": round(base, 2),
        "description": " ".join((p.get("description") or "").split())[:200],
        "pos": {"item": variants[0][0]},
        "pos_key": p["id"],
    }
    if not any(v[3] for v in variants):
        item["available"] = False
    real = [v for v in variants if v[1] != "Default Title"]
    if len(real) > 1 or (len(real) == 1 and len(variants) > 1):
        names = {o.get("name") for v in variants for o in v[4]}
        if len(names) == 1 and all(len(v[4]) == 1 for v in variants):
            opt = (next(iter(names)) or "choice").lower()
            label = lambda v: v[4][0].get("value") or v[1]  # noqa: E731
        else:
            opt, label = "choice", (lambda v: v[1])
        choices, ids = {}, {}
        for v in variants:
            name = label(v)
            if name in choices:
                continue
            choices[name] = round(v[2] - base, 2)
            ids[name] = {"variation": v[0]}
        item["options"] = {opt: {"choices": choices, "required": True}}
        item["pos"]["choices"] = {opt: ids}
    return item
