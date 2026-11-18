# Demo-Ordering-Agent

A demo AI agent that takes restaurant pickup orders **over the phone**. Callers dial a
Twilio number and talk to Claude, which builds the order with tools, reads it back,
places it and gives an order number.

```
Caller ──phone──▶ Twilio ──POST /voice──▶ this server ──TwiML <ConversationRelay>
                    │  speech-to-text / text-to-speech
                    └──── WebSocket /ws ◀──▶ OrderingAgent ──▶ Claude (Messages API, streaming)
                                                 │
                                                 └─ order tools ─▶ Order (validation) ─▶ SQLite
```

- **Twilio ConversationRelay** converts speech to text and text to speech. We get the caller's words as
  text and stream Claude's reply back token by token, so the caller hears it right away.
- **Claude** (`claude-opus-5-5`, low effort for snappy turns) decides what to say and
  calls the order tools: `add_item`, `remove_item`, `update_quantity`, `view_order`,
  `set_customer_name`, `place_order`, `end_call`.
- **The menu is in the system prompt** (cached), so answering menu questions needs no
  tool round-trip. **Prices and totals come only from code** (`ordering_agent/order.py`).
  The model can't invent items, skip required options, or miscompute a total.
- **Interruptions:** if the caller talks over the agent, the in-flight reply is cancelled
  and the agent is told what the caller actually heard.

See [docs/DESIGN.md](docs/DESIGN.md) for the full high-level design.

## Project layout

