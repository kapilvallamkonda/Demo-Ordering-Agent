"""Phone front end: Twilio ConversationRelay <-> PhoneAgent.

Call flow:
  1. A caller dials a business's Twilio number. Twilio POSTs to /voice with the
     dialed number (To), which picks the business.
  2. /voice answers with TwiML that <Connect>s the call to ConversationRelay, pointing
     it at our /ws WebSocket (with the business id in the URL). Twilio does the
     speech-to-text and text-to-speech.
  3. For each caller utterance Twilio sends {"type": "prompt", "voicePrompt": ...}.
     We stream Claude's reply back as {"type": "text", "token": ...} messages, so
     the caller hears it while it is still being generated.

Each call's state lives on its WebSocket connection, so you can run as many
instances of this server as you like behind a load balancer.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape, quoteattr

import anthropic
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from twilio.request_validator import RequestValidator

from . import brand
from .agent import PhoneAgent
from .google_auth import router as google_router
from .manage import router as manage_router
from .numbers import TwilioNumbers
from .onboarding import router as onboarding_router
from .catalog import Catalog
from .payments import handle_stripe_event, payment_service_from_env, sms_from_env
from .pos import PosError, PosHub, check_state, payments_for, providers_from_env
from .businesses import BusinessDirectory
from .storage import Store

log = logging.getLogger("phone_agent.server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
WORDS_PER_SECOND = 2.5  # rough speaking rate, used to let the goodbye finish before hanging up
# An interrupted reply that is mid tool call (e.g. placing the order) is allowed this long
# to finish before it is cancelled anyway.
TOOL_FINISH_SECONDS = 30.0
NOT_SET_UP_SECONDS = 4.0  # let "this number isn't taking orders" play before hanging up
# Calls one business can have at once. Past this its callers hear "all our lines are busy",
# so one busy business can't use up the server or the shared Claude rate limit.
MAX_CALLS_PER_BUSINESS = int(os.environ.get("MAX_CALLS_PER_BUSINESS", "20"))
CALL_TOKEN_SECONDS = 600  # a /voice answer's WebSocket URL must be used within this time
NOT_TAKING_CALLS = "Sorry, this number isn't taking orders right now. Goodbye."
LINES_BUSY = "Sorry, all our lines are busy right now. Please call back in a few minutes. Goodbye."

# Voice settings: env var -> <ConversationRelay> attribute. Unset ones are left off,
# so Twilio's defaults apply.
VOICE_ENV_TO_ATTR = {
    "TTS_PROVIDER": "ttsProvider",  # text-to-speech engine, e.g. ElevenLabs, Google, Amazon
    "TTS_VOICE": "voice",  # a voice ID/name valid for that provider
    "VOICE_LANGUAGE": "language",  # e.g. en-US
    "STT_PROVIDER": "transcriptionProvider",  # speech-to-text engine, e.g. Google, Deepgram
    "STT_MODEL": "speechModel",  # speech-to-text model for that provider
}


def relay_voice_attrs() -> dict[str, str]:
    """Voice attributes for <ConversationRelay>, read from the environment on each call.

    RELAY_EXTRA_ATTRS takes a JSON object of any other ConversationRelay attributes,
    e.g. '{"interruptible": "speech"}', so new Twilio options need no code change.
    """
    attrs = {attr: os.environ[env].strip() for env, attr in VOICE_ENV_TO_ATTR.items()
             if os.environ.get(env, "").strip()}
    extra = os.environ.get("RELAY_EXTRA_ATTRS", "").strip()
    if extra:
        try:
            parsed = json.loads(extra)
        except json.JSONDecodeError:
            log.error("RELAY_EXTRA_ATTRS is not valid JSON; ignoring it")
            parsed = {}
        if not isinstance(parsed, dict):
            log.error("RELAY_EXTRA_ATTRS must be a JSON object; ignoring it")
            parsed = {}
        for name, value in parsed.items():
            if name in {"url", "welcomeGreeting"} or not str(name).isidentifier():
                log.error("RELAY_EXTRA_ATTRS: skipping attribute %r", name)
                continue
            attrs[name] = str(value)
    return attrs


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store(os.environ.get("DATABASE_PATH", "data/phone_agent.db"))
    app.state.businesses = BusinessDirectory(app.state.store)
    app.state.active_calls = {}  # business id -> calls in progress (this process)
    listing = app.state.businesses.listing()
    log.info("%d business(es): %s", len(listing),
             ", ".join(f"{r['slug']} {r['phone_number'] or '(no number)'}" for r in listing) or "none")
    for r in listing:
        if r["problem"]:
            log.error("business %s is offline: %s", r["slug"], r["problem"])
    # One client shared by every call, so connections get pooled.
    app.state.client = getattr(app.state, "client", None) or anthropic.AsyncAnthropic()
    # Pay-by-text links; None means customers pay at pickup.
    if not hasattr(app.state, "payments"):
        app.state.payments = payment_service_from_env(PUBLIC_BASE_URL)
    log.info("payment links %s", "on" if app.state.payments else "off (pay at pickup)")
    # Point-of-sale integrations; None unless POS_SECRET_KEY is set (it encrypts the tokens).
    if not hasattr(app.state, "pos"):
        app.state.pos = (PosHub(app.state.store, app.state.businesses, providers_from_env(), sms=sms_from_env())
                         if os.environ.get("POS_SECRET_KEY", "").strip() else None)
    # Buying phone numbers when a business goes live; None without Twilio credentials.
    if not hasattr(app.state, "numbers"):
        app.state.numbers = TwilioNumbers.from_env()
    if app.state.pos:
        log.info("POS integrations on: %s; resending %d unsent orders",
                 ", ".join(app.state.pos.providers) or "no provider apps configured",
                 app.state.pos.resume())
    yield


app = FastAPI(title=brand.name(), lifespan=lifespan)
app.include_router(manage_router)  # /manage: business staff edit catalogs and settings
app.include_router(onboarding_router)  # self-serve: create a business, read a menu, go live, team ...
app.include_router(google_router)  # /auth/google: sign in with Google


ICONS = Path(__file__).parent / "static" / "icons"
ICON_TYPES = {"favicon.svg": "image/svg+xml", "favicon.ico": "image/x-icon", "apple-touch-icon.png": "image/png",
              "icon-192.png": "image/png", "icon-512.png": "image/png", "icon-maskable-512.png": "image/png"}


def _icon_route(name: str, media_type: str) -> None:
    @app.get("/" + name, include_in_schema=False)
    async def icon() -> Response:
        return Response((ICONS / name).read_bytes(), media_type=media_type,
                        headers={"Cache-Control": "public, max-age=86400"})


for _name, _type in ICON_TYPES.items():
    _icon_route(_name, _type)


@app.get("/site.webmanifest", include_in_schema=False)
async def web_manifest() -> Response:
    """Lets phones add the manager to the home screen with the app icon and the brand name."""
    return Response(json.dumps({
        "name": brand.name(), "short_name": brand.name(), "start_url": "/manage", "display": "standalone",
        "background_color": "#f6f5f2", "theme_color": "#b4441f",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }), media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    from .manage import PAGE_HEADERS

    return HTMLResponse(brand.page("landing.html", MAX_CALLS=MAX_CALLS_PER_BUSINESS), headers=PAGE_HEADERS)


def _public_base(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def _ws_url(base: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/ws"
    return base + "/ws"


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/voice")
async def voice(request: Request) -> Response:
    """Twilio voice webhook: hand the call to ConversationRelay."""
    form = dict(await request.form())
    if TWILIO_AUTH_TOKEN:
        url = _public_base(request) + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        signature = request.headers.get("x-twilio-signature", "")
        if not RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form, signature):
            log.warning("rejected /voice request with bad Twilio signature")
            raise HTTPException(status_code=403, detail="invalid signature")

    try:
        catalog = request.app.state.businesses.for_number(form.get("To"))
    except Exception:  # never let one business's problem become a Twilio error tone
        log.exception("call to %s: business lookup failed", form.get("To"))
        catalog = None
    if catalog is None:
        log.warning("call to %s: no active business has this number", form.get("To"))
        return _say_and_hang_up(NOT_TAKING_CALLS)
    if _active_calls(request.app, catalog.business_id) >= MAX_CALLS_PER_BUSINESS:
        log.warning("business %s: %d calls in progress; telling a new caller the lines are busy",
                    catalog.slug, MAX_CALLS_PER_BUSINESS)
        return _say_and_hang_up(LINES_BUSY)

    greeting = PhoneAgent(catalog, client=request.app.state.client).greeting
    ws_url = f"{_ws_url(_public_base(request))}?business={catalog.business_id}"
    token = call_token(catalog.business_id, form.get("CallSid", ""))
    if token:
        ws_url += f"&token={token}"
    attrs = {"url": ws_url, "welcomeGreeting": greeting, **relay_voice_attrs()}
    rendered = " ".join(f"{name}={quoteattr(value)}" for name, value in attrs.items())
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><ConversationRelay {rendered} /></Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


def _say_and_hang_up(text: str) -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Say>'
                            f"{escape(text)}</Say><Hangup/></Response>",
                    media_type="application/xml")


def _active_calls(app_: FastAPI, business_id: int) -> int:
    return getattr(app_.state, "active_calls", {}).get(business_id, 0)


# ---- binding a call's WebSocket to its business ---------------------------------------
# /voice (Twilio-signed) puts a token in the WebSocket URL: an HMAC of the business id,
# the call SID and an expiry, keyed from TWILIO_AUTH_TOKEN. /ws only starts a call whose
# token matches the business in the URL and the call SID in Twilio's setup message, so a
# WebSocket can't be opened for some business without a real call to it.


def _token_key() -> bytes:
    return hashlib.sha256(b"ws-call-token:" + TWILIO_AUTH_TOKEN.encode()).digest()


def call_token(business_id: int, call_sid: str, now: float | None = None) -> str | None:
    if not TWILIO_AUTH_TOKEN:
        return None  # local development without Twilio
    expires = int((now or time.time()) + CALL_TOKEN_SECONDS)
    sig = hmac.new(_token_key(), f"{business_id}.{call_sid}.{expires}".encode(), hashlib.sha256)
    return f"{expires}.{sig.hexdigest()[:32]}"


def check_call_token(token: str, business_id: int, call_sid: str, now: float | None = None) -> bool:
    if not TWILIO_AUTH_TOKEN:
        return True
    try:
        expires, sig = token.split(".")
        expires_at = int(expires)
    except ValueError:
        return False
    want = hmac.new(_token_key(), f"{business_id}.{call_sid}.{expires_at}".encode(), hashlib.sha256)
    return hmac.compare_digest(want.hexdigest()[:32], sig) and (now or time.time()) <= expires_at


def _require_token(request: Request) -> None:
    """Staff-only endpoints. Disabled (404) unless ADMIN_API_TOKEN is set."""
    if not ADMIN_API_TOKEN:
        raise HTTPException(status_code=404)
    if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {ADMIN_API_TOKEN}"):
        raise HTTPException(status_code=401)


@app.get("/businesses")
async def businesses(request: Request) -> list[dict]:
    _require_token(request)
    return request.app.state.businesses.listing()


@app.get("/orders")
async def orders(request: Request, limit: int = 20, business: int | None = None) -> list[dict]:
    """Recent placed orders, newest first; all businesses unless `business` (id) is given."""
    _require_token(request)
    return request.app.state.store.recent(max(1, min(limit, 500)), business_id=business)


@app.get("/calls")
async def calls(request: Request, limit: int = 50, business: int | None = None) -> list[dict]:
    """Recent phone calls, newest first, including ones that didn't place an order."""
    _require_token(request)
    return request.app.state.store.recent_calls(max(1, min(limit, 500)), business_id=business)


