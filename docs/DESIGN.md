# High-Level Design: Phone Agent for Businesses

Status: demo, hosted on Fly.io since 2026-11-18; no phone order saved yet. Last updated: 2026-11-22 (business templates).

## 1. Goal

Let a caller phone a business and get what they called for by talking to an AI agent.
Today that's ordering: the agent answers questions about what the business sells,
builds the order, reads it back, places it after the caller confirms, and gives an
order number and pickup time. It then texts the order details and a secure payment
link to the caller, so card details are never shared over the phone.

The agent is generic: a **template** (section 15) says what kind of business it is
(restaurant, shop; salons and other services are coming soon) and which **modules** it
uses (orders now; bookings later). The business itself enters its details and
catalog in the web manager (section 14). Restaurants were first, so examples below
often use them.

**In scope:** pickup orders, menu questions, changing the order during the call,
payment by texted Stripe link (or at pickup).
**Out of scope (for now):** delivery, reservations, complaints, refunds. The agent
says politely that this line only takes pickup orders.

## 2. Architecture

```mermaid
flowchart LR
    Caller((Caller)) -- phone call --> Twilio
    subgraph Twilio
        STT[Speech-to-text]
        TTS[Text-to-speech]
    end
    Twilio -- "POST /voice" --> Server
    Server -- "TwiML: ConversationRelay" --> Twilio
    Twilio <-- "WebSocket /ws (text)" --> Server
    subgraph Server[FastAPI server]
        Relay[RelayCall: one per call]
        Agent[OrderingAgent]
        Order[Order: business rules]
    end
    Relay --> Agent
    Agent <-- "streaming Messages API" --> Claude[(Claude API)]
    Agent -- tool calls --> Order
    Order -- placed orders --> DB[(SQLite)]
    Agent -- "place_order: Checkout Session" --> Stripe[(Stripe)]
    Agent -- "SMS: order + pay link" --> TwilioSMS[Twilio Messaging]
    Stripe -- "POST /stripe/webhook" --> Server
    DB -- "placed orders (background)" --> POS[(Square / Clover)]
```

| Component | File | Responsibility |
|---|---|---|
| Twilio ConversationRelay | (Twilio) | Phone line, speech-to-text, text-to-speech, interruption detection. |
| Voice webhook | `ordering_agent/server.py` `/voice` | Finds the restaurant by the dialed number (`To`) and answers with that restaurant's greeting and TwiML that connects the call to ConversationRelay (restaurant id in the `/ws` URL), including voice settings. Unknown numbers get a short apology and hang-up. |
| Restaurant directory | `ordering_agent/restaurants.py` | Maps a dialed number or id to the restaurant's current menu (parsed menus cached by menu version). Also the admin commands: add a restaurant, publish a menu, set its number, activate/deactivate. |
| Relay session | `server.py` `RelayCall`, `/ws` | One per call. Turns relay messages into agent turns, streams reply text back, handles interruptions and hang-up. |
| Agent | `ordering_agent/agent.py` | Streaming Claude tool-use loop for one call. Yields text chunks as they arrive. |
| System prompt | `ordering_agent/prompts.py` | Speaking style for phone, ordering procedure, the full menu. |
| Tools | `ordering_agent/tools.py` | Tool schemas for Claude, input type checks, dispatch to the order. |
| Order | `ordering_agent/order.py` | Cart state and all business rules: valid items, required options, extras, quantities, tax, totals. |
| Menu | `ordering_agent/menu.py`; menus stored per restaurant in the database, loaded from `data/restaurants/<slug>.json` files | Restaurant info and settings, optional restaurant notes, and menu, rendered into the prompt deterministically. Validated before it's saved. Dishes sold in several versions (e.g. chicken/fish/shrimp) are one item with a required option priced as deltas from the cheapest version. Tests use `tests/fixtures/menu.json`. |
| Payments | `ordering_agent/payments.py` | Creates a Stripe Checkout link per order, texts it with the order details via Twilio, applies Stripe webhook events (paid / expired) to the order. |
| Stripe webhook | `server.py` `/stripe/webhook`, `/paid` | Verifies Stripe's signature and updates payment and order status; `/paid` is the page shown after paying. |
| Storage | `ordering_agent/storage.py` | Restaurants, menu versions, placed orders (order and payment status, per-restaurant order numbers) and call transcripts, in SQLite. All SQL is in this file. Adds new columns to older databases on startup. |
| POS integrations | `ordering_agent/pos.py`, `pos_square.py`, `pos_clover.py`; `server.py` `/pos/<provider>/callback` | Connects a restaurant's Square or Clover account (OAuth, encrypted tokens), sends each placed order to it in the background with retries, records the online payment there, imports the POS menu into a menu file. Section 13. |
| Terminal client | `ordering_agent/cli.py` | Same agent without a phone, for testing. |
| Templates | `ordering_agent/templates.py`; starters in `data/templates/` | What kind of business it is: modules (which tools a call gets), words, prompt role and rules, greeting, tags and settings changes. Section 15. |
| Menu manager | `ordering_agent/manage.py` (`/manage`, `/api/manage/...`), `static/manage.html`, `accounts.py`, `settings_schema.py`, `menu_check.py` | Web page where restaurant staff sign in and edit their own menu (dishes, descriptions, prices, choices, extras, sold out) and settings. Edits autosave to a draft; publishing makes a new menu version used from the next call. History with restore. Section 14. |
| Orders dashboard | `server.py` `/dashboard`, `/orders`, `/calls`; `ordering_agent/static/dashboard.html` | Staff view of phone orders: today's count and sales, each order's items, total, order and payment status; filter, search, refreshes every 10 s. A Calls tab lists every call (with or without an order) and its transcript. The page holds no data and reads the API with the admin token. |