| Path | What it is |
|---|---|
| `data/menu.json` | The restaurant and its menu (an Indian restaurant's menu, about 190 items). Edit this to make it yours. Optional `restaurant.notes` lines are added to the prompt. |
| `tests/fixtures/menu.json` | Small fixed menu the tests use, so they don't depend on the real menu. |
| `ordering_agent/order.py` | Order state and every business rule (options, extras, quantities, tax). |
| `ordering_agent/tools.py` | Tool schemas for Claude and the dispatcher that runs them. |
| `ordering_agent/prompts.py` | The phone-voice system prompt. |
| `ordering_agent/agent.py` | Streaming tool-use loop: one `OrderingAgent` per call. |
| `ordering_agent/server.py` | FastAPI app: `/voice` (Twilio webhook) and `/ws` (ConversationRelay). |
| `ordering_agent/cli.py` | Chat with the agent in a terminal, no phone needed. |
| `ordering_agent/payments.py` | Pay by text: Stripe Checkout link per order, sent by SMS; Stripe webhook handling. |
| `ordering_agent/static/dashboard.html` | Orders dashboard served at `/dashboard`. |
| `ordering_agent/storage.py` | Saves placed orders (SQLite) with order and payment status. |

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY (or run `ant auth login`)
set -a; . ./.env; set +a
```

### 1. Try it in the terminal

```bash
python -m ordering_agent.cli
```

### 2. Take real phone calls

1. Start the server: `uvicorn ordering_agent.server:app --port 8000`
2. Expose it publicly, e.g. `ngrok http 8000`, and set `PUBLIC_BASE_URL` to the https URL.
3. In the Twilio Console, buy a phone number with voice capability. Under *Voice
   Configuration*, set **A call comes in** → Webhook → `https://<your-host>/voice` (HTTP POST).
   ConversationRelay may require accepting Twilio's AI/ML features terms in the Console first.
4. Set `TWILIO_AUTH_TOKEN` so the server validates Twilio's request signatures.
5. Call the number.

### 3. Payment links by text (optional)

After an order is placed, the agent texts the caller the order details and a secure
Stripe payment link, so no one reads card numbers over the phone.

1. Get your Stripe secret key (use a `sk_test_...` key to try it) and set `STRIPE_SECRET_KEY`.
2. Set `TWILIO_ACCOUNT_SID` and `TWILIO_SMS_FROM` (your Twilio number, e.g. `+19135551234`).
   Texting US numbers from a local number requires A2P 10DLC registration in the
   Twilio Console, or carriers block the messages.
3. In Stripe, add a webhook endpoint `https://<your-host>/stripe/webhook` for
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed` and `checkout.session.expired`, and set
   `STRIPE_WEBHOOK_SECRET` to its signing secret.
4. Choose the restaurant's rule in `data/menu.json`:
   `"payment": {"required_before_cooking": false, "pay_within_minutes": 30}`.
   With `true`, orders wait as `awaiting_payment` until paid and are cancelled if the
   link expires.

Without `STRIPE_SECRET_KEY`, customers pay at pickup.

Placed orders land in `data/orders.db`.

### 4. Orders dashboard

Set `ORDERS_API_TOKEN` to a long random string, restart, and open `/dashboard`
(e.g. `http://127.0.0.1:8000/dashboard`). Enter the token once; the browser remembers it.
The page shows today's order count and sales, and every order with items, total, order
status and payment status. You can filter and search, and it refreshes every 10 seconds.
The same data is at `GET /orders?limit=50` with `Authorization: Bearer <token>`.

### 5. Host it on Fly.io

The repo has a `Dockerfile` and `fly.toml`: one always-on machine (calls must be answered
instantly) with a volume at `/data` for the orders database.

```bash
fly apps create <app-name>                      # then put the name in fly.toml
fly volumes create orders_data --region ord --size 1 --app <app-name>
fly secrets set ANTHROPIC_API_KEY=... TWILIO_AUTH_TOKEN=... ORDERS_API_TOKEN=... \
    PUBLIC_BASE_URL=https://<app-name>.fly.dev   # plus Stripe/Twilio SMS settings if used
fly deploy
```

Then point the Twilio number's voice webhook (and the Stripe webhook) at
`https://<app-name>.fly.dev`. The dashboard is at `https://<app-name>.fly.dev/dashboard`.
Logs: `fly logs`. Keep it to one machine while orders are in SQLite.

### Tests

```bash
python -m pytest
```

The tests use a scripted fake Claude client (`tests/fakes.py`), so they need no API key.
They include a full phone order over the WebSocket.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODEL` | `claude-opus-5-5` | Claude model. |
| `AGENT_EFFORT` | `low` | Thinking effort. `low` is fastest; try `medium` if complex orders go wrong. |
| `AGENT_FAST_MODE` | `false` | Opus 5.5 fast mode: up to ~2.5× faster output at premium pricing. |
| `TTS_PROVIDER` | Twilio default | Text-to-speech engine (`ttsProvider`), e.g. ElevenLabs, Google, Amazon. |
| `TTS_VOICE` | Twilio default | Voice for that provider (`voice`). |
| `VOICE_LANGUAGE` | Twilio default | Spoken language (`language`), e.g. `en-US`. |
| `STT_PROVIDER` | Twilio default | Speech-to-text engine (`transcriptionProvider`), e.g. Google, Deepgram. |
| `STT_MODEL` | Twilio default | Speech-to-text model (`speechModel`). |
| `RELAY_EXTRA_ATTRS` | unset | JSON object of any other `<ConversationRelay>` attributes. |
| `TWILIO_AUTH_TOKEN` | unset | Enables Twilio signature validation; also used to send texts. |
| `TWILIO_ACCOUNT_SID` | unset | Twilio account, for sending payment-link texts. |
| `TWILIO_SMS_FROM` | unset | Twilio number the texts come from (E.164). |
| `STRIPE_SECRET_KEY` | unset | Turns on payment links by text. |
| `STRIPE_WEBHOOK_SECRET` | unset | Enables `/stripe/webhook` and checks Stripe's signature. |
| `PUBLIC_BASE_URL` | from request | Public URL Twilio uses; needed for signature checks behind proxies. |
| `ORDERS_DB` | `data/orders.db` | SQLite file for placed orders. |
| `MENU_PATH` | `data/menu.json` | Menu file. |
| `ORDERS_API_TOKEN` | unset | Enables the `/dashboard` page and `GET /orders`. |

Voice settings are read on every incoming call, so changing them only needs a server
restart, no code change. Speech costs are billed through your Twilio account. Valid provider
names and voice IDs are listed in Twilio's ConversationRelay docs.

Server-side refusal fallbacks are on (`fallbacks: "default"`): if a safety classifier
declines a turn, the API re-runs it on a fallback model so the caller isn't left in silence.

## Scaling

The Messages API is stateless HTTP, so scale is about this server, not the API:

- Each call's state lives on that call's WebSocket, and a call stays on one instance for
  its whole duration. Run as many instances as you like behind a load balancer that supports
  WebSockets. No sticky sessions or shared cache are required.
- One `AsyncAnthropic` client per process pools connections; each call is a lightweight
  asyncio task, so one process handles many concurrent calls.
- Before production: swap SQLite for a shared database or post orders to your POS
  (`storage.py`), and check your Anthropic rate limits against peak concurrent calls.

## Known limitations / next steps

- Pickup only; no delivery or reservations (the agent says so politely). Refunds are
  done in Stripe.
- Hang-up timing after the goodbye is estimated from the word count (`server.py`). Tune it
  after real calls.
- `/ws` rejects a bad Twilio signature, but doesn't yet require one to be present.
- DTMF (keypad) input is ignored.
