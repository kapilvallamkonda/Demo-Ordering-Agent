"""Read a business's menu or price list from photos, a PDF or a web page into catalog items.

One Claude call (vision + PDF input) with a JSON-schema output, so the result always
parses. The items go into the business's draft for review; nothing is published or
priced without the owner seeing it. Prices that weren't printed are marked
`placeholder_price` so the checks ask the owner to confirm them.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid

from .schedule import DAYS, MAX_NOTICE_DAYS

log = logging.getLogger("phone_agent.catalog_import")

MODEL = os.environ.get("IMPORT_MODEL", "claude-opus-5-5")
MAX_FILES = 10
MAX_TOTAL_BYTES = 20_000_000  # raw; the request limit is 32 MB after base64
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_TYPE = "application/pdf"
TEXT_TYPE = "text/plain"


class ImportError_(Exception):
    """Shown to the owner."""


# Arrays instead of free-form maps: structured outputs need additionalProperties false.
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "price": {"type": "number"},
                    "price_was_printed": {"type": "boolean"},
                    "description": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "required": {"type": "boolean"},
                                "choices": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"},
                                                       "added_price": {"type": "number"}},
                                        "required": ["name", "added_price"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["name", "required", "choices"],
                            "additionalProperties": False,
                        },
                    },
                    "extras": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
                            "required": ["name", "price"],
                            "additionalProperties": False,
                        },
                    },
                    "days": {"type": "array", "items": {"type": "string", "enum": list(DAYS)}},
                    "notice_days": {"type": "integer"},
                    "in_person_only": {"type": "boolean"},
                },
                "required": ["name", "category", "price", "price_was_printed", "description", "options", "extras",
                             "days", "notice_days", "in_person_only"],
                "additionalProperties": False,
            },
        },
        "business": {
            "type": "object",
            "properties": {
                "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "address": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "hours": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["name", "address", "hours"],
            "additionalProperties": False,
        },
        "about": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "faq": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question", "answer"],
                "additionalProperties": False,
            },
        },
        "guides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
                "required": ["title", "text"],
                "additionalProperties": False,
            },
        },
        "left_out": {"type": "array", "items": {"type": "string"}},
        "unreadable": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "business", "about", "faq", "guides", "left_out", "unreadable"],
    "additionalProperties": False,
}

PROMPT = """These are photos, a PDF or the text of a web page showing the {catalog} of a {business} \
called "{name}". Turn everything orderable into items for a phone ordering system. On a web page, \
ignore navigation, links to other locations and anything that isn't the {catalog} itself.

