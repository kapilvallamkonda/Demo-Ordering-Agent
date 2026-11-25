import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from phone_agent import languages, server
from phone_agent.agent import PhoneAgent
from phone_agent.catalog import load_catalog
from phone_agent.storage import Store

from .conftest import BELLA_PHONE, FIXTURE_CATALOG, seed_business
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
    agent = PhoneAgent(load_catalog(FIXTURE_CATALOG), client=client, caller_phone="+15551234567")
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
    agent = PhoneAgent(load_catalog(FIXTURE_CATALOG), client=client)
    asyncio.run(collect(agent.respond("a margherita")))
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "size" in result["content"]
    assert agent.session.order.lines == []


def test_refusal_gives_spoken_fallback():
    client = FakeClient([message(stop_reason="refusal")])
    agent = PhoneAgent(load_catalog(FIXTURE_CATALOG), client=client)
    out = asyncio.run(collect(agent.respond("something off topic")))
    assert "catalog" in out


def test_interrupt_note_is_passed_to_next_turn():
    client = FakeClient([message(text_block("ok"))])
    agent = PhoneAgent(load_catalog(FIXTURE_CATALOG), client=client)
    agent.note_interrupted("Our specials today are")
    asyncio.run(collect(agent.respond("Just a soda")))
    first = client.requests[0]["messages"][0]["content"]
    assert "interrupted" in first and first.endswith("Just a soda")


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "orders.db"))
    monkeypatch.setenv("CATALOG_PATH", str(FIXTURE_CATALOG))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    fake = FakeClient([])
    server.app.state.client = fake
    with TestClient(server.app) as c:
        seed_business(server.app.state.store)
        yield c, fake
    del server.app.state.client


def test_voice_webhook_returns_conversation_relay_twiml(app_client):
    c, _ = app_client
    r = c.post("/voice", data={"CallSid": "CA1", "To": BELLA_PHONE}, headers={"host": "example.ngrok.app",
                                                           "x-forwarded-proto": "https"})
    assert r.status_code == 200
    assert '<ConversationRelay url="wss://example.ngrok.app/ws?business=1"' in r.text
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
    with c.websocket_connect("/ws?business=1") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA9", "from": "+15550001111", "to": BELLA_PHONE}))
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


def test_voice_settings_from_env(app_client, monkeypatch):
    c, _ = app_client
    r = c.post("/voice", data={"To": BELLA_PHONE})
    for attr in ['ttsProvider="ElevenLabs"', 'voice="mCQMfsqGDT6IDkEKR20a-v4_turbo"',
                 'ttsLanguage="en-IN"',  # Indian English by default
                 'welcomeGreetingInterruptible="dtmf"', 'interruptSensitivity="low"',
                 'ignoreBackchannel="true"']:  # background noise doesn't cut the agent off
        assert attr in r.text
    assert "transcriptionLanguage" not in r.text and " language=" not in r.text  # listening: Twilio's

    for name in ("TTS_PROVIDER", "TTS_VOICE", "TTS_LANGUAGE", "GREETING_INTERRUPTIBLE",
                 "INTERRUPT_SENSITIVITY", "IGNORE_BACKCHANNEL"):
        monkeypatch.setenv(name, "none")
    r = c.post("/voice", data={"To": BELLA_PHONE})
    assert "ttsProvider" not in r.text and "voice=" not in r.text and "ttsLanguage" not in r.text
    assert "Interruptible" not in r.text and "interruptSensitivity" not in r.text
    assert languages.english_switch_message()["ttsLanguage"] == "en-US"
    monkeypatch.delenv("TTS_LANGUAGE")
    assert languages.english_switch_message() == {
        "type": "language", "ttsLanguage": "en-IN", "transcriptionLanguage": "en-US"}

    monkeypatch.setenv("TTS_PROVIDER", "ElevenLabs")
    monkeypatch.setenv("TTS_VOICE", "abc123")
    monkeypatch.setenv("VOICE_LANGUAGE", "en-US")
    monkeypatch.setenv("STT_PROVIDER", "Deepgram")
    monkeypatch.setenv("RELAY_EXTRA_ATTRS", '{"interruptible": "speech", "url": "evil", "bad name": "x"}')
    r = c.post("/voice", data={"To": BELLA_PHONE})
    for attr in ['ttsProvider="ElevenLabs"', 'voice="abc123"', 'language="en-US"',
                 'transcriptionProvider="Deepgram"', 'interruptible="speech"']:
        assert attr in r.text
    assert "evil" not in r.text and "bad name" not in r.text


def test_bad_extra_attrs_json_is_ignored(app_client, monkeypatch):
    c, _ = app_client
    monkeypatch.setenv("RELAY_EXTRA_ATTRS", "{not json")
    assert c.post("/voice", data={"To": BELLA_PHONE}).status_code == 200


def test_dashboard_and_orders_need_token(app_client, monkeypatch):
    c, _ = app_client
    monkeypatch.setattr(server, "ADMIN_API_TOKEN", "")
    assert c.get("/dashboard").status_code == 404
    assert c.get("/orders").status_code == 404
    monkeypatch.setattr(server, "ADMIN_API_TOKEN", "tok")
    page = c.get("/dashboard")
    assert page.status_code == 200 and "<title>Ringcart · Orders</title>" in page.text
    assert c.get("/orders").status_code == 401
    assert c.get("/orders", headers={"authorization": "Bearer nope"}).status_code == 401
    r = c.get("/orders?limit=5", headers={"authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json() == []


def test_moving_domains_keeps_the_old_address_working(app_client, monkeypatch):
    """PUBLIC_BASE_URL is the new domain and the old one is an alias: Twilio calls signed for
    either address are accepted and stay on the address they came in on; browsers on the old
    address go to the new one; anything else is rejected."""
    from twilio.request_validator import RequestValidator
    c, _ = app_client
    new, old = "https://ringcart.ai", "https://phone-agent-604b.fly.dev"
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", new)
    monkeypatch.setenv("PUBLIC_BASE_URL_ALIASES", old)
    form = {"CallSid": "CA1", "To": BELLA_PHONE}
    sign = lambda base: RequestValidator("secret").compute_signature(base + "/voice", form)
    host = lambda base: {"host": base.split("//")[1], "x-forwarded-proto": "https"}

    for base in (new, old):
        r = c.post("/voice", data=form, headers={**host(base), "x-twilio-signature": sign(base)})
        assert r.status_code == 200
        assert f'url="{base.replace("https", "wss")}/ws?business=1' in r.text  # relay on the same address
    elsewhere = "https://evil.example"
    r = c.post("/voice", data=form, headers={**host(elsewhere), "x-twilio-signature": sign(elsewhere)})
    assert r.status_code == 403

    r = c.get("/manage?x=1", headers=host(old), follow_redirects=False)  # a person on the old address
    assert r.status_code == 308 and r.headers["location"] == new + "/manage?x=1"
    assert c.get("/health", headers=host(old)).status_code == 200  # machines aren't redirected
    assert c.get("/manage", headers=host(new), follow_redirects=False).status_code != 308
