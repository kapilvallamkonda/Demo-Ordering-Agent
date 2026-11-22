"""Which restaurant a call is for, and managing restaurants.

Each restaurant has a slug (e.g. "hyderabad-house"), the Twilio number callers dial,
and a current menu (menu JSON, including its settings block). A call is routed by the
number that was dialed.

Admin commands (run on the server, e.g. `fly ssh console -C "python -m ..."`):

    python -m ordering_agent.restaurants list
    python -m ordering_agent.restaurants templates          (kinds of business; starter files)
    python -m ordering_agent.restaurants add SLUG --phone +19135551234 --menu data/restaurants/hyderabad-house.json
    python -m ordering_agent.restaurants set-menu SLUG --menu new-menu.json
    python -m ordering_agent.restaurants set-phone SLUG --phone +19135551234   (or --none)
    python -m ordering_agent.restaurants deactivate SLUG   /   activate SLUG
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

from .menu import Menu, MenuError, parse_menu
from .order import OrderError, normalize_phone
from .storage import OrderStore, RestaurantRow

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class RestaurantDirectory:
    """Looks up restaurants and their current menu. Parsed menus are cached by menu id, so
    each call costs one small query, and a newly published menu is used on the next call."""

    def __init__(self, store: OrderStore):
        self.store = store
        self._menus: dict[int, Menu] = {}
        self._lock = threading.Lock()

    def for_number(self, dialed: str | None) -> Menu | None:
        """The active restaurant whose Twilio number was dialed."""
        if not dialed:
            return None
        try:
            phone = normalize_phone(dialed)
        except OrderError:
            return None
        return self._menu(self.store.restaurant_by_phone(phone))

    def get(self, restaurant_id: int | None, active_only: bool = False) -> Menu | None:
        """A restaurant by id. Inactive ones too unless active_only (webhooks for old
        orders still need their restaurant)."""
        if restaurant_id is None:
            return None
        return self._menu(self.store.restaurant(restaurant_id), require_active=active_only)

    def by_slug(self, slug: str | None) -> Menu | None:
        if not slug:
            return None
        return self._menu(self.store.restaurant_by_slug(slug), require_active=False)

    def _menu(self, row: RestaurantRow | None, require_active: bool = True) -> Menu | None:
        if row is None or row.menu_id is None or (require_active and not row.active):
            return None
        with self._lock:
            menu = self._menus.get(row.menu_id)
        if menu is None:
            payload = self.store.menu_payload(row.menu_id)
            if payload is None:
                return None
            menu = parse_menu(payload, restaurant_id=row.id, slug=row.slug, menu_id=row.menu_id)
            with self._lock:
                self._menus[row.menu_id] = menu
        return menu

    def menu_version(self, restaurant_id: int, menu_id: int | None) -> Menu | None:
        """The menu an old order was taken from (or the current one if that's unknown)."""
        row = self.store.restaurant(restaurant_id)
        if row is None:
            return None
        if menu_id is None or menu_id == row.menu_id:
            return self._menu(row, require_active=False)
        with self._lock:
            menu = self._menus.get(menu_id)
        if menu is None:
            payload = self.store.menu_payload(menu_id)
            if payload is None:
                return self._menu(row, require_active=False)
            menu = parse_menu(payload, restaurant_id=row.id, slug=row.slug, menu_id=menu_id)
            with self._lock:
                self._menus[menu_id] = menu
        return menu

    def listing(self) -> list[dict]:
        out = []
        for row in self.store.restaurants():
            menu = self._menu(row, require_active=False)
            out.append({"id": row.id, "slug": row.slug, "phone_number": row.phone_number,
                        "active": row.active, "name": menu.restaurant.get("name") if menu else None,
                        "template": menu.template_id if menu else None,
                        "menu_items": len(menu.items) if menu else 0})
        return out


# ---- admin commands ------------------------------------------------------------------


def _read_menu(path: str) -> dict:
    raw = json.loads(Path(path).read_text())
    parse_menu(raw)  # validate before saving
    return raw


def _phone(value: str) -> str:
    try:
        return normalize_phone(value)
    except OrderError as e:
        raise SystemExit(f"bad phone number {value!r}: {e}")


def main(argv: list[str] | None = None, store: OrderStore | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ordering_agent.restaurants")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("templates")
    add = sub.add_parser("add")
    add.add_argument("slug")
    add.add_argument("--menu", required=True)
    add.add_argument("--phone")
    add.add_argument("--adopt-existing", action="store_true",
                     help="assign orders and calls saved before restaurants existed to this restaurant")
    sm = sub.add_parser("set-menu")
    sm.add_argument("slug")
    sm.add_argument("--menu", required=True)
    sp = sub.add_parser("set-phone")
    sp.add_argument("slug")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--phone")
    g.add_argument("--none", action="store_true")
    for name in ("activate", "deactivate"):
        sub.add_parser(name).add_argument("slug")
    args = ap.parse_args(argv)

    store = store or OrderStore(os.environ.get("ORDERS_DB", "data/orders.db"))
    directory = RestaurantDirectory(store)

    def existing(slug: str) -> RestaurantRow:
        row = store.restaurant_by_slug(slug)
        if row is None:
            raise SystemExit(f"no restaurant {slug!r}")
        return row

    try:
        if args.cmd == "list":
            for r in directory.listing():
                print(f"{r['id']:>3}  {r['slug']:<24} {r['phone_number'] or '-':<14} "
                      f"{'active' if r['active'] else 'inactive':<9} {r['template'] or '-':<11} "
                      f"{r['name']} ({r['menu_items']} items)")
        elif args.cmd == "templates":
            from .templates import TEMPLATES

            starters = Path(__file__).resolve().parent.parent / "data" / "templates"
            for t in TEMPLATES.values():
                starter = starters / f"{t.id}.json"
                print(f"{t.id:<11} {t.status:<12} {t.label}: {t.description}"
                      + (f"\n{'':<24} starter: {starter.relative_to(starters.parent.parent)}" if starter.exists() else ""))
        elif args.cmd == "add":
            if not SLUG_RE.match(args.slug):
                raise SystemExit("slug must be lowercase letters, digits and dashes, e.g. hyderabad-house")
            if store.restaurant_by_slug(args.slug):
                raise SystemExit(f"restaurant {args.slug!r} already exists")
            phone = _phone(args.phone) if args.phone else None
            if phone and store.restaurant_by_phone(phone):
                raise SystemExit(f"{phone} already belongs to another restaurant")
            rid = store.add_restaurant(args.slug, phone, _read_menu(args.menu))
            print(f"added restaurant {rid} ({args.slug})")
            if args.adopt_existing:
                orders, calls = store.adopt_unassigned(rid)
                print(f"assigned {orders} existing orders and {calls} calls to it")
        elif args.cmd == "set-menu":
            menu_id = store.set_menu(existing(args.slug).id, _read_menu(args.menu))
            print(f"published menu version {menu_id} for {args.slug}")
        elif args.cmd == "set-phone":
            row = existing(args.slug)
            if args.none:
                store.update_restaurant(row.id, clear_phone=True)
            else:
                phone = _phone(args.phone)
                other = store.restaurant_by_phone(phone)
                if other and other.id != row.id:
                    raise SystemExit(f"{phone} already belongs to {other.slug}")
                store.update_restaurant(row.id, phone_number=phone)
            print("updated")
        elif args.cmd in ("activate", "deactivate"):
            store.update_restaurant(existing(args.slug).id, active=args.cmd == "activate")
            print(f"{args.slug} {args.cmd}d")
    except (MenuError, json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"menu not saved: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
