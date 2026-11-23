"""Point-of-sale integrations: put phone orders on the business's own POS, and import
its catalog from the POS.

A business connects its POS once (OAuth: the owner opens a connect link and approves
access). From then on every placed order is sent to the POS in the background, with
retries, and its result is shown on the dashboard (`orders.pos_status`). The call never
waits on the POS: the caller already has their order number.

Providers: Square (pos_square.py), Clover (pos_clover.py) and Shopify (pos_shopify.py).
Each one implements `Provider` below. Square and Shopify take payment by a link we text
the caller (LinkPayments); Clover gets the order straight away. Access tokens are stored encrypted with POS_SECRET_KEY.

Admin commands (run on the server, e.g. `fly ssh console -C "python -m ..."`):

    python -m phone_agent.pos status
    python -m phone_agent.pos connect-link SLUG square|clover|shopify [--shop STORE.myshopify.com]
                                    (prints a link for the owner)
    python -m phone_agent.pos connect-token SLUG square|clover|shopify --merchant ID [--location ID]
                                    (Shopify: --merchant is the store's .myshopify.com address)
                                    (sandbox/testing: token read from POS_ACCESS_TOKEN)
    python -m phone_agent.pos set-location SLUG LOCATION_ID          (Square, several locations)
    python -m phone_agent.pos import-catalog SLUG > catalog.json           (catalog from the POS)
    python -m phone_agent.pos retry SLUG [ORDER_NUMBER]               (resend failed orders)
    python -m phone_agent.pos disconnect SLUG
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from .catalog import Catalog
from .storage import Store

log = logging.getLogger("phone_agent.pos")

RETRY_DELAYS = (5, 30, 120, 600)  # seconds between attempts after the first one fails
PER_BUSINESS_JOBS = 2  # POS jobs one business can have running at once
BUSY_RECHECK = 2.0  # seconds before a job that had to wait for its business's slot tries again
RESUME_HOURS = 6  # on startup, resend orders from this many hours back that never made it
LINK_POLL_SECONDS = 20  # how often a waiting POS payment link is checked
LINK_GRACE_SECONDS = 3600  # give up on a link we can't reach this long after it should have expired
STATE_MAX_AGE = 3600  # a connect link works for an hour
REFRESH_MARGIN = 3 * 24 * 3600  # refresh an access token this long before it expires


class PosError(Exception):
    """The POS refused or failed. `retry` says whether trying again could help."""

    def __init__(self, message: str, retry: bool = True, partial_id: str | None = None):
        super().__init__(message)
        self.retry = retry
        self.partial_id = partial_id  # an order created on the POS before the failure


def http_call(http, method: str, url: str, *, token: str | None = None, headers: dict | None = None,
              json_body: dict | None = None, params: dict | None = None, what: str = "POS") -> dict:
    """One JSON request. Network errors, 429 and 5xx raise a retryable PosError; other
    4xx raise a permanent one."""
    import httpx

    h = {"Accept": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        resp = http.request(method, url, headers=h, json=json_body, params=params)
    except httpx.HTTPError as e:
        raise PosError(f"{what} unreachable: {type(e).__name__}") from e
    if resp.status_code >= 400:
        detail = resp.text[:300]
        retry = resp.status_code == 429 or resp.status_code >= 500
        raise PosError(f"{what} {method} {url.split('?')[0].rsplit('/', 2)[-2:]} -> "
                       f"{resp.status_code}: {detail}", retry=retry)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


# ---- what we send ---------------------------------------------------------------------


@dataclass
class PosLine:
    item_id: str
    name: str
    quantity: int
    unit_price: float  # dollars, including option and extra prices
    base_price: float  # the catalog item's base price
    options: dict[str, str]
    option_prices: dict[str, float]  # option name -> the chosen choice's price delta
    extras: dict[str, float]  # extra -> price
    notes: str
    ids: dict = field(default_factory=dict)  # the item's POS ids, if they're for this provider

    def label(self) -> str:
        """The line as one readable name, for POSes that get it as a custom item."""
        bits = list(self.options.values()) + list(self.extras)
        return self.name + (f" ({', '.join(bits)})" if bits else "")


@dataclass
class PosOrder:
    order_id: int
    number: int
    customer_name: str
    customer_phone: str
    lines: list[PosLine]
    subtotal: float
    tax: float
    total: float
    tax_rate: float
    pickup_minutes: int
    paid_online: bool
    pay_first: bool
    business_name: str
    link_sent: bool = True  # a payment link was texted (False: plain pay at pickup)
    pickup_at: str | None = None  # ISO time with offset, for a scheduled pickup
    pickup_words: str | None = None

    @property
    def note(self) -> str:
        pay = ("PAID online by payment link" if self.paid_online
               else "Payment link sent; must be paid before cooking" if self.pay_first and self.link_sent
               else "Pay at pickup (payment link also texted)" if self.link_sent
               else "Pay at pickup")
        when = f" PICKUP {self.pickup_words.upper()}." if self.pickup_at and self.pickup_words else ""
        return f"Phone order #{self.number} for {self.customer_name}.{when} {pay}."


def build_pos_order(rec: dict, catalog: Catalog, provider: str) -> PosOrder:
    """Turn a saved order (Store.order_for_pos) into what providers send."""
    lines = []
    for it in rec.get("items") or []:
        item = catalog.get(it["item_id"])
        ids = item.pos if item and catalog.pos_provider == provider else {}
        option_prices = {}
        for name, choice in (it.get("options") or {}).items():
            grp = item.options.get(name) if item else None
            option_prices[name] = float(grp.choices.get(choice, 0.0)) if grp else 0.0
        extras = {e: float(item.extras.get(e, 0.0)) if item else 0.0 for e in it.get("extras") or []}
        lines.append(PosLine(
            item_id=it["item_id"], name=it["name"], quantity=int(it["quantity"]),
            unit_price=float(it["unit_price"]), base_price=float(item.price) if item else float(it["unit_price"]),
            options=dict(it.get("options") or {}), option_prices=option_prices, extras=extras,
            notes=it.get("notes") or "", ids=ids,
        ))
    if not lines:
        raise PosError("order has no structured lines (placed before POS support)", retry=False)
    r = catalog.settings
    return PosOrder(
        order_id=rec["id"], number=rec["number"], customer_name=rec.get("customer_name") or "Phone order",
        customer_phone=rec.get("customer_phone") or "", lines=lines, subtotal=rec["subtotal"],
        tax=rec["tax"], total=rec["total"], tax_rate=catalog.tax_rate,
        pickup_minutes=int(r.get("pickup_minutes") or 20), paid_online=rec["payment_status"] == "paid",
        pay_first=rec["status"] == "awaiting_payment" or catalog.payment_required,
        business_name=r.get("name", ""),
        link_sent=rec.get("payment_status", "not_requested") != "not_requested",
        pickup_at=rec.get("pickup_at"), pickup_words=rec.get("pickup"),
    )


def cents(amount: float) -> int:
    return int(round(amount * 100))


# ---- providers ------------------------------------------------------------------------


@dataclass
class Connection:
    """A business's POS connection, with its decrypted tokens."""

    business_id: int
    provider: str
    environment: str  # "sandbox" or "production"
    merchant_id: str | None
    location_id: str | None
    tokens: dict  # access_token, refresh_token, expires_at (unix seconds or None)
    settings: dict

    @property
    def access_token(self) -> str:
        return self.tokens["access_token"]


