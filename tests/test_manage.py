import json

import pytest
from fastapi.testclient import TestClient

from phone_agent import accounts as accounts_cli
from phone_agent import manage, server
from phone_agent.accounts import Accounts, LoginLimiter, hash_password, verify_password
from phone_agent.catalog import parse_catalog
from phone_agent.catalog_check import check_catalog
from phone_agent.order import Order, OrderError
from phone_agent.prompts import build_greeting

from .conftest import BELLA_PHONE, FIXTURE_CATALOG, seed_business
from .fakes import FakeClient

TACO_PHONE = "+15550008888"
W = {"X-Manage": "1"}


def fixture_catalog(**business):
    catalog = json.loads(FIXTURE_CATALOG.read_text())
    catalog["settings"].update({"payment": {"required_before_cooking": False}, **business})
    return catalog


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "orders.db"))
    monkeypatch.setenv("ADMIN_API_TOKEN", "optok")
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(manage, "limiter", LoginLimiter())
    server.app.state.client = FakeClient([])
    server.app.state.payments = None
    with TestClient(server.app) as c:
        store = server.app.state.store
        bella = seed_business(store, catalog=fixture_catalog())
        taco = seed_business(store, "taco-town", TACO_PHONE, fixture_catalog(name="Taco Town"))
        yield c, store, bella, taco
    del server.app.state.client, server.app.state.payments


def sign_in(c, store, business_id, email="ana@example.com", password="correct horse battery"):
    token = Accounts(store).invite(email, business_id, "Ana")
    r = c.post("/api/manage/accept-invite", json={"token": token, "password": password}, headers=W)
    assert r.status_code == 200, r.text
    return token


def editor(c, rid, **kw):
    r = c.get(f"/api/manage/businesses/{rid}/editor", **kw)
    assert r.status_code == 200, r.text
    return r.json()


# ---- accounts --------------------------------------------------------------------------------


def test_password_hashing():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h) and not verify_password("x", None) and not verify_password("x", "junk")


def test_invite_signs_in_and_only_shows_own_business(app):
    c, store, bella, taco = app
    token = sign_in(c, store, bella)
    me = c.get("/api/manage/me").json()
    assert me["who"] == "ana@example.com" and [r["id"] for r in me["businesses"]] == [bella]
    assert c.get(f"/api/manage/businesses/{taco}/editor").status_code == 404
    # The invite works once.
    again = c.post("/api/manage/accept-invite", json={"token": token, "password": "another password 1"}, headers=W)
    assert again.status_code == 400 and "expired" in again.json()["detail"]
    # Signing out ends the session.
    c.post("/api/manage/logout", headers=W)
    assert c.get("/api/manage/me").status_code == 401


