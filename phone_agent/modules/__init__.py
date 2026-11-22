"""Modules: what an agent can do on a call.

A template (templates.py) lists the modules its kind of business uses. Each module
brings, in one place:

  tools    the tool schemas Claude can call for it
  handle   handle(session, name, args) -> result dict; raises session.ToolError (or a
           subclass) for anything the caller must resolve. Business rules live in the
           module's own code, never in the prompt.
  prompt   prompt(catalog, payment_links) -> its section of the system prompt
  start    start(session) -> sets up its per-call state on the CallSession

Built: "orders" (cart, pickup, payment links, POS). Coming soon: "bookings" (services,
staff, availability, appointments). A module with no handler isn't built: templates that
need it are "coming soon" and can't be used.

To add a module: write phone_agent/modules/<name>.py exposing MODULE, add it to
MODULES below, give it a template (templates.py), and add its storage, manager pages
and dashboard views. Tool names must be unique across modules (checked at import).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import orders


@dataclass(frozen=True)
class Module:
    name: str
    description: str
    tools: tuple[dict, ...] = ()
    handle: Callable | None = None
    prompt: Callable | None = None
    start: Callable | None = None

    @property
    def built(self) -> bool:
        return self.handle is not None

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t["name"] for t in self.tools)


ORDERS = Module(
    name="orders",
    description="Take an order from the catalog for pickup, with payment by texted link "
                "or at pickup, and send it to the business's POS.",
    tools=tuple(orders.TOOLS),
    handle=orders.handle,
    prompt=orders.prompt,
    start=orders.start,
)

BOOKINGS = Module(
    name="bookings",
    description="Book appointments for services with a length and a staff member. Coming soon.",
)

MODULES: dict[str, Module] = {m.name: m for m in (ORDERS, BOOKINGS)}
BUILT = frozenset(name for name, m in MODULES.items() if m.built)


def _check_unique_tools() -> None:
    seen: dict[str, str] = {}
    for m in MODULES.values():
        for name in m.tool_names:
            if name in seen:
                raise RuntimeError(f"tool {name!r} is defined by both {seen[name]} and {m.name}")
            seen[name] = m.name


_check_unique_tools()


def modules_for(names) -> list[Module]:
    return [MODULES[n] for n in names]
