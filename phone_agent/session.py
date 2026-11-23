"""What every call has, whatever the business: the call session, the tool-schema helper,
input checks for tool handlers, and ToolError.

Modules (phone_agent/modules/) build on this. Requests stream with
`eager_input_streaming`, so the API doesn't validate tool inputs for us: every handler
checks its input types with req()/opt(), and the module's own code enforces its rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .catalog import Catalog
    from .order import Order
    from .payments import PaymentService
    from .pos import PosHub
    from .storage import Store


class ToolError(ValueError):
    """Something the caller has to resolve (unknown item, missing choice ...). The message
    is returned to the model as the tool's error result, so it can ask the caller."""


class BadInput(ToolError):
    """The model sent a tool input of the wrong shape."""


def tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """A tool schema for the Messages API."""
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def req(args: dict, key: str, typ: type | tuple[type, ...]) -> Any:
    if key not in args:
        raise BadInput(f"missing required field '{key}'")
    return opt(args, key, typ, None)


def opt(args: dict, key: str, typ: type | tuple[type, ...], default: Any) -> Any:
    if key not in args or args[key] is None:
        return default
    val = args[key]
    # bool is a subclass of int; don't accept true/false as a number
    if typ is int and isinstance(val, bool):
        raise BadInput(f"field '{key}' must be an integer")
    if not isinstance(val, typ):
        raise BadInput(f"field '{key}' has the wrong type")
    return val


@dataclass
class CallSession:
    """Everything one call owns. Shared services (store, payments, POS) are passed in;
    each module keeps its own per-call state here (the orders module: `order`)."""

    catalog: "Catalog | None" = None
    store: "Store | None" = None
    call_id: str | None = None
    caller_phone: str | None = None  # caller ID in E.164, if Twilio gave one
    payments: "PaymentService | None" = None
    pos: "PosHub | None" = None
    end_requested: bool = False
    language: str = "en-US"  # the call's language; switch_language changes it (languages.py)
    log: list[dict] = field(default_factory=list)
    # orders module state
    order: "Order | None" = None
    order_number: int | None = None  # per-business number, what the caller hears
    order_id: int | None = None  # internal row id

    def __post_init__(self) -> None:
        if self.catalog is None and self.order is not None:
            self.catalog = self.order.catalog
