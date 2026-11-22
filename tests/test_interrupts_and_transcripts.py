import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ordering_agent import agent as agent_module
from ordering_agent import server
from ordering_agent.agent import OrderingAgent
from ordering_agent.menu import load_menu
from ordering_agent.storage import OrderStore
from ordering_agent.tools import run_tool

from .conftest import BELLA_PHONE, FIXTURE_MENU, seed_restaurant
from .fakes import FakeClient, message, text_block, tool_block


def slow_place_order(started: threading.Event, seconds: float = 0.4):
    def run(session, name, args):
        if name == "place_order":
            started.set()
            time.sleep(seconds)  # Stripe + Twilio round trips
        return run_tool(session, name, args)
    return run


def test_interrupt_during_tool_call_finishes_the_tool(monkeypatch):
    started = threading.Event()
    monkeypatch.setattr(agent_module, "run_tool", slow_place_order(started))
    client = FakeClient([
        message(text_block("Placing it now. "),
                tool_block("place_order", {"caller_confirmed": True}), stop_reason="tool_use"),
        message(text_block("never spoken")),  # must not be requested after the interrupt
        message(text_block("You're all set, order number 1.")),
    ])
    agent = OrderingAgent(load_menu(FIXTURE_MENU), client=client, store=OrderStore(":memory:"))
    agent.session.order.add_item("tiramisu")
    agent.session.order.set_customer("Ana")

    async def scenario():
        spoken = []

        async def speak():
            async for chunk in agent.respond("yes, place it"):
                spoken.append(chunk)

        task = asyncio.create_task(speak())
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert agent.stop_speaking() is True  # mid tool call: finish, don't cancel
        await task
        return spoken

    spoken = asyncio.run(scenario())
    assert spoken == ["Placing it now. "]
    assert agent.session.order_number == 1
    assert len(client.requests) == 1  # stopped after the tool result; no follow-up turn
    assert agent.messages[-1]["content"][0]["type"] == "tool_result"

    client.script.pop(0)
    agent.note_interrupted("Placing it now.")
    out = asyncio.run(_collect(agent.respond("hello?")))
    assert out == "You're all set, order number 1."
    last_user = client.requests[-1]["messages"][-1]
    assert last_user["role"] == "user"
    assert last_user["content"][0]["type"] == "tool_result"  # merged, not two user turns
    assert "interrupted" in last_user["content"][1]["text"]


def test_interrupt_before_any_tool_cancels():
    agent = OrderingAgent(load_menu(FIXTURE_MENU), client=FakeClient([]))
    assert agent.stop_speaking() is False


def test_cancelled_turn_merges_with_next_caller_turn():
    client = FakeClient([message(text_block("ok"))])
    agent = OrderingAgent(load_menu(FIXTURE_MENU), client=client)
    agent.messages.append({"role": "user", "content": "two samosas"})  # reply was cancelled
    asyncio.run(_collect(agent.respond("actually three")))
    msgs = client.requests[0]["messages"]
    assert len(msgs) == 1 and [b["text"] for b in msgs[0]["content"]] == ["two samosas", "actually three"]


async def _collect(agen):
    return "".join([c async for c in agen])


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDERS_DB", str(tmp_path / "orders.db"))
    monkeypatch.setenv("MENU_PATH", str(FIXTURE_MENU))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "ORDERS_API_TOKEN", "tok")
    fake = FakeClient([])
    server.app.state.client = fake
    server.app.state.payments = None
    with TestClient(server.app) as c:
        seed_restaurant(server.app.state.store)
        yield c, fake
    del server.app.state.client
    del server.app.state.payments


def read_until_last(ws):
    tokens = []
    while True:
        m = json.loads(ws.receive_text())
        tokens.append(m.get("token", ""))
        if m.get("last"):
            return "".join(tokens)


def test_interrupted_order_is_saved_and_transcribed(app_client, monkeypatch):
    c, fake = app_client
    started = threading.Event()
    monkeypatch.setattr(agent_module, "run_tool", slow_place_order(started))
    fake.script = [
        message(tool_block("add_item", {"item_id": "tiramisu"}, id="t1"),
                tool_block("set_customer_name", {"name": "Lee"}, id="t2"), stop_reason="tool_use"),
        message(text_block("One tiramisu for Lee. Shall I place it?")),
        message(text_block("Placing it now. "),
                tool_block("place_order", {"caller_confirmed": True}, id="t3"), stop_reason="tool_use"),
        message(text_block("Sorry about that! You're order number 1.")),
    ]
    with c.websocket_connect("/ws?restaurant=1") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA7", "from": "+15550001111", "to": BELLA_PHONE}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "a tiramisu, I'm Lee", "last": True}))
        assert read_until_last(ws) == "One tiramisu for Lee. Shall I place it?"

        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "yes", "last": True}))
        assert json.loads(ws.receive_text())["token"] == "Placing it now. "
        assert started.wait(2)
        ws.send_text(json.dumps({"type": "interrupt", "utteranceUntilInterrupt": "Placing it"}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "did it go through?", "last": True}))
        assert read_until_last(ws) == "Sorry about that! You're order number 1."

    store = server.app.state.store
    assert store.recent()[0]["customer_name"] == "Lee"

    auth = {"Authorization": "Bearer tok"}
    assert c.get("/calls").status_code == 401
    for _ in range(50):  # the call-end row is written as the socket closes
        calls = c.get("/calls", headers=auth).json()
        if calls[0]["ended_at"]:
            break
        time.sleep(0.02)
    assert calls[0]["call_id"] == "CA7" and calls[0]["order_number"] == 1
    assert calls[0]["caller_phone"] == "+15550001111" and calls[0]["caller_turns"] == 3

    events = c.get("/calls/CA7", headers=auth).json()["events"]
    lines = [(e["role"], e["text"]) for e in events]
    roles = [r for r, _ in lines]
    assert lines[0][0] == "agent" and "Bella Cucina" in lines[0][1]  # greeting first
    assert ("caller", "a tiramisu, I'm Lee") in lines
    assert any(r == "tool" and t.startswith("add_item(") for r, t in lines)
    i_spoken = lines.index(("agent", "Placing it now."))
    i_place = next(i for i, (r, t) in enumerate(lines) if r == "tool" and t.startswith("place_order"))
    assert i_spoken < i_place  # transcript is in the order things happened
    assert any(r == "event" and "Placing it" in t for r, t in lines)
    assert lines[-1] == ("event", "Call ended; order #1 placed")
    assert roles.count("caller") == 3
