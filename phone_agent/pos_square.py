"""Square: send orders with the Orders API, import the catalog from the Catalog API.

Orders are created as pickup orders (a PICKUP fulfillment: ASAP with the prep time from
the catalog settings, or SCHEDULED at the pickup time the caller chose). Square only shows an API order in Square POS and the Seller
Dashboard once it is paid, so:
  - When the payment link is paid, we record an EXTERNAL payment (source "Stripe payment
    link") for the order's total, and the order appears for the kitchen.
  - A pay-at-pickup order exists in Square but isn't shown on the POS until it's paid.
    Businesses on Square should require payment before cooking, or keep using our
    dashboard for unpaid orders. (docs/DESIGN.md, section 13.)

Every request uses an idempotency key derived from our order id, so retries never
create duplicates.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import urlencode

from .pos import Connection, PosError, PosLine, PosOrder, cents, http_call

API_VERSION = os.environ.get("SQUARE_API_VERSION", "2025-01-23")
BASES = {"production": "https://connect.squareup.com", "sandbox": "https://connect.squareupsandbox.com"}
SCOPES = ["ORDERS_WRITE", "ORDERS_READ", "ITEMS_READ", "PAYMENTS_WRITE", "MERCHANT_PROFILE_READ"]
SIZE_WORDS = re.compile(r"\b(small|medium|large|regular|x-?large|half|full|\d+\s*(in|inch|oz|pc|piece)s?)\b", re.I)


class SquareProvider:
    name = "square"
    refresh_margin = 7 * 24 * 3600  # access tokens last 30 days

    def __init__(self, application_id: str, application_secret: str, environment: str = "sandbox",
                 http=None):
        import httpx

        self.application_id = application_id
        self.application_secret = application_secret
        self.environment = environment
        self.http = http or httpx.Client(timeout=20)

    @classmethod
    def from_env(cls) -> "SquareProvider":
        return cls(os.environ.get("SQUARE_APPLICATION_ID", ""), os.environ.get("SQUARE_APPLICATION_SECRET", ""),
                   os.environ.get("SQUARE_ENVIRONMENT", "sandbox"))

    # ---- helpers

    def _base(self, environment: str | None = None) -> str:
        return BASES[environment or self.environment]

    def _call(self, conn: Connection | None, method: str, path: str, body: dict | None = None,
              params: dict | None = None, environment: str | None = None) -> dict:
        base = self._base(conn.environment if conn else environment)
        return http_call(self.http, method, base + path, token=conn.access_token if conn else None,
                         headers={"Square-Version": API_VERSION}, json_body=body, params=params,
                         what="Square")

    # ---- connecting

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = {"client_id": self.application_id, "scope": " ".join(SCOPES), "state": state}
        if self.environment == "production":
            params["session"] = "false"  # always show the sign-in page (not supported in sandbox)
        return f"{self._base()}/oauth2/authorize?{urlencode(params)}"

    def _tokens(self, resp: dict) -> dict:
        expires = resp.get("expires_at")
        return {"access_token": resp["access_token"], "refresh_token": resp.get("refresh_token"),
                "expires_at": _parse_time(expires) if expires else None}

    def exchange_code(self, code: str, query: dict, redirect_uri: str) -> tuple[dict, str | None]:
        resp = self._call(None, "POST", "/oauth2/token", {
            "client_id": self.application_id, "client_secret": self.application_secret,
            "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
        })
        return self._tokens(resp), resp.get("merchant_id")

    def refresh(self, conn: Connection) -> dict:
        resp = self._call(None, "POST", "/oauth2/token", {
            "client_id": self.application_id, "client_secret": self.application_secret,
            "grant_type": "refresh_token", "refresh_token": conn.tokens["refresh_token"],
        }, environment=conn.environment)
        tokens = self._tokens(resp)
        tokens["refresh_token"] = tokens["refresh_token"] or conn.tokens["refresh_token"]
        return tokens

    def setup(self, conn: Connection) -> tuple[str | None, dict]:
        locations = [loc for loc in self._call(conn, "GET", "/v2/locations").get("locations", [])
                     if loc.get("status", "ACTIVE") == "ACTIVE"]
        chosen = next((loc for loc in locations if loc["id"] == conn.location_id), None)
        if chosen is None and len(locations) == 1:
            chosen = locations[0]
        settings = {"locations": {loc["id"]: loc.get("name", "") for loc in locations}}
        if chosen:
            settings["currency"] = chosen.get("currency", "USD")
        return (chosen["id"] if chosen else None), settings

    # ---- orders

    def order_body(self, conn: Connection, order: PosOrder) -> dict:
        currency = conn.settings.get("currency", "USD")
        body: dict = {
            "location_id": conn.location_id,
            "reference_id": f"phone-{order.number}",
            "ticket_name": f"Phone #{order.number} {order.customer_name}"[:30],
            "source": {"name": "Phone order"},
            "line_items": [_line(line, currency) for line in order.lines],
            "fulfillments": [{
                "type": "PICKUP",
                "state": "PROPOSED",
                "pickup_details": {
                    "recipient": {"display_name": order.customer_name[:255],
                                  **({"phone_number": order.customer_phone} if order.customer_phone else {})},
                    **({"schedule_type": "SCHEDULED", "pickup_at": order.pickup_at} if order.pickup_at else
                       {"schedule_type": "ASAP", "prep_time_duration": f"PT{max(1, order.pickup_minutes)}M"}),
                    "note": order.note[:500],
                },
            }],
        }
        if order.tax_rate > 0:
            body["taxes"] = [{"uid": "sales-tax", "name": "Sales tax", "scope": "ORDER",
                              "percentage": _percent(order.tax_rate)}]
        return body

    def push(self, conn: Connection, order: PosOrder, existing_id: str | None) -> str:
        if not conn.location_id:
            raise PosError("this Square account has several locations; run "
                           "`python -m phone_agent.pos set-location SLUG LOCATION_ID`", retry=False)
        resp = self._call(conn, "POST", "/v2/orders", {
            "idempotency_key": f"phone-order-{conn.business_id}-{order.order_id}",
            "order": self.order_body(conn, order),
        })
        return resp["order"]["id"]

    def mark_paid(self, conn: Connection, order: PosOrder, pos_order_id: str, payment_ref: str) -> None:
        sq_order = self._call(conn, "GET", f"/v2/orders/{pos_order_id}")["order"]
        self._call(conn, "POST", "/v2/payments", {
            "idempotency_key": f"phone-paid-{conn.business_id}-{order.order_id}",
            "source_id": "EXTERNAL",
            "amount_money": sq_order["total_money"],
            "order_id": pos_order_id,
            "location_id": conn.location_id,
            "external_details": {"type": "CARD", "source": "Stripe payment link",
                                 **({"source_id": payment_ref[:255]} if payment_ref else {})},
            "note": f"Phone order #{order.number}, paid by text link",
        })

    # ---- catalog

    def _catalog_objects(self, conn: Connection) -> list[dict]:
        out, cursor = [], None
        for _ in range(200):  # safety bound on pages
            params = {"types": "ITEM,MODIFIER_LIST,CATEGORY"}
            if cursor:
                params["cursor"] = cursor
            resp = self._call(conn, "GET", "/v2/catalog/list", params=params)
            out.extend(resp.get("objects", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return [o for o in out if not o.get("is_deleted")]

    def catalog(self, conn: Connection) -> list[dict]:
        objs = self._catalog_objects(conn)
        cats = {o["id"]: o.get("category_data", {}).get("name", "") for o in objs if o["type"] == "CATEGORY"}
        mlists = {o["id"]: o.get("modifier_list_data", {}) for o in objs if o["type"] == "MODIFIER_LIST"}
        items = []
        for o in objs:
            if o["type"] != "ITEM" or not _at_location(o, conn.location_id):
                continue
            item = _item(o, cats, mlists, conn.location_id)
            if item:
                items.append(item)
        return items


# ---- building order lines ------------------------------------------------------------------


def _money(amount: float, currency: str) -> dict:
    return {"amount": cents(amount), "currency": currency}


def _percent(rate: float) -> str:
    return f"{rate * 100:.4f}".rstrip("0").rstrip(".")


def _line(line: PosLine, currency: str) -> dict:
    li: dict = {"quantity": str(line.quantity)}
    if line.notes:
        li["note"] = line.notes[:500]
    ids = line.ids
    if not ids.get("item"):
        li["name"] = line.label()[:500]
        li["base_price_money"] = _money(line.unit_price, currency)
        return li
    variation, mods = ids["item"], []
    for opt, choice in line.options.items():
        mapped = (ids.get("choices") or {}).get(opt, {}).get(choice) or {}
        if "variation" in mapped:
            variation = mapped["variation"]
        elif "modifier" in mapped:
            mods.append({"catalog_object_id": mapped["modifier"], "quantity": "1"})
        else:
            mods.append({"name": f"{opt}: {choice}"[:255],
                         "base_price_money": _money(line.option_prices.get(opt, 0.0), currency)})
    for extra, price in line.extras.items():
        mid = (ids.get("extras") or {}).get(extra)
        mods.append({"catalog_object_id": mid, "quantity": "1"} if mid
                    else {"name": extra[:255], "base_price_money": _money(price, currency)})
    li["catalog_object_id"] = variation
    if mods:
        li["modifiers"] = mods
    return li


# ---- reading the catalog ----------------------------------------------------------------------


def _at_location(obj: dict, location_id: str | None) -> bool:
    if not location_id:
        return True
    if location_id in (obj.get("absent_at_location_ids") or []):
        return False
    return obj.get("present_at_all_locations", True) or location_id in (obj.get("present_at_location_ids") or [])


def _item(o: dict, cats: dict, mlists: dict, location_id: str | None) -> dict | None:
    data = o.get("item_data", {})
    if data.get("is_archived"):
        return None
    variations = []
    for v in data.get("variations", []):
        vd = v.get("item_variation_data", {})
        if v.get("is_deleted") or not _at_location(v, location_id):
            continue
        if vd.get("pricing_type", "FIXED_PRICING") != "FIXED_PRICING" or not vd.get("price_money"):
            continue  # variable price: can't be quoted on the phone
        variations.append((v["id"], vd.get("name") or "Regular", vd["price_money"]["amount"] / 100))
    if not variations:
        return None
    variations.sort(key=lambda v: v[2])
    base = variations[0][2]
    cat_ids = [c["id"] for c in data.get("categories") or []] or ([data["category_id"]] if data.get("category_id") else [])
    item: dict = {
        "name": data.get("name", "Item"),
        "category": next((cats[c] for c in cat_ids if cats.get(c)), "Catalog"),
        "price": round(base, 2),
        "description": (data.get("description_plaintext") or data.get("description") or "")[:200],
    }
    options: dict = {}
    pos: dict = {"item": variations[0][0]}
    choices_ids: dict = {}
    extras: dict = {}
    extra_ids: dict = {}
    if len(variations) > 1:
        opt = "size" if all(SIZE_WORDS.search(n) for _, n, _ in variations) else "type"
        options[opt] = {"choices": {n: round(p - base, 2) for _, n, p in variations}, "required": True}
        choices_ids[opt] = {n: {"variation": vid} for vid, n, _ in variations}
    for info in data.get("modifier_list_info") or []:
        if info.get("enabled") is False:
            continue
        ml = mlists.get(info.get("modifier_list_id"))
        if not ml:
            continue
        mods = [(m["id"], m.get("modifier_data", {})) for m in ml.get("modifiers", []) if not m.get("is_deleted")]
        mods = [(mid, md.get("name", ""), (md.get("price_money") or {}).get("amount", 0) / 100)
                for mid, md in mods if md.get("name")]
        if not mods:
            continue
        single = ml.get("selection_type") == "SINGLE" or info.get("max_selected_modifiers") == 1
        if single and len(mods) >= 2:
            name = (ml.get("name") or "choice").lower()
            while name in options:
                name += " 2"
            required = (info.get("min_selected_modifiers") or 0) >= 1
            options[name] = {"choices": {n: round(p, 2) for _, n, p in mods}, "required": required}
            choices_ids[name] = {n: {"modifier": mid} for mid, n, _ in mods}
        else:
            for mid, n, p in mods:
                extras[n] = round(p, 2)
                extra_ids[n] = mid
    if options:
        item["options"] = options
        pos["choices"] = choices_ids
    if extras:
        item["extras"] = extras
        pos["extras"] = extra_ids
    item["pos"] = pos
    item["pos_key"] = o["id"]
    return item


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

