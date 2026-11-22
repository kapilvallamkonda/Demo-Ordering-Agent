"""Order (cart) state and business rules.

All validation lives here, so the model can never put something into an order
that isn't on the menu, priced wrong, or missing a required choice.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .menu import Menu

MAX_QUANTITY = 50


class OrderError(ValueError):
    """A request the order can't satisfy; the message is shown to the model."""


def normalize_phone(raw: str) -> str:
    """Return a phone number in E.164 form (+15551234567), or raise OrderError.

    Bare 10-digit numbers are treated as US/Canada.
    """
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise OrderError(f"'{raw}' doesn't look like a full phone number. Ask for all ten digits.")


@dataclass
class LineItem:
    line_id: int
    item_id: str
    name: str
    quantity: int
    unit_price: float
    options: dict[str, str] = field(default_factory=dict)
    extras: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def total(self) -> float:
        return round(self.unit_price * self.quantity, 2)

    def describe(self) -> str:
        parts = [f"{self.quantity} x {self.name}"]
        if self.options:
            parts.append(", ".join(f"{k}: {v}" for k, v in self.options.items()))
        if self.extras:
            parts.append("with " + ", ".join(self.extras))
        if self.notes:
            parts.append(f"note: {self.notes}")
        return " | ".join(parts)


@dataclass
class Order:
    menu: Menu
    lines: list[LineItem] = field(default_factory=list)
    customer_name: str = ""
    customer_phone: str = ""
    order_type: str = "pickup"
    placed: bool = False
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), repr=False)

    # ---- mutations -------------------------------------------------------

    def add_item(
        self,
        item_id: str,
        quantity: int = 1,
        options: dict[str, str] | None = None,
        extras: list[str] | None = None,
        notes: str = "",
    ) -> LineItem:
        self._ensure_open()
        item = self.menu.get(item_id)
        if item is None:
            raise OrderError(f"Unknown item_id '{item_id}'. Use an item_id from the menu.")
        if not item.available:
            raise OrderError(f"{item.name} is sold out today. Tell the caller and suggest something similar.")
        quantity = _check_quantity(quantity)
        options = dict(options or {})
        extras = list(dict.fromkeys(extras or []))  # dedupe, keep order

        price = item.price
        for name in options:
            if name not in item.options:
                raise OrderError(f"{item.name} has no option '{name}'.")
        resolved: dict[str, str] = {}
        for name, grp in item.options.items():
            choice = options.get(name)
            if choice is None:
                if grp.required:
                    raise OrderError(
                        f"{item.name} needs a '{name}' choice: {', '.join(grp.choices)}. Ask the caller."
                    )
                continue
            if choice not in grp.choices:
                raise OrderError(
                    f"'{choice}' isn't a valid {name} for {item.name}. Choices: {', '.join(grp.choices)}."
                )
            price += grp.choices[choice]
            resolved[name] = choice
        for extra in extras:
            if extra not in item.extras:
                avail = ", ".join(item.extras) or "none"
                raise OrderError(f"'{extra}' isn't available on {item.name}. Available extras: {avail}.")
            price += item.extras[extra]

        line = LineItem(
            line_id=next(self._ids),
            item_id=item.id,
            name=item.name,
            quantity=quantity,
            unit_price=round(price, 2),
            options=resolved,
            extras=extras,
            notes=notes.strip()[:200],
        )
        self.lines.append(line)
        return line

    def remove_item(self, line_id: int) -> LineItem:
        self._ensure_open()
        line = self._find(line_id)
        self.lines.remove(line)
        return line

    def update_quantity(self, line_id: int, quantity: int) -> LineItem | None:
        self._ensure_open()
        line = self._find(line_id)
        if quantity == 0:
            self.lines.remove(line)
            return None
        line.quantity = _check_quantity(quantity)
        return line

    def set_customer(self, name: str, phone: str | None = None) -> None:
        self._ensure_open()
        name = name.strip()
        if not name:
            raise OrderError("Customer name can't be empty.")
        self.customer_name = name[:80]
        if phone:
            self.set_phone(phone)

    def set_phone(self, phone: str) -> None:
        self._ensure_open()
        self.customer_phone = normalize_phone(phone)

    def mark_placed(self) -> None:
        self._ensure_open()
        if not self.lines:
            raise OrderError("The order is empty.")
        if not self.customer_name:
            raise OrderError("Get the caller's name for the order before placing it.")
        self.placed = True

    # ---- reads -----------------------------------------------------------

    @property
    def subtotal(self) -> float:
        return round(sum(l.total for l in self.lines), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.menu.tax_rate, 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.tax, 2)

    def summary(self) -> dict:
        return {
            "lines": [
                {"line_id": l.line_id, "description": l.describe(), "line_total": l.total}
                for l in self.lines
            ],
            "subtotal": self.subtotal,
            "tax": self.tax,
            "total": self.total,
            "customer_name": self.customer_name or None,
            "customer_phone": self.customer_phone or None,
            "order_type": self.order_type,
            "placed": self.placed,
        }

    # ---- helpers ---------------------------------------------------------

    def _find(self, line_id: int) -> LineItem:
        for line in self.lines:
            if line.line_id == line_id:
                return line
        raise OrderError(f"No line with line_id {line_id}. Call view_order to see current lines.")

    def _ensure_open(self) -> None:
        if self.placed:
            raise OrderError("This order has already been placed and can't be changed.")


def _check_quantity(quantity: int) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise OrderError("quantity must be a whole number.")
    if not 1 <= quantity <= MAX_QUANTITY:
        raise OrderError(f"quantity must be between 1 and {MAX_QUANTITY}.")
    return quantity