## 3. Call flow

```mermaid
sequenceDiagram
    participant C as Caller
    participant T as Twilio
    participant S as Server
    participant A as Claude
    C->>T: dials a restaurant's number
    T->>S: POST /voice (To = dialed number)
    S->>S: look up restaurant by To
    S-->>T: TwiML <Connect><ConversationRelay url=/ws?restaurant=ID ...>
    T->>C: speaks that restaurant's greeting
    T->>S: WS {"type":"setup", callSid, from, to}
    S->>S: create OrderingAgent with that restaurant's menu
    C->>T: "Two pepperoni pizzas"
    T->>S: {"type":"prompt", voicePrompt, last:true}
    S->>A: stream request (history + tools)
    A-->>S: text + tool_use(add_item)
    S-->>T: {"type":"text", token} (streamed)
    S->>S: run tool against Order
    S->>A: tool_result
    A-->>S: "What size would you like?"
    S-->>T: text tokens, then last:true
    T->>C: speaks reply
    Note over C,S: ...order built, read back, caller confirms...
    A-->>S: tool_use(place_order) -> saved, order number
    S->>S: create Stripe Checkout link, text it + order details to caller
    A-->>S: goodbye + tool_use(end_call)
    S-->>T: {"type":"end"} after goodbye is spoken
    Note over C,S: later: caller pays on Stripe's page
    S->>S: POST /stripe/webhook -> order paid (and confirmed), "payment received" text
```

