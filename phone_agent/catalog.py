"""Catalog loading and lookup.

A Catalog is one business's catalog plus its settings (the "settings" block: name, tax,
pickup time, payment rule, notes). Catalogs come from the database for live calls (see
businesses.py) or from a JSON file for tests and the terminal chat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .templates import DEFAULT as DEFAULT_TEMPLATE
from .templates import Template, get_template

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "businesses" / "hyderabad-house.json"


@dataclass(frozen=True)
class OptionGroup:
    name: str
    choices: dict[str, float]  # choice -> price delta
    required: bool = False


@dataclass(frozen=True)
class CatalogItem:
    id: str
    name: str
    category: str
    price: float
    description: str = ""
    options: dict[str, OptionGroup] = field(default_factory=dict)
    extras: dict[str, float] = field(default_factory=dict)
    # Point-of-sale ids, written by a catalog import (see pos.py): {"item": id,
    # "choices": {option: {choice: {"variation"|"modifier": id}}}, "extras": {extra: id}}
    pos: dict = field(default_factory=dict)
    available: bool = True  # False: sold out / off the catalog for now (staff toggle it in /manage)


@dataclass(frozen=True)
class Catalog:
    settings: dict  # the "settings" block: name, tax, pickup time, payment rule, notes ...
    items: dict[str, CatalogItem]
    business_id: int | None = None  # set when the catalog belongs to a business in the database
    slug: str | None = None
    catalog_id: int | None = None  # the catalog version in the database
    pos_provider: str | None = None  # the POS the item "pos" ids came from ("square", "clover")
    template_id: str = DEFAULT_TEMPLATE  # the kind of business (templates.py)

    @property
    def template(self) -> Template:
        return get_template(self.template_id)

    @property
    def tax_rate(self) -> float:
        return float(self.settings.get("tax_rate", 0.0))

    @property
    def payment_required(self) -> bool:
        """Business rule: the order is only cooked after the payment link is paid."""
        return bool(self.settings.get("payment", {}).get("required_before_cooking", False))

    @property
    def pay_within_minutes(self) -> int:
        """How long the payment link stays valid. Stripe allows 30 minutes to 24 hours."""
        minutes = int(self.settings.get("payment", {}).get("pay_within_minutes", 30))
        return max(30, min(minutes, 24 * 60))

    def get(self, item_id: str) -> CatalogItem | None:
        return self.items.get(item_id)

    def to_prompt_text(self) -> str:
        """Render the catalog as compact plain text for the system prompt.

        Output is deterministic (stable ordering) so it stays prompt-cache friendly.
        """
        r = self.settings
        words = self.template.words
        unset = "not available on this line yet; say you don't have that information"
        lines = [
            f"{words['business'].capitalize()}: {r.get('name')}",
            f"Address: {r.get('address') or unset}",
            f"Hours: {r.get('hours') or unset}",
            f"Order types: {', '.join(r.get('order_types', []))}",
            f"Typical pickup time: {r.get('pickup_minutes')} minutes",
        ]
        notes = r.get("notes") or []
        if notes:
            lines.append("Notes:")
            lines.extend(f"- {n}" for n in notes)
        lines += [
            "",
            f"{words['catalog'].upper()} (item_id | name | base price | description)",
        ]
        by_cat: dict[str, list[CatalogItem]] = {}
        for item in self.items.values():
            by_cat.setdefault(item.category, []).append(item)
        for cat, items in by_cat.items():
            lines.append(f"\n## {cat}")
            for it in items:
                sold_out = "" if it.available else f" | {words['unavailable'].upper()}: can't be ordered"
                lines.append(f"- {it.id} | {it.name} | ${it.price:.2f} | {it.description}{sold_out}")
                for grp in it.options.values():
                    choices = ", ".join(
                        f"{c}" + (f" (+${d:.2f})" if d else "") for c, d in grp.choices.items()
                    )
                    req = "required" if grp.required else "optional"
                    lines.append(f"    option '{grp.name}' ({req}): {choices}")
                if it.extras:
                    extras = ", ".join(f"{e} (+${p:.2f})" for e, p in it.extras.items())
                    lines.append(f"    extras: {extras}")
        return "\n".join(lines)


class CatalogError(ValueError):
    pass


def load_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> Catalog:
    return parse_catalog(json.loads(Path(path).read_text()))


def parse_catalog(raw: dict, business_id: int | None = None, slug: str | None = None,
               catalog_id: int | None = None) -> Catalog:
    """Build a Catalog from catalog JSON. Raises CatalogError if it isn't usable."""
    try:
        return _parse(raw, business_id, slug, catalog_id)
    except CatalogError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise CatalogError(f"invalid catalog: {e!r}") from e


def _parse(raw: dict, business_id: int | None, slug: str | None, catalog_id: int | None) -> Catalog:
    if not isinstance(raw.get("settings"), dict) or not raw["settings"].get("name"):
        raise CatalogError("catalog needs a business block with a name")
    items: dict[str, CatalogItem] = {}
    for it in raw["items"]:
        if it["id"] in items:
            raise CatalogError(f"duplicate item id {it['id']!r}")
        options = {
            name: OptionGroup(name=name, choices=dict(g["choices"]), required=bool(g.get("required")))
            for name, g in it.get("options", {}).items()
        }
        items[it["id"]] = CatalogItem(
            id=it["id"],
            name=it["name"],
            category=it["category"],
            price=float(it["price"]),
            description=it.get("description", ""),
            options=options,
            extras=dict(it.get("extras", {})),
            pos=dict(it.get("pos") or {}),
            available=it.get("available", True) is not False,
        )
    if not items:
        raise CatalogError("catalog has no items")
    template_id = raw.get("template") or DEFAULT_TEMPLATE
    try:
        template = get_template(template_id)
    except KeyError:
        raise CatalogError(f"unknown template {template_id!r}")
    if template.status != "available":
        raise CatalogError(f"the {template.label} template is coming soon "
                        f"(needs: {', '.join(template.modules)}); it can't be used yet")
    pos = raw.get("pos") or {}
    return Catalog(settings=raw["settings"], items=items, business_id=business_id, slug=slug,
                catalog_id=catalog_id, pos_provider=pos.get("provider") if isinstance(pos, dict) else None,
                template_id=template_id)
