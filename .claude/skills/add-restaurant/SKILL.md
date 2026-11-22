---
name: add-restaurant
description: Onboard a new business (restaurant, or a shop selling products) onto the phone agent - turn its menu or product list (photos, PDF or text) into a catalog file, check it, buy and wire a Twilio number, register it on the Fly.io server, and verify the line answers as that restaurant. Use when the user wants to add, onboard or set up a restaurant, shop or other business, or give one its own phone line. Also use to republish an existing restaurant's menu.
---

# Add a restaurant

One server serves every restaurant. A call is routed by the number dialed, so a
restaurant is live once it has (1) a menu in the database and (2) a Twilio number whose
voice webhook is our `/voice`, registered on that restaurant. Background:
`docs/DESIGN.md` sections 12 and 15, and `ordering_agent/restaurants.py`.

**Shops and other businesses:** the same steps work for any template. Pick the template
first (`python -m ordering_agent.restaurants templates`): `restaurant` or `retail`. Start
the file from `data/templates/<template>.json` (it sets `"template"`). Salons and other
appointment businesses are coming soon (bookings): tell the user it can't be set up yet.
"Menu" below means the business's catalog.

Fixed facts (check `fly.toml` and the memory notes if they look stale):

- Fly app `phone-ordering-agent-604b`; voice webhook `https://phone-ordering-agent-604b.fly.dev/voice` (POST).
- Twilio account: `TWILIO_ACCOUNT_SID` in `.env` (the Twilio MCP's default account); every
  number is bought in this main account, not a subaccount.
- Menus live in git at `data/restaurants/<slug>.json` and ship in the image.

Work through the steps in order and keep a checklist. Stop at each **Ask** and wait for
the user's answer.

## 1. Gather

Get from the user: the restaurant's name, its menu (photos, PDF, link or text), and as
much of this as they know: address, hours, sales tax rate, typical pickup minutes,
whether the kitchen must wait for payment before cooking, preferred area code for the
number, cuisine and allergy notes, and **which POS they use**. Square and Clover can be
connected (step 6b), and the menu can then be imported from the POS instead of photos.
Toast isn't supported yet (partner application pending). Don't block on the optional ones; anything missing
gets a placeholder (step 2) and goes on the hand-off list.

Pick the slug: lowercase words joined by dashes (`taj-palace`). Check it isn't taken
on the live server (the `list` command in step 6), not just in `data/restaurants/`.

## 2. Write the menu file

Read `menu-format.md` (next to this file) first, and open
`data/restaurants/hyderabad-house.json` as the worked example. Write
`data/restaurants/<slug>.json`.

- Read every photo fully; menus often continue across images. Copy names and prices
  exactly.
- **Ask** before inventing anything: for each section without prices, ask whether to
  leave it out (the agent will offer it at the counter, via a note) or estimate prices
  marked `"placeholder_price": true`.
- Settings you had to guess go in `restaurant.placeholders`.

## 3. Check it

```bash
.venv/bin/python .claude/skills/add-restaurant/scripts/check_menu.py data/restaurants/<slug>.json
```

Fix every ERROR. Show the user the category counts, greeting, BLOCKERs and WARNINGs.
Then spot-check by eye: pick five dishes from the photos and confirm name, price and
options match the file.

## 4. Try it in the terminal

Needs `ANTHROPIC_API_KEY` (`set -a; . ./.env; set +a`). Use a scratch database so
nothing touches real orders:

```bash
ORDERS_DB=<scratchpad>/try.db MENU_PATH=data/restaurants/<slug>.json \
  .venv/bin/python -m ordering_agent.cli <<'EOF'
<a normal order using a dish with a required option>
<answer the option question>
what do you have that's vegetarian?
<ask for something the restaurant doesn't sell>
EOF
```

Check the agent names the restaurant, asks for required options, quotes prices that
match the menu, and declines unknown items gracefully. Fix the menu or notes and repeat
if not.

## 5. Get the phone number

**Ask** before buying: say the price (about $1.15/month for a US local number, check
the Twilio response) and the area code. If the restaurant already has a Twilio number
for us, skip buying and just set its webhook.

Buy with the Twilio MCP `TwilioApiV2010--CreateIncomingPhoneNumber`: `AccountSid` =
`TWILIO_ACCOUNT_SID`, `AreaCode`, `FriendlyName` = the restaurant name, `VoiceUrl` = the webhook above,
`VoiceMethod` = `POST`. For an existing number use `UpdateIncomingPhoneNumber` with the
same `VoiceUrl`. Note the number in E.164 (`+1...`).

## 6. Register it on the live server

The menu file must be in the deployed image, so commit, push to `main` (this repo's
rule), and deploy:

```bash
git add data/restaurants/<slug>.json && git commit -m "Add <name> menu" && git push origin main
fly deploy --app phone-ordering-agent-604b --ha=false --yes
```

**Ask** before registering if step 3 still shows BLOCKERs: callers would hear those
placeholder prices from the first call.

```bash
fly ssh console --app phone-ordering-agent-604b -C \
  "python -m ordering_agent.restaurants add <slug> --phone <+1number> --menu /app/data/restaurants/<slug>.json"
fly ssh console --app phone-ordering-agent-604b -C "python -m ordering_agent.restaurants list"
```

To update an existing restaurant's menu instead, deploy the edited file and use
`set-menu <slug> --menu /app/data/restaurants/<slug>.json` (used from the next call, no
restart).

If ssh says there are no started machines: `fly status` / `fly logs`. "Trial machine
stopping" means the Fly account has no card yet (https://fly.io/trial); start it with
`fly machine start <id>` to finish, and tell the user the line won't stay up.

## 6b. Connect their POS (Square or Clover, optional)

Needs `POS_SECRET_KEY` and the provider's app credentials as Fly secrets (see README).
Print a connect link and send it to the owner; it works for an hour:

```bash
fly ssh console --app phone-ordering-agent-604b -C "python -m ordering_agent.pos connect-link <slug> square"
fly ssh console --app phone-ordering-agent-604b -C "python -m ordering_agent.pos status"
```

After they approve, `status` shows the merchant and location. If Square has several
locations, ask which one and run `set-location <slug> <LOCATION_ID>`.

To take the menu from the POS (keeps POS ids, so orders land as real POS items):

```bash
fly ssh console --app phone-ordering-agent-604b -C "python -m ordering_agent.pos import-menu <slug>" \
  > data/restaurants/<slug>.json
```

Then repeat steps 3 (check; add Vegetarian/Spicy tags and notes by hand), 6 (commit,
deploy, `set-menu`). Re-import whenever they change prices on the POS. Tell the user that
on Square, orders show on the POS only once paid (recommend pay-before-cooking).

## 7. Verify

A signed request to `/voice` for the new number must return its greeting:

```bash
set -a; . ./.env; set +a
.venv/bin/python - <<'EOF'
import os, urllib.parse, urllib.request
from twilio.request_validator import RequestValidator
url = "https://phone-ordering-agent-604b.fly.dev/voice"
params = {"CallSid": "CAcheck", "From": "+15550001111", "To": "<+1number>"}
sig = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"]).compute_signature(url, params)
req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(),
                             headers={"X-Twilio-Signature": sig})
print(urllib.request.urlopen(req, timeout=20).read().decode())
EOF
```

Expect `Thanks for calling <name>` and `/ws?restaurant=<id>`. "isn't taking orders"
means the number isn't registered (or is on another restaurant). Then ask the user to
place a real test call and read its transcript:
`curl -s -H "Authorization: Bearer $ORDERS_API_TOKEN" "https://phone-ordering-agent-604b.fly.dev/calls?restaurant=<id>&limit=1"`
then `/calls/<CallSid>`.

## 8. Hand off

Tell the user, briefly:

- The restaurant's new number, and that it's live (or what's blocking it).
- How the restaurant can use it: publish it directly, or forward their existing line to
  it (always, or only when staff don't answer). Their phone carrier controls how many
  forwarded calls can ring at once.
- Everything still a placeholder or missing (from step 3), to confirm with the
  restaurant, and that fixing them is: edit the file, re-run step 3, deploy, `set-menu`.
- Menu manager: invite the restaurant's staff so they can fix prices, mark dishes sold
  out and edit settings themselves:
  `fly ssh console --app phone-ordering-agent-604b -C "python -m ordering_agent.accounts invite <email> --restaurant <slug>"`.
  Send them the printed link (one use, 72 hours). They edit at `/manage`; their changes
  go live when they publish. Menu files in git are then only the starting point; the
  live menu is in the database (`set-menu` from a file replaces their edits, so check
  `/manage` history first).
- Dashboard: `https://phone-ordering-agent-604b.fly.dev/dashboard`, restaurant picker at
  the top. The token sees every restaurant, so don't share it with restaurant staff yet.
- Payment links: still sent from the shared `TWILIO_SMS_FROM` number (per-restaurant
  texting is phase 3).
- POS: connected or not; if connected, place a test order and check it appears on the
  POS (dashboard badge "On POS"; "Not on POS" shows the error on hover).

If a step changed how onboarding works (not just added a restaurant), update
`docs/DESIGN.md` per `CLAUDE.md`. Adding a restaurant by itself needs no design update.