class Provider(Protocol):
    name: str

    def authorize_url(self, state: str, redirect_uri: str) -> str: ...

    def exchange_code(self, code: str, query: dict, redirect_uri: str) -> tuple[dict, str | None]:
        """Returns (tokens, merchant_id)."""

    def refresh(self, conn: Connection) -> dict:
        """Returns new tokens."""

    def setup(self, conn: Connection) -> tuple[str | None, dict]:
        """After connecting: returns (location_id, settings), e.g. the pickup order type."""

    def push(self, conn: Connection, order: PosOrder, existing_id: str | None) -> str:
        """Create the order on the POS; returns its POS id. existing_id is a partly created
        order from a failed attempt, for providers without idempotency keys."""

    def mark_paid(self, conn: Connection, order: PosOrder, pos_order_id: str, payment_ref: str) -> None: ...

    def catalog(self, conn: Connection) -> list[dict]:
        """The POS catalog as items in our catalog format (with "pos" ids, and "pos_key", a
        stable POS id used to keep our item ids across re-imports)."""


def providers_from_env() -> dict[str, Provider]:
    """Every provider whose app credentials are set."""
    from .pos_clover import CloverProvider
    from .pos_shopify import ShopifyProvider
    from .pos_square import SquareProvider

    out: dict[str, Provider] = {}
    if os.environ.get("SQUARE_APPLICATION_ID") or os.environ.get("POS_ALLOW_TOKENS"):
        out["square"] = SquareProvider.from_env()
    if os.environ.get("CLOVER_APP_ID") or os.environ.get("POS_ALLOW_TOKENS"):
        out["clover"] = CloverProvider.from_env()
    if os.environ.get("SHOPIFY_CLIENT_ID") or os.environ.get("POS_ALLOW_TOKENS"):
        out["shopify"] = ShopifyProvider.from_env()
    return out