@app.get("/calls/{call_id}")
async def call_transcript(request: Request, call_id: str) -> dict:
    """What was said on one call: caller, agent, tool calls and events, in order."""
    _require_token(request)
    return {"call_id": call_id, "events": request.app.state.store.transcript(call_id)}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """Orders dashboard. The page holds no data; it asks for ADMIN_API_TOKEN and reads /orders."""
    if not ADMIN_API_TOKEN:
        raise HTTPException(status_code=404)
    return brand.page("dashboard.html")


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    """Stripe tells us a payment link was paid or expired."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=404)
    import stripe

    payload = await request.body()
    try:
        stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
    except Exception:
        log.warning("rejected Stripe webhook with bad signature")
        raise HTTPException(status_code=400, detail="invalid signature")
    state = request.app.state
    payments = state.payments
    pos = getattr(state, "pos", None)
    outcome = await asyncio.to_thread(
        handle_stripe_event, json.loads(payload), state.store, state.businesses.get,
        payments.sms if payments else None, pos.order_paid if pos else None,
    )
    return {"ok": True, "result": outcome}


@app.get("/paid", response_class=HTMLResponse)
async def paid_page(request: Request, order: int = 0, cancelled: int = 0, r: str = "") -> str:
    """Where Stripe sends the customer after the payment page."""
    catalog = request.app.state.businesses.by_slug(r)
    name = html.escape(catalog.settings.get("name", "the business") if catalog else "Thank you")
    msg = ("Payment cancelled. You can use the link in your text again, or pay at pickup."
           if cancelled else f"Thank you! Your payment for order #{order} was received.")
    return (f"<!doctype html><meta name=viewport content='width=device-width'>"
            f"<title>{name}</title><body style='font-family:sans-serif;padding:24px'>"
            f"<h2>{name}</h2><p>{msg}</p></body>")


def _page(title: str, msg: str) -> str:
    return (f"<!doctype html><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title><body style='font-family:sans-serif;padding:24px'>"
            f"<h2>{html.escape(title)}</h2><p>{html.escape(msg)}</p></body>")


@app.get("/pos/{provider}/callback", response_class=HTMLResponse)
async def pos_callback(request: Request, provider: str, code: str = "", state: str = "") -> HTMLResponse:
    """Where Square or Clover sends the business owner after they approve the connection.
    The link they started from (pos connect-link) carries a signed state naming the business."""
    hub: PosHub | None = getattr(request.app.state, "pos", None)
    if hub is None or provider not in hub.providers:
        raise HTTPException(status_code=404)
    query = dict(request.query_params)
    if query.get("error") or not code:
        log.warning("%s connect was declined: %s", provider, query.get("error_description") or query.get("error"))
        return HTMLResponse(_page("Not connected", "The connection wasn't approved. You can close this page."),
                            status_code=400)
    try:
        business_id = check_state(state, provider)
        row = request.app.state.store.business(business_id)
        if row is None:
            raise PosError("unknown business", retry=False)
        redirect = f"{_public_base(request)}/pos/{provider}/callback"
        prov = hub.provider(provider)
        tokens, merchant_id = await asyncio.to_thread(prov.exchange_code, code, query, redirect)
        conn = await asyncio.to_thread(hub.save_connection, business_id, provider, prov.environment,
                                       tokens, merchant_id)
    except PosError as e:
        log.warning("%s connect failed: %s", provider, e)
        return HTMLResponse(_page("Not connected", f"Something went wrong: {e}"), status_code=400)
    name = request.app.state.store.business_name(business_id) or row.slug
    log.info("business %s connected to %s (merchant %s, location %s)", row.slug, provider,
             merchant_id, conn.location_id)
    extra = ("" if conn.location_id or provider != "square" else
             " One more step on our side: this account has several locations, and we'll pick yours.")
    return HTMLResponse(_page(f"{name} is connected",
                              f"Phone orders will now appear in {provider.title()}.{extra} You can close this page."))


class RelayCall:
    """One phone call's ConversationRelay WebSocket session."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.agent: PhoneAgent | None = None
        self.reply_task: asyncio.Task | None = None
        self.call_id: str | None = None
        self.store: Store | None = None
        self._unrecorded: list[str] = []  # reply text sent but not yet in the transcript
        self.business_id: int | None = None  # set once the call is accepted (counted as active)
        self.slug: str | None = None

    def flush_spoken(self, suffix: str = "") -> None:
        text = "".join(self._unrecorded).strip()
        self._unrecorded = []
        if text or suffix:
            self.record("agent", f"{text} {suffix}".strip())

    def record(self, role: str, text: str) -> None:
        """Add a line to this call's transcript. Never lets a storage error break the call."""
        if not (self.store and self.call_id and text):
            return
        try:
            self.store.log_event(self.call_id, role, text)
        except Exception:
            log.exception("couldn't record transcript line")

    async def send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

    async def decline(self, text: str) -> None:
        """Say a short message and end the call (no agent is started)."""
        await self.send({"type": "text", "last": True, "token": text})
        await asyncio.sleep(NOT_SET_UP_SECONDS)
        await self.send({"type": "end"})

    async def run(self) -> None:
        try:
            async for raw in self.ws.iter_text():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("non-JSON message from relay: %r", raw[:200])
                    continue
                await self.handle(msg)
        except WebSocketDisconnect:
            pass
        finally:
            await self.cancel_reply()
            if self.business_id is not None:
                counts = self.ws.app.state.active_calls
                counts[self.business_id] = max(0, counts.get(self.business_id, 1) - 1)
            if self.agent:
                s = self.agent.session
                log.info("call %s for %s ended; order number=%s", s.call_id, self.slug, s.order_number)
                self.record("event", "Call ended" + (f"; order #{s.order_number} placed"
                                                     if s.order_number else "; no order placed"))
                if self.store and self.call_id:
                    try:
                        self.store.end_call(self.call_id, s.order_number)
                    except Exception:
                        log.exception("couldn't record call end")

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "setup":
            app_state = self.ws.app.state
            try:
                catalog = self.business_for_call(app_state.businesses, msg)
            except Exception:
                log.exception("call %s: business lookup failed", msg.get("callSid"))
                catalog = None
            if catalog is None:
                return await self.decline(NOT_TAKING_CALLS)
            if _active_calls(self.ws.app, catalog.business_id) >= MAX_CALLS_PER_BUSINESS:
                log.warning("business %s: too many calls; declining call %s", catalog.slug, msg.get("callSid"))
                return await self.decline(LINES_BUSY)
            try:
                # A Square-connected business takes Square payment links (and requires payment).
                payments, catalog = payments_for(getattr(app_state, "pos", None), app_state.store, catalog,
                                                 app_state.payments)
                self.agent = PhoneAgent(
                    catalog,
                    client=app_state.client,
                    store=app_state.store,
                    call_id=msg.get("callSid"),
                    caller_phone=msg.get("from"),
                    payments=payments,
                    pos=getattr(app_state, "pos", None),
                )
            except Exception:
                log.exception("business %s: couldn't start the agent for call %s", catalog.slug,
                              msg.get("callSid"))
                return await self.decline(NOT_TAKING_CALLS)
            self.business_id, self.slug = catalog.business_id, catalog.slug
            app_state.active_calls[self.business_id] = app_state.active_calls.get(self.business_id, 0) + 1
            self.agent.on_tool = self.record_tool
            self.call_id, self.store = msg.get("callSid"), app_state.store
            if self.call_id:
                try:
                    session = self.agent.session
                    caller = (session.order.customer_phone if session.order else None) or msg.get("from")
                    self.store.start_call(self.call_id, caller, catalog.business_id)
                except Exception:
                    log.exception("couldn't record call start")
            self.record("agent", self.agent.greeting)
            log.info("call %s started for %s", msg.get("callSid"), catalog.slug)
        elif kind == "prompt":
            if self.agent is None or not msg.get("last", True):
                return
            text = (msg.get("voicePrompt") or "").strip()
            if not text:
                return
            await self.cancel_reply()
            self.record("caller", text)
            self.reply_task = asyncio.create_task(self.reply(text))
        elif kind == "interrupt":
            heard = msg.get("utteranceUntilInterrupt")
            self.record("event", "Caller interrupted" + (f'; they heard: "{heard}"' if heard else ""))
            await self.cancel_reply()
            if self.agent:
                self.agent.note_interrupted(heard)
        elif kind == "dtmf":
            log.info("dtmf digit %s ignored", msg.get("digit"))
        elif kind == "error":
            log.error("relay error: %s", msg.get("description"))

    def record_tool(self, name: str, args: dict, result: str, is_error: bool) -> None:
        self.flush_spoken()  # keep the transcript in the order things happened
        args_text = json.dumps(args, ensure_ascii=False) if args else ""
        self.record("tool", f"{name}({args_text}) {'ERROR ' if is_error else ''}-> {result}")

    def business_for_call(self, directory: BusinessDirectory, setup: dict) -> Catalog | None:
        """The business /voice chose (in the URL). The call is refused unless the URL's
        token proves /voice issued it for this call, and the dialed number (if Twilio sent
        one) belongs to the same business."""
        call_sid = setup.get("callSid") or ""
        raw = self.ws.query_params.get("business", "")
        if not raw.isdigit():
            log.warning("call %s: no business in the WebSocket URL", call_sid)
            return None
        business_id = int(raw)
        if not check_call_token(self.ws.query_params.get("token", ""), business_id, call_sid):
            log.warning("call %s: refused, missing or bad call token for business %s", call_sid, business_id)
            return None
        catalog = directory.get(business_id, active_only=True)
        if catalog is None:
            log.warning("call %s: business %s isn't taking calls", call_sid, business_id)
            return None
        if setup.get("to"):
            dialed = directory.for_number(setup.get("to"))
            if dialed is None or dialed.business_id != business_id:
                log.warning("call %s: refused, URL says business %s but %s was dialed",
                            call_sid, business_id, setup.get("to"))
                return None
        return catalog

    async def cancel_reply(self) -> None:
        """Stop the current reply. A reply that is mid tool call (say, placing the order)
        is muted and allowed to finish, so the order isn't lost to an interruption."""
        task, self.reply_task = self.reply_task, None
        if not task or task.done():
            return
        if self.agent and self.agent.stop_speaking():
            self.record("event", "Interrupted during a tool call; finishing it silently")
            try:
                await asyncio.wait_for(asyncio.shield(task), TOOL_FINISH_SECONDS)
                return
            except asyncio.TimeoutError:
                log.error("tool call still running after %ss; cancelling it", TOOL_FINISH_SECONDS)
            except Exception:
                return  # reply() logs its own errors
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def reply(self, text: str) -> None:
        assert self.agent is not None
        spoken: list[str] = []
        self._unrecorded = []
        try:
            async for chunk in self.agent.respond(text):
                spoken.append(chunk)
                self._unrecorded.append(chunk)
                await self.send({"type": "text", "token": chunk, "last": False})
        except asyncio.CancelledError:
            if self._unrecorded:
                self.flush_spoken("[cut off]")
            raise
        except Exception:
            log.exception("call %s for %s: agent failed", self.call_id, self.slug)
            self.flush_spoken("[error] Sorry, something went wrong.")
            await self.send({"type": "text", "token": "Sorry, something went wrong.", "last": True})
            return
        self.flush_spoken()
        if self.reply_task is not asyncio.current_task():
            return  # interrupted mid tool call and finished silently; the next prompt takes over
        await self.send({"type": "text", "token": "", "last": True})

        if self.agent.end_requested:
            # Give text-to-speech time to finish the goodbye before ending the session.
            words = len("".join(spoken).split())
            await asyncio.sleep(min(10.0, 1.0 + words / WORDS_PER_SECOND))
            await self.send({"type": "end"})


@app.websocket("/ws")
async def relay(ws: WebSocket) -> None:
    if TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL:
        signature = ws.headers.get("x-twilio-signature")
        url = _ws_url(PUBLIC_BASE_URL) + (f"?{ws.url.query}" if ws.url.query else "")
        if signature and not RequestValidator(TWILIO_AUTH_TOKEN).validate(url, {}, signature):
            log.warning("rejected /ws connection with bad Twilio signature")
            await ws.close(code=1008)
            return
    await ws.accept()
    await RelayCall(ws).run()
