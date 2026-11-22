# Project notes for Claude

Phone agent for businesses (restaurants, shops; bookings coming soon): Claude (Messages
API) + Twilio ConversationRelay. Each business has a template (`phone_agent/templates.py`).
See `docs/DESIGN.md` for the high-level design.

## Keep the design doc current

Whenever a change affects the design (architecture, call flow, components, tools,
configuration, state/scaling, security, failure handling, or a key decision), update
`docs/DESIGN.md` in the same commit:

- Edit the affected sections so they describe the system as it is now.
- Update "Open issues and next steps" (remove what's resolved, add what's new).
- Add a row to the "Change log" table and update "Last updated" at the top.

Pure refactors, test-only changes and typo fixes don't need a design update.

## Working in this repo

- Setup: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt`
- Tests: `python -m pytest` (no API key needed; uses `tests/fakes.py`)
- Terminal chat: `python -m phone_agent.cli` (needs `ANTHROPIC_API_KEY`)
- Business rules belong in code (the module's own code, e.g. `phone_agent/order.py` for
  orders), not in the prompt. New capabilities are modules (`phone_agent/modules/`); new
  kinds of business are templates (`phone_agent/templates.py`).
- Keep businesses isolated (design doc section 5): anything stored per business is keyed
  by `business_id` and queried with it; one business's bad data, load or failing
  integration must never affect another's calls (no shared blocking work, per-business
  limits). Add a test in `tests/test_isolation.py` for new shared resources.
- New settings go in `.env.example`, the README configuration table, and the design doc.
