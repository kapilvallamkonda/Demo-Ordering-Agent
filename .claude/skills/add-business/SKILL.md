---
name: add-business
description: Onboard a new business (a restaurant, or a shop selling products) onto the phone agent - pick its template, turn its menu or product list (photos, PDF or text) into a catalog file, check it, buy and wire a Twilio number, register it on the Fly.io server, invite its staff, and verify the line answers as that business. Use when the user wants to add, onboard or set up a restaurant, shop or other business, or give one its own phone line. Also use to republish an existing business's catalog.
---

# Add a business

**Businesses can now set themselves up** at `/` (sign in with Google, upload menu photos,
test, go live; docs/DESIGN.md section 16). Use this skill when the operator is doing it
for them, or for things the web app doesn't do (importing a big catalog from a file,
reusing an existing Twilio number, a POS import).

One server serves every business. A call is routed by the number dialed, so a business
is live once it has (1) a catalog in the database and (2) a Twilio number whose voice
webhook is our `/voice`, registered on that business. Background: `docs/DESIGN.md`
sections 12 and 15, `phone_agent/businesses.py` and `phone_agent/templates.py`.

Fixed facts (check `fly.toml` and the memory notes if they look stale):

- Fly app `phone-agent-604b`; voice webhook `https://phone-agent-604b.fly.dev/voice` (POST).
- Twilio account: `TWILIO_ACCOUNT_SID` in `.env` (the Twilio MCP's default account); every
  number is bought in this main account, not a subaccount.
- Catalog files live in git at `data/businesses/<slug>.json` and ship in the image.

Work through the steps in order and keep a checklist. Stop at each **Ask** and wait for
the user's answer.

## 1. Gather

Pick the **template** first: `.venv/bin/python -m phone_agent.businesses templates`.
`restaurant` (menu of dishes) or `retail` (a shop's products: bakery, florist, liquor,
gift, convenience). Salons and other appointment businesses need bookings, which are
**coming soon**: tell the user they can't be set up yet.

Get from the user: the business's name, what it sells (menu or product list: photos,
PDF, link or text), and as much of this as they know: address, hours, sales tax rate,
typical minutes until an order is ready for pickup, whether to wait for payment before
preparing orders, preferred area code for the number, notes for the agent (cuisine and
allergy guidance for a restaurant; policies like ID checks for a shop), the staff
email(s) to invite, and **which POS they use**. Square and Clover can be connected
(step 6b), and the catalog can then be imported from the POS instead of photos. Toast
isn't supported yet (partner application pending). Don't block on the optional ones;
anything missing gets a placeholder (step 2) and goes on the hand-off list.

Pick the slug: lowercase words joined by dashes (`taj-palace`). Check it isn't taken on
the live server (the `list` command in step 6), not just in `data/businesses/`.

## 2. Write the catalog file

Read `catalog-format.md` (next to this file) first. Start from the template's starter,
`data/templates/<template>.json`; `data/businesses/hyderabad-house.json` is a full
restaurant example. Write `data/businesses/<slug>.json` with `"template"` set.

- Read every photo fully; menus and price lists often continue across images. Copy
  names and prices exactly.
- **Ask** before inventing anything: for each section without prices, ask whether to
  leave it out (the agent will offer it in person, via a note) or estimate prices
  marked `"placeholder_price": true`.
- Settings you had to guess go in `settings.placeholders`.

## 3. Check it

```bash
.venv/bin/python .claude/skills/add-business/scripts/check_catalog.py data/businesses/<slug>.json
```

Fix every ERROR. Show the user the category counts, greeting, BLOCKERs and WARNINGs.
Then spot-check by eye: pick five items from the photos and confirm name, price and
options match the file.

## 4. Try it in the terminal

Needs `ANTHROPIC_API_KEY` (`set -a; . ./.env; set +a`). Use a scratch database so
nothing touches real data:

```bash
DATABASE_PATH=<scratchpad>/try.db CATALOG_PATH=data/businesses/<slug>.json \
  .venv/bin/python -m phone_agent.cli <<'EOF'
<a normal order using an item with a required option>
<answer the option question>
<a question the notes or descriptions answer, e.g. what's vegetarian / what's good as a gift>
<ask for something the business doesn't sell>
EOF
```

Check the agent names the business, uses the template's words (menu vs catalog),
asks for required options, quotes prices that match the file, and declines unknown
items gracefully. Fix the file or notes and repeat if not.

## 5. Get the phone number

**Ask** before buying: say the price (about $1.15/month for a US local number, check
the Twilio response) and the area code. If the business already has a Twilio number
for us, skip buying and just set its webhook.

