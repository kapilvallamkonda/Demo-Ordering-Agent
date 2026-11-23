"""The orders module: take an order from the catalog for pickup.

Tools for the cart (add/remove/change items, name, phone) and place_order, which saves
the order, texts the payment link (payments.py) and queues it for the business's POS
(pos.py). Every rule about what can be ordered and what it costs is in order.py.
"""

from __future__ import annotations

import logging

from ..order import Order, OrderError, normalize_phone
from ..session import BadInput, CallSession, opt, req, tool

log = logging.getLogger("phone_agent.modules.orders")

TOOLS: list[dict] = [
    tool(
        "add_item",
        "Add an item to the caller's order. Only call once the caller has chosen every "
        "required option. If the tool returns an error, ask the caller to resolve it.",
        {
            "item_id": {"type": "string", "description": "item_id exactly as listed."},
            "quantity": {"type": "integer", "minimum": 1, "description": "How many. Defaults to 1."},
            "options": {
                "type": "object",
                "description": "Option choices keyed by option name, e.g. {\"size\": \"16 inch\"}.",
                "additionalProperties": {"type": "string"},
            },
            "extras": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paid extras exactly as listed, e.g. [\"extra cheese\"].",
            },
            "notes": {
                "type": "string",
                "description": "Free-text notes for preparing this item, e.g. 'well done', 'no onions', "
                "'gift wrap'.",
            },
        },
        ["item_id"],
    ),
    tool(
        "remove_item",
        "Remove a line from the order. Get line_id from view_order or an earlier add_item result.",
        {"line_id": {"type": "integer"}},
        ["line_id"],
    ),
    tool(
        "update_quantity",
        "Change how many of an existing line the caller wants. Quantity 0 removes the line.",
        {"line_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 0}},
        ["line_id", "quantity"],
    ),
    tool(
        "view_order",
        "Get the current order: lines with line_ids, subtotal, tax and total.",
        {},
        [],
    ),
    tool(
        "set_customer_name",
        "Record the name the order is under.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    tool(
        "set_customer_phone",
        "Record the mobile number to text the order details and payment link to. Use when the "
        "caller wants a different number than the one they're calling from, or caller ID is missing.",
        {"phone": {"type": "string", "description": "The number as the caller said it, all digits."}},
        ["phone"],
    ),
    tool(
        "set_pickup_time",
        "Schedule the pickup for a later day or time when the caller asks for one, in the "
        "business's local time. Leave out date to go back to as soon as possible. Refused if it's "
        "too soon or something on the order can't be picked up then; tell the caller why.",
        {
            "date": {"type": "string", "description": "Pickup day, YYYY-MM-DD."},
            "time": {"type": "string", "description": "Pickup time, HH:MM in 24-hour time."},
        },
        [],
    ),
    tool(
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
]


def start(session: CallSession) -> None:
    """A new call: an empty order, with the caller ID as the phone for the text."""
    session.order = Order(catalog=session.catalog)
    if session.caller_phone:
        try:
            session.order.customer_phone = normalize_phone(session.caller_phone)
        except OrderError:
            pass  # hidden or odd caller ID; the agent asks for a number if it needs one


def _place_order(session: CallSession) -> dict:
    order = session.order
    payments = session.payments if session.store is not None else None
    if payments and not order.customer_phone:
        raise OrderError("Ask for a mobile number to text the order details and payment link to, "
                         "then call set_customer_phone.")
    order.mark_placed()
    catalog = order.catalog
    required = bool(payments) and catalog.payment_required
    if session.store is not None:
        saved = session.store.save(
            order, call_id=session.call_id,
            status="awaiting_payment" if required else "confirmed",
            payment_status="unpaid" if payments else "not_requested",
        )
        session.order_number, session.order_id = saved.number, saved.id
        if session.pos is not None:
            try:
                session.pos.order_placed(saved.id, catalog.business_id)  # in the background
            except Exception:
                log.exception("couldn't queue order %s for the POS", saved.number)
    result: dict = {"placed": True, "order_number": session.order_number, "total": order.total}
    if order.pickup_at:
        result["pickup"] = order.pickup_words()
    else:
        result["ready_in_minutes"] = catalog.settings.get("pickup_minutes")
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
        "link_valid_minutes": catalog.pay_within_minutes,
    }
    if required and not order.pickup_at:
        result["ready_in_minutes"] = f"{catalog.settings.get('pickup_minutes')} after payment"
    return result


def handle(session: CallSession, name: str, args: dict) -> dict:
    order = session.order
    if name == "add_item":
        options = opt(args, "options", dict, {})
        extras = opt(args, "extras", list, [])
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in options.items()):
            raise BadInput("options must map strings to strings")
        if not all(isinstance(e, str) for e in extras):
            raise BadInput("extras must be a list of strings")
        line = order.add_item(
            item_id=req(args, "item_id", str),
            quantity=opt(args, "quantity", int, 1),
            options=options,
            extras=extras,
            notes=opt(args, "notes", str, ""),
        )
        return {"added": line.describe(), "line_id": line.line_id, "line_total": line.total,
                "order_total": order.total}
    if name == "remove_item":
        line = order.remove_item(req(args, "line_id", int))
        return {"removed": line.describe(), "order_total": order.total}
    if name == "update_quantity":
        line = order.update_quantity(req(args, "line_id", int), req(args, "quantity", int))
        return {"updated": line.describe() if line else "removed", "order_total": order.total}
    if name == "view_order":
        return order.summary()
    if name == "set_customer_name":
        order.set_customer(req(args, "name", str))
        return {"customer_name": order.customer_name}
    if name == "set_customer_phone":
        order.set_phone(req(args, "phone", str))
        return {"customer_phone": order.customer_phone}
    if name == "set_pickup_time":
        order.set_pickup(opt(args, "date", str, ""), opt(args, "time", str, ""))
        return {"pickup": order.pickup_words()}
    if name == "place_order":
        if req(args, "caller_confirmed", bool) is not True:
            raise OrderError("Read the order and total back and get the caller's confirmation first.")
        return _place_order(session)
    raise KeyError(name)


