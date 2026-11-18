import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ordering_agent import server
from ordering_agent.agent import OrderingAgent
from ordering_agent.menu import load_menu
from ordering_agent.storage import OrderStore

from .fakes import FakeClient, message, text_block, tool_block


async def collect(agen):
    return "".join([c async for c in agen])


def test_agent_runs_tool_loop_and_streams_text():
    client = FakeClient([
        message(text_block("Sure, adding that. "),
                tool_block("add_item", {"item_id": "wings", "options": {"sauce": "bbq"}}),
                stop_reason="tool_use"),
        message(text_block("Ten BBQ wings, twelve dollars. Anything else?")),
    ])
    agent = OrderingAgent(load_menu(), client=client, caller_phone="+15551234567")
    out = asyncio.run(collect(agent.respond("Ten BBQ wings please")))

    assert out == "Sure, adding that. Ten BBQ wings, twelve dollars. Anything else?"
    assert agent.session.order.lines[0].options == {"sauce": "bbq"}
    assert agent.session.order.customer_phone == "+15551234567"
    # second request carries the tool_result right after the assistant tool_use
    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["is_error"] is False
    # request shape
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default"
    assert all(t["eager_input_streaming"] for t in req["tools"])


def test_agent_reports_tool_errors_to_model():
    client = FakeClient([
        message(tool_block("add_item", {"item_id": "margherita"}), stop_reason="tool_use"),
        message(text_block("What size would you like?")),
    ])
    agent = OrderingAgent(load_menu(), client=client)
    asyncio.run(collect(agent.respond("a margherita")))
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "size" in result["content"]
    assert agent.session.order.lines == []


def test_refusal_gives_spoken_fallback():
    client = FakeClient([message(stop_reason="refusal")])
    agent = OrderingAgent(load_menu(), client=client)
    out = asyncio.run(collect(agent.respond("something off topic")))
    assert "menu" in out


def test_interrupt_note_is_passed_to_next_turn():
    client = FakeClient([message(text_block("ok"))])
    agent = OrderingAgent(load_menu(), client=client)
    agent.note_interrupted("Our specials today are")
    asyncio.run(collect(agent.respond("Just a soda")))
    first = client.requests[0]["messages"][0]["content"]
    assert "interrupted" in first and first.endswith("Just a soda")


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDERS_DB", str(tmp_path / "orders.db"))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    fake = FakeClient([])
    server.app.state.client = fake
    with TestClient(server.app) as c:
        yield c, fake
    del server.app.state.client


def test_voice_webhook_returns_conversation_relay_twiml(app_client):
    c, _ = app_client
    r = c.post("/voice", data={"CallSid": "CA1"}, headers={"host": "example.ngrok.app",
                                                           "x-forwarded-proto": "https"})
    assert r.status_code == 200
    assert '<ConversationRelay url="wss://example.ngrok.app/ws"' in r.text
    assert "Thanks for calling Bella Cucina" in r.text


def test_voice_webhook_rejects_bad_signature(app_client, monkeypatch):
    c, _ = app_client
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "secret")
    r = c.post("/voice", data={"CallSid": "CA1"}, headers={"x-twilio-signature": "nope"})
    assert r.status_code == 403


def test_full_phone_order_over_websocket(app_client, monkeypatch):
    c, fake = app_client
    fake.script = [
        message(tool_block("add_item", {"item_id": "tiramisu", "quantity": 2}, id="t1"),
                tool_block("set_customer_name", {"name": "Lee"}, id="t2"),
                stop_reason="tool_use"),
        message(text_block("Two tiramisu for Lee, $17.28. Shall I place it?")),
        message(tool_block("place_order", {"caller_confirmed": True}), stop_reason="tool_use"),
        message(text_block("Done! Order number 1, ready in 20 minutes. Bye!"),
                tool_block("end_call", {}, id="t3"), stop_reason="tool_use"),
    ]
    monkeypatch.setattr(server, "WORDS_PER_SECOND", 1e9)  # skip the goodbye wait
    with c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA9", "from": "+15550001111"}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Two tiramisu, name is Lee",
                                 "last": True}))
        tokens = []
        while True:
            m = json.loads(ws.receive_text())
            tokens.append(m)
            if m.get("last"):
                break
        assert "".join(t["token"] for t in tokens) == "Two tiramisu for Lee, $17.28. Shall I place it?"

        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "yes", "last": True}))
        msgs = []
        while True:
            m = json.loads(ws.receive_text())
            msgs.append(m)
            if m["type"] == "end":
                break
    orders = server.app.state.store.recent()
    assert orders[0]["customer_name"] == "Lee"
    assert orders[0]["customer_phone"] == "+15550001111"
    assert orders[0]["total"] == 17.28