def test_login_rules(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    c.cookies.clear()
    bad = c.post("/api/manage/login", json={"email": "ana@example.com", "password": "nope"}, headers=W)
    assert bad.status_code == 401
    ok = c.post("/api/manage/login", json={"email": " ANA@example.com ", "password": "correct horse battery"}, headers=W)
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    short = c.post("/api/manage/accept-invite", json={"token": "x", "password": "short"}, headers=W)
    assert short.status_code == 400 and "10 characters" in short.json()["detail"]
    for _ in range(8):
        c.post("/api/manage/login", json={"email": "ana@example.com", "password": "nope"}, headers=W)
    assert c.post("/api/manage/login", json={"email": "ana@example.com", "password": "correct horse battery"},
                  headers=W).status_code == 429


def test_disabled_user_is_signed_out(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    store.set_user_active(store.user_by_email("ana@example.com")["id"], False)
    assert c.get("/api/manage/me").status_code == 401


def test_writes_need_the_header_and_same_origin(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    ed = editor(c, bella)
    url = f"/api/manage/businesses/{bella}/draft"
    assert c.put(url, json={"catalog": ed["catalog"], "revision": None}).status_code == 403
    evil = c.put(url, json={"catalog": ed["catalog"], "revision": None}, headers={**W, "Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert c.put(url, json={"catalog": ed["catalog"], "revision": None},
                 headers={**W, "Origin": "http://testserver"}).status_code == 200


def test_operator_token_sees_every_business(app):
    c, store, bella, taco = app
    auth = {"Authorization": "Bearer optok"}
    me = c.get("/api/manage/me", headers=auth).json()
    assert me["operator"] and {r["id"] for r in me["businesses"]} == {bella, taco}
    assert c.get("/api/manage/me", headers={"Authorization": "Bearer wrong"}).status_code == 401


# ---- drafts, publishing, history ----------------------------------------------------------------


def test_edit_draft_then_publish_reaches_the_next_call(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    ed = editor(c, bella)
    assert ed["draft"] is None and ed["check"]["errors"] == []
    assert {f["path"] for f in ed["schema"]} >= {"name", "tax_rate", "notes", "greeting"}
    catalog = ed["catalog"]
    catalog["settings"]["greeting"] = "Ciao! Bella Cucina here. What would you like?"
    tiramisu = next(i for i in catalog["items"] if i["id"] == "tiramisu")
    tiramisu["available"] = False
    catalog["items"].append({"id": "cannoli", "name": "Cannoli", "category": "Desserts", "price": 6.5,
                          "description": "Vegetarian."})
    url = f"/api/manage/businesses/{bella}"
    r = c.put(url + "/draft", json={"catalog": catalog, "revision": None}, headers=W)
    assert r.status_code == 200 and r.json()["revision"] == 1
    assert any("sold out" in w for w in r.json()["check"]["warnings"])
    # Not live until published.
    assert "Ciao" not in c.post("/voice", data={"To": BELLA_PHONE}).text
    assert editor(c, bella)["draft"]["updated_by"] == "ana@example.com"

    r = c.post(url + "/publish", json={"revision": 1}, headers=W)
    assert r.status_code == 200, r.text
    assert r.json()["draft"] is None
    assert "Ciao! Bella Cucina here." in c.post("/voice", data={"To": BELLA_PHONE}).text
    live = server.app.state.businesses.get(bella)
    assert live.get("cannoli").price == 6.5
    with pytest.raises(OrderError, match="sold out"):
        Order(catalog=live).add_item("tiramisu")
    assert "SOLD OUT" in live.to_prompt_text()
    versions = c.get(url + "/versions").json()
    assert versions[0]["live"] and versions[0]["created_by"] == "ana@example.com"
    assert versions[1]["created_by"] == "operator"


def test_stale_revision_is_a_conflict(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    catalog = editor(c, bella)["catalog"]
    url = f"/api/manage/businesses/{bella}/draft"
    assert c.put(url, json={"catalog": catalog, "revision": None}, headers=W).json()["revision"] == 1
    assert c.put(url, json={"catalog": catalog, "revision": 1}, headers=W).json()["revision"] == 2
    stale = c.put(url, json={"catalog": catalog, "revision": 1}, headers=W)  # another tab, behind
    assert stale.status_code == 409
    assert c.post(f"/api/manage/businesses/{bella}/publish", json={"revision": 1}, headers=W).status_code == 409


def test_publish_is_refused_with_errors(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    catalog = editor(c, bella)["catalog"]
    catalog["settings"]["tax_rate"] = 0.2  # 20%: out of range
    catalog["items"][0]["price"] = 0
    url = f"/api/manage/businesses/{bella}"
    r = c.put(url + "/draft", json={"catalog": catalog, "revision": None}, headers=W)
    errors = r.json()["check"]["errors"]
    assert any("Sales tax must be between 0% and 15%" in e for e in errors)
    assert any("price must be more than $0" in e for e in errors)
    r = c.post(url + "/publish", json={"revision": 1}, headers=W)
    assert r.status_code == 422 and r.json()["check"]["errors"]
    assert len(store.catalog_versions(bella)) == 1


def test_publish_over_a_newer_catalog_needs_confirmation(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    catalog = editor(c, bella)["catalog"]
    url = f"/api/manage/businesses/{bella}"
    c.put(url + "/draft", json={"catalog": catalog, "revision": None}, headers=W)
    store.set_catalog(bella, fixture_catalog(name="Bella Nuova"))  # the operator publishes meanwhile
    assert editor(c, bella)["draft"]["live_changed"] is True
    r = c.post(url + "/publish", json={"revision": 1}, headers=W)
    assert r.status_code == 409 and r.json()["code"] == "live_changed"
    assert c.post(url + "/publish", json={"revision": 1, "overwrite": True}, headers=W).status_code == 200


def test_restore_and_discard(app):
    c, store, bella, _ = app
    sign_in(c, store, bella)
    first = store.catalog_versions(bella)[0]["id"]
    store.set_catalog(bella, fixture_catalog(name="Renamed"))
    url = f"/api/manage/businesses/{bella}"
    ed = c.post(f"{url}/versions/{first}/restore", json={"revision": None}, headers=W).json()
    assert ed["catalog"]["settings"]["name"] == "Bella Cucina" and ed["draft"]["revision"] == 1
    assert server.app.state.businesses.get(bella).settings["name"] == "Renamed"  # not live yet
    ed = c.delete(url + "/draft", headers=W).json()
    assert ed["draft"] is None and ed["catalog"]["settings"]["name"] == "Renamed"
    assert c.post(f"{url}/versions/999/restore", json={}, headers=W).status_code == 404


def test_manage_page_is_served_with_safe_headers(app):
    c, *_ = app
    r = c.get("/manage")
    assert r.status_code == 200 and "<title>Phone Agent</title>" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


# ---- catalog rules and checks -------------------------------------------------------------------


def test_catalog_checks_on_the_real_catalog_have_no_errors():
    real = json.loads(open("data/businesses/hyderabad-house.json").read())
    result = check_catalog(real)
    assert result["errors"] == [] and result["summary"]["items"] > 200
    assert any("estimated prices" in b for b in result["blockers"])


def test_greeting_default_and_custom():
    catalog = parse_catalog(fixture_catalog())
    assert build_greeting(catalog).startswith("Thanks for calling Bella Cucina")
    assert build_greeting(parse_catalog(fixture_catalog(greeting="  Hello there!  "))) == "Hello there!"


def test_accounts_cli(app, monkeypatch, capsys):
    _, store, bella, _ = app
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    accounts_cli.main(["invite", "Raj@Example.com", "--business", "bella-cucina"], store=store)
    out = capsys.readouterr().out
    assert "https://example.test/manage#invite=" in out
    accounts_cli.main(["list"], store=store)
    assert "raj@example.com" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="doesn't look like an email"):
        accounts_cli.main(["invite", "not-an-email"], store=store)
    with pytest.raises(SystemExit, match="no business"):
        accounts_cli.main(["invite", "a@b.co", "--business", "nope"], store=store)


def test_unset_address_and_hours_are_described_to_the_agent():
    raw = fixture_catalog()
    del raw["settings"]["address"], raw["settings"]["hours"]
    text = parse_catalog(raw).to_prompt_text()
    assert "Address: not available on this line yet" in text and "None" not in text
