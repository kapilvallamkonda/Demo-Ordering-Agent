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
                },
                "required": ["name", "category", "price", "price_was_printed", "description", "options", "extras"],
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
        "notes": {"type": "array", "items": {"type": "string"}},
        "unreadable": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "business", "notes", "unreadable"],
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
- Leave out daily specials and "market price" items; mention them in notes instead.
- business: the name, address and opening hours if they are printed, otherwise null.
- notes: short facts a phone agent should know (cuisine, what's included, policies printed on the \
{catalog}). unreadable: anything you could see but not read."""


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
                       progress: Progress | None = None) -> dict:
    """Returns {"items": [...catalog items...], "business": {...}, "notes": [...], "unreadable": [...]}.
    Pass a Progress to follow along while it reads."""
    words = template.words
    prompt = PROMPT.format(catalog=words["catalog"], business=words["business"], item=words["item"],
                           name=settings.get("name") or "the business",
                           tags=", ".join(t.rstrip(".") for t in template.tags) or "none")
    content = build_content(files, prompt)
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
    except Exception as e:  # the SDK's typed errors; the owner just needs to retry
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
    return {"items": [to_catalog_item(it) for it in data.get("items", []) if it.get("name")],
            "business": data.get("business") or {}, "notes": data.get("notes") or [],
            "unreadable": data.get("unreadable") or []}


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
