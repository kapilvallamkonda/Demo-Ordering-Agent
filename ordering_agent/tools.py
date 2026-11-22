"""Tool definitions the model can call, plus the dispatcher that runs them.

Requests stream with `eager_input_streaming`, so the API doesn't validate tool
inputs for us. Every handler checks its input types itself, and Order enforces
the business rules.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .order import Order, OrderError
from .payments import PaymentService
from .storage import OrderStore

log = logging.getLogger("ordering_agent.tools")


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
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


TOOLS: list[dict] = [
    _tool(
        "add_item",
        "Add a menu item to the caller's order. Only call once the caller has chosen every "
        "required option. If the tool returns an error, ask the caller to resolve it.",
        {
            "item_id": {"type": "string", "description": "item_id exactly as listed in the menu."},
            "quantity": {"type": "integer", "minimum": 1, "description": "How many. Defaults to 1."},
            "options": {
                "type": "object",
                "description": "Option choices keyed by option name, e.g. {\"size\": \"16 inch\"}.",
                "additionalProperties": {"type": "string"},
            },
            "extras": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paid extras exactly as named in the menu, e.g. [\"extra cheese\"].",
            },
            "notes": {
                "type": "string",
                "description": "Free-text kitchen notes, e.g. 'well done', 'no onions'.",
            },
        },
        ["item_id"],
    ),
    _tool(
        "remove_item",
        "Remove a line from the order. Get line_id from view_order or an earlier add_item result.",
        {"line_id": {"type": "integer"}},
        ["line_id"],
    ),
    _tool(
        "update_quantity",
        "Change how many of an existing line the caller wants. Quantity 0 removes the line.",
        {"line_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 0}},
        ["line_id", "quantity"],
    ),
    _tool(
        "view_order",
        "Get the current order: lines with line_ids, subtotal, tax and total.",
        {},
        [],
    ),
    _tool(
        "set_customer_name",
        "Record the name the order is under.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "set_customer_phone",
        "Record the mobile number to text the order details and payment link to. Use when the "
        "caller wants a different number than the one they're calling from, or caller ID is missing.",
        {"phone": {"type": "string", "description": "The number as the caller said it, all digits."}},
        ["phone"],
    ),
    _tool(
        "place_order",
        "Submit the order. Only call after reading the full order and total back to the caller "
        "and hearing them confirm it. If payment links are on, this texts the order details and "
        "a secure payment link to the customer's phone; the result says whether it was sent.",
        {
            "caller_confirmed": {
                "type": "boolean",
                "description": "True only if the caller explicitly confirmed the read-back order.",
            }
        },
        ["caller_confirmed"],
    ),
    _tool(
        "end_call",
        "Hang up after you have said goodbye. Use when the caller is done or asks to end the call.",
        {},
        [],
    ),
]


@dataclass
class CallSession:
    """Everything one call/conversation owns."""

    order: Order
    store: OrderStore | None = None
    call_id: str | None = None
    payments: PaymentService | None = None
    order_number: int | None = None  # per-restaurant number, what the caller hears
    order_id: int | None = None  # internal row id
    end_requested: bool = False
    log: list[dict] = field(default_factory=list)


def _place_order(session: CallSession) -> dict:
    order = session.order
    payments = session.payments if session.store is not None else None
    if payments and not order.customer_phone:
        raise OrderError("Ask for a mobile number to text the order details and payment link to, "
                         "then call set_customer_phone.")
    order.mark_placed()
    menu = order.menu
    required = bool(payments) and menu.payment_required
    if session.store is not None:
        saved = session.store.save(
            order, call_id=session.call_id,
            status="awaiting_payment" if required else "confirmed",
            payment_status="unpaid" if payments else "not_requested",
        )
        session.order_number, session.order_id = saved.number, saved.id
    result: dict = {"placed": True, "order_number": session.order_number, "total": order.total,
                    "ready_in_minutes": menu.restaurant.get("pickup_minutes")}
    if not payments:
        result["payment"] = "pay at pickup"
        return result
    try:
        link = payments.request_payment(order, session.order_number)
    except Exception:
        log.exception("payment link for order %s failed", session.order_number)
        result["payment"] = {
            "link_sent": False,
            "tell_caller": "The text with the payment link couldn't be sent. They can pay at pickup"
            + ("; the kitchen will start the order once they pay at the counter." if required else "."),
        }
        return result
    session.store.set_payment_link(session.order_id, link.id, link.url)
    result["payment"] = {
        "link_sent": True,
        "texted_to_number_ending": order.customer_phone[-4:],
        "required_before_cooking": required,
        "link_valid_minutes": menu.pay_within_minutes,
    }
    if required:
        result["ready_in_minutes"] = f"{menu.restaurant.get('pickup_minutes')} after payment"
    return result


class _BadInput(Exception):
    pass


def _req(args: dict, key: str, typ: type | tuple[type, ...]) -> Any:
    if key not in args:
        raise _BadInput(f"missing required field '{key}'")
    return _opt(args, key, typ, None)


def _opt(args: dict, key: str, typ: type | tuple[type, ...], default: Any) -> Any:
    if key not in args or args[key] is None:
        return default
    val = args[key]
    # bool is a subclass of int; don't accept true/false as a number
    if typ is int and isinstance(val, bool):
        raise _BadInput(f"field '{key}' must be an integer")
    if not isinstance(val, typ):
        raise _BadInput(f"field '{key}' has the wrong type")
    return val


def run_tool(session: CallSession, name: str, args: Any) -> tuple[str, bool]:
    """Execute one tool call. Returns (result_text, is_error). Never raises for bad input."""
    if not isinstance(args, dict):
        return json.dumps({"INVALID_JSON": json.dumps(args)}), True
    order = session.order
    try:
        if name == "add_item":
            options = _opt(args, "options", dict, {})
            extras = _opt(args, "extras", list, [])
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in options.items()):
                raise _BadInput("options must map strings to strings")
            if not all(isinstance(e, str) for e in extras):
                raise _BadInput("extras must be a list of strings")
            line = order.add_item(
                item_id=_req(args, "item_id", str),
                quantity=_opt(args, "quantity", int, 1),
                options=options,
                extras=extras,
                notes=_opt(args, "notes", str, ""),
            )
            result = {"added": line.describe(), "line_id": line.line_id, "line_total": line.total,
                      "order_total": order.total}
        elif name == "remove_item":
            line = order.remove_item(_req(args, "line_id", int))
            result = {"removed": line.describe(), "order_total": order.total}
        elif name == "update_quantity":
            line = order.update_quantity(_req(args, "line_id", int), _req(args, "quantity", int))
            result = {"updated": line.describe() if line else "removed", "order_total": order.total}
        elif name == "view_order":
            result = order.summary()
        elif name == "set_customer_name":
            order.set_customer(_req(args, "name", str))
            result = {"customer_name": order.customer_name}
        elif name == "set_customer_phone":
            order.set_phone(_req(args, "phone", str))
            result = {"customer_phone": order.customer_phone}
        elif name == "place_order":
            if _req(args, "caller_confirmed", bool) is not True:
                raise OrderError("Read the order and total back and get the caller's confirmation first.")
            result = _place_order(session)
        elif name == "end_call":
            session.end_requested = True
            result = {"ok": True}
        else:
            return f"Unknown tool '{name}'.", True
    except _BadInput as e:
        return f"Invalid input: {e}.", True
    except OrderError as e:
        return str(e), True

    session.log.append({"tool": name, "args": args, "result": result})
    return json.dumps(result), False
