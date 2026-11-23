"""The business settings staff can edit, as data.

The /manage page builds its settings form from these fields, and the server validates
saved catalogs against them, so a new setting is one entry here (plus whatever code
reads it). SETTINGS are shared by every kind of business; a template (templates.py) can
change a field's label or help (`setting_overrides`) and add its own (`extra_settings`):
use settings_for(template).
Each field lives at `path` inside the catalog's "settings" block ("payment.pay_within_minutes"
is business["payment"]["pay_within_minutes"]). Keys not listed here (order_types,
placeholders) are kept as they are.

Field types:
  text, textarea  string, max_length
  integer         whole number, min/max
  percent         stored as a fraction (0.0825), shown and entered as a percent (8.25)
  boolean         true/false
  string_list     list of short strings, max_items / max_length each
  select          one of `options` ([value, label] pairs)
  timezone        an IANA time zone name; `options` are the usual ones
  languages       list of language codes callers can switch to (languages.py), shown as checkboxes
  faq             list of {"q": question, "a": answer}, max_items; optional "from_menu": true
                  marks one the menu reader drafted, until the owner edits or confirms it
  guides          list of {"title", "text"}, max_items, max_length per text; "from_menu" as for faq
  phone           a phone number (US 10 digits or E.164)

A field with "hide_when_empty" is only shown in the form when it has a value.
"""

from __future__ import annotations

from typing import Any

from .schedule import TIMEZONES, valid_timezone

SETTINGS: list[dict] = [
    {"path": "name", "group": "Basics", "type": "text", "label": "Business name", "required": True,
     "max_length": 80, "help": "Said in the greeting and in texts to customers."},
    {"path": "greeting", "group": "Basics", "type": "text", "label": "Greeting", "max_length": 300,
     "help": "The first thing callers hear. Leave blank for: \"Thanks for calling <name>! "
             "What can I get started for you today?\""},
    {"path": "address", "group": "Basics", "type": "text", "label": "Address", "max_length": 200,
     "help": "Read to callers who ask where to pick up."},
    {"path": "hours", "group": "Basics", "type": "textarea", "label": "Opening hours", "max_length": 400,
     "help": "As you'd say them, e.g. \"Mon-Thu 11am-10pm, Fri-Sun 11am-11pm\"."},
    {"path": "timezone", "group": "Basics", "type": "timezone", "label": "Time zone",
     "options": [list(t) for t in TIMEZONES],
     "help": "Used for pickup days and which days items are available. Set from your browser when "
             "you signed up."},
    {"path": "languages", "group": "Basics", "type": "languages", "label": "Other languages",
     "help": "The greeting asks callers whether they'd like English or one of these, and they "
             "can switch any time by asking. English is always on."},
    {"path": "pickup_minutes", "group": "Orders", "type": "integer", "label": "Typical pickup time (minutes)",
     "min": 5, "max": 180, "required": True, "help": "How long until an order is usually ready."},
    {"path": "max_advance_days", "group": "Orders", "type": "integer",
     "label": "Furthest pickup date (days ahead)", "min": 1, "max": 365,
     "help": "How far ahead callers can order for. Leave blank for 30 days."},
    {"path": "tax_rate", "group": "Orders", "type": "percent", "label": "Sales tax", "min": 0, "max": 0.15,
     "required": True, "help": "Added to every order. Enter the percent, e.g. 8.25."},
    {"path": "payment.required_before_cooking", "group": "Payment", "type": "boolean",
     "label": "Wait for payment before preparing orders",
     "help": "On: you get the order once the texted payment link is paid. "
             "Off: customers can pay at pickup."},
    {"path": "payment.pay_within_minutes", "group": "Payment", "type": "integer",
     "label": "Payment link valid for (minutes)", "min": 30, "max": 1440,
     "help": "Between 30 minutes and 24 hours."},
    {"path": "about", "group": "What your agent knows", "type": "textarea", "label": "About your business",
     "max_length": 500, "help": "One or two sentences the agent uses to describe you."},
    {"path": "faq", "group": "What your agent knows", "type": "faq", "label": "Common questions",
     "max_items": 40, "max_length": 600,
     "help": "What callers ask that isn't about one item, and what you tell them. The agent uses "
             "these answers; for anything else it says it doesn't know."},
    {"path": "guides", "group": "What your agent knows", "type": "guides", "label": "Guides",
     "max_items": 30, "max_length": 6000,
     "help": "Longer write-ups the agent reads when a caller needs advice: what's needed for a "
             "ceremony, sizes and which to choose, how far ahead to order. Write them as you'd "
             "explain it to a customer."},
    {"path": "notify_phone", "group": "Requests", "type": "phone", "label": "Text new requests to",
     "help": "A mobile number that gets a text when a caller leaves a custom request (a quote, a "
             "large order, something not in your catalog). You'll also see them under Requests."},
]

