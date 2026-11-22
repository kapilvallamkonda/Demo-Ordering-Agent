"""Talk to the phone agent in the terminal, no phone needed.

    python -m phone_agent.cli                      # catalog from CATALOG_PATH or data/businesses/hyderabad-house.json
    python -m phone_agent.cli --business SLUG    # a business saved in DATABASE_PATH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from .agent import PhoneAgent
from .catalog import DEFAULT_CATALOG_PATH, load_catalog
from .businesses import BusinessDirectory
from .storage import Store


async def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m phone_agent.cli")
    ap.add_argument("--business", help="slug of a business saved in the database")
    args = ap.parse_args()
    store = Store(os.environ.get("DATABASE_PATH", "data/phone_agent.db"))
    if args.business:
        catalog = BusinessDirectory(store).by_slug(args.business)
        if catalog is None:
            raise SystemExit(f"no business {args.business!r}; see python -m phone_agent.businesses list")
    else:
        catalog = load_catalog(os.environ.get("CATALOG_PATH") or DEFAULT_CATALOG_PATH)
    agent = PhoneAgent(catalog, store=store, call_id="cli", caller_phone="+15550000000")
    print(f"Agent: {agent.greeting}")
    while not agent.end_requested:
        try:
            text = (await asyncio.to_thread(input, "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        print("Agent: ", end="", flush=True)
        async for chunk in agent.respond(text):
            print(chunk, end="", flush=True)
        print()
    s = agent.session
    if s.order.placed:
        print(f"\n[Order #{s.order_number} saved]\n" + json.dumps(s.order.summary(), indent=2))
    else:
        print("\n[Call ended without placing an order]")


if __name__ == "__main__":
    asyncio.run(main())
