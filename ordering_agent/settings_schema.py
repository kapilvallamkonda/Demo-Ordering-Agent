"""The restaurant settings staff can edit, as data.

The /manage page builds its settings form from SETTINGS, and the server validates saved
menus against it, so a new setting is one entry here (plus whatever code reads it).
Each field lives at `path` inside the menu's "restaurant" block ("payment.pay_within_minutes"
is restaurant["payment"]["pay_within_minutes"]). Keys not listed here (order_types,
placeholders) are kept as they are.

Field types:
  text, textarea  string, max_length
  integer         whole number, min/max
  percent         stored as a fraction (0.0825), shown and entered as a percent (8.25)
  boolean         true/false
  string_list     list of short strings, max_items / max_length each
"""

from __future__ import annotations

from typing import Any

SETTINGS: list[dict] = [
    {"path": "name", "group": "Basics", "type": "text", "label": "Restaurant name", "required": True,
     "max_length": 80, "help": "Said in the greeting and in texts to customers."},
    {"path": "greeting", "group": "Basics", "type": "text", "label": "Greeting", "max_length": 300,
     "help": "The first thing callers hear. Leave blank for: \"Thanks for calling <name>! "
             "What can I get started for you today?\""},
    {"path": "address", "group": "Basics", "type": "text", "label": "Address", "max_length": 200,
     "help": "Read to callers who ask where to pick up."},
    {"path": "hours", "group": "Basics", "type": "textarea", "label": "Opening hours", "max_length": 400,
     "help": "As you'd say them, e.g. \"Mon-Thu 11am-10pm, Fri-Sun 11am-11pm\"."},
    {"path": "pickup_minutes", "group": "Orders", "type": "integer", "label": "Typical pickup time (minutes)",
     "min": 5, "max": 180, "required": True, "help": "How long until an order is usually ready."},
    {"path": "tax_rate", "group": "Orders", "type": "percent", "label": "Sales tax", "min": 0, "max": 0.15,
     "required": True, "help": "Added to every order. Enter the percent, e.g. 8.25."},
    {"path": "payment.required_before_cooking", "group": "Payment", "type": "boolean",
     "label": "Wait for payment before cooking",
     "help": "On: the kitchen gets the order once the texted payment link is paid. "
             "Off: customers can pay at pickup."},
    {"path": "payment.pay_within_minutes", "group": "Payment", "type": "integer",
     "label": "Payment link valid for (minutes)", "min": 30, "max": 1440,
     "help": "Between 30 minutes and 24 hours."},
    {"path": "notes", "group": "Instructions for the phone agent", "type": "string_list",
     "label": "Notes", "max_items": 30, "max_length": 400,
     "help": "Anything the agent should know: cuisine, how spice levels work, allergy guidance, "
             "what's only sold in person. One note per line."},
]

BY_PATH = {f["path"]: f for f in SETTINGS}


def get_path(block: dict, path: str) -> Any:
    cur: Any = block
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def validate_settings(restaurant: Any) -> list[str]:
    """Problems with the restaurant block, as messages naming the field."""
    if not isinstance(restaurant, dict):
        return ["restaurant settings are missing"]
    errors = []
    for f in SETTINGS:
        value = get_path(restaurant, f["path"])
        label = f["label"]
        if value is None or value == "":
            if f.get("required"):
                errors.append(f"{label} is required")
            continue
        t = f["type"]
        if t in ("text", "textarea"):
            if not isinstance(value, str):
                errors.append(f"{label} must be text")
            elif len(value) > f.get("max_length", 1000):
                errors.append(f"{label} is longer than {f['max_length']} characters")
        elif t in ("integer", "percent"):
            ok_type = isinstance(value, int) if t == "integer" else isinstance(value, (int, float))
            if isinstance(value, bool) or not ok_type:
                errors.append(f"{label} must be a {'whole ' if t == 'integer' else ''}number")
            elif not f.get("min", float("-inf")) <= value <= f.get("max", float("inf")):
                lo, hi = f.get("min"), f.get("max")
                if t == "percent":
                    lo, hi = f"{lo * 100:g}%", f"{hi * 100:g}%"
                errors.append(f"{label} must be between {lo} and {hi}")
        elif t == "boolean":
            if not isinstance(value, bool):
                errors.append(f"{label} must be on or off")
        elif t == "string_list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f"{label} must be a list of text")
            elif len(value) > f.get("max_items", 100):
                errors.append(f"{label}: at most {f['max_items']}")
            elif any(len(v) > f.get("max_length", 1000) for v in value):
                errors.append(f"{label}: each one at most {f['max_length']} characters")
    placeholders = restaurant.get("placeholders", [])
    if not isinstance(placeholders, list) or not all(isinstance(p, str) for p in placeholders):
        errors.append("placeholders must be a list of setting names")
    return errors
