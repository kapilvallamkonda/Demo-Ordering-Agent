"""The inquiries module: custom requests the business quotes on, instead of an order.

For what the catalog can't price (a custom garland design, a full wedding setup, catering
for 200, a rental, a large or unusual quantity), the agent doesn't guess: it collects what
the caller wants and records an inquiry. Staff see inquiries in /manage (Requests) and
call or text the customer back with a quote. If the business set a staff mobile number
(settings.notify_phone), it gets a short text for each new inquiry, sent in the background
so the call never waits on it.

Stored per business (Store.save_inquiry), numbered per business like orders.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date

from ..order import OrderError, normalize_phone
from ..session import BadInput, CallSession, ToolError, opt, req, tool

log = logging.getLogger("phone_agent.modules.inquiries")

TOOLS: list[dict] = [
    tool(
        "record_inquiry",
        "Record a custom request for the team to quote on or call back about: something the "
        "catalog can't price or order (custom work, setups, rentals, services, catering, large "
        "quantities). Only after reading the request back and hearing the caller confirm it.",
        {
            "summary": {"type": "string", "description": "One line: what they want, e.g. 'Custom rose and "
                        "jasmine garlands for a wedding, 2 pairs'."},
            "details": {"type": "string", "description": "Everything else they said that matters: "
                        "quantities, colours, flowers, ceremonies, catalog items they liked, delivery or "
                        "pickup, questions for the team."},
            "event_date": {"type": "string", "description": "The event's date, YYYY-MM-DD, if they gave one."},
            "budget": {"type": "string", "description": "Their budget as they said it, if they gave one."},
            "name": {"type": "string", "description": "The caller's name."},
            "phone": {"type": "string", "description": "Number to reach them on, if not the one they're "
                      "calling from."},
            "caller_confirmed": {"type": "boolean", "description": "True only if the caller confirmed the read-back request."},
        },
        ["summary", "name", "caller_confirmed"],
    ),
]


def start(session: CallSession) -> None:
    session.inquiry_number = None


def handle(session: CallSession, name: str, args: dict) -> dict:
    if name != "record_inquiry":
        raise KeyError(name)
    if req(args, "caller_confirmed", bool) is not True:
        raise ToolError("Read the request back to the caller and get their confirmation first.")
    summary = " ".join(req(args, "summary", str).split())[:300]
    customer = " ".join(req(args, "name", str).split())[:80]
    if not summary:
        raise BadInput("summary is empty")
    if not customer:
        raise ToolError("Ask the caller's name for the request.")
    phone_said = opt(args, "phone", str, "").strip()
    try:
        phone = normalize_phone(phone_said or session.caller_phone or "")
    except OrderError:
        raise ToolError("Ask for a phone number the team can call or text back on.")
    event_date = opt(args, "event_date", str, "").strip()
    if event_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
            raise ToolError("Give the event date as YYYY-MM-DD.")
        try:
            when = date.fromisoformat(event_date)
        except ValueError:
            raise ToolError("That event date isn't a real date; check it with the caller.")
        from .. import schedule

        if when < schedule.now_in(session.catalog.tz).date():
            raise ToolError("That event date has already passed; check it with the caller.")
    record = {
        "summary": summary,
        "details": opt(args, "details", str, "").strip()[:2000],
        "event_date": event_date or None,
        "budget": opt(args, "budget", str, "").strip()[:100] or None,
        "customer_name": customer,
        "customer_phone": phone,
    }
    catalog = session.catalog
    number = None
    if session.store is not None and catalog.business_id is not None:
        number = session.store.save_inquiry(catalog.business_id, call_id=session.call_id, **record)
        session.store.track("inquiry_recorded", catalog.business_id, call_id=session.call_id,
                            has_date=bool(event_date), has_budget=bool(record["budget"]))
        _notify_staff(session, number, record)
    session.inquiry_number = number
    return {"recorded": True, "request_number": number,
            "tell_caller": "The team will call or text them back about it; don't promise a price or a time."}


def _notify_staff(session: CallSession, number: int | None, record: dict) -> None:
    """Text the business's staff number about the new request, off the call's path."""
    to = str(session.catalog.settings.get("notify_phone") or "").strip()
    sms = session.sms
    if not to or sms is None:
        return
    try:
        to = normalize_phone(to)
    except OrderError:
        log.warning("business %s: notify_phone %r isn't a usable number", session.catalog.business_id, to)
        return
    when = f" for {record['event_date']}" if record["event_date"] else ""
    body = (f"New request #{number}{when} from {record['customer_name']} ({record['customer_phone']}): "
            f"{record['summary']}")[:600]
    store, business_id = session.store, session.catalog.business_id

    def send():
        try:
            sms.send(to, body)
        except Exception:
            log.exception("couldn't text staff about request #%s", number)
            if store is not None:
                store.track("sms_failed", business_id, what="inquiry_alert")

    threading.Thread(target=send, daemon=True, name="inquiry-alert").start()


_SECTION = """\
Custom requests:
- For anything you can't order from the {catalog} ({examples}), or a price you don't have, \
don't guess a price or say yes or no on the team's behalf. Take down a request instead: what \
they want, the date of their event, quantities, colours or style, their budget if they'll say, \
and their name; the number they're calling from is used unless they give another.
- Read the request back, and once they confirm, call record_inquiry. Tell them the team will \
call or text them back about it."""


def prompt(catalog, payment_links: bool) -> str:
    examples = {"restaurant": "catering, large party orders, dishes not on the menu",
                "events": "custom designs, full setups, rentals, services, large quantities"}
    return _SECTION.format(catalog=catalog.template.words["catalog"],
                           examples=examples.get(catalog.template.id, "custom orders, large quantities, items "
                                                                       "you don't carry"))
