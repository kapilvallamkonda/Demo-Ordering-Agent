"""Clover: send orders with the v3 Orders API, import the menu from inventory.

An order is created open (it shows in the Clover Orders app right away), with a title
like "Phone #12 Ana", a note saying how it's being paid, and its total. Lines are added
in one bulk call, one Clover line per unit (that's how Clover counts quantity), then any
modifiers. Items imported from Clover are added as inventory items (Clover applies their
own prices and tax rates); anything else is added as a custom item with our price and
the merchant's default tax rate.

Clover has no idempotency keys. If an attempt fails after the order was created, the
next attempt deletes that partial order and starts again.

When the payment link is paid we prefix the order note with "PAID ONLINE". (Recording a
real Clover payment needs a custom tender; see docs/DESIGN.md section 13.)
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlencode

from .pos import Connection, PosError, PosOrder, cents, http_call

# Clover North America. (Europe and Latin America use other hosts; add them when needed.)
API = {"production": "https://api.clover.com", "sandbox": "https://apisandbox.dev.clover.com"}
WEB = {"production": "https://www.clover.com", "sandbox": "https://sandbox.dev.clover.com"}
PICKUP_TYPE = re.compile(r"pick ?up|to ?go|take ?out|carry ?out|phone", re.I)
RATE_SCALE = 10_000_000  # Clover tax rates: 10% is 1000000


class CloverProvider:
    name = "clover"
    refresh_margin = 300  # access tokens are short-lived; refresh when within 5 minutes

    def __init__(self, app_id: str, app_secret: str, environment: str = "sandbox", http=None):
        import httpx

        self.app_id = app_id
        self.app_secret = app_secret
        self.environment = environment
        self.http = http or httpx.Client(timeout=20)

    @classmethod
    def from_env(cls) -> "CloverProvider":
        return cls(os.environ.get("CLOVER_APP_ID", ""), os.environ.get("CLOVER_APP_SECRET", ""),
                   os.environ.get("CLOVER_ENVIRONMENT", "sandbox"))

    def _call(self, conn: Connection, method: str, path: str, body: dict | None = None,
              params: dict | None = None) -> dict:
        url = f"{API[conn.environment]}/v3/merchants/{conn.merchant_id}{path}"
        return http_call(self.http, method, url, token=conn.access_token, json_body=body,
                         params=params, what="Clover")

    def _paged(self, conn: Connection, path: str, params: dict) -> list[dict]:
        out, offset = [], 0
        for _ in range(100):
            page = self._call(conn, "GET", path, params={**params, "limit": 1000, "offset": offset})
            elements = page.get("elements", [])
            out.extend(elements)
            if len(elements) < 1000:
                break
            offset += 1000
        return out

    # ---- connecting

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        q = urlencode({"client_id": self.app_id, "redirect_uri": redirect_uri, "state": state})
        return f"{WEB[self.environment]}/oauth/v2/authorize?{q}"

    @staticmethod
    def _tokens(resp: dict) -> dict:
        return {"access_token": resp["access_token"], "refresh_token": resp.get("refresh_token"),
                "expires_at": resp.get("access_token_expiration"),
                "refresh_expires_at": resp.get("refresh_token_expiration")}

    def exchange_code(self, code: str, query: dict, redirect_uri: str) -> tuple[dict, str | None]:
        resp = http_call(self.http, "POST", f"{API[self.environment]}/oauth/v2/token",
                         json_body={"client_id": self.app_id, "client_secret": self.app_secret, "code": code},
                         what="Clover")
        return self._tokens(resp), query.get("merchant_id")

    def refresh(self, conn: Connection) -> dict:
        resp = http_call(self.http, "POST", f"{API[conn.environment]}/oauth/v2/refresh",
                         json_body={"client_id": self.app_id, "refresh_token": conn.tokens["refresh_token"]},
                         what="Clover")
        return self._tokens(resp)

    def setup(self, conn: Connection) -> tuple[str | None, dict]:
        if not conn.merchant_id:
            raise PosError("Clover didn't say which merchant was connected", retry=False)
        settings: dict = {}
        types = self._call(conn, "GET", "/order_types").get("elements", [])
        pickup = next((t for t in types if PICKUP_TYPE.search(t.get("label", ""))), None)
        if pickup:
            settings["order_type_id"] = pickup["id"]
        rates = self._call(conn, "GET", "/tax_rates").get("elements", [])
        default = next((r for r in rates if r.get("isDefault")), None)
        if default:
            settings["tax_rate"] = {"id": default["id"], "name": default.get("name", "Tax"),
                                    "rate": default.get("rate", 0)}
        return None, settings

    # ---- orders

    def line_items(self, conn: Connection, order: PosOrder) -> tuple[list[dict], list[list[str]]]:
        """Clover bulk line items (one per unit), and the modifier ids for each."""
        tax = conn.settings.get("tax_rate")
        items, mods = [], []
        for line in order.lines:
            ids = line.ids
            unmapped, line_mods = [], []
            for opt, choice in line.options.items():
                mid = ((ids.get("choices") or {}).get(opt, {}).get(choice) or {}).get("modifier")
                (line_mods.append(mid) if mid and ids.get("item") else unmapped.append(f"{opt}: {choice}"))
            for extra in line.extras:
                mid = (ids.get("extras") or {}).get(extra)
                (line_mods.append(mid) if mid and ids.get("item") else unmapped.append(extra))
            if ids.get("item"):
                entry: dict = {"item": {"id": ids["item"]}}
                note = "; ".join(x for x in (", ".join(unmapped), line.notes) if x)
            else:
                entry = {"name": line.label()[:127], "price": cents(line.unit_price)}
                if tax:
                    entry["taxRates"] = [tax]
                note = line.notes
                line_mods = []
            if note:
                entry["note"] = note[:255]
            for _ in range(line.quantity):
                items.append(dict(entry))
                mods.append(line_mods)
        return items, mods

    def push(self, conn: Connection, order: PosOrder, existing_id: str | None) -> str:
        if existing_id:
            try:
                self._call(conn, "DELETE", f"/orders/{existing_id}")
            except PosError as e:
                if "404" not in str(e):
                    raise
        body: dict = {"state": "open", "title": f"Phone #{order.number} {order.customer_name}"[:127],
                      "note": order.note, "currency": "USD", "total": cents(order.total)}
        if conn.settings.get("order_type_id"):
            body["orderType"] = {"id": conn.settings["order_type_id"]}
        created = self._call(conn, "POST", "/orders", body)
        oid = created["id"]
        try:
            items, mods = self.line_items(conn, order)
            resp = self._call(conn, "POST", f"/orders/{oid}/bulk_line_items", {"items": items})
            made = resp if isinstance(resp, list) else resp.get("elements") or resp.get("items") or []
            for li, line_mods in zip(made, mods):
                for mid in line_mods:
                    self._call(conn, "POST", f"/orders/{oid}/line_items/{li['id']}/modifications",
                               {"modifier": {"id": mid}})
        except PosError as e:
            raise PosError(str(e), retry=e.retry, partial_id=oid) from e
        except Exception as e:
            raise PosError(f"{type(e).__name__}: {e}", partial_id=oid) from e
        return oid

    def mark_paid(self, conn: Connection, order: PosOrder, pos_order_id: str, payment_ref: str) -> None:
        self._call(conn, "POST", f"/orders/{pos_order_id}",
                   {"note": f"PAID ONLINE (payment link). Phone order #{order.number} for {order.customer_name}."})

    # ---- menu

    def catalog(self, conn: Connection) -> list[dict]:
        groups = {g["id"]: g for g in self._paged(conn, "/modifier_groups", {"expand": "modifiers"})}
        items = []
        for it in self._paged(conn, "/items", {"expand": "categories,modifierGroups"}):
            if it.get("hidden") or it.get("deleted") or it.get("available") is False:
                continue
            if it.get("priceType", "FIXED") != "FIXED" or not it.get("price"):
                continue  # variable or per-unit price: can't be quoted on the phone
            cats = (it.get("categories") or {}).get("elements") or []
            entry: dict = {"name": it.get("name", "Item"),
                           "category": cats[0].get("name", "Menu") if cats else "Menu",
                           "price": round(it["price"] / 100, 2),
                           "description": (it.get("alternateName") or "")[:200]}
            options, extras, choice_ids, extra_ids = {}, {}, {}, {}
            for ref in (it.get("modifierGroups") or {}).get("elements") or []:
                g = groups.get(ref["id"], ref)
                mods = [(m["id"], m.get("name", ""), (m.get("price") or 0) / 100)
                        for m in (g.get("modifiers") or {}).get("elements") or [] if m.get("name")]
                if not mods:
                    continue
                if g.get("maxAllowed") == 1 and len(mods) >= 2:
                    name = (g.get("name") or "choice").lower()
                    while name in options:
                        name += " 2"
                    options[name] = {"choices": {n: round(p, 2) for _, n, p in mods},
                                     "required": (g.get("minRequired") or 0) >= 1}
                    choice_ids[name] = {n: {"modifier": mid} for mid, n, _ in mods}
                else:
                    for mid, n, p in mods:
                        extras[n] = round(p, 2)
                        extra_ids[n] = mid
            pos: dict = {"item": it["id"]}
            if options:
                entry["options"] = options
                pos["choices"] = choice_ids
            if extras:
                entry["extras"] = extras
                pos["extras"] = extra_ids
            entry["pos"] = pos
            entry["pos_key"] = it["id"]
            items.append(entry)
        return items
