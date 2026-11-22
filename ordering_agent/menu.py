"""Menu loading and lookup.

A Menu is one restaurant's menu plus its settings (the "restaurant" block: name, tax,
pickup time, payment rule, notes). Menus come from the database for live calls (see
restaurants.py) or from a JSON file for tests and the terminal chat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MENU_PATH = Path(__file__).resolve().parent.parent / "data" / "restaurants" / "hyderabad-house.json"


@dataclass(frozen=True)
class OptionGroup:
    name: str
    choices: dict[str, float]  # choice -> price delta
    required: bool = False


@dataclass(frozen=True)
class MenuItem:
    id: str
    name: str
    category: str
    price: float
    description: str = ""
    options: dict[str, OptionGroup] = field(default_factory=dict)
    extras: dict[str, float] = field(default_factory=dict)
    # Point-of-sale ids, written by a menu import (see pos.py): {"item": id,
    # "choices": {option: {choice: {"variation"|"modifier": id}}}, "extras": {extra: id}}
    pos: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Menu:
    restaurant: dict
    items: dict[str, MenuItem]
    restaurant_id: int | None = None  # set when the menu belongs to a restaurant in the database
    slug: str | None = None
    menu_id: int | None = None  # the menu version in the database
    pos_provider: str | None = None  # the POS the item "pos" ids came from ("square", "clover")

    @property
    def tax_rate(self) -> float:
        return float(self.restaurant.get("tax_rate", 0.0))

    @property
    def payment_required(self) -> bool:
        """Restaurant rule: the order is only cooked after the payment link is paid."""
        return bool(self.restaurant.get("payment", {}).get("required_before_cooking", False))

    @property
    def pay_within_minutes(self) -> int:
        """How long the payment link stays valid. Stripe allows 30 minutes to 24 hours."""
        minutes = int(self.restaurant.get("payment", {}).get("pay_within_minutes", 30))
        return max(30, min(minutes, 24 * 60))

    def get(self, item_id: str) -> MenuItem | None:
        return self.items.get(item_id)

    def to_prompt_text(self) -> str:
        """Render the menu as compact plain text for the system prompt.

        Output is deterministic (stable ordering) so it stays prompt-cache friendly.
        """
        r = self.restaurant
        lines = [
            f"Restaurant: {r.get('name')}",
            f"Address: {r.get('address')}",
            f"Hours: {r.get('hours')}",
            f"Order types: {', '.join(r.get('order_types', []))}",
            f"Typical pickup time: {r.get('pickup_minutes')} minutes",
        ]
        notes = r.get("notes") or []
        if notes:
            lines.append("Notes:")
            lines.extend(f"- {n}" for n in notes)
        lines += [
            "",
            "MENU (item_id | name | base price | description)",
        ]
        by_cat: dict[str, list[MenuItem]] = {}
        for item in self.items.values():
            by_cat.setdefault(item.category, []).append(item)
        for cat, items in by_cat.items():
            lines.append(f"\n## {cat}")
            for it in items:
                lines.append(f"- {it.id} | {it.name} | ${it.price:.2f} | {it.description}")
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


class MenuError(ValueError):
    pass


def load_menu(path: str | Path = DEFAULT_MENU_PATH) -> Menu:
    return parse_menu(json.loads(Path(path).read_text()))


def parse_menu(raw: dict, restaurant_id: int | None = None, slug: str | None = None,
               menu_id: int | None = None) -> Menu:
    """Build a Menu from menu JSON. Raises MenuError if it isn't usable."""
    try:
        return _parse(raw, restaurant_id, slug, menu_id)
    except MenuError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise MenuError(f"invalid menu: {e!r}") from e


def _parse(raw: dict, restaurant_id: int | None, slug: str | None, menu_id: int | None) -> Menu:
    if not isinstance(raw.get("restaurant"), dict) or not raw["restaurant"].get("name"):
        raise MenuError("menu needs a restaurant block with a name")
    items: dict[str, MenuItem] = {}
    for it in raw["items"]:
        if it["id"] in items:
            raise MenuError(f"duplicate item id {it['id']!r}")
        options = {
            name: OptionGroup(name=name, choices=dict(g["choices"]), required=bool(g.get("required")))
            for name, g in it.get("options", {}).items()
        }
        items[it["id"]] = MenuItem(
            id=it["id"],
            name=it["name"],
            category=it["category"],
            price=float(it["price"]),
            description=it.get("description", ""),
            options=options,
            extras=dict(it.get("extras", {})),
            pos=dict(it.get("pos") or {}),
        )
    if not items:
        raise MenuError("menu has no items")
    pos = raw.get("pos") or {}
    return Menu(restaurant=raw["restaurant"], items=items, restaurant_id=restaurant_id, slug=slug,
                menu_id=menu_id, pos_provider=pos.get("provider") if isinstance(pos, dict) else None)
