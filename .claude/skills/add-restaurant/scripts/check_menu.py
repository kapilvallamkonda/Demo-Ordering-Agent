"""Check a restaurant menu file before it goes live.

    .venv/bin/python .claude/skills/add-restaurant/scripts/check_menu.py data/restaurants/<slug>.json

Loads the file with the app's own menu parser, then reports:
  ERROR    the app can't use it (bad structure, duplicate ids, bad prices or options)
  BLOCKER  callers would hear wrong or missing information (placeholder prices, settings
           listed in restaurant.placeholders, no tax rate)
  WARNING  worth confirming with the restaurant (hours/address not set, no notes)

Exit code: 0 = no errors or blockers, 1 = blockers only, 2 = errors.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from ordering_agent.menu import MenuError, parse_menu  # noqa: E402
from ordering_agent.prompts import build_greeting  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ID_RE = re.compile(r"^[a-z0-9_]+$")
NOT_SET = "not available on this line yet"


def check(path: Path) -> int:
    errors: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR    can't read {path}: {e}")
        return 2
    try:
        menu = parse_menu(raw)
    except MenuError as e:
        print(f"ERROR    {e}")
        return 2

    if not SLUG_RE.match(path.stem):
        errors.append(f"file name {path.name!r} should be the slug, e.g. hyderabad-house.json")

    r = menu.restaurant
    if not isinstance(r.get("tax_rate"), (int, float)) or r.get("tax_rate", 0) <= 0:
        blockers.append("restaurant.tax_rate missing or 0: every total would be wrong")
    elif r["tax_rate"] > 0.15:
        errors.append(f"restaurant.tax_rate {r['tax_rate']} looks like a percent; use a fraction like 0.08")
    if not isinstance(r.get("pickup_minutes"), int) or r["pickup_minutes"] <= 0:
        blockers.append("restaurant.pickup_minutes missing: the agent can't say when it's ready")
    if r.get("order_types") != ["pickup"]:
        errors.append("restaurant.order_types must be [\"pickup\"] (delivery isn't supported)")
    pay = r.get("payment")
    if not isinstance(pay, dict) or not isinstance(pay.get("required_before_cooking"), bool):
        blockers.append("restaurant.payment.required_before_cooking not set (true or false)")
    for key in ("hours", "address"):
        if not r.get(key) or NOT_SET in str(r.get(key)):
            warnings.append(f"restaurant.{key} not set: the agent will say it doesn't have it")
    if not r.get("notes"):
        warnings.append("restaurant.notes empty: add cuisine, spice and allergy guidance")

    unconfirmed = r.get("placeholders") or []
    if unconfirmed:
        blockers.append("restaurant settings not confirmed by the restaurant: " + ", ".join(unconfirmed))

    placeholders = [it for it in raw["items"] if it.get("placeholder_price")]
    if placeholders:
        blockers.append(f"{len(placeholders)} items have placeholder prices: "
                        + ", ".join(it["id"] for it in placeholders))

    for it in raw["items"]:
        iid = it["id"]
        if not ID_RE.match(iid):
            errors.append(f"item id {iid!r}: use lowercase letters, digits and underscores")
        if float(it["price"]) <= 0:
            errors.append(f"{iid}: price must be more than 0")
        for name, grp in (it.get("options") or {}).items():
            choices = grp.get("choices") or {}
            if len(choices) < 2:
                errors.append(f"{iid}: option {name!r} needs at least 2 choices")
            if any(float(v) < 0 for v in choices.values()):
                errors.append(f"{iid}: option {name!r} has a negative price delta; "
                              "set the base price to the cheapest choice")
            if grp.get("required") and min((float(v) for v in choices.values()), default=0) != 0:
                warnings.append(f"{iid}: required option {name!r} has no $0 choice; "
                                "the base price should be the cheapest version")
        for extra, price in (it.get("extras") or {}).items():
            if float(price) < 0:
                errors.append(f"{iid}: extra {extra!r} has a negative price")

    names = Counter(it["name"].lower() for it in raw["items"])
    for name, n in names.items():
        if n > 1:
            warnings.append(f"{n} items are named {name!r}; make the names distinct so callers can tell them apart")

    cats = Counter(it.category for it in menu.items.values())
    print(f"{r['name']}: {len(menu.items)} items in {len(cats)} categories")
    for cat, n in cats.items():
        print(f"  {n:>3}  {cat}")
    print(f"Greeting: \"{build_greeting(menu)}\"")
    print(f"Tax {r.get('tax_rate')}, pickup {r.get('pickup_minutes')} min, "
          f"pay before cooking: {(pay or {}).get('required_before_cooking')}")
    print()
    for label, msgs in (("ERROR", errors), ("BLOCKER", blockers), ("WARNING", warnings)):
        for m in msgs:
            print(f"{label:<8} {m}")
    if not (errors or blockers or warnings):
        print("OK       nothing to fix")
    return 2 if errors else 1 if blockers else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(check(Path(sys.argv[1])))
