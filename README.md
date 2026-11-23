# Ringcart

Ringcart (code name Phone Agent: package `phone_agent`, repo `phone-agent`) is a demo AI phone agent for businesses. Callers dial a business's Twilio number and talk
to Claude, which takes their pickup order with tools, reads it back, places it and
gives an order number. Each business picks a **template** (restaurant or shop today;
salons and other appointment-based services are coming soon). Businesses sign up
themselves: sign in with Google, upload photos of the menu (Claude reads them into a
catalog), test the agent in the browser, and get a phone number, all at `/manage`.

```
Caller ──phone──▶ Twilio ──POST /voice──▶ this server ──TwiML <ConversationRelay>
                    │  speech-to-text / text-to-speech
                    └──── WebSocket /ws ◀──▶ PhoneAgent ──▶ Claude (Messages API, streaming)
                                                 │
                                                 └─ order tools ─▶ Order (validation) ─▶ SQLite
```

- **Twilio ConversationRelay** converts speech to text and text to speech. We get the caller's words as
  text and stream Claude's reply back token by token, so the caller hears it right away.
- **Claude** (`claude-opus-5-5`, low effort for snappy turns) decides what to say and
  calls the order tools: `add_item`, `remove_item`, `update_quantity`, `view_order`,
  `set_customer_name`, `place_order`, `end_call`.
- **The catalog is in the system prompt** (cached), so answering questions about it needs no
  tool round-trip. **Prices and totals come only from code** (`phone_agent/order.py`).
  The model can't invent items, skip required options, or miscompute a total.
- **Interruptions:** if the caller talks over the agent, the in-flight reply is cancelled
  and the agent is told what the caller actually heard. If the agent was in the middle of
  a tool call (such as placing the order), that call finishes silently first, so it isn't lost.
- **Call transcripts:** every call is recorded (caller, agent, tool calls, interruptions)
  and readable on the dashboard's Calls tab.

See [docs/DESIGN.md](docs/DESIGN.md) for the full high-level design.

## Project layout