**Payment rule (per restaurant, `restaurant.payment` in the restaurant's menu):**
- `required_before_cooking: false` (default): the order is confirmed right away; paying
  by link is optional and the caller can pay at pickup.
- `required_before_cooking: true`: the order is saved as `awaiting_payment` and only
  becomes `confirmed` when Stripe reports it paid. If the link expires unpaid
  (`pay_within_minutes`, 30 minutes to 24 hours, Stripe's limits), the order is
  `cancelled` and the caller gets a text saying so.

**Interruptions:** when the caller talks over the agent, Twilio sends `interrupt` (or
a new prompt arrives). If the reply is only talking, the server cancels it. If the model
has started a tool call (for example `place_order`, which also calls Stripe and Twilio),
cancelling would lose the order or leave a tool call without its result in the history,
so instead the reply is muted: the tool finishes, its result is recorded, nothing more
is spoken, and the server waits (up to 30 s) before handling what the caller said.
Either way the agent's next turn is told what the caller actually heard, and that any
tool results after that point haven't been announced.

**Transcripts:** every call gets a row in `calls` and a line in `call_events` for each
thing that happened, in order: the greeting, what the caller said (as Twilio heard it),
what the agent said (marked when cut off), each tool call with its input and result,
interruptions and the call's end. Staff read them on the dashboard's Calls tab. This
is how to find out why a call didn't produce an order.

## 4. Key design decisions

| Decision | Why |
|---|---|
| **Messages API, loop hosted in our server** (not Managed Agents) | Phone calls need the lowest latency: streaming tokens go straight to speech. The tools are local functions, so there's no need for a hosted sandbox. |
| **Twilio ConversationRelay** for speech | Twilio handles speech-to-text, text-to-speech and interruption detection. We only deal with text. The speech provider and voice are configuration, not code. |
| **Menu in the system prompt**, not a lookup tool | Menu questions need no tool round-trip, which saves latency. The prompt is identical across calls, so it's cached. |
| **All prices and rules in code** (`order.py`) | The model can't invent items, skip required options, or miscompute totals. Tool errors are returned to the model so it can ask the caller. |
| **Confirm before placing** | `place_order` requires `caller_confirmed: true` and the prompt requires a read-back first. |
| **Model `claude-opus-5-5` at `low` effort** | Short, quick turns for the phone. Effort and fast mode are configurable. |
| **Pay by texted Stripe Checkout link**, not card details by voice | Customers never read card numbers to an AI or over a phone line, and we never handle card data (Stripe does). The link has the exact items and total. |
| **Templates and modules, as code** (section 15) | Kinds of business differ in wording, prompt rules and what a call does. A template per kind (with its business rules in code) keeps the agent precise; a fully generic schema would make it vague and validation weak. |
| **Pay-first is a restaurant setting** | Some restaurants want prepayment to avoid no-shows, others don't. It's a rule in the menu file, enforced in code, and the prompt tells the caller which applies. |
| **Server-side refusal fallback** (`fallbacks: "default"`) | If a turn is declined, the API retries on a fallback model so the caller isn't left in silence. |
| **Streamed tool inputs** (`eager_input_streaming`) with our own validation | Lower latency. The API doesn't validate streamed inputs, so `tools.py` checks types and `order.py` checks rules. |

## 5. State and scaling

- **Per-call state** (conversation history, the order in progress) lives in memory on
  the call's WebSocket connection. A call stays on one server instance for its whole
  duration, so instances share nothing during a call.
- **Scaling out:** run more instances behind a load balancer that supports WebSockets.
  No sticky sessions or shared cache needed. One `AsyncAnthropic` client per process
  pools connections, and each call is a lightweight asyncio task.
- **Shared state** is only placed orders. SQLite is fine for one instance; use a shared
  database or send orders to the POS for more.
- **Simultaneous calls:** a Twilio number is not a single line. Every incoming call gets
  its own `/voice` request and its own WebSocket, so several callers can order at once and
  nobody gets a busy signal. On our side each call is one WebSocket; `fly.toml` caps a
  machine at 100 connections (soft limit 50). If a restaurant forwards its existing
  number to ours, its phone carrier decides how many calls it forwards at once; check
  that per restaurant.
- **Limits to plan for:** Anthropic rate limits at peak concurrent calls, and Twilio
  concurrency on the account.
- **Hosting:** Fly.io (`Dockerfile`, `fly.toml`), one always-on `shared-cpu-1x` machine
  in `ord` (Chicago) with a 1 GB volume at `/data` for `orders.db`. Always on because a
  cold start would leave a caller in silence; one machine because SQLite lives on one
  volume. Secrets are Fly secrets, never in the image (`.dockerignore` excludes `.env`
  and local databases). Live at `https://phone-ordering-agent-604b.fly.dev` (region
  `ord`); the Twilio number's voice webhook points there.
  Local development still works with uvicorn plus a tunnel.

## 6. Data model

- **Restaurant:** id, slug (e.g. `hyderabad-house`), the Twilio number callers dial
  (unique), active flag, current menu version.
- **Menu version:** restaurant, time, the full menu JSON (items plus the `restaurant`
  settings block: name, hours, address, tax rate, pickup minutes, payment rule, notes).
  Publishing a menu adds a version; old ones are kept.
- **Order:** lines, customer name, caller phone (from Twilio, not from the model),
  order type (pickup), placed flag. Subtotal, tax and total are computed.
- **Line item:** line_id, menu item, quantity, chosen options, extras, kitchen notes,
  unit price (computed from the menu).
- **Saved order row:** internal id, restaurant, per-restaurant order number (each
  restaurant's first order is #1), time, Twilio call SID, name, phone, total, full
  order summary as JSON, order status (`confirmed` / `awaiting_payment` / `cancelled`),
  payment status (`not_requested` / `unpaid` / `paid` / `expired`), Stripe Checkout
  Session ID and URL, paid time, the menu version it was taken from, and structured
  lines (item ids, options, extras) for sending it to a POS. POS fields: status
  (empty when no POS is connected, `held` until paid for pay-first restaurants,
  `pending`, `sent`, `failed`), the POS's order id,
  the last error, attempts, and whether the online payment was recorded on the POS.
- **POS connection:** restaurant (one each), provider (`square` / `clover`),
  environment (`sandbox` / `production`), merchant and location ids, access and refresh
  tokens encrypted with `POS_SECRET_KEY`, settings picked at connect time (Clover's
  pickup order type and default tax rate; Square's location currency).
- **Menu item POS ids** (optional, written by a menu import): `pos.item` (Square item
  variation or Clover item), `pos.choices` (each option choice to a variation or
  modifier id), `pos.extras`, and `pos_key` (keeps our item id stable across
  re-imports). The menu's top-level `pos.provider` says which POS they belong to.
- **User:** email (unique), name, scrypt password hash (empty until they accept their
  invite), active flag; linked to one or more restaurants (`user_restaurants`, role
  `manager`).
- **Auth token:** SHA-256 of a session or invite token, kind, user, expiry (sessions 14
  days, invites 72 hours).
- **Menu draft:** one per restaurant: the edited menu JSON, the live version it started
  from, a revision number (for conflict checks), who and when last saved. Published
  menu versions record who published them (`created_by`: an email or `operator`).
- **Template:** the catalog JSON's top-level `template` key (`restaurant` when missing);
  it's versioned with the catalog. Internally businesses are still `restaurants` rows,
  catalogs are `menus`, and the settings block is the `restaurant` key (section 15).
- **Menu item extras:** `available: false` marks a dish sold out (the agent says so and
  `add_item` refuses it); `restaurant.greeting` overrides the opening line.
- **Call row:** Twilio call SID, restaurant, start and end time, caller phone, order number (if any).
- **Call event:** call SID, time, role (`caller` / `agent` / `tool` / `event`), text
  (at most 4,000 characters).
- **Phone numbers** are stored in E.164 form (`+15551234567`).

## 7. Agent tools

| Tool | Effect |
|---|---|
| `add_item` | Add a menu item with options, extras, notes. Rejects unknown items, missing required options, invalid extras. |
| `remove_item` | Remove a line. |
| `update_quantity` | Change a line's quantity; 0 removes it. |
| `view_order` | Current lines, subtotal, tax, total. |
| `set_customer_name` | Name the order is under. |
| `set_customer_phone` | Mobile number for the text, when it differs from caller ID or caller ID is hidden. Must be a full number. |
| `place_order` | Save the order; requires caller confirmation, a name, at least one item, and (with payments on) a phone number. Then creates the payment link and texts it; the result tells the agent whether it was sent. |
| `end_call` | Hang up after the goodbye. |

## 8. Configuration

All settings are environment variables; see `.env.example` and the README table.
Groups: Claude (model, effort, fast mode), voice (text-to-speech and speech-to-text
provider, voice, language, extra ConversationRelay attributes), Twilio (auth token,
public URL, account SID and SMS sender number), Stripe (secret key, webhook secret),
storage (database file; menu file for the terminal chat), admin (`/orders` token),
POS (`POS_SECRET_KEY`, which turns POS sending on and encrypts tokens; Square and Clover
app credentials and environment). Payment links are on
only when `STRIPE_SECRET_KEY` and the Twilio SMS settings are all set; otherwise
customers pay at pickup. Per-restaurant settings (tax, pickup time, pay-first rule,
hours, notes) live in each restaurant's menu, not in environment variables.

## 9. Security

- `/voice` validates Twilio's request signature when `TWILIO_AUTH_TOKEN` is set.
- `/ws` rejects a bad signature (checked over the full URL, including the restaurant
  id), but doesn't yet require one (open issue). The restaurant id in the URL is
  cross-checked against the dialed number in the setup message; a mismatch is logged.
- Every order and call row carries its restaurant, and the APIs filter by it. Until
  staff logins exist, the single `ORDERS_API_TOKEN` sees every restaurant, so only the
  operator should have it.
- `/orders`, `/calls` and `/dashboard` (customer names, phone numbers, call
  transcripts) are off unless `ORDERS_API_TOKEN` is set. The dashboard page has no data in it; it sends the token
  (kept in the browser's local storage) as a Bearer header, compared in constant time.
- The phone number defaults to caller ID from Twilio. The caller can give a different
  number for the text (`set_customer_phone`); the agent reads back the last four digits
  before placing the order.
- No card data touches this server or the call: payment happens on Stripe's page.
- `/stripe/webhook` is off unless `STRIPE_WEBHOOK_SECRET` is set, and rejects events
  without a valid Stripe signature. Retried events don't change state twice or send
  duplicate texts.
- `/manage`: staff sign in with email and password (scrypt; 10+ characters), set from a
  one-time invite link (token in the URL fragment, so it isn't logged). Sessions are
  random tokens in an HttpOnly, SameSite=Lax cookie (Secure on https); only hashes are
  stored. Failed sign-ins are limited per email and per IP (8 per 15 minutes). Writes
  need an `X-Manage` header and a matching Origin (CSRF). Each user only sees their own
  restaurants (other ids return 404); the operator can use `ORDERS_API_TOKEN` as a
  Bearer token. The page is served with a strict CSP, `X-Frame-Options: DENY` and
  `no-store`. Every save is validated on the server (menu_check.py), whatever the page
  sends.
- `RELAY_EXTRA_ATTRS` can't override `url` or `welcomeGreeting`.
- POS access tokens are encrypted at rest (Fernet, `POS_SECRET_KEY`); losing the key
  means reconnecting every restaurant. Connect links carry an HMAC-signed state naming
  the restaurant and expire after an hour, so a callback can't attach a POS to the wrong
  restaurant. Sandbox tokens for testing are read from `POS_ACCESS_TOKEN`, never from
  the command line.

## 10. Failure handling

| Failure | Behavior |
|---|---|
| Claude API error | Agent says it's having trouble and asks the caller to repeat. |
| Refusal (after fallback) | Agent politely steers back to the menu. |
| Unparseable streamed tool input | Turn retried up to twice, then the error line. |
| Runaway tool loop | Capped at 8 model rounds per caller turn. |
| Invalid tool input or rule violation | Error returned to the model, which asks the caller. |
| Caller hangs up | WebSocket closes. A reply that is only talking is cancelled; one mid tool call finishes first, so a confirmed order is still saved. The call's end is recorded. |
| Caller interrupts mid tool call | The tool finishes silently, then the agent answers the new speech knowing what wasn't heard. If it takes over 30 s it's cancelled. |
| Transcript write fails | Logged; the call carries on. |
| Stripe or SMS fails at `place_order` | The order is still saved. The agent tells the caller the text didn't go through and they can pay at pickup (pay-first restaurants: the order starts when they pay at the counter). |
| Text after payment fails | Logged; payment status is still updated. |
| POS unreachable, rate-limited or 5xx | The call isn't affected (sending runs in the background). Retried after 5 s, 30 s, 2 min and 10 min; the dashboard shows "Sending to POS" / "Not on POS" with the error. Orders not sent are resent on server start (last 6 hours) or with `pos retry`. Square requests use idempotency keys; Clover partial orders are deleted before retrying. |
| POS rejects the order (other 4xx) | Not retried; shown as "Not on POS" with the error for staff. |

## 11. Testing

`python -m pytest` runs with a scripted fake Claude client (`tests/fakes.py`), with no
API key or network. Coverage: order rules, tool validation, the agent loop, TwiML and
voice settings, signature rejection, a full phone order over the WebSocket, phone
number checks, both payment rules (optional and pay-first), link/SMS failure, Stripe
webhook signature checks and duplicate events (with fake Stripe and SMS), an
interruption during `place_order` (order still saved, history stays valid), call
transcripts through `/calls`, and multiple restaurants: routing by dialed number,
unknown and inactive numbers, per-restaurant order numbers, API filtering, payment texts
in the right restaurant's name, migrating a pre-restaurant database, and admin-command
validation. POS tests (fake Square and Clover APIs over `httpx.MockTransport`): order
bodies, catalog-linked lines and modifiers, retries with one idempotency key, permanent
rejections, external payment after Stripe, Clover partial-order cleanup, token refresh,
encrypted tokens, signed connect state, the OAuth callback, menu import (and keeping item
ids on re-import), and admin commands. Not yet tried against the real sandboxes. Menu manager tests: invites and sign-in, password rules, rate limiting, disabled users,
CSRF header and Origin checks, access to other restaurants, operator token, draft
revisions and conflicts, publish reaching the next call (greeting, new dish, sold-out
dish refused), errors blocking publish, publishing over a newer menu, restore and
discard, page security headers, and the checks on the real menu. Template tests: valid starters, the
retail wording in the prompt and greeting, out-of-stock refusal, the coming-soon and
unknown templates refused, tools per module, settings labels per template, the editor
API sending the template, and the restaurant prompt unchanged by the refactor.

## 12. Multi-restaurant plan (phase 1 built, except Postgres)

Goal: one deployment serves many restaurants. Each call is routed to one restaurant,
and each restaurant's menu, settings, orders, transcripts, staff and money are kept
separate. The agent, tools and order rules are already restaurant-neutral (everything
restaurant-specific is in the restaurant's menu file), so the change is mostly data, routing and
accounts.

**What changes**

| Today | Multi-restaurant |
|---|---|
| One menu file, loaded at startup | `restaurants` table plus versioned menus in the database, loaded per call and cached |
| One Twilio number to `/voice` | A number per restaurant; the dialed number (`To`) selects the restaurant |
| Orders and calls have no restaurant column; one order-number sequence | `restaurant_id` on every row; order numbers counted per restaurant |
| One shared dashboard token | Staff logins scoped to one restaurant |
| One Stripe key; all money to one account | Stripe Connect: each restaurant connects its own Stripe account and is paid directly; optional platform fee |
| One SMS sender | The restaurant's own number sends its texts; A2P 10DLC registration per restaurant brand (Twilio ISV flow) |
| SQLite on one machine | Postgres (multiple machines, backups) |
| Tax, hours, pickup time, pay-first rule in the menu file | Per-restaurant settings, editable in an admin page |

**Call routing**

1. `/voice` (Twilio-signed) looks up the restaurant by the dialed number and returns
   that restaurant's greeting. Unknown number: a short apology and hang up.
2. The `/ws` URL in the TwiML carries the restaurant ID. Twilio signs that URL, so the
   `/ws` signature becomes required (it is optional today).
3. The agent is built from that restaurant's menu, settings and payment account. The
   system prompt differs only in the menu block, so prompt caching works per restaurant.
4. Orders, transcripts, texts and payment links are tagged with the restaurant; the
   Stripe webhook (Connect events carry the connected account) finds the order through it.

Restaurants can keep their published number and forward calls to ours, either always or
only when staff don't pick up ("no answer" forwarding is an easy first step for them).

**New components**

- Onboarding: menu photos to structured menu data with Claude vision, then a review screen
  where the restaurant confirms prices and required options before going live.
- Restaurant admin: menu editing, "sold out today", hours (the agent knows when the
  restaurant is closed), tax, pickup time, pay-first rule, voice.
- Kitchen hand-off: the dashboard first; POS connectors (Toast, Square, Clover) later, one
  adapter per POS.
- Tenant-isolation tests: no query, endpoint or webhook may return another restaurant's data.
- Later: billing restaurants for the service (Stripe Billing).

**Phases**

1. **Done 2026-11-22 (on SQLite):** restaurants table, routing by dialed number,
   `restaurant_id` on orders and calls, per-restaurant order numbers, versioned menus
   in the database, admin commands, restaurant picker on the dashboard. Hyderabad House
   is restaurant #1. **Still to do:** move to Postgres (provider not chosen; Netlify
   Database was looked at and isn't a fit, since it's tied to apps deployed on Netlify;
   Neon directly or Fly Managed Postgres are the candidates).
2. Staff logins and per-restaurant dashboards. **Started 2026-11-22:** staff accounts and
   the menu manager (section 14). The orders dashboard still uses the shared token.
3. Stripe Connect and per-restaurant SMS (needed before payment links go live for anyone).
4. Onboarding from photos and the admin page, so restaurant #2 needs no hand-edited JSON.
   The menu manager (section 14) now covers editing after onboarding.
   Until then, onboarding is done with the Claude Code skill
   `.claude/skills/add-restaurant/` (menu file from photos, `check_menu.py`, Twilio
   number, registration on Fly, signed test call). Menu files live in git at
   `data/restaurants/<slug>.json` and ship in the image; `restaurant.placeholders` and
   `placeholder_price` mark guessed values, which the checker blocks on.
5. POS integrations: Square and Clover built (section 13); Toast application drafted.

**Decisions**

- Phone numbers: bought in our main Twilio account, one per restaurant (decided
  2026-11-22). Subaccounts can come later if billing needs separating.
- Chains: not in scope for now; each restaurant is one location (decided 2026-11-22).
  A chain's locations can be separate restaurants; a brand level can be added later.
- Database: Postgres provider still to choose.

## 13. POS integrations

Phone orders go onto the restaurant's own point of sale, so the kitchen works from the
system it already uses, and a menu can be imported from the POS instead of typed in.
Code: `ordering_agent/pos.py` (connections, background sending, import, admin
commands), `pos_square.py`, `pos_clover.py`.

**Connecting.** The operator runs `pos connect-link SLUG square|clover` and sends the
link to the owner, who approves access (OAuth). The callback (`/pos/<provider>/callback`)
stores the tokens encrypted and picks the location (Square, when there's one) or the
pickup order type and default tax rate (Clover). Square tokens last 30 days and are
refreshed a week early; Clover's are short-lived and refreshed when within 5 minutes.
For sandbox testing, `pos connect-token` stores a token from `POS_ACCESS_TOKEN`.

**Sending orders.** After `place_order` saves the order, it's queued for the POS and
sent by a small thread pool; the caller never waits. For a pay-first restaurant the
order is held until the payment link is paid, so the kitchen never sees an unpaid
order (if the link expires it never reaches the POS; if the text failed, staff see it
only on our dashboard). Lines from an imported menu go as
catalog items with their modifiers; anything else goes as a custom line with our name
and price. The order total and tax on the POS come from the POS for catalog items, so a
menu that is out of date with the POS can show a different total there; re-import after
POS price changes.

| | Square | Clover |
|---|---|---|
| Order | Orders API, PICKUP fulfillment (ASAP, prep time from the menu), ticket name `Phone #N Name`, order-level sales tax at the menu's rate. | v3 order, state open, title `Phone #N Name`, pickup order type, total; one line per unit (bulk), then modifiers. |
| Shows on the POS | **Only once paid** (a Square rule). | Immediately. |
| Payment link paid | EXTERNAL payment (source "Stripe payment link") for Square's order total; the order then appears. | Order note becomes "PAID ONLINE". |
| Retries | Idempotency keys from our order id. | Partial order deleted, then recreated. |
| Menu import | Catalog: variations become a required `size`/`type` option, single-select modifier lists become options, multi-select become extras; variable-price items skipped. | Items with modifier groups (max 1 is an option, otherwise extras); hidden and variable-price items skipped. |

**Importing a menu.** `pos import-menu SLUG > data/restaurants/SLUG.json` writes a menu
file using the restaurant's current settings block (or `--base`), then it goes through
the normal checker, review, commit, deploy and `set-menu`. Item ids of items imported
before are kept (matched by POS id), since old orders refer to them. Imported items have
no vegetarian/spice tags; add them in review.

**Decisions**

- Square and Clover first: both open self-serve developer platforms (2026-11-22).
  Toast needs the partner program (application drafted in
  `docs/toast-partner-application.md`); Lightspeed and others later, or through a
  middleware such as Deliverect or Otter.
- The POS doesn't replace our dashboard: unpaid Square orders and failed sends are only
  visible there.

## 14. Menu manager (restaurant web interface)

Restaurant staff edit their own phone menu and settings at `/manage`, without us
editing JSON files.

**Accounts.** The operator invites people with
`python -m ordering_agent.accounts invite EMAIL --restaurant SLUG`, which prints a
one-time link where they choose a password. Resetting a password is the same command.
`list`, `grant`, `revoke`, `disable` manage access. A person can manage several
restaurants (a picker appears).

**Editing.** The page loads the restaurant's draft (or the live menu), and every change
is saved to the draft about a second later. Callers keep hearing the live menu until
someone presses Publish, which validates the draft, stores it as a new menu version
(recording who published it) and deletes the draft; the next call uses it, with no
restart. Discard drops the draft; History lists published versions and can restore one
into the draft.

- Menu tab: dishes by section, search, filters (sold out, estimated price), a
  sold-out switch on every dish, rename a section, add/delete dishes. The dish panel
  edits name, section, price, description (with tag chips such as Vegetarian, Spicy,
  Contains nuts), availability, choices (required or not, each option with its added
  price) and extras. New dishes get an id from their name; ids never change after that.
  Changing an estimated price, or pressing "It's right", clears the estimate flag.
- Settings tab: built from `settings_schema.py` (name, greeting, address, hours,
  pickup time, sales tax, pay-first rule, link validity, notes for the agent).
  Estimated settings show a confirm prompt.
- Checks (`menu_check.py`, shared with the add-restaurant skill) run on every save:
  errors (out-of-range values, missing names or prices, bad choices) block publishing;
  "please confirm" items (estimated prices or settings, no tax) and suggestions are shown.

**Adding a setting later:** add an entry to `SETTINGS` in `settings_schema.py` (path,
type, label, help, limits) and read it where it's used. The form and server-side
validation follow automatically. Supported types: text, textarea, integer, percent,
boolean, string_list.

**Conflicts.** Each draft save carries the revision it was based on; a stale one gets
409 and the page asks to reload. If someone published a newer menu after the draft was
started (for example the operator or a POS import), publishing asks for confirmation
before replacing it.

**Decisions**

- Draft then publish, rather than live edits, so a half-finished change never reaches
  callers (2026-11-22).
- Invite links instead of operator-chosen passwords, and no email provider yet: the
  operator sends the link (2026-11-22).
- One plain HTML page with no build step, like the dashboard.

## 15. Business templates (making the agent generic)

The core works for any business: phone calls, routing by number, transcripts, staff
sign-in, the web manager, payment links, dashboard, POS. What differs by kind of
business is a **template** (`ordering_agent/templates.py`):

| Part | What it is | Example (restaurant / shop) |
|---|---|---|
| `modules` | What a call can do. Each module brings its tools (`tools.MODULE_TOOLS`) and business rules in code. | `orders` / `orders` |
| `words` | How the manager and the agent name things. | menu, dish, section, "sold out today" / catalog, product, category, "out of stock" |
| `role`, `rules`, `declines` | The agent's role line, extra rules, and what it politely declines. | "phone ordering assistant", declines delivery and reservations / declines shipping, returns, special orders |
| `greeting` | Default first line (a business can set its own). | "What can I get started for you today?" / "How can I help you today?" |
| `tags` | Description tag chips in the editor. | Vegetarian, Spicy / Bestseller, Gift-wrappable |
| `setting_overrides`, `extra_settings` | Changes to the settings form (labels, help) and settings only this kind of business has. | "Restaurant name", "Wait for payment before cooking" / "Business name" |

A business's template is the `template` key of its catalog JSON. Starter catalogs are
in `data/templates/<id>.json`; `python -m ordering_agent.restaurants templates` lists
them. The prompt is assembled from the phone rules (shared), the orders rules with
the template's words, the template's role and rules, the payment rule and the catalog.
For restaurants it's byte-for-byte the prompt from before templates.

**Templates now:** `restaurant` and `retail` (shops: bakeries, florists, liquor, gift and
convenience stores) are available. `salon` (services and appointments) is listed as
**coming soon**: it needs the `bookings` module, so `parse_menu` refuses it and it can't
be registered.

**Adding a kind of business** that only needs orders: a `Template` entry and a starter
file. **Adding a module** (bookings): its tools in `tools.py`/`MODULE_TOOLS`, its rules in
code (like `order.py`), its prompt section, its storage, its parts of the manager and
dashboard, and `BUILT_MODULES` in `templates.py`. Planned bookings pieces: services with
a length and price, staff and their hours, availability, appointments with a no-double-
booking guarantee (wants Postgres), reschedule/cancel, confirmation and reminder texts,
read-back of date and time on the call, and later sync with Google Calendar or Square
Appointments.

**Names.** Code and storage still say restaurant/menu (tables `restaurants`, `menus`,
`restaurant_id`, the `restaurant` settings key, `ordering_agent/restaurants.py`, the
`add-restaurant` skill). Renaming them means migrating live data for no change in
behavior; it's planned with the move to Postgres. Everything people see (the manager,
the agent's words) follows the template.

**Decisions**

- Templates and modules are code, not configuration (2026-11-22).
- Bookings are "coming soon"; the extension points exist, the module doesn't
  (2026-11-22).

## 16. Open issues and next steps

- One short live call completed end to end; a full order over the phone hasn't been placed
  yet. ConversationRelay message field names still need confirming against the docs.
- Menu gaps: address and hours are not set. Drinks, shakes, desserts and pastries (17
  items tagged `"placeholder_price": true` in `data/restaurants/hyderabad-house.json`) use typical prices, not the
  restaurant's; confirm or correct them before real customers order. Daily specials
  aren't on the phone menu; the agent offers them at the counter.
- The full menu adds roughly 6k tokens to the (cached) system prompt; watch first-turn latency.
- Hang-up delay after the goodbye is estimated from word count; tune after real calls.
- Require a Twilio signature on `/ws`.
- DTMF (keypad) input is ignored.
- Payments not yet tried live: needs Stripe keys, the webhook registered in Stripe, and
  US SMS registration (A2P 10DLC) for the Twilio number, or US carriers block the texts.
- The dashboard is read-only: no "ready" / "picked up" status, and it polls rather than
  pushing new orders. Cancelled pay-first orders show there but aren't announced.
- The orders dashboard still uses one shared token that sees every restaurant; move it
  onto staff sign-in (scoped to their restaurants) next, now that accounts exist.
- Menu manager (section 14): no email sending, so invites and password resets are links
  the operator sends by hand; no roles beyond "manager"; no photo upload or
  menu-from-photos; editing a POS-linked dish's price doesn't change the POS. Sign-in
  limits are in memory (reset on restart). Two people editing at once get a conflict
  message and must reload, not a merge.
- Refunds and order changes after payment are handled manually in Stripe.
- Before scaling past one Fly machine, move to Postgres (SQLite is on one volume). Order
  numbers are assigned as max + 1 under a process lock, which is only safe on one
  machine; Postgres needs a per-restaurant counter or a unique constraint with retry.
- Transcripts are kept forever and include names and phone numbers; add a retention
  period (delete after N days) before real customers call.
- The first live order that wasn't saved (call `CA277d47…`, before transcripts) is still
  unexplained; the next failed call will have a transcript to check.
- Multi-restaurant support (section 12): finish phase 1 with Postgres, then the
  rest of phase 2 (dashboard on staff sign-in). Payment texts still come from one `TWILIO_SMS_FROM` number for every
  restaurant until phase 3.
- POS (section 13): not yet tried against the real Square or Clover sandboxes; needs
  developer accounts and app credentials. To test: a Square sandbox token and seller
  test account, and a Clover sandbox merchant. Field names were written from the API
  docs and need checking against real responses (Clover bulk line item response,
  Square catalog fields).
- Square shows API orders on the POS only once paid. Pay-at-pickup orders for a Square
  restaurant are only on our dashboard. Options: require payment before cooking for
  Square restaurants, or record an "unpaid" external tender (bad for their reports).
- Clover: the online payment is only noted on the order, not recorded as a Clover
  payment (needs a custom tender). Clover outside North America needs other hosts.
- Apps need review before production: Clover App Market approval (or a private app);
  Square OAuth apps can go live without the App Marketplace.
- Toast: submit the partner application (`docs/toast-partner-application.md`); expect
  discovery, an agreement, sandbox credentials, certification, then an alpha restaurant.
- Bookings module and the salon template (section 15), then self-serve signup where a
  business picks a template and enters its details itself.
- Rename restaurant/menu in code and storage to business/catalog with the Postgres move.
- Possible features: delivery, kitchen display, order status from the POS back to the
  dashboard ("ready").

## 17. Change log

| Date | Change |
|---|---|
| 2026-11-18 | Initial design: Claude Messages API agent, Twilio ConversationRelay, order tools, SQLite storage, terminal client. |
| 2026-11-18 | Text-to-speech and speech-to-text provider, voice and language made configurable via environment variables. |
| 2026-11-18 | Added this design document. |
| 2026-11-18 | Replaced the placeholder pizza menu with a real Indian restaurant menu; added restaurant notes to the prompt; tests moved to a fixture menu. |
| 2026-11-18 | Pay by text: Stripe Checkout link per order sent by SMS with the order details, Stripe webhook for paid/expired, pay-first rule per restaurant, `set_customer_phone` tool, order and payment status in storage. |
| 2026-11-18 | Orders dashboard at `/dashboard` (reads `/orders`, which now takes `limit`). |
| 2026-11-18 | Hosting on Fly.io: Dockerfile, fly.toml, one always-on machine with a volume for orders. |
| 2026-11-18 | Call transcripts (`calls`, `call_events` tables, `/calls` API, dashboard Calls tab). Interruptions during a tool call no longer cancel it, so a confirmed order can't be lost to the caller talking over the agent. |
| 2026-11-18 | Deployed to Fly.io (`phone-ordering-agent-604b`); Twilio number moved off the cloudflared tunnel. |
| 2026-11-18 | Restaurant named Hyderabad House. Agent no longer tells callers to phone the restaurant; unpriced desserts/drinks/specials are offered at the counter instead. |
| 2026-11-22 | Drinks, shakes, desserts and pastries added to the phone menu with placeholder prices (`placeholder_price` tag). Only daily specials are still counter-only. |
| 2026-11-22 | Multi-restaurant plan (section 12); note on simultaneous calls in section 5. |
| 2026-11-22 | Multi-restaurant phase 1 on SQLite: restaurants and versioned menus in the database, calls routed by dialed number, `restaurant_id` on orders and calls, per-restaurant order numbers, admin commands (`ordering_agent/restaurants.py`), `/restaurants` API, restaurant filter on `/orders` and `/calls`, dashboard restaurant picker. |
| 2026-11-22 | `add-restaurant` skill and menu checker; menus moved to `data/restaurants/<slug>.json`; `restaurant.placeholders` marks unconfirmed settings (Hyderabad House's tax rate). |
| 2026-11-22 | POS integrations: Square and Clover (OAuth connect, encrypted tokens, orders sent in the background with retries, payment recorded after Stripe, menu import with POS ids), POS status on the dashboard, `ordering_agent.pos` admin commands; Toast partner application drafted. |
| 2026-11-22 | Menu manager at `/manage`: staff accounts with invite links (`ordering_agent.accounts`), sessions, drafts with autosave, publish, history and restore, settings form built from `settings_schema.py`, shared menu checks (`menu_check.py`), sold-out dishes (`available: false`), custom greeting. Unset address/hours no longer reach the prompt as "None". |
| 2026-11-22 | Business templates: the agent is generic, with a template per kind of business (`ordering_agent/templates.py`: modules, words, prompt rules, greeting, tags, settings). `restaurant` (unchanged behavior) and `retail` available, `salon` coming soon (bookings module). Tools come from the template's modules; the manager's wording, tags and settings form follow the template; starter catalogs in `data/templates/`; `restaurants templates` command. |