# ---- the module's part of the system prompt -------------------------------------------

_SECTION = """\
How to take the order:
- Help the caller choose; answer {catalog} questions from the {catalog} below only. If you don't \
know something (allergens, ingredients not listed), say so and don't guess.
- Before adding an item, make sure every required option is chosen. Mention available \
extras only if relevant; don't upsell more than once per call.
- Use the tools to change the order. The order tools are the source of truth for prices \
and totals, so never compute totals yourself.
- Orders are {order_types} only. Get the name for the order.
- Pickup is as soon as possible unless the caller asks for a later day or time; then call \
set_pickup_time, keeping to the opening hours. Some items say which days they're available, \
how many days ahead they must be ordered, or that they're in person only. The tools enforce \
this, so if one refuses, tell the caller why and offer what does work.
- When the caller is finished, read the whole order and the total back (call view_order \
first), and ask them to confirm. Only after a clear yes, call place_order.
- After placing, give the order number and the pickup time (or how soon it'll be ready), ask if there's anything else, and \
when they're done say goodbye and call end_call.
{payment}
{rules}- If the caller wants something you can't do ({declines}), say \
politely that you can only take pickup orders on this line and that staff can help them \
in person. Never tell the caller to call the {business}; this call is the {business}.
- Stay on the topic of ordering from this {business}."""

_PAY_BY_TEXT = """\
- Payment: never ask for or accept card numbers or other payment details over the phone. \
If the caller offers them, stop them politely. After the order is placed, we text the order \
details and a secure payment link to their phone. {rule}
- Before placing the order, confirm the mobile number for the text. If view_order shows a \
customer_phone, read back its last four digits ("the number ending in 1234"). If there is \
none, or they want a different number, ask for it and call set_customer_phone.
- After placing, tell them the text is on its way, using the place_order result. If it \
says the link wasn't sent, tell them what it says."""

_PAY_AT_PICKUP = """\
- Payment is at pickup. Never ask for or accept card numbers or other payment details \
over the phone."""


def prompt(catalog, payment_links: bool) -> str:
    t = catalog.template
    words = t.words
    if payment_links:
        rule = (f"This {words['business']} starts {words['prepare']} only after the link is paid; "
                f"the link is good for "
                f"{catalog.pay_within_minutes} minutes, and unpaid orders are cancelled. Tell the "
                "caller this before they confirm."
                if catalog.payment_required else
                "Paying by the link is optional; they can also pay at pickup.")
        payment = _PAY_BY_TEXT.format(rule=rule)
    else:
        payment = _PAY_AT_PICKUP
    return _SECTION.format(
        catalog=words["catalog"],
        business=words["business"],
        declines=t.declines,
        rules="".join(f"- {rule}\n" for rule in t.rules),
        order_types=" and ".join(catalog.settings.get("order_types", ["pickup"])),
        payment=payment,
    )
