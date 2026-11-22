"""Checks a catalog (catalog JSON) before it goes live. Used by the /manage editor on every
save and publish, and by the add-business skill's check_catalog.py.

  errors    the app can't use it, or a value is out of range: publishing is refused
  blockers  callers would hear wrong or guessed information (placeholder prices,
            settings not confirmed, no tax rate): allowed, but shown prominently
  warnings  worth a look (hours/address not set, duplicate names, ...)
"""

from __future__ import annotations

import re
from collections import Counter

from .catalog import CatalogError, parse_catalog
from .prompts import build_greeting
from .settings_schema import settings_for, validate_settings
from .templates import get_template

ID_RE = re.compile(r"^[a-z0-9_]+$")
NOT_SET = "not available on this line yet"
MAX_ITEMS = 1500
MAX_PRICE = 1000.0
LIMITS = {"name": 80, "category": 100, "description": 400}


def check_catalog(raw: dict) -> dict:
    errors: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []
    result = {"errors": errors, "blockers": blockers, "warnings": warnings, "summary": None}

    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        errors.append("catalog must have a business block and a list of items")
        return result
    try:
        template = get_template(raw.get("template"))
    except KeyError:
        template = None  # parse_catalog reports it
    errors.extend(validate_settings(raw.get("settings"), template))
    by_path = {f["path"]: f for f in settings_for(template)}
    try:
        catalog = parse_catalog(raw)
    except CatalogError as e:
        errors.append(str(e))
        return result

    r = catalog.settings
    if len(raw["items"]) > MAX_ITEMS:
        errors.append(f"at most {MAX_ITEMS} items")
    if isinstance(r.get("tax_rate"), (int, float)) and r["tax_rate"] <= 0:  # missing is an error already
        blockers.append("Sales tax is 0: every total would leave tax out")
    if r.get("order_types", ["pickup"]) != ["pickup"]:
        errors.append("order_types must be [\"pickup\"] (delivery isn't supported)")
    pay = r.get("payment")
    if not isinstance(pay, dict) or not isinstance(pay.get("required_before_cooking"), bool):
        blockers.append("\"Wait for payment before cooking\" isn't set")
    for key in ("hours", "address"):
        if not r.get(key) or NOT_SET in str(r.get(key)):
            warnings.append(f"{by_path[key]['label']} not set: the agent will say it doesn't have it")
    if not r.get("notes"):
        warnings.append("No notes for the phone agent: add cuisine, spice and allergy guidance")

    unconfirmed = r.get("placeholders") or []
    if unconfirmed:
        names = [by_path[p]["label"] if p in by_path else p for p in unconfirmed]
        blockers.append("Settings estimated, not yet confirmed: " + ", ".join(names))
    estimated = [it for it in raw["items"] if it.get("placeholder_price")]
    if estimated:
        blockers.append(f"{len(estimated)} items have estimated prices: "
                        + ", ".join(it["name"] for it in estimated[:12])
                        + (" ..." if len(estimated) > 12 else ""))

    for it in raw["items"]:
        iid = it["id"]
        label = it.get("name") or iid
        if not isinstance(iid, str) or not ID_RE.match(iid):
            errors.append(f"item id {iid!r}: use lowercase letters, digits and underscores")
        for key, limit in LIMITS.items():
            value = it.get(key, "")
            if not isinstance(value, str) or (key != "description" and not value.strip()):
                errors.append(f"{label}: {key} is required" if key != "description" else f"{label}: bad description")
            elif len(value) > limit:
                errors.append(f"{label}: {key} is longer than {limit} characters")
        price = it.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price <= MAX_PRICE:
            errors.append(f"{label}: price must be more than $0 and at most ${MAX_PRICE:.0f}")
        if "available" in it and not isinstance(it["available"], bool):
            errors.append(f"{label}: available must be true or false")
        for name, grp in (it.get("options") or {}).items():
            choices = grp.get("choices") or {}
            if not str(name).strip():
                errors.append(f"{label}: an option has no name")
            if len(choices) < 2:
                errors.append(f"{label}: option {name!r} needs at least 2 choices")
            if any(not str(c).strip() for c in choices):
                errors.append(f"{label}: option {name!r} has a blank choice")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= MAX_PRICE
                   for v in choices.values()):
                errors.append(f"{label}: option {name!r} prices must be $0 or more "
                              "(set the base price to the cheapest choice)")
            elif grp.get("required") and choices and min(choices.values()) != 0:
                warnings.append(f"{label}: required option {name!r} has no $0 choice; "
                                "the base price should be the cheapest version")
        for extra, xp in (it.get("extras") or {}).items():
            if not str(extra).strip():
                errors.append(f"{label}: an extra has no name")
            if isinstance(xp, bool) or not isinstance(xp, (int, float)) or not 0 <= xp <= MAX_PRICE:
                errors.append(f"{label}: extra {extra!r} price must be $0 or more")

    names = Counter(it["name"].strip().lower() for it in raw["items"] if isinstance(it.get("name"), str))
    for name, n in names.items():
        if n > 1:
            warnings.append(f"{n} items are named {name!r}; make the names distinct so callers can tell them apart")
    sold_out = [it["name"] for it in raw["items"] if it.get("available") is False]
    if sold_out:
        warnings.append(f"{len(sold_out)} items marked {catalog.template.words['unavailable_short'].lower()}: " + ", ".join(sold_out[:12])
                        + (" ..." if len(sold_out) > 12 else ""))

    cats = Counter(it.category for it in catalog.items.values())
    result["summary"] = {
        "name": r.get("name"), "template": catalog.template_id, "items": len(catalog.items),
        "categories": dict(cats),
        "greeting": build_greeting(catalog), "tax_rate": r.get("tax_rate"),
        "pickup_minutes": r.get("pickup_minutes"),
        "pay_before_cooking": (pay or {}).get("required_before_cooking"),
    }
    return result
