"""Which business a call is for, and managing businesses.

Each business has a slug (e.g. "hyderabad-house"), the Twilio number callers dial,
and a current catalog (catalog JSON, including its settings block). A call is routed by the
number that was dialed.

Admin commands (run on the server, e.g. `fly ssh console -C "python -m ..."`):

    python -m phone_agent.businesses list
    python -m phone_agent.businesses templates          (kinds of business; starter files)
    python -m phone_agent.businesses add SLUG --phone +19135551234 --catalog data/businesses/hyderabad-house.json
    python -m phone_agent.businesses set-catalog SLUG --catalog new-catalog.json
    python -m phone_agent.businesses set-phone SLUG --phone +19135551234   (or --none)
    python -m phone_agent.businesses deactivate SLUG   /   activate SLUG
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

from .catalog import Catalog, CatalogError, parse_catalog
from .order import OrderError, normalize_phone
from .storage import Store, BusinessRow

log = logging.getLogger("phone_agent.businesses")

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BusinessDirectory:
    """Looks up businesses and their current catalog. Parsed catalogs are cached by catalog id, so
    each call costs one small query, and a newly published catalog is used on the next call.

    Isolation: a catalog that can't be loaded (say, its template was removed) only takes
    its own business offline. It's logged, reported in listing() as "broken", and every
    other business keeps working."""

    def __init__(self, store: Store):
        self.store = store
        self._catalogs: dict[int, Catalog] = {}
        self._broken: dict[int, str] = {}  # catalog id -> why it couldn't be loaded
        self._lock = threading.Lock()

    def for_number(self, dialed: str | None) -> Catalog | None:
        """The active business whose Twilio number was dialed."""
        if not dialed:
            return None
        try:
            phone = normalize_phone(dialed)
        except OrderError:
            return None
        return self._catalog(self.store.business_by_phone(phone))

    def get(self, business_id: int | None, active_only: bool = False) -> Catalog | None:
        """A business by id. Inactive ones too unless active_only (webhooks for old
        orders still need their business)."""
        if business_id is None:
            return None
        return self._catalog(self.store.business(business_id), require_active=active_only)

    def by_slug(self, slug: str | None) -> Catalog | None:
        if not slug:
            return None
        return self._catalog(self.store.business_by_slug(slug), require_active=False)

    def _catalog(self, row: BusinessRow | None, require_active: bool = True) -> Catalog | None:
        if row is None or row.catalog_id is None or (require_active and not row.active):
            return None
        return self._load(row, row.catalog_id)

    def _load(self, row: BusinessRow, catalog_id: int) -> Catalog | None:
        with self._lock:
            catalog = self._catalogs.get(catalog_id)
            if catalog is None and catalog_id in self._broken:
                return None
        if catalog is None:
            payload = self.store.catalog_version_payload(row.id, catalog_id)
            if payload is None:
                return None
            try:
                catalog = parse_catalog(payload, business_id=row.id, slug=row.slug, catalog_id=catalog_id)
            except CatalogError as e:
                log.error("business %s: catalog version %s can't be loaded, so it's offline: %s",
                          row.slug, catalog_id, e)
                with self._lock:
                    self._broken[catalog_id] = str(e)
                return None
            with self._lock:
                self._catalogs[catalog_id] = catalog
        return catalog

    def problem(self, row: BusinessRow) -> str | None:
        """Why this business can't take calls, or None."""
        if row.catalog_id is None:
            return "no catalog"
        if self._load(row, row.catalog_id) is None:
            with self._lock:
                return self._broken.get(row.catalog_id, "catalog missing")
        return None

    def catalog_version(self, business_id: int, catalog_id: int | None) -> Catalog | None:
        """The catalog an old order was taken from (or the current one if that's unknown)."""
        row = self.store.business(business_id)
        if row is None:
            return None
        if catalog_id is None or catalog_id == row.catalog_id:
            return self._catalog(row, require_active=False)
        # Only this business's own versions (the id comes from its order).
        return self._load(row, catalog_id) or self._catalog(row, require_active=False)

    def listing(self) -> list[dict]:
        out = []
        for row in self.store.businesses():
            catalog = self._catalog(row, require_active=False)
            problem = self.problem(row)
            out.append({"id": row.id, "slug": row.slug, "phone_number": row.phone_number,
                        "active": row.active, "status": "broken" if problem else "ok", "problem": problem,
                        "name": catalog.settings.get("name") if catalog else None,
                        "template": catalog.template_id if catalog else None,
                        "catalog_items": len(catalog.items) if catalog else 0})
        return out


# ---- admin commands ------------------------------------------------------------------


def _read_catalog(path: str) -> dict:
    raw = json.loads(Path(path).read_text())
    parse_catalog(raw)  # validate before saving
    return raw


def _phone(value: str) -> str:
    try:
        return normalize_phone(value)
    except OrderError as e:
        raise SystemExit(f"bad phone number {value!r}: {e}")


def main(argv: list[str] | None = None, store: Store | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m phone_agent.businesses")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("templates")
    add = sub.add_parser("add")
    add.add_argument("slug")
    add.add_argument("--catalog", required=True)
    add.add_argument("--phone")
    sm = sub.add_parser("set-catalog")
    sm.add_argument("slug")
    sm.add_argument("--catalog", required=True)
    sp = sub.add_parser("set-phone")
    sp.add_argument("slug")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--phone")
    g.add_argument("--none", action="store_true")
    for name in ("activate", "deactivate"):
        sub.add_parser(name).add_argument("slug")
    args = ap.parse_args(argv)

    store = store or Store(os.environ.get("DATABASE_PATH", "data/phone_agent.db"))
    directory = BusinessDirectory(store)

    def existing(slug: str) -> BusinessRow:
        row = store.business_by_slug(slug)
        if row is None:
            raise SystemExit(f"no business {slug!r}")
        return row

    try:
        if args.cmd == "list":
            for r in directory.listing():
                print(f"{r['id']:>3}  {r['slug']:<24} {r['phone_number'] or '-':<14} "
                      f"{'active' if r['active'] else 'inactive':<9} {r['template'] or '-':<11} "
                      f"{r['name']} ({r['catalog_items']} items)"
                      + (f"  BROKEN: {r['problem']}" if r["problem"] else ""))
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
            if store.business_by_slug(args.slug):
                raise SystemExit(f"business {args.slug!r} already exists")
            phone = _phone(args.phone) if args.phone else None
            if phone and store.business_by_phone(phone):
                raise SystemExit(f"{phone} already belongs to another business")
            rid = store.add_business(args.slug, phone, _read_catalog(args.catalog))
            print(f"added business {rid} ({args.slug})")
        elif args.cmd == "set-catalog":
            catalog_id = store.set_catalog(existing(args.slug).id, _read_catalog(args.catalog))
            print(f"published catalog version {catalog_id} for {args.slug}")
        elif args.cmd == "set-phone":
            row = existing(args.slug)
            if args.none:
                store.update_business(row.id, clear_phone=True)
            else:
                phone = _phone(args.phone)
                other = store.business_by_phone(phone)
                if other and other.id != row.id:
                    raise SystemExit(f"{phone} already belongs to {other.slug}")
                store.update_business(row.id, phone_number=phone)
            print("updated")
        elif args.cmd in ("activate", "deactivate"):
            store.update_business(existing(args.slug).id, active=args.cmd == "activate")
            print(f"{args.slug} {args.cmd}d")
    except (CatalogError, json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"catalog not saved: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
