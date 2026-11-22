"""Talk to the ordering agent in the terminal, no phone needed.

    python -m ordering_agent.cli                      # menu from MENU_PATH or data/menu.json
    python -m ordering_agent.cli --restaurant SLUG    # a restaurant saved in ORDERS_DB
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from .agent import OrderingAgent
from .menu import DEFAULT_MENU_PATH, load_menu
from .restaurants import RestaurantDirectory
from .storage import OrderStore


async def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m ordering_agent.cli")
    ap.add_argument("--restaurant", help="slug of a restaurant saved in the database")
    args = ap.parse_args()
    store = OrderStore(os.environ.get("ORDERS_DB", "data/orders.db"))
    if args.restaurant:
        menu = RestaurantDirectory(store).by_slug(args.restaurant)
        if menu is None:
            raise SystemExit(f"no restaurant {args.restaurant!r}; see python -m ordering_agent.restaurants list")
    else:
        menu = load_menu(os.environ.get("MENU_PATH") or DEFAULT_MENU_PATH)
    agent = OrderingAgent(menu, store=store, call_id="cli", caller_phone="+15550000000")
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
