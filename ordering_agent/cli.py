"""Talk to the ordering agent in the terminal, no phone needed.

    python -m ordering_agent.cli
"""

from __future__ import annotations

import asyncio
import json
import os

from .agent import OrderingAgent
from .menu import DEFAULT_MENU_PATH, load_menu
from .storage import OrderStore


async def main() -> None:
    menu = load_menu(os.environ.get("MENU_PATH") or DEFAULT_MENU_PATH)
    store = OrderStore(os.environ.get("ORDERS_DB", "data/orders.db"))
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
