# Project notes for Claude

Phone ordering agent for a restaurant: Claude (Messages API) + Twilio ConversationRelay.
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
- Terminal chat: `python -m ordering_agent.cli` (needs `ANTHROPIC_API_KEY`)
- Business rules belong in `ordering_agent/order.py`, not in the prompt.
- New settings go in `.env.example`, the README configuration table, and the design doc.