| Path | What it is |
|---|---|
| `data/businesses/<slug>.json` | One catalog file per business (template, settings and items), e.g. `hyderabad-house.json`, a restaurant with about 210 items. Optional `settings.about` and `settings.faq` (common questions with answers) are added to the prompt; items can carry day and notice rules. Live calls use the copy stored in the database; these files are what you load in. |
| `.claude/skills/add-business/` | Claude Code skill (`/add-business`) that onboards a business end to end: menu or product photos to a catalog file, checks, Twilio number, registration on Fly, verification. Includes `scripts/check_catalog.py` and the catalog format guide. |
| `tests/fixtures/catalog.json` | Small fixed catalog the tests use, so they don't depend on real ones. |
| `phone_agent/modules/` | What a call can do: one module per capability (`orders.py` now; bookings coming soon), each with its tools, rules, prompt section and per-call state. Add a module here. |
| `phone_agent/session.py` | Per-call state and the helpers modules use for tools. |
| `phone_agent/order.py` | Order state and every business rule (options, extras, quantities, tax). |
| `phone_agent/tools.py` | Tool schemas for Claude and the dispatcher that runs them. |
| `phone_agent/prompts.py` | The phone-voice system prompt. |
| `phone_agent/agent.py` | Streaming tool-use loop: one `PhoneAgent` per call. |
| `phone_agent/server.py` | FastAPI app: `/voice` (Twilio webhook) and `/ws` (ConversationRelay). |
| `phone_agent/businesses.py` | Routes each call to a business by the number dialed; admin commands to add businesses and publish catalogs. |
| `phone_agent/cli.py` | Chat with the agent in a terminal, no phone needed. |
| `phone_agent/payments.py` | Pay by text: Stripe Checkout link per order, sent by SMS; Stripe webhook handling. |
| `phone_agent/pos.py`, `pos_square.py`, `pos_clover.py` | Point-of-sale integrations: connect a business's Square or Clover, send each placed order to it, import its catalog. Admin commands: `python -m phone_agent.pos --help`. |
| `phone_agent/templates.py` | Kinds of business (restaurant, retail shop; salon coming soon): modules, wording, prompt rules, tags, settings. Add a kind of business here. |
| `data/templates/<id>.json` | Starter catalogs for each template. |
| `phone_agent/onboarding.py` | Self-serve API: create a business, read a menu from photos, test chat, go live (buys a number), team, orders and calls, POS, operator approvals; usage limits. |
| `phone_agent/google_auth.py` | Sign in with Google (`/auth/google`). |
| `phone_agent/catalog_import.py` | Reads menu or price-list photos and PDFs into catalog items (one Claude call with a JSON schema). |
| `phone_agent/numbers.py` | Buys a Twilio number for a business going live and points it at `/voice`. |
| `phone_agent/static/landing.html` | The public home page at `/`. |
| `phone_agent/manage.py`, `static/manage.html` | Manager at `/manage`: business staff sign in and edit their catalog and settings (draft, publish, history). |
| `phone_agent/accounts.py` | Staff accounts: invite links, passwords, sessions. `python -m phone_agent.accounts --help`. |
| `phone_agent/settings_schema.py` | The business settings staff can edit; the settings form is built from it. Add a setting here. |
| `phone_agent/catalog_check.py` | Catalog checks used by the manager and the add-business skill. |
| `phone_agent/static/dashboard.html` | Orders dashboard served at `/dashboard`. |
| `phone_agent/storage.py` | Businesses, catalog versions, orders, calls, accounts (SQLite). |

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY (or run `ant auth login`)
set -a; . ./.env; set +a
```

### 1. Try it in the terminal

```bash
python -m phone_agent.cli                            # uses data/businesses/hyderabad-house.json
python -m phone_agent.cli --business hyderabad-house  # a business saved in the database
```

### Add a business

One server serves many businesses. Each has a slug, the Twilio number callers dial
(that's how a call finds its business) and a catalog, whose `template` says what kind
of business it is (`python -m phone_agent.businesses templates`; starters in `data/templates/`):

```bash
python -m phone_agent.businesses add hyderabad-house --phone +19135783983 --catalog data/businesses/hyderabad-house.json
python -m phone_agent.businesses list
python -m phone_agent.businesses set-catalog hyderabad-house --catalog data/businesses/hyderabad-house.json  # publish an edited catalog
python -m phone_agent.businesses set-phone hyderabad-house --phone +19135551234
python -m phone_agent.businesses deactivate hyderabad-house                     # stop taking calls
```

In Claude Code, `/add-business` walks through the whole onboarding (catalog, checks,
number, registration, test). To check a catalog file by hand:
`.venv/bin/python .claude/skills/add-business/scripts/check_catalog.py data/businesses/<slug>.json`.

Catalogs are checked before they're saved, and every version is kept. A new one is used
from the next call on, no restart needed. A call to a number no business has hears a
short apology and is hung up. On Fly.io, run these with
`fly ssh console -C "python -m phone_agent.businesses list"`.

### 2. Take real phone calls

1. Start the server: `uvicorn phone_agent.server:app --port 8000`
2. Expose it publicly, e.g. `ngrok http 8000`, and set `PUBLIC_BASE_URL` to the https URL.
3. In the Twilio Console, buy a phone number with voice capability for each business.
   Under *Voice Configuration*, set **A call comes in** → Webhook → `https://<your-host>/voice`
   (HTTP POST); every number uses the same URL. Register the number on its business
   (`businesses add ... --phone` or `set-phone`).
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
4. Choose each business's rule in its settings (in `/manage`, or the catalog file then `set-catalog`):
   `"payment": {"required_before_cooking": false, "pay_within_minutes": 30}`.
   With `true`, orders wait as `awaiting_payment` until paid and are cancelled if the
   link expires.

Without `STRIPE_SECRET_KEY`, customers pay at pickup.

Businesses, catalogs, orders, transcripts and accounts are in `data/phone_agent.db`.

