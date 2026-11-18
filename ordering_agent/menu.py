"""Menu loading and lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MENU_PATH = Path(__file__).resolve().parent.parent / "data" / "menu.json"


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


@dataclass(frozen=True)
class Menu:
    restaurant: dict
    items: dict[str, MenuItem]

    @property
    def tax_rate(self) -> float:
        return float(self.restaurant.get("tax_rate", 0.0))

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


def load_menu(path: str | Path = DEFAULT_MENU_PATH) -> Menu:
    raw = json.loads(Path(path).read_text())
    items: dict[str, MenuItem] = {}
    for it in raw["items"]:
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
        )
    return Menu(restaurant=raw["restaurant"], items=items)
