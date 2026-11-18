"""Pay-by-text: a Stripe Checkout link for each order, sent to the caller by SMS.

Card details are never taken over the phone. After the order is placed we create a
Stripe Checkout Session with the exact line items and tax, text the link (with the
order details) to the caller, and a Stripe webhook tells us when it's paid.

Whether the kitchen waits for payment is a restaurant rule in data/menu.json:
  "payment": {"required_before_cooking": false, "pay_within_minutes": 30}
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol

from .menu import Menu
from .order import Order

log = logging.getLogger("ordering_agent.payments")

SMS_MAX_LINES = 8  # list at most this many items in the text; the payment page shows all


@dataclass(frozen=True)
class PaymentLink:
    id: str
    url: str


class LinkProvider(Protocol):
    def create(self, order: Order, order_number: int, expires_minutes: int) -> PaymentLink: ...


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> None: ...


class StripeLinks:
    """Creates a Stripe Checkout Session per order."""

    def __init__(self, secret_key: str, base_url: str, currency: str = "usd"):
        import stripe  # imported here so the rest of the app works without it configured

        self._stripe = stripe
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.currency = currency

    def create(self, order: Order, order_number: int, expires_minutes: int) -> PaymentLink:
        line_items = [
            {
                "quantity": line.quantity,
                "price_data": {
                    "currency": self.currency,
                    "unit_amount": _cents(line.unit_price),
                    "product_data": {"name": _line_name(line)[:250]},
                },
            }
            for line in order.lines
        ]
        if order.tax:
            line_items.append({
                "quantity": 1,
                "price_data": {"currency": self.currency, "unit_amount": _cents(order.tax),
                               "product_data": {"name": "Sales tax"}},
            })
        session = self._stripe.checkout.Session.create(
            api_key=self.secret_key,
            mode="payment",
            line_items=line_items,
            client_reference_id=str(order_number),
            metadata={"order_number": str(order_number)},
            payment_intent_data={"metadata": {"order_number": str(order_number)}},
            expires_at=int(time.time()) + expires_minutes * 60,
            success_url=f"{self.base_url}/paid?order={order_number}",
            cancel_url=f"{self.base_url}/paid?order={order_number}&cancelled=1",
        )
        return PaymentLink(id=session["id"], url=session["url"])


class TwilioSms:
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        from twilio.rest import Client

        self._client = Client(account_sid, auth_token)
        self.from_number = from_number

    def send(self, to: str, body: str) -> None:
        self._client.messages.create(to=to, from_=self.from_number, body=body)


@dataclass
class PaymentService:
    links: LinkProvider
    sms: SmsSender

    def request_payment(self, order: Order, order_number: int) -> PaymentLink:
        """Create the link and text it with the order details. Raises on failure."""
        menu = order.menu
        link = self.links.create(order, order_number, menu.pay_within_minutes)
        self.sms.send(order.customer_phone, order_text(order, order_number, link.url))
        return link


def payment_service_from_env(base_url: str) -> PaymentService | None:
    """Build the service from env vars, or None (pay at pickup) if not configured."""
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not stripe_key:
        return None
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.environ.get("TWILIO_SMS_FROM", "").strip()
    missing = [n for n, v in [("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", token),
                              ("TWILIO_SMS_FROM", sender), ("PUBLIC_BASE_URL", base_url)] if not v]
    if missing:
        log.error("STRIPE_SECRET_KEY is set but %s missing; payment links disabled", ", ".join(missing))
        return None
    return PaymentService(links=StripeLinks(stripe_key, base_url), sms=TwilioSms(sid, token, sender))


# ---- Stripe webhook -----------------------------------------------------------


def handle_stripe_event(event: dict, store, menu: Menu, sms: SmsSender | None) -> str:
    """Apply a (signature-verified) Stripe event to the order. Returns what happened."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return "ignored"
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") != "paid":
            return "pending"  # delayed payment methods settle later
        changed = store.mark_paid(payment_id)
        text = paid_text(menu, changed["order_number"]) if changed else None
        outcome = "paid"
    elif kind in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        changed = store.mark_expired(payment_id)
        # Only text when the order was cancelled; otherwise they can still pay at pickup.
        text = (expired_text(menu, changed["order_number"])
                if changed and changed["status"] == "cancelled" else None)
        outcome = "expired"
    else:
        return "ignored"
    if not changed:
        return "duplicate"
    log.info("order %s payment %s", changed["order_number"], outcome)
    if text and sms and changed.get("customer_phone"):
        try:
            sms.send(changed["customer_phone"], text)
        except Exception:
            log.exception("couldn't text payment update for order %s", changed["order_number"])
    return outcome


# ---- message text -----------------------------------------------------------


def order_text(order: Order, order_number: int, url: str) -> str:
    menu = order.menu
    name = menu.restaurant.get("name", "the restaurant")
    lines = [f"{name}: order #{order_number} for {order.customer_name}"]
    for line in order.lines[:SMS_MAX_LINES]:
        lines.append(f"{line.quantity} x {_line_name(line)} ${line.total:.2f}")
    if len(order.lines) > SMS_MAX_LINES:
        lines.append(f"...and {len(order.lines) - SMS_MAX_LINES} more")
    lines.append(f"Subtotal ${order.subtotal:.2f}, tax ${order.tax:.2f}, total ${order.total:.2f}")
    lines.append(f"Pay securely: {url}")
    if menu.payment_required:
        lines.append(f"We'll start your order once it's paid. Link expires in {menu.pay_within_minutes} min.")
    else:
        lines.append(f"Ready in about {menu.restaurant.get('pickup_minutes')} min. "
                     "You can also pay at pickup.")
    return "\n".join(lines)


def paid_text(menu: Menu, order_number: int) -> str:
    name = menu.restaurant.get("name", "the restaurant")
    return (f"{name}: payment received for order #{order_number}. Thank you! "
            f"Ready in about {menu.restaurant.get('pickup_minutes')} min.")


def expired_text(menu: Menu, order_number: int) -> str:
    name = menu.restaurant.get("name", "the restaurant")
    return (f"{name}: order #{order_number} was cancelled because it wasn't paid in time. "
            "Call us to order again.")


def _line_name(line) -> str:
    extra = list(line.options.values()) + list(line.extras)
    return line.name + (f" ({', '.join(extra)})" if extra else "")


def _cents(amount: float) -> int:
    return int(round(amount * 100))