BY_PATH = {f["path"]: f for f in SETTINGS}


def _with_options(fields: list[dict]) -> list[dict]:
    from .languages import available, default_codes

    for f in fields:
        if f["type"] == "languages":
            f["options"] = [[lang.code, f"{lang.label} ({lang.native})"] for lang in available().values()]
            f["default"] = default_codes()  # shown ticked until the business chooses
    return fields


def settings_for(template=None) -> list[dict]:
    """The settings form for a kind of business (templates.Template, or None for the base)."""
    if template is None:
        return _with_options([dict(f) for f in SETTINGS])
    fields = [{**f, **template.setting_overrides.get(f["path"], {})} for f in SETTINGS]
    for f in fields:
        if f["path"] == "greeting":  # show this template's default greeting
            f["help"] = ("The first thing callers hear. Leave blank for: "
                         f"\"{template.greeting.format(name='<name>')}\"")
    return _with_options(fields + [dict(f) for f in template.extra_settings])


def get_path(block: dict, path: str) -> Any:
    cur: Any = block
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def validate_settings(business: Any, template=None) -> list[str]:
    """Problems with the settings block, as messages naming the field."""
    if not isinstance(business, dict):
        return ["business settings are missing"]
    errors = []
    for f in settings_for(template):
        value = get_path(business, f["path"])
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
        elif t in ("select", "timezone"):
            ok = valid_timezone(value) if t == "timezone" and isinstance(value, str) else \
                value in [o[0] for o in f.get("options", [])]
            if not ok:
                errors.append(f"{label}: choose one of the options")
        elif t == "languages":
            from .languages import available

            if not isinstance(value, list) or any(v not in available() for v in value):
                errors.append(f"{label}: choose from {', '.join(lang.label for lang in available().values())}")
        elif t == "faq":
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                errors.append(f"{label} must be a list of questions and answers")
            elif len(value) > f.get("max_items", 100):
                errors.append(f"{label}: at most {f['max_items']}")
            else:
                for v in value:
                    q, a = v.get("q", ""), v.get("a", "")
                    if not isinstance(q, str) or not isinstance(a, str):
                        errors.append(f"{label}: questions and answers must be text")
                    elif len(q) > 200 or len(a) > f.get("max_length", 1000):
                        errors.append(f"{label}: \"{q[:40]}\" is too long")
                    elif not isinstance(v.get("from_menu", False), bool):
                        errors.append(f"{label}: from_menu must be true or false")
                    else:
                        continue
                    break
        elif t == "guides":
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                errors.append(f"{label} must be a list of titles and texts")
            elif len(value) > f.get("max_items", 100):
                errors.append(f"{label}: at most {f['max_items']}")
            else:
                for v in value:
                    title, text = v.get("title", ""), v.get("text", "")
                    if not isinstance(title, str) or not isinstance(text, str):
                        errors.append(f"{label}: titles and texts must be text")
                    elif len(title) > 120 or len(text) > f.get("max_length", 6000):
                        errors.append(f"{label}: \"{title[:40]}\" is too long")
                    elif not isinstance(v.get("from_menu", False), bool):
                        errors.append(f"{label}: from_menu must be true or false")
                    else:
                        continue
                    break
        elif t == "phone":
            from .order import OrderError, normalize_phone

            try:
                if not isinstance(value, str):
                    raise OrderError("not text")
                normalize_phone(value)
            except OrderError:
                errors.append(f"{label}: enter a mobile number, e.g. 214 555 0123")
        elif t == "string_list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f"{label} must be a list of text")
            elif len(value) > f.get("max_items", 100):
                errors.append(f"{label}: at most {f['max_items']}")
            elif any(len(v) > f.get("max_length", 1000) for v in value):
                errors.append(f"{label}: each one at most {f['max_length']} characters")
    placeholders = business.get("placeholders", [])
    if not isinstance(placeholders, list) or not all(isinstance(p, str) for p in placeholders):
        errors.append("placeholders must be a list of setting names")
    return errors