### 4. Orders dashboard

Set `ADMIN_API_TOKEN` to a long random string, restart, and open `/dashboard`
(e.g. `http://127.0.0.1:8000/dashboard`). Enter the token once; the browser remembers it.
The page shows today's order count and sales, and every order with items, total, order
status and payment status. You can filter and search, and it refreshes every 10 seconds.
The Calls tab lists every call, including ones that didn't end in an order; click one to
read its transcript. With more than one business, a picker at the top shows one
business or all of them. The same data is at `GET /businesses`,
`GET /orders?limit=50&business=<id>`, `GET /calls?limit=50&business=<id>` and
`GET /calls/<CallSid>`, all with `Authorization: Bearer <token>`. The token sees every
business; moving the dashboard onto staff sign-in comes next.

### 5. Host it on Fly.io

The repo has a `Dockerfile` and `fly.toml`: one always-on machine (calls must be answered
instantly) with a volume at `/data` for the orders database.

```bash
fly apps create <app-name>                      # then put the name in fly.toml
fly volumes create app_data --region ord --size 1 --app <app-name>
fly secrets set ANTHROPIC_API_KEY=... TWILIO_AUTH_TOKEN=... ADMIN_API_TOKEN=... \
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
| `TTS_PROVIDER` | `ElevenLabs` | Text-to-speech engine (`ttsProvider`), e.g. ElevenLabs, Google, Amazon. |
| `TTS_VOICE` | `mCQMfsqGDT6IDkEKR20a-v4_turbo` | Voice for that provider (`voice`): Indian English, ElevenLabs v4. |
| `TTS_LANGUAGE` | `en-IN` | Language the English voice speaks (`ttsLanguage`). Indian English says Indian dish names correctly. |
| `VOICE_LANGUAGE` | Twilio default | Speech and listening language (`language`), e.g. `en-US`. Listening stays English (US) unless set. |
| `STT_PROVIDER` | Twilio default | Speech-to-text engine (`transcriptionProvider`), e.g. Google, Deepgram. |
| `STT_MODEL` | Twilio default | Speech-to-text model (`speechModel`). |
| `GREETING_INTERRUPTIBLE` | `dtmf` | What can cut off the greeting (`welcomeGreetingInterruptible`): only the keypad, so background noise can't. |
| `INTERRUPT_SENSITIVITY` | `low` | How easily the caller's sound cuts the agent off (`interruptSensitivity`: high, medium, low). Low keeps TV or kitchen noise from interrupting. |
| `IGNORE_BACKCHANNEL` | `true` | "Uh-huh", "okay" while the agent talks don't interrupt it (`ignoreBackchannel`). |
| `RELAY_EXTRA_ATTRS` | unset | JSON object of any other `<ConversationRelay>` attributes. |
| `TWILIO_AUTH_TOKEN` | unset | Enables Twilio signature validation; also used to send texts. |
| `TWILIO_ACCOUNT_SID` | unset | Twilio account, for sending payment-link texts. |
| `TWILIO_SMS_FROM` | unset | Twilio number the texts come from (E.164). |
| `STRIPE_SECRET_KEY` | unset | Turns on payment links by text. |
| `STRIPE_WEBHOOK_SECRET` | unset | Enables `/stripe/webhook` and checks Stripe's signature. |
| `PUBLIC_BASE_URL` | from request | Public URL Twilio uses; needed for signature checks behind proxies. |
| `CALL_REVIEWS` | `on` | `off` stops the AI review of each finished call (analytics). |
| `CALL_REVIEW_MODEL` | `claude-haiku-5-5` | Model for call reviews. |
| `CALL_REVIEWS_PER_BUSINESS_PER_DAY` | `300` | Daily cap on call reviews per business. |
| `ANALYTICS_SALT` | `POS_SECRET_KEY` | Secret used to hash caller numbers for repeat-caller counts. |
| `PRICE_AGENT_INPUT`, `PRICE_AGENT_OUTPUT`, `PRICE_AGENT_CACHE_READ`, `PRICE_AGENT_CACHE_WRITE`, `PRICE_REVIEW_INPUT`, `PRICE_REVIEW_OUTPUT` | Opus 5.5 / Haiku 5.5 list prices | Dollars per million tokens, for the cost figures in Analytics. |
| `POSTHOG_KEY`, `POSTHOG_HOST` | unset, `https://us.i.posthog.com` | PostHog in the browser: page views, funnels, session replays (text masked on business pages). |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_RELEASE` | unset, `production`, `0.1` in production and `1.0` elsewhere, the Fly image | Sentry for the server: errors (including logged ones), tracing and logs, with no personal data or call content. |
| `DEFAULT_LANGUAGES` | `te-IN,hi-IN,ta-IN,kn-IN` | Languages a business offers until it chooses in Settings (comma-separated): Telugu, Hindi, Tamil, Kannada. Empty turns the default off for every business. |
| `<LANG>_SWITCH_STT` (`TELUGU_`, `HINDI_`, `TAMIL_`, `KANNADA_`) | `Deepgram:nova-3-general:multi` | Recognition set when the call switches to that language (provider:model:code); `none` uses the declared Google recognition. |
| `<LANG>_VOICE`, `<LANG>_SPEECH_MODEL`, `<LANG>_TTS_PROVIDER`, `<LANG>_STT_PROVIDER` | Telugu `UgBBYS2sOqTuMpoF3BR0-v4_turbo`, others `mCQMfsqGDT6IDkEKR20a-v4_turbo`; `long`, `ElevenLabs`, `Google` | Voice and speech recognition for calls switched to that language (businesses that offer it in Settings). |
| `DEFAULT_TIMEZONE` | `America/Chicago` | Time zone for businesses that haven't set one (new businesses take it from the owner's browser). Pickup days and item day rules use the business's time zone. |
| `BRAND_NAME` | `Ringcart` | Product name shown on the website, sign-in and dashboards (`phone_agent/brand.py`). |
| `DATABASE_PATH` | `data/phone_agent.db` | SQLite file: businesses, catalogs, orders, transcripts, accounts. |
| `CATALOG_PATH` | `data/businesses/hyderabad-house.json` | Catalog file for the terminal chat when `--business` isn't given. Live calls use catalogs in the database. |
| `ADMIN_API_TOKEN` | unset | Enables the `/dashboard` page, `GET /orders` and `GET /calls` (transcripts). |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | unset | Sign in with Google for business owners and staff. Without them, `/manage` falls back to email + password (invited accounts only). |
| `OPERATOR_EMAILS` | unset | Comma-separated emails of operators: they see every business and approve go-live requests. |
| `ALLOW_PASSWORD_LOGIN` | unset | Also offer email + password sign-in when Google is configured. |
| `IMPORT_MODEL` | `claude-opus-5-5` | Model that reads menu photos and PDFs. |
| `MAX_BUSINESSES_PER_ACCOUNT` | `3` | Businesses one account can create. |
| `FREE_NUMBERS_PER_ACCOUNT` | `1` | Phone numbers bought automatically for one account (in total); more wait for an operator. |
| `NUMBERS_PER_DAY` | `5` | Phone numbers bought automatically per day across all accounts; more wait for an operator. |
| `IMPORTS_PER_BUSINESS_PER_DAY` | `10` | Menu reads from photos per business per day. |
| `TEST_TURNS_PER_BUSINESS_PER_DAY` | `60` | Test-chat messages per business per day. |
| `MAX_CALLS_PER_BUSINESS` | `20` | Calls one business can have at once; past this its callers hear "all our lines are busy", so one busy business can't crowd out the others. |
| `POS_SECRET_KEY` | unset | Turns on POS integrations and encrypts their tokens. A Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep it safe: without it every business must reconnect. |
| `SQUARE_APPLICATION_ID`, `SQUARE_APPLICATION_SECRET` | unset | Square app credentials (Developer Console), for connect links. |
| `SQUARE_ENVIRONMENT` | `sandbox` | `sandbox` or `production`. |
| `SQUARE_API_VERSION` | `2025-01-23` | `Square-Version` header. |
| `CLOVER_APP_ID`, `CLOVER_APP_SECRET` | unset | Clover app credentials (developer dashboard), for connect links. |
| `CLOVER_ENVIRONMENT` | `sandbox` | `sandbox` or `production` (North America). |
| `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | unset | Shopify app credentials (Dev Dashboard), for connect links. The app's redirect URL is `<PUBLIC_BASE_URL>/pos/shopify/callback`; scopes `read_products`, `write_draft_orders`, `read_orders`. |
| `SHOPIFY_API_VERSION` | `2026-10` | Admin GraphQL API version. |
| `POS_ALLOW_TOKENS` | unset | Enables every provider without app credentials, for `pos connect-token` with a sandbox token. |

Voice settings are read on every incoming call, so changing them only needs a server
restart, no code change. `none` turns a default off (Twilio's own default applies). Speech costs are billed through your Twilio account. Valid provider
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
- Before production: swap SQLite for a shared database (`storage.py`), and check your Anthropic rate limits against peak concurrent calls.

## Known limitations / next steps

- Pickup only; no delivery or reservations (the agent says so politely). Refunds are
  done in Stripe.
- Hang-up timing after the goodbye is estimated from the word count (`server.py`). Tune it
  after real calls.
- Some resources are still shared by all businesses (Anthropic key, Stripe account, SMS number, one SQLite file); see the design doc, section 5.
- DTMF (keypad) input is ignored.
- Square and Clover integrations are tested against fake APIs only so far; Toast needs
  its partner program first (`docs/toast-partner-application.md`).

## For businesses: self-serve sign-up

Owners start at `/` → "Get started with Google", then:

1. Pick the kind of business (restaurant or shop) and type its name.
2. Upload photos or a PDF of the menu or price list. Claude reads it into a catalog
   (prices it can't read are marked "estimated"); edit anything by hand.
3. Fill in tax, pickup time and hours, and try the agent in the browser chat.
4. Publish, then "Get my phone number". The first number per account is bought
   automatically (within a daily limit); others wait for an operator to approve
   (Operator tab). Owners can forward their existing line to the new number.
5. Watch orders and calls (with transcripts), add teammates by email, connect Square or
   Clover.

Operators (`OPERATOR_EMAILS`) see every business and the go-live queue. See
[docs/DESIGN.md](docs/DESIGN.md) section 16.

## Manager (for business staff)

Businesses edit their own catalog at `/manage`: items, descriptions, prices,
choices and extras, sold-out switches, and settings (greeting, hours, address, pickup
time, tax, payment rule, time zone, About and Common questions). Changes save to a draft automatically and
reach callers when someone presses Publish. See [docs/DESIGN.md](docs/DESIGN.md) section 14.

```bash
# On the server: invite someone (prints a one-time link where they choose a password)
python -m phone_agent.accounts invite owner@example.com --business hyderabad-house --name "Owner"
python -m phone_agent.accounts list
```

To add a setting staff can edit, add an entry to `SETTINGS` in `phone_agent/settings_schema.py`.

## POS integrations (Square, Clover)

Placed orders can go straight onto the business's Square or Clover POS, and its
catalog can be imported from there. See [docs/DESIGN.md](docs/DESIGN.md) section 13.

```bash
# On the server (fly ssh console -C "..."), with POS_SECRET_KEY and the app credentials set:
python -m phone_agent.pos connect-link hyderabad-house square   # send this link to the owner
python -m phone_agent.pos status
python -m phone_agent.pos import-catalog hyderabad-house > catalog.json   # review, check_catalog.py, set-catalog
python -m phone_agent.pos retry hyderabad-house                  # resend orders that failed
```

Square only shows API orders on the POS once they're paid; pay-at-pickup orders for a
Square business appear on our dashboard only.