# ---- token encryption and connect links -------------------------------------------------


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("POS_SECRET_KEY", "").strip()
    if not key:
        raise PosError("POS_SECRET_KEY isn't set", retry=False)
    return Fernet(key.encode())


def seal(tokens: dict) -> str:
    return _fernet().encrypt(json.dumps(tokens).encode()).decode()


def unseal(secret: str) -> dict:
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(_fernet().decrypt(secret.encode()))
    except InvalidToken:
        raise PosError("stored POS tokens can't be decrypted (POS_SECRET_KEY changed?)", retry=False)


def _state_key() -> bytes:
    return hashlib.sha256(b"pos-connect-state:" + os.environ.get("POS_SECRET_KEY", "").encode()).digest()


def make_state(provider: str, business_id: int, now: float | None = None) -> str:
    """Signed, short-lived OAuth state naming the business being connected."""
    body = f"{provider}.{business_id}.{int(now or time.time())}"
    sig = hmac.new(_state_key(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def check_state(state: str, provider: str, now: float | None = None) -> int:
    """Returns the business id, or raises PosError."""
    try:
        prov, rid, issued, sig = state.split(".")
        body = f"{prov}.{rid}.{issued}"
    except ValueError:
        raise PosError("bad connect link", retry=False)
    want = base64.urlsafe_b64encode(hmac.new(_state_key(), body.encode(), hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(want.rstrip("="), sig) or prov != provider:
        raise PosError("bad connect link", retry=False)
    if (now or time.time()) - int(issued) > STATE_MAX_AGE:
        raise PosError("this connect link has expired; ask for a new one", retry=False)
    return int(rid)


# ---- connections and syncing --------------------------------------------------------------


class PosHub:
    """Loads connections (refreshing tokens), and sends orders in the background.

    Isolation: jobs never sleep in a worker (retries wait on timers), and one business can
    have at most PER_BUSINESS_JOBS running at once, so a business whose POS is slow or down
    can't hold up other businesses' orders."""

    def __init__(self, store: Store, directory, providers: dict[str, Provider],
                 retry_delays: tuple[float, ...] = RETRY_DELAYS, workers: int = 4,
                 run_inline: bool = False, per_business: int = PER_BUSINESS_JOBS, sms=None):
        self.store = store
        self.sms = sms  # texts the customer when a POS payment link is paid or expires (payments.SmsSender)
        self.directory = directory  # businesses.BusinessDirectory
        self.providers = providers
        self.retry_delays = retry_delays
        self.run_inline = run_inline  # tests: run jobs on the calling thread
        self._pool = None if run_inline else ThreadPoolExecutor(max_workers=workers,
                                                                 thread_name_prefix="pos")
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._refresh_lock = threading.Lock()  # refresh tokens can be single-use
        self.per_business = per_business
        self._running: dict[int, int] = {}  # business id -> jobs running
        self._running_guard = threading.Lock()

    # connections

    def connection(self, business_id: int | None) -> Connection | None:
        if business_id is None:
            return None
        with self._refresh_lock:
            row = self.store.pos_connection(business_id)
            if row is None:
                return None
            conn = Connection(business_id, row["provider"], row["environment"], row["merchant_id"],
                              row["location_id"], unseal(row["secret"]), row["settings"])
            expires = conn.tokens.get("expires_at")
            provider = self.provider(conn.provider)
            margin = getattr(provider, "refresh_margin", REFRESH_MARGIN)
            if expires and conn.tokens.get("refresh_token") and expires - time.time() < margin:
                conn.tokens = provider.refresh(conn)
                self.store.update_pos_connection(business_id, secret=seal(conn.tokens))
                log.info("refreshed %s token for business %s", conn.provider, business_id)
            return conn

    def provider(self, name: str) -> Provider:
        if name not in self.providers:
            raise PosError(f"{name} isn't configured on this server", retry=False)
        return self.providers[name]

    def save_connection(self, business_id: int, provider: str, environment: str, tokens: dict,
                        merchant_id: str | None, location_id: str | None = None) -> Connection:
        """Store a new connection, then let the provider pick its location and settings."""
        prov = self.provider(provider)
        conn = Connection(business_id, provider, environment, merchant_id, location_id, tokens, {})
        found_location, settings = prov.setup(conn)
        conn.location_id = location_id or found_location
        conn.settings = settings
        self.store.set_pos_connection(business_id, provider, environment, seal(tokens),
                                      merchant_id=merchant_id, location_id=conn.location_id,
                                      settings=settings)
        return conn

    # sending orders

    def order_placed(self, order_id: int, business_id: int | None) -> None:
        """Called right after an order is saved. Marks it pending and sends it in the background."""
        if business_id is None or self.store.pos_connection(business_id) is None:
            return
        rec = self.store.order_for_pos(order_id)
        if rec and rec["status"] == "awaiting_payment":
            # Pay-first business: the kitchen mustn't see it until it's paid (order_paid sends it).
            self.store.set_pos_status(order_id, "held")
            return
        self.store.set_pos_status(order_id, "pending")
        self._run(self._send_attempt, order_id, 0)

    def order_paid(self, order_id: int) -> None:
        """The payment link was paid: record it on the POS (if the order is there already;
        otherwise the send includes it)."""
        self._run(self._payment_attempt, order_id, 0)

    def resume(self) -> int:
        """Resend orders from the last few hours that never reached their POS, and go back to
        watching payment links that were waiting when the server stopped."""
        ids = self.store.orders_to_send(time.time() - RESUME_HOURS * 3600)
        for oid in ids:
            self._run(self._send_attempt, oid, 0)
        waiting = self.store.orders_awaiting_link(time.time() - 25 * 3600)  # links last up to 24 hours
        for oid in waiting:
            self._run(self._watch_attempt, oid, 0)
        return len(ids) + len(waiting)

    # pay by a POS payment link (Square): see SquarePayments

    def create_payment_link(self, order_id: int):
        """Create the POS payment link for a saved order; the POS creates the order with it."""
        from .payments import PaymentLink

        rec = self.store.order_for_pos(order_id)
        if rec is None:
            raise PosError("order not found", retry=False)
        conn = self.connection(rec["business_id"])
        if conn is None:
            raise PosError("business has no POS connected", retry=False)
        prov = self.provider(conn.provider)
        if not hasattr(prov, "payment_link"):
            raise PosError(f"{conn.provider} payment links aren't supported", retry=False)
        catalog = self.directory.catalog_version(rec["business_id"], rec["catalog_id"])
        link_id, url, pos_order_id = prov.payment_link(conn, build_pos_order(rec, catalog, conn.provider))
        self.store.set_payment_link(order_id, link_id, url)
        self.store.set_pos_status(order_id, "held", pos_order_id=pos_order_id, attempted=True)
        log.info("order #%s: %s payment link %s", rec["number"], conn.provider, link_id)
        return PaymentLink(link_id, url)

    def watch_payment(self, order_id: int) -> None:
        """Check the link every LINK_POLL_SECONDS until it's paid or expires (tests call
        check_payment instead)."""
        if not self.run_inline:
            self._run(self._watch_attempt, order_id, 0, delay=LINK_POLL_SECONDS)

    def _watch_attempt(self, order_id: int, attempt: int) -> None:
        if self.check_payment(order_id) == "waiting" and not self.run_inline:
            self._run(self._watch_attempt, order_id, attempt + 1, delay=LINK_POLL_SECONDS)

    def check_payment(self, order_id: int, now: float | None = None) -> str:
        """One look at a waiting payment link. Returns "paid", "expired", "waiting" or "done"
        (nothing to watch). Paid: the order is confirmed (it's on the POS already) and the
        customer gets a text. Past the business's payment window: the link is cancelled (the
        POS drops its draft order), the order is cancelled and the customer is told."""
        from .payments import expired_text, paid_text

        now = now or time.time()
        with self._lock(order_id):
            rec = self.store.order_for_pos(order_id)
            if rec is None or rec["status"] != "awaiting_payment" or not rec["payment_id"] or not rec["pos_order_id"]:
                return "done"
            catalog = self.directory.catalog_version(rec["business_id"], rec["catalog_id"])
            deadline = rec["created_at"] + catalog.pay_within_minutes * 60
            try:
                conn = self.connection(rec["business_id"])
                if conn is None:
                    raise PosError("business has no POS connected", retry=False)
                prov = self.provider(conn.provider)
                paid = prov.link_paid(conn, rec["pos_order_id"])
                if paid is False and now > deadline:
                    prov.cancel_link(conn, rec["payment_id"])
                    paid = prov.link_paid(conn, rec["pos_order_id"])  # paid in the last seconds?
            except PosError as e:
                if now < deadline + LINK_GRACE_SECONDS:
                    log.warning("order id %s: checking the payment link failed: %s", order_id, e)
                    return "waiting"
                paid = None  # can't reach the POS long after expiry: give up on it
            if paid:
                changed = self.store.mark_paid(rec["payment_id"])
                self.store.set_pos_status(order_id, "sent", pos_order_id=rec["pos_order_id"])
                self.store.set_pos_paid(order_id)
                log.info("order #%s paid by payment link; it's on the POS", rec["number"])
                self.store.track("payment_link_paid", rec["business_id"], provider=conn.provider,
                                 minutes=round((now - rec["created_at"]) / 60, 1))
                self._text(rec, changed and paid_text(catalog, rec["number"], changed.get("pickup")))
                return "paid"
            if paid is None or now > deadline:
                changed = self.store.mark_expired(rec["payment_id"])
                self.store.set_pos_status(order_id, "cancelled", error="not paid in time; the payment link was cancelled")
                log.info("order #%s: payment link expired unpaid; order cancelled", rec["number"])
                self.store.track("payment_link_expired", rec["business_id"])
                self._text(rec, changed and expired_text(catalog, rec["number"]))
                return "expired"
            return "waiting"

    def _text(self, rec: dict, body: str | None) -> None:
        if body and self.sms and rec.get("customer_phone"):
            try:
                self.sms.send(rec["customer_phone"], body)
            except Exception:
                log.exception("couldn't text payment update for order #%s", rec["number"])
                self.store.track("sms_failed", rec.get("business_id"), what="payment_update")

    def retry(self, order_id: int) -> None:
        self.store.set_pos_status(order_id, "pending")
        self._run(self._send_attempt, order_id, 0)

    # scheduling: fn(order_id, attempt) runs on the pool, gated per business

    def _run(self, fn, order_id: int, attempt: int, delay: float = 0) -> None:
        if self.run_inline:
            self._guard(fn, order_id, attempt)  # tests: no waiting, no threads
        elif delay:
            timer = threading.Timer(delay, self._run, (fn, order_id, attempt))
            timer.daemon = True
            timer.start()
        else:
            self._pool.submit(self._guard, fn, order_id, attempt)

    def _guard(self, fn, order_id: int, attempt: int) -> None:
        rec = None if self.run_inline else self.store.order_for_pos(order_id)
        business = rec["business_id"] if rec else None  # inline (tests): no per-business gating
        with self._running_guard:
            busy = business is not None and self._running.get(business, 0) >= self.per_business
            if not busy and business is not None:
                self._running[business] = self._running.get(business, 0) + 1
        if busy:
            self._run(fn, order_id, attempt, delay=BUSY_RECHECK)  # let other businesses' jobs run
            return
        try:
            fn(order_id, attempt)
        except Exception:
            log.exception("POS job %s(order id %s) crashed", fn.__name__, order_id)
        finally:
            if business is not None:
                with self._running_guard:
                    self._running[business] = max(0, self._running.get(business, 1) - 1)

    def _retry_later(self, fn, order_id: int, attempt: int, what: str) -> None:
        if attempt < len(self.retry_delays):
            self._run(fn, order_id, attempt + 1, delay=self.retry_delays[attempt])
        else:
            log.error("order id %s: gave up %s after %d attempts", order_id, what, attempt + 1)

    def _lock(self, order_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(order_id, threading.Lock())

    def _send_attempt(self, order_id: int, attempt: int) -> None:
        if self.send(order_id) == "retry":
            self._retry_later(self._send_attempt, order_id, attempt, "sending to the POS")

    def send(self, order_id: int) -> str:
        """One attempt. Returns "sent", "skipped", "failed" (permanent) or "retry"."""
        with self._lock(order_id):
            rec = self.store.order_for_pos(order_id)
            if rec is None or rec["pos_status"] == "sent":
                return "skipped"
            try:
                conn = self.connection(rec["business_id"])
                if conn is None:
                    self.store.set_pos_status(order_id, "failed", error="business has no POS connected")
                    return "failed"
                if rec["status"] == "awaiting_payment":
                    self.store.set_pos_status(order_id, "held")
                    return "skipped"
                if rec["status"] == "cancelled":
                    self.store.set_pos_status(order_id, "failed", error="order was cancelled before it was sent")
                    return "failed"
                catalog = self.directory.catalog_version(rec["business_id"], rec["catalog_id"])
                order = build_pos_order(rec, catalog, conn.provider)
                prov = self.provider(conn.provider)
                pos_id = prov.push(conn, order, rec["pos_order_id"])
                self.store.set_pos_status(order_id, "sent", pos_order_id=pos_id, attempted=True)
                log.info("order #%s sent to %s as %s", rec["number"], conn.provider, pos_id)
                if order.paid_online and not rec["pos_paid"]:
                    prov.mark_paid(conn, order, pos_id, rec["payment_id"] or "")
                    self.store.set_pos_paid(order_id)
                return "sent"
            except PosError as e:
                self.store.set_pos_status(order_id, "failed", error=str(e), attempted=True,
                                          pos_order_id=e.partial_id)
                log.warning("order id %s: POS send failed (%s): %s", order_id,
                            "will retry" if e.retry else "permanent", e)
                self.store.track("pos_send_failed", rec["business_id"], error=str(e)[:160], retry=e.retry)
                return "retry" if e.retry else "failed"
            except Exception as e:  # network errors, bugs: keep the order visible and retry
                self.store.set_pos_status(order_id, "failed", error=f"{type(e).__name__}: {e}", attempted=True)
                log.exception("order id %s: POS send crashed", order_id)
                return "retry"

    def _payment_attempt(self, order_id: int, attempt: int) -> None:
        if self.record_payment(order_id) == "retry":
            self._retry_later(self._payment_attempt, order_id, attempt, "recording the payment on the POS")

    def record_payment(self, order_id: int) -> str:
        """One attempt at recording the online payment on the POS. Returns "done" or "retry"."""
        with self._lock(order_id):
            rec = self.store.order_for_pos(order_id)
            if rec is None or rec["pos_paid"] or rec["pos_status"] is None:
                return "done"
            if rec["pos_status"] == "sent":
                try:
                    conn = self.connection(rec["business_id"])
                    if conn is None:
                        return "done"
                    catalog = self.directory.catalog_version(rec["business_id"], rec["catalog_id"])
                    order = build_pos_order(rec, catalog, conn.provider)
                    self.provider(conn.provider).mark_paid(conn, order, rec["pos_order_id"],
                                                           rec["payment_id"] or "")
                    self.store.set_pos_paid(order_id)
                    log.info("order #%s marked paid on %s", rec["number"], conn.provider)
                    return "done"
                except PosError as e:
                    log.warning("order id %s: recording payment on POS failed: %s", order_id, e)
                    return "retry" if e.retry else "done"
                except Exception:
                    log.exception("order id %s: recording payment on POS crashed", order_id)
                    return "retry"
        # Not on the POS yet: sending it records the payment too.
        return "retry" if self.send(order_id) == "retry" else "done"


class LinkPayments:
    """How a business connected to Square or Shopify takes payment: the POS's own payment
    link (a Square payment link, a Shopify draft order's checkout), texted to the caller.
    The money goes straight to the business's account, and the order shows in its POS once
    paid. Square only shows orders from other apps once they're paid, and a Shopify draft
    only becomes an order at checkout, so for these businesses paying is required to
    confirm the order (always_required). Same interface as payments.PaymentService."""

    always_required = True

    def __init__(self, hub: PosHub, sms):
        self.hub = hub
        self.sms = sms

    def request_payment(self, order, order_number: int, order_id: int | None = None):
        from .payments import order_text

        if order_id is None:
            raise PosError("the order must be saved before its payment link is made", retry=False)
        link = self.hub.create_payment_link(order_id)
        self.hub.watch_payment(order_id)  # before texting, so an unsent link still expires
        self.sms.send(order.customer_phone, order_text(order, order_number, link.url))
        return link


SquarePayments = LinkPayments  # the earlier name


def pays_by_link(provider) -> bool:
    return bool(getattr(provider, "pays_by_link", False))


def payments_for(hub: PosHub | None, store: Store, catalog: Catalog, default):
    """The payment service for a call to this business, and its catalog as the agent should
    see it: businesses on Square or Shopify take that POS's payment link and always require
    payment; everyone else uses the server-wide service (or none: pay at pickup)."""
    import dataclasses

    if hub is None or hub.sms is None or catalog.business_id is None:
        return default, catalog
    row = store.pos_connection(catalog.business_id)
    if row is None or not pays_by_link(hub.providers.get(row["provider"])):
        return default, catalog
    settings = dict(catalog.settings)
    settings["payment"] = {**(settings.get("payment") or {}), "required_before_cooking": True}
    return LinkPayments(hub, hub.sms), dataclasses.replace(catalog, settings=settings)


# ---- catalog import ----------------------------------------------------------------------


def _slug_id(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "item"


def merge_import(current: dict | None, imported: list[dict], provider: str,
                 base_settings: dict | None = None) -> tuple[dict, list[str]]:
    """Build a catalog file from POS items. Keeps the current business settings and, for
    items already imported before, their item ids (old orders refer to them).
    Returns (catalog, notes about what changed)."""
    business = (current or {}).get("settings") or base_settings
    if not business:
        raise PosError("no business settings to start from; pass --base", retry=False)
    old_by_key = {it["pos_key"]: it["id"] for it in (current or {}).get("items", []) if it.get("pos_key")}
    taken: set[str] = set()
    items, notes = [], []
    for it in imported:
        iid = old_by_key.get(it["pos_key"])
        if iid is None or iid in taken:
            base = iid or _slug_id(it["name"])
            iid, n = base, 2
            while iid in taken:
                iid, n = f"{base}_{n}", n + 1
        taken.add(iid)
        items.append({"id": iid, **it})
    gone = set(old_by_key.values()) - taken
    if gone:
        notes.append(f"{len(gone)} items are no longer on the POS: {', '.join(sorted(gone))}")
    kept = sum(1 for it in imported if it["pos_key"] in old_by_key)
    notes.append(f"{len(items)} items imported from {provider} ({kept} matched earlier imports)")
    return {"template": (current or {}).get("template", "restaurant"), "settings": business,
            "pos": {"provider": provider, "imported_at": int(time.time())},
            "items": items}, notes


# ---- admin commands -------------------------------------------------------------------


def _hub(store: Store) -> PosHub:
    from .businesses import BusinessDirectory

    return PosHub(store, BusinessDirectory(store), providers_from_env(), run_inline=True)


def connect_url(provider: str, business_id: int, base_url: str, providers: dict[str, Provider],
                shop: str = "") -> str:
    """The owner's connect link. Shopify needs the store's .myshopify.com address (shop)."""
    if provider not in providers:
        raise PosError(f"{provider} app credentials aren't set on this server", retry=False)
    redirect = f"{base_url.rstrip('/')}/pos/{provider}/callback"
    state = make_state(provider, business_id)
    if provider == "shopify":
        return providers[provider].authorize_url(state, redirect, shop=shop)
    return providers[provider].authorize_url(state, redirect)


def main(argv: list[str] | None = None, store: Store | None = None, hub: PosHub | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m phone_agent.pos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("connect-link", "connect-token"):
        p = sub.add_parser(name)
        p.add_argument("slug")
        p.add_argument("provider", choices=["square", "clover", "shopify"])
        if name == "connect-link":
            p.add_argument("--shop", default="", help="Shopify: the store's .myshopify.com address")
        if name == "connect-token":
            p.add_argument("--merchant", help="merchant id (Clover: required)")
            p.add_argument("--location", help="location id (Square: if it has several)")
            p.add_argument("--environment", choices=["sandbox", "production"], default="sandbox")
    p = sub.add_parser("set-location")
    p.add_argument("slug")
    p.add_argument("location")
    p = sub.add_parser("import-catalog")
    p.add_argument("slug")
    p.add_argument("--base", help="catalog file whose business settings to use (default: current catalog)")
    p = sub.add_parser("retry")
    p.add_argument("slug")
    p.add_argument("number", nargs="?", type=int)
    sub.add_parser("disconnect").add_argument("slug")
    args = ap.parse_args(argv)

    store = store or Store(os.environ.get("DATABASE_PATH", "data/phone_agent.db"))
    hub = hub or _hub(store)

    def business(slug: str):
        row = store.business_by_slug(slug)
        if row is None:
            raise SystemExit(f"no business {slug!r}")
        return row

    try:
        if args.cmd == "status":
            for row in store.businesses():
                c = store.pos_connection(row.id)
                desc = (f"{c['provider']} ({c['environment']}) merchant {c['merchant_id'] or '-'}"
                        f" location {c['location_id'] or '-'}" if c else "no POS")
                failed = len(store.orders_to_send(0, row.id, ("failed",)))
                print(f"{row.id:>3}  {row.slug:<24} {desc}" + (f"  {failed} failed orders" if failed else ""))
        elif args.cmd == "connect-link":
            base = os.environ.get("PUBLIC_BASE_URL", "")
            if not base:
                raise SystemExit("PUBLIC_BASE_URL isn't set")
            print(connect_url(args.provider, business(args.slug).id, base, hub.providers, shop=args.shop))
        elif args.cmd == "connect-token":
            token = os.environ.get("POS_ACCESS_TOKEN", "").strip()
            if not token:
                raise SystemExit("put the access token in POS_ACCESS_TOKEN (not on the command line)")
            if args.provider in ("clover", "shopify") and not args.merchant:
                raise SystemExit(f"{args.provider.title()} needs --merchant")
            if args.provider == "shopify":
                from .pos_shopify import normalize_shop

                args.merchant = normalize_shop(args.merchant)
            conn = hub.save_connection(business(args.slug).id, args.provider, args.environment,
                                       {"access_token": token}, args.merchant, args.location)
            print(f"connected {args.slug} to {args.provider} ({args.environment}),"
                  f" location {conn.location_id or '-'}, settings {conn.settings}")
        elif args.cmd == "set-location":
            row = business(args.slug)
            if store.pos_connection(row.id) is None:
                raise SystemExit(f"{args.slug} has no POS connected")
            store.update_pos_connection(row.id, location_id=args.location)
            print("updated")
        elif args.cmd == "import-catalog":
            row = business(args.slug)
            conn = hub.connection(row.id)
            if conn is None:
                raise SystemExit(f"{args.slug} has no POS connected")
            current = store.catalog_payload(row.catalog_id) if row.catalog_id else None
            base = json.loads(open(args.base).read())["settings"] if args.base else None
            if base:
                current = {**(current or {}), "settings": base}
            catalog, notes = merge_import(current, hub.provider(conn.provider).catalog(conn), conn.provider)
            for n in notes:
                print(n, file=sys.stderr)
            print(json.dumps(catalog, indent=2, ensure_ascii=False))
        elif args.cmd == "retry":
            row = business(args.slug)
            if args.number:
                oid = store.order_id_by_number(row.id, args.number)
                if oid is None:
                    raise SystemExit(f"no order #{args.number}")
                ids = [oid]
            else:
                ids = store.orders_to_send(0, row.id, ("failed",))
            for oid in ids:
                hub.store.set_pos_status(oid, "pending")
                print(f"order id {oid}: {hub.send(oid)}")
        elif args.cmd == "disconnect":
            print("disconnected" if store.delete_pos_connection(business(args.slug).id) else "wasn't connected")
    except PosError as e:
        raise SystemExit(f"POS error: {e}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    sys.exit(main())