Buy with the Twilio MCP `TwilioApiV2010--CreateIncomingPhoneNumber`: `AccountSid` =
`TWILIO_ACCOUNT_SID`, `AreaCode`, `FriendlyName` = the business name, `VoiceUrl` = the
webhook above, `VoiceMethod` = `POST`. For an existing number use
`UpdateIncomingPhoneNumber` with the same `VoiceUrl`. Note the number in E.164 (`+1...`).

## 6. Register it on the live server

The catalog file must be in the deployed image, so commit, push to `main` (this repo's
rule), and deploy:

```bash
git add data/businesses/<slug>.json && git commit -m "Add <name> catalog" && git push origin main
fly deploy --app phone-agent-604b --ha=false --yes
```

**Ask** before registering if step 3 still shows BLOCKERs: callers would hear those
placeholder values from the first call.

```bash
fly ssh console --app phone-agent-604b -C \
  "python -m phone_agent.businesses add <slug> --phone <+1number> --catalog /app/data/businesses/<slug>.json"
fly ssh console --app phone-agent-604b -C "python -m phone_agent.businesses list"
```

To replace an existing business's catalog from a file instead, deploy the edited file
and use `set-catalog <slug> --catalog /app/data/businesses/<slug>.json` (used from the
next call, no restart). Once staff edit in `/manage`, the live catalog is in the
database and `set-catalog` from a file overwrites their edits: check `/manage` history
first.

If ssh says there are no started machines: `fly status` / `fly logs`.

## 6b. Connect their POS (Square or Clover, optional)

Needs `POS_SECRET_KEY` and the provider's app credentials as Fly secrets (see README).
Print a connect link and send it to the owner; it works for an hour:

```bash
fly ssh console --app phone-agent-604b -C "python -m phone_agent.pos connect-link <slug> square"
fly ssh console --app phone-agent-604b -C "python -m phone_agent.pos status"
```

After they approve, `status` shows the merchant and location. If Square has several
locations, ask which one and run `set-location <slug> <LOCATION_ID>`.

To take the catalog from the POS (keeps POS ids, so orders land as real POS items):

```bash
fly ssh console --app phone-agent-604b -C "python -m phone_agent.pos import-catalog <slug>" \
  > data/businesses/<slug>.json
```

Then repeat steps 3 (check; add description tags and notes by hand) and 6 (commit,
deploy, `set-catalog`). Re-import whenever they change prices on the POS. Tell the user
that on Square, orders show on the POS only once paid (recommend paying first).

## 7. Verify

A signed request to `/voice` for the new number must return its greeting:

```bash
set -a; . ./.env; set +a
.venv/bin/python - <<'EOF'
import os, urllib.parse, urllib.request
from twilio.request_validator import RequestValidator
url = "https://phone-agent-604b.fly.dev/voice"
params = {"CallSid": "CAcheck", "From": "+15550001111", "To": "<+1number>"}
sig = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"]).compute_signature(url, params)
req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(),
                             headers={"X-Twilio-Signature": sig})
print(urllib.request.urlopen(req, timeout=20).read().decode())
EOF
```

Expect `Thanks for calling <name>` (or their custom greeting) and `/ws?business=<id>`.
"isn't taking orders" means the number isn't registered (or is on another business).
Then ask the user to place a real test call and read its transcript:
`curl -s -H "Authorization: Bearer $ADMIN_API_TOKEN" "https://phone-agent-604b.fly.dev/calls?business=<id>&limit=1"`
then `/calls/<CallSid>`.

## 8. Hand off

Invite the staff (one link per person, one use, 72 hours):
`fly ssh console --app phone-agent-604b -C "python -m phone_agent.accounts invite <email> --business <slug>"`.

Tell the user, briefly:

- The business's new number, and that it's live (or what's blocking it).
- How the business can use it: publish it directly, or forward their existing line to
  it (always, or only when staff don't answer). Their phone carrier controls how many
  forwarded calls can ring at once.
- The invite link(s) to send to staff: they edit their catalog, mark items unavailable
  and change settings at `/manage`; changes go live when they publish.
- Everything still a placeholder or missing (from step 3), to confirm with the business;
  staff can fix these themselves in `/manage`.
- Dashboard: `https://phone-agent-604b.fly.dev/dashboard`, business picker at the top.
  Its token sees every business, so don't share it with business staff.
- Payment links: still sent from the shared `TWILIO_SMS_FROM` number (per-business
  texting is phase 3).
- POS: connected or not; if connected, place a test order and check it appears on the
  POS (dashboard badge "On POS"; "Not on POS" shows the error on hover).

If a step changed how onboarding works (not just added a business), update
`docs/DESIGN.md` per `CLAUDE.md`. Adding a business by itself needs no design update.
