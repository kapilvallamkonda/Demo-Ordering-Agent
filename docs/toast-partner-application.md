# Toast integration partner application (draft)

Toast's APIs for ordering need the Toast Partner Integrations program. This file is the
prepared material for the application. Items in **[brackets]** are for you to fill in;
they're company and contact details that shouldn't go in the repo.

Apply at: https://pos.toasttab.com/partners/integration-partner-application

## How the process works (from Toast's docs)

1. **Application form** on the page above: you accept the API Documentation License
   Agreement, Toast emails you API documentation access, and the email links the final
   application form. Toast gets more applications than it accepts, and replies can be slow.
2. **Discovery:** Toast reviews business fit and technical readiness, maybe a call.
   Before any development: compliance, privacy, security and legal review, a signed
   partner agreement, and an assigned integrations contact.
3. **Sandbox credentials**, then build against Toast's ordering integration checklist.
4. **Certification:** a one-hour demo call (endpoints used, polling, data sent to Toast).
5. **Alpha:** production credentials, one restaurant, about a week.
6. **Beta:** 3 to 5 locations for several weeks.
7. **General availability:** listed in Toast's integrations, and restaurants add it themselves.

A restaurant that only wants its own custom integration can instead ask its Toast
representative for API access; that doesn't work for a product used by many restaurants.

## Answers to prepare

**Company:** [legal name], [website], [country/state], [contact name, role, email, phone].

**Integration category:** Online ordering (phone ordering). Ordering partner.

**One-line description:** An AI phone agent that answers a restaurant's calls and takes
pickup orders by voice, then puts each order straight onto the restaurant's Toast POS
and kitchen screens.

**Problem it solves:** Restaurants miss phone orders at busy times, and staff taking
orders by phone retype them into the POS. Our agent answers every call at once, knows
the full menu (options, prices, tax), reads the order back, confirms it, texts a payment
link, and sends the order to the POS with no retyping.

**How it works (data flow):**
- Caller dials the restaurant's ordering number; speech goes through Twilio, and the
  conversation is run by Claude (Anthropic) with strict order rules in code (the model
  can't invent items or prices).
- **Menus (read):** we read the restaurant's menus (menus API V3 for ordering partners)
  so the agent offers exactly what's on Toast, with modifiers and prices. Re-read on
  menu change.
- **Orders (write):** each confirmed order is POSTed to the orders API as a takeout/pickup
  order (dining option for takeout, revenue center if the restaurant uses them), with
  customer name and phone, line modifiers and kitchen notes, ASAP with the restaurant's
  prep time. Prices come from Toast's menu.
- **Payment:** customers pay by a texted Stripe Checkout link or at pickup. We'd record
  the online payment against the Toast order (to discuss with Toast which payment
  method/tender to use), or send orders as unpaid for pay-at-pickup.
- **Restaurant info (read):** hours, online ordering availability, prep times, so the
  agent doesn't take orders when the restaurant is closed or snoozed.
- We don't need labor, cash, or historical sales data. No polling beyond menu refreshes
  and order status.

**Scopes we expect to need:** menus (V3, `menus.channel:read`), orders write and read,
restaurant configuration (dining options, revenue centers), restaurant info/availability.
[Confirm against the scope list in the docs Toast sends.]

**Current status:** Live on one restaurant's phone line with our own dashboard.
Square and Clover integrations built the same way (orders sent in the background with
retries and idempotency, OAuth, encrypted tokens). Hosted on Fly.io (US).

**Security and privacy:**
- Access tokens encrypted at rest; per-restaurant data separation; signed webhooks
  (Twilio, Stripe); no card data handled by us (Stripe-hosted payment page).
- Customer data stored: name, phone, order, call transcript. [Retention period: e.g. 90
  days for transcripts, which we still need to implement.]
- [Privacy policy URL], [terms URL], [security contact].

**Restaurants:** [number of restaurants live/in pipeline], [how many use Toast].
**Alpha restaurant:** [a Toast restaurant willing to pilot, with its location].
**Beta:** [3 to 5 Toast locations you can name].

**Pricing to restaurants:** [your price]. Toast often lists partner integrations at a
monthly fee per location; [your answer].

**Go-to-market:** [direct sales to independent restaurants in X area, etc.].

## Before you submit

- [ ] Fill in the bracketed items.
- [ ] Have a privacy policy and terms page published (Toast's legal/privacy review asks).
- [ ] Line up at least one Toast restaurant for the alpha. Toast weighs real demand.
- [ ] After the follow-up email arrives, paste the final form's questions here and I'll
      draft answers to each.
