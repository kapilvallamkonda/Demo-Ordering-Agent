"""Phone front end: Twilio ConversationRelay <-> OrderingAgent.

Call flow:
  1. A caller dials your Twilio number. Twilio POSTs to /voice.
  2. /voice answers with TwiML that <Connect>s the call to ConversationRelay, pointing
     it at our /ws WebSocket. Twilio does the speech-to-text and text-to-speech.
  3. For each caller utterance Twilio sends {"type": "prompt", "voicePrompt": ...}.
     We stream Claude's reply back as {"type": "text", "token": ...} messages, so
     the caller hears it while it is still being generated.

Each call's state lives on its WebSocket connection, so you can run as many
instances of this server as you like behind a load balancer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from xml.sax.saxutils import quoteattr

import anthropic
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.request_validator import RequestValidator

from .agent import OrderingAgent
from .menu import DEFAULT_MENU_PATH, load_menu
from .storage import OrderStore

log = logging.getLogger("ordering_agent.server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ORDERS_API_TOKEN = os.environ.get("ORDERS_API_TOKEN", "")
WORDS_PER_SECOND = 2.5  # rough speaking rate, used to let the goodbye finish before hanging up


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.menu = load_menu(os.environ.get("MENU_PATH") or DEFAULT_MENU_PATH)
    app.state.store = OrderStore(os.environ.get("ORDERS_DB", "data/orders.db"))
    # One client shared by every call, so connections get pooled.
    app.state.client = getattr(app.state, "client", None) or anthropic.AsyncAnthropic()
    yield


app = FastAPI(title="Restaurant ordering agent", lifespan=lifespan)


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

    greeting = OrderingAgent(request.app.state.menu, client=request.app.state.client).greeting
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<ConversationRelay url={quoteattr(_ws_url(_public_base(request)))}"
        f" welcomeGreeting={quoteattr(greeting)} />"
        "</Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.get("/orders")
async def orders(request: Request) -> list[dict]:
    """Recent placed orders (demo kitchen view). Disabled unless ORDERS_API_TOKEN is set."""
    if not ORDERS_API_TOKEN:
        raise HTTPException(status_code=404)
    if request.headers.get("authorization") != f"Bearer {ORDERS_API_TOKEN}":
        raise HTTPException(status_code=401)
    return request.app.state.store.recent()


class RelayCall:
    """One phone call's ConversationRelay WebSocket session."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.agent: OrderingAgent | None = None
        self.reply_task: asyncio.Task | None = None

    async def send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

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
            if self.agent:
                s = self.agent.session
                log.info("call %s ended; order placed=%s number=%s", s.call_id, s.order.placed,
                         s.order_number)

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "setup":
            app_state = self.ws.app.state
            self.agent = OrderingAgent(
                app_state.menu,
                client=app_state.client,
                store=app_state.store,
                call_id=msg.get("callSid"),
                caller_phone=msg.get("from"),
            )
            log.info("call %s started", msg.get("callSid"))
        elif kind == "prompt":
            if self.agent is None or not msg.get("last", True):
                return
            text = (msg.get("voicePrompt") or "").strip()
            if not text:
                return
            await self.cancel_reply()
            self.reply_task = asyncio.create_task(self.reply(text))
        elif kind == "interrupt":
            await self.cancel_reply()
            if self.agent:
                self.agent.note_interrupted(msg.get("utteranceUntilInterrupt"))
        elif kind == "dtmf":
            log.info("dtmf digit %s ignored", msg.get("digit"))
        elif kind == "error":
            log.error("relay error: %s", msg.get("description"))

    async def cancel_reply(self) -> None:
        task, self.reply_task = self.reply_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def reply(self, text: str) -> None:
        assert self.agent is not None
        spoken: list[str] = []
        try:
            async for chunk in self.agent.respond(text):
                spoken.append(chunk)
                await self.send({"type": "text", "token": chunk, "last": False})
        except Exception:
            log.exception("agent failed")
            await self.send({"type": "text", "token": "Sorry, something went wrong.", "last": True})
            return
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
        if signature and not RequestValidator(TWILIO_AUTH_TOKEN).validate(
            _ws_url(PUBLIC_BASE_URL), {}, signature
        ):
            log.warning("rejected /ws connection with bad Twilio signature")
            await ws.close(code=1008)
            return
    await ws.accept()
    await RelayCall(ws).run()