Rules:
- One item per {item}. Copy names exactly as printed. Use the printed section heading as the category.
- Price is the base price in dollars. If something is sold in several versions (sizes, proteins, \
Small/Large, Chicken/Lamb), make ONE item priced at the cheapest version, with a required option \
whose choices carry the extra cost over that price (the cheapest choice adds 0).
- If a section says every item comes with a choice (a sauce, a side, rice or naan), add that as a \
required option on each of those items.
- Paid add-ons ("add cheese +$1") are extras.
- If a price isn't printed or can't be read, estimate a typical price and set price_was_printed to \
false. Never invent items that aren't shown.
- description: short. End it with any of these tags that clearly apply: {tags}
- Ordering rules, only when printed (otherwise leave them empty, 0 or false): days, if an {item} \
or its section is only available on certain days ("weekends only" is sat and sun); notice_days, \
how many days ahead it must be ordered ("cakes need a day's notice" is 1); in_person_only, if it \
can't be ordered for pickup ("dine-in only").
- Leave out daily specials and "market price" items, and list them in left_out.
- business: the name, address and opening hours if they are printed, otherwise null.
- about: one or two plain sentences on what kind of {business} this is and what it sells, the way \
the owner would say it on the phone. No marketing words. Null if you can't tell.
- faq: policies printed on the {catalog} that callers ask about (delivery, catering, allergies, \
custom orders, deposits, what comes with a dish), each as the question a caller would ask and a \
short plain answer. Only what's printed; never invent a policy. Don't repeat rules that are \
already on items (their days, notice or in-person only).
- guides: longer advice printed on the page that a caller might need explained (a buying or size \
guide, what's needed for an occasion or ceremony, how far ahead to order, care instructions), each \
with a short title and its content rewritten as plain sentences the owner would say, at most a few \
hundred words each. Only what's printed; never add advice. Usually empty for a menu.
- unreadable: anything you could see but not read."""


def build_content(files: list[tuple[str, bytes]], prompt: str) -> list[dict]:
    """files: (media_type, data). Raises ImportError_ for unsupported or oversized input."""
    if not files:
        raise ImportError_("Add at least one photo or PDF.")
    if len(files) > MAX_FILES:
        raise ImportError_(f"Upload at most {MAX_FILES} files at a time.")
    if sum(len(d) for _, d in files) > MAX_TOTAL_BYTES:
        raise ImportError_("Those files are too large together (20 MB max). Try fewer or smaller photos.")
    content: list[dict] = []
    for media_type, data in files:
        b64 = base64.standard_b64encode(data).decode()
        if media_type == PDF_TYPE:
            content.append({"type": "document", "source": {"type": "base64", "media_type": PDF_TYPE, "data": b64}})
        elif media_type == TEXT_TYPE:  # a web page's text, from a menu link
            content.append({"type": "document", "title": "Menu web page",
                            "source": {"type": "text", "media_type": TEXT_TYPE, "data": data.decode(errors="replace")}})
        elif media_type in IMAGE_TYPES:
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
        else:
            raise ImportError_("Use photos (JPEG, PNG, WebP) or a PDF.")
    content.append({"type": "text", "text": prompt})
    return content


class Progress:
    """Follows the answer as it streams in, so the owner can watch the menu being read.

    Structured output writes each item's properties in schema order, so every item starts
    with `"name": "...", "category": "..."`; option choices and extras have names but no
    category after them. Only complete strings match, so a chunk ending mid-item is
    picked up on the next one."""

    _ITEM = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"category"\s*:\s*"((?:[^"\\]|\\.)*)"')

    def __init__(self):
        self.text = ""
        self._pos = 0
        self.items = 0
        self.sections: dict[str, int] = {}  # in the order they were read
        self.current: str | None = None
        self.last_item: str | None = None

    def feed(self, delta: str) -> None:
        self.text += delta
        for m in self._ITEM.finditer(self.text, self._pos):
            self._pos = m.end()
            name, category = _unquote(m.group(1)), _unquote(m.group(2))
            self.items += 1
            self.sections[category] = self.sections.get(category, 0) + 1
            self.current, self.last_item = category, name

    def snapshot(self) -> dict:
        return {"items": self.items, "current": self.current, "last_item": self.last_item,
                "sections": [{"name": k, "items": v} for k, v in self.sections.items()]}


def _unquote(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


async def read_catalog(client, files: list[tuple[str, bytes]], template, settings: dict,
                       progress: Progress | None = None, ai_session_id: str | None = None,
                       distinct_id: str | None = None) -> dict:
    """Returns {"items": [...catalog items...], "business": {...}, "about": str, "faq": [{q, a}],
    "left_out": [...], "unreadable": [...]}.
    Pass a Progress to follow along while it reads."""
    words = template.words
    prompt = PROMPT.format(catalog=words["catalog"], business=words["business"], item=words["item"],
                           name=settings.get("name") or "the business",
                           tags=", ".join(t.rstrip(".") for t in template.tags) or "none")
    content = build_content(files, prompt)
    t0 = time.monotonic()
    try:
        async with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            if progress is not None:
                async for event in stream:
                    if event.type == "text":
                        progress.feed(event.text)
            message = await stream.get_final_message()
        _report(getattr(message, "model", MODEL), getattr(message, "usage", None), t0, ai_session_id, distinct_id)
    except Exception as e:  # the SDK's typed errors; the owner just needs to retry
        _report(MODEL, None, t0, ai_session_id, distinct_id, error=True)
        log.exception("catalog import call failed")
        raise ImportError_("Reading the files didn't work this time. Please try again.") from e
    if message.stop_reason == "refusal":
        raise ImportError_("These files couldn't be read. Try clearer photos of the menu or price list.")
    if message.stop_reason == "max_tokens":
        raise ImportError_("That's too much to read at once. Upload a few pages at a time.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportError_("Reading the files didn't work this time. Please try again.") from e
    faq = [{"q": f["question"].strip()[:200], "a": f["answer"].strip()[:600]}
           for f in data.get("faq") or [] if f.get("question", "").strip() and f.get("answer", "").strip()]
    guides = [{"title": g["title"].strip()[:120], "text": g["text"].strip()[:6000]}
              for g in data.get("guides") or [] if g.get("title", "").strip() and g.get("text", "").strip()]
    return {"items": [to_catalog_item(it) for it in data.get("items", []) if it.get("name")],
            "business": data.get("business") or {}, "about": (data.get("about") or "").strip()[:500],
            "faq": faq, "guides": guides, "left_out": data.get("left_out") or [], "unreadable": data.get("unreadable") or []}


def _report(model, usage, t0, session_id, distinct_id, error=False) -> None:
    """The menu reading as a PostHog AI event (tokens and timing only)."""
    from .web_analytics import capture_ai_generation

    capture_ai_generation(model=model, usage=usage, latency_s=time.monotonic() - t0, trace_id=str(uuid.uuid4()),
                          session_id=session_id or "menu-import", span_name="menu_import",
                          distinct_id=distinct_id, error=error)


def to_catalog_item(it: dict) -> dict:
    """One extracted item in our catalog format (id is assigned when it's merged)."""
    out: dict = {"name": it["name"].strip()[:80], "category": (it.get("category") or "Other").strip()[:100],
                 "price": round(max(float(it.get("price") or 0), 0.01), 2),
                 "description": (it.get("description") or "").strip()[:400]}
    options = {}
    for opt in it.get("options") or []:
        choices = {c["name"].strip(): round(max(float(c.get("added_price") or 0), 0), 2)
                   for c in opt.get("choices") or [] if c.get("name", "").strip()}
        name = (opt.get("name") or "choice").strip().lower()
        if len(choices) >= 2 and name not in options:
            options[name] = {"choices": choices, "required": bool(opt.get("required"))}
    if options:
        out["options"] = options
    extras = {e["name"].strip(): round(max(float(e.get("price") or 0), 0), 2)
              for e in it.get("extras") or [] if e.get("name", "").strip()}
    if extras:
        out["extras"] = extras
    if not it.get("price_was_printed", True):
        out["placeholder_price"] = True
    days = [d for d in DAYS if d in set(it.get("days") or ())]
    if days and len(days) < 7:
        out["days"] = days
    notice = it.get("notice_days")
    if isinstance(notice, int) and not isinstance(notice, bool) and notice > 0:
        out["notice_days"] = min(notice, MAX_NOTICE_DAYS)
    if it.get("in_person_only") is True:
        out["in_person_only"] = True
    if any(k in out for k in ("days", "notice_days", "in_person_only")):
        out["rules_from_menu"] = True  # the owner confirms these in the editor
    return out


def slug_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "item"


def merge_items(existing: list[dict], new: list[dict], replace: bool) -> list[dict]:
    """Add the new items with unique ids. With replace, existing items whose name matches
    are updated in place (keeping their id) and the rest are dropped."""
    by_name = {it["name"].strip().lower(): it for it in existing}
    kept = [] if replace else list(existing)
    taken = {it["id"] for it in kept}
    if replace:  # ids of items that will be kept (matched by name) aren't free for new ones
        taken |= {by_name[k]["id"] for k in (it["name"].strip().lower() for it in new) if k in by_name}
    for it in new:
        old = by_name.get(it["name"].strip().lower())
        if old is not None:
            if replace:
                kept.append({**it, "id": old["id"]})
                taken.add(old["id"])
            continue  # appending: the dish is already there
        base, n = slug_id(it["name"]), 2
        iid = base
        while iid in taken:
            iid, n = f"{base}_{n}", n + 1
        taken.add(iid)
        kept.append({"id": iid, **it})
    return kept
