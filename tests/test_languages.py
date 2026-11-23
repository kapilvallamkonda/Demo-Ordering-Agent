"""Languages besides English (Telugu): the setting, the call setup, and switching mid-call."""

import json

import pytest
from fastapi.testclient import TestClient

from phone_agent import server
from phone_agent.catalog import parse_catalog
from phone_agent.prompts import build_greeting, build_system_prompt
from phone_agent.settings_schema import validate_settings
from phone_agent.templates import get_template
from phone_agent.tools import CallSession, run_tool, tools_for

from .conftest import BELLA_PHONE, FIXTURE_CATALOG, seed_business
from .fakes import FakeClient, message, text_block, tool_block


def telugu_catalog():
    raw = json.loads(FIXTURE_CATALOG.read_text())
    raw["settings"]["languages"] = ["te-IN"]
    return raw


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    fake = FakeClient([])
    server.app.state.client, server.app.state.payments = fake, None
    with TestClient(server.app) as c:
        yield c, server.app.state.store, fake
    for attr in ("client", "payments"):
        if hasattr(server.app.state, attr):
            delattr(server.app.state, attr)


def test_setting_accepts_only_offered_languages():
    t = get_template("restaurant")
    base = {"name": "X", "pickup_minutes": 20, "tax_rate": 0.08}
    assert validate_settings({**base, "languages": ["te-IN"]}, t) == []
    assert any("Other languages" in e for e in validate_settings({**base, "languages": ["xx-YY"]}, t))


def test_greeting_prompt_and_tools_follow_the_setting():
    plain = parse_catalog(json.loads(FIXTURE_CATALOG.read_text()))
    telugu = parse_catalog(telugu_catalog())
    assert "press 2" not in build_greeting(plain)
    assert build_greeting(telugu).endswith("For Telugu, press 2.")
    assert "Telugu (te-IN)" in build_system_prompt(telugu) and "Telugu" not in build_system_prompt(plain)
    names = lambda tools: {t["name"] for t in tools}
    assert "switch_language" not in names(tools_for(plain.template.modules, plain.settings.get("languages")))
    assert "switch_language" in names(tools_for(telugu.template.modules, telugu.settings.get("languages")))


def test_switch_language_tool_only_switches_to_offered_languages():
    session = CallSession(catalog=parse_catalog(telugu_catalog()))
    text, err = run_tool(session, "switch_language", {"language": "hi-IN"})
    assert err and "te-IN" in text and session.language == "en-US"
    _, err = run_tool(session, "switch_language", {"language": "te-IN"})
    assert not err and session.language == "te-IN"


def test_voice_declares_telugu_only_for_businesses_that_offer_it(app):
    c, store, _ = app
    seed_business(store, catalog=telugu_catalog())
    seed_business(store, slug="plain", phone="+15550008888")
    twiml = c.post("/voice", data={"CallSid": "CA1", "From": "+15551112222", "To": BELLA_PHONE}).text
    assert 'dtmfDetection="true"' in twiml and "For Telugu, press 2." in twiml
    assert '<Language code="te-IN" ttsProvider="Google" voice="te-IN-Chirp3-HD-Kore"' in twiml
    assert 'transcriptionProvider="Google" speechModel="long"' in twiml
    plain = c.post("/voice", data={"CallSid": "CA2", "From": "+15551112222", "To": "+15550008888"}).text
    assert "Language" not in plain and "dtmfDetection" not in plain and "<ConversationRelay " in plain


def read_until_last(ws, sent):
    while True:
        m = json.loads(ws.receive_text())
        sent.append(m)
        if m.get("type") == "text" and m.get("last"):
            return


def test_pressing_2_switches_the_call_before_the_agent_speaks_telugu(app):
    c, store, fake = app
    bid = seed_business(store, catalog=telugu_catalog())
    fake.script = [
        message(text_block("నమస్కారం! మీకు ఏమి కావాలి?")),  # reply to the keypad switch
        message(tool_block("switch_language", {"language": "en-US"}, id="t1"), stop_reason="tool_use"),
        message(text_block("Sure, English it is.")),
    ]
    sent = []
    with c.websocket_connect(f"/ws?business={bid}") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA9", "from": "+15550001111", "to": BELLA_PHONE}))
        ws.send_text(json.dumps({"type": "dtmf", "digit": "2"}))
        read_until_last(ws, sent)
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "English please", "last": True}))
        read_until_last(ws, sent)
    kinds = [(m["type"], m.get("ttsLanguage") or m.get("token")) for m in sent]
    first_switch = kinds.index(("language", "te-IN"))
    first_telugu = next(i for i, k in enumerate(kinds) if k[0] == "text" and "నమస్కారం" in (k[1] or ""))
    assert first_switch < first_telugu  # voice and listening switch before the Telugu words
    assert sent[first_switch]["transcriptionLanguage"] == "te-IN"
    assert ("language", "en-US") in kinds  # the agent switched back on request
    assert "already switched to Telugu" in json.dumps(fake.requests[0]["messages"])
    # Listening switched at the keypress, before any agent text (the caller may speak first).
    assert sent[0]["type"] == "language" and sent[0]["transcriptionLanguage"] == "te-IN"
    assert any(t["name"] == "switch_language" for t in fake.requests[0]["tools"])


def test_telugu_is_the_default_until_the_business_chooses(monkeypatch):
    monkeypatch.setenv("DEFAULT_LANGUAGES", "te-IN")
    raw = json.loads(FIXTURE_CATALOG.read_text())
    assert "languages" not in raw["settings"]
    assert build_greeting(parse_catalog(raw)).endswith("For Telugu, press 2.")  # not chosen: default
    raw["settings"]["languages"] = []
    assert "Telugu" not in build_greeting(parse_catalog(raw))  # chose none: respected
    from phone_agent.settings_schema import settings_for
    field = next(f for f in settings_for(get_template("restaurant")) if f["path"] == "languages")
    assert field["default"] == ["te-IN"]  # the form shows it ticked
    monkeypatch.setenv("DEFAULT_LANGUAGES", "")  # the switch-off for everyone
    raw["settings"].pop("languages")
    assert "Telugu" not in build_greeting(parse_catalog(raw))

