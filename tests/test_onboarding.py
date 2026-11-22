"""Self-serve: Google sign-in, creating a business, reading a menu, test chat, going live, team."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from phone_agent import manage, server
from phone_agent.accounts import Accounts, LoginLimiter
from phone_agent.catalog_import import merge_items, to_catalog_item
from phone_agent.numbers import BoughtNumber, NumberError

from .conftest import FIXTURE_CATALOG, seed_business
from .fakes import FakeClient, message, text_block

W = {"X-Manage": "1"}


class FakeNumbers:
    def __init__(self, fail=None):
        self.bought, self.fail, self.n = [], fail, 0

    def buy(self, area_code, friendly_name, voice_url):
        if self.fail:
            raise NumberError(self.fail)
        self.n += 1
        number = f"+1{area_code or '555'}555{self.n:04d}"
        self.bought.append((number, friendly_name, voice_url))
        return BoughtNumber(number, f"PN{self.n}")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("ADMIN_API_TOKEN", "optok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agent.example")
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.com")
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(manage, "limiter", LoginLimiter())
    fake, numbers = FakeClient([]), FakeNumbers()
    server.app.state.client, server.app.state.payments, server.app.state.numbers = fake, None, numbers
    with TestClient(server.app, base_url="https://agent.example") as c:
        yield c, server.app.state.store, fake, numbers
    for attr in ("client", "payments", "numbers", "test_chats", "google_http", "menu_http", "menu_resolve"):
        if hasattr(server.app.state, attr):
            delattr(server.app.state, attr)


def sign_in(c, store, email="ana@example.com"):
    """A signed-in browser session for this email (as after Google sign-in)."""
    user = store.user_by_email(email)
    uid = user["id"] if user else store.add_user(email, email.split("@")[0].title())
    c.cookies.set(manage.COOKIE, Accounts(store).start_session(uid))
    return uid


def new_business(c, name="Rosa's Bakery", template="retail"):
    r = c.post("/api/manage/businesses", json={"name": name, "template": template}, headers=W)
    assert r.status_code == 200, r.text
    return r.json()


def fill_and_publish(c, business_id, items=None):
    ed = c.get(f"/api/manage/businesses/{business_id}/editor").json()
    catalog = ed["catalog"]
    catalog["settings"]["tax_rate"] = 0.08
    catalog["items"] = items or [{"id": "croissant", "name": "Croissant", "category": "Pastry", "price": 3.5,
                                  "description": "Butter."}]
    rev = ed["draft"]["revision"]
    r = c.put(f"/api/manage/businesses/{business_id}/draft", json={"catalog": catalog, "revision": rev}, headers=W)
    assert r.status_code == 200, r.text
    r = c.post(f"/api/manage/businesses/{business_id}/publish", json={"revision": r.json()["revision"]}, headers=W)
    assert r.status_code == 200, r.text


# ---- Google sign-in ---------------------------------------------------------------------------


def google_transport(profile, token_status=200):
    def handler(request: httpx.Request):
        if request.url.host == "oauth2.googleapis.com":
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["authorization_code"] and body["code_verifier"][0]
            return httpx.Response(token_status, json={"access_token": "at", "id_token": "x"})
        assert request.headers["authorization"] == "Bearer at"
        return httpx.Response(200, json=profile)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def google_sign_in(c, profile, monkeypatch, token_status=200):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    server.app.state.google_http = google_transport(profile, token_status)
    start = c.get("/auth/google", follow_redirects=False)
    assert start.status_code == 302
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["redirect_uri"] == ["https://agent.example/auth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
    return c.get("/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)


def test_google_sign_in_creates_the_account_and_signs_in(app, monkeypatch):
    c, store, *_ = app
    r = google_sign_in(c, {"sub": "g-1", "email": "Ana@Example.com", "email_verified": True, "name": "Ana"},
                       monkeypatch)
    assert r.status_code == 302 and r.headers["location"] == "/manage"
    me = c.get("/api/manage/me").json()
    assert me["email"] == "ana@example.com" and me["businesses"] == [] and me["name"] == "Ana"
    assert store.user_by_google("g-1")["email"] == "ana@example.com"


def test_google_sign_in_links_a_teammate_added_by_email(app, monkeypatch):
    c, store, *_ = app
    bid = seed_business(store)
    uid = store.add_user("raj@example.com")
    store.grant(uid, bid, "manager")
    google_sign_in(c, {"sub": "g-raj", "email": "raj@example.com", "email_verified": True}, monkeypatch)
    me = c.get("/api/manage/me").json()
    assert [(b["id"], b["role"]) for b in me["businesses"]] == [(bid, "manager")]


def test_google_sign_in_rejects_unverified_email_and_bad_state(app, monkeypatch):
    c, store, *_ = app
    r = google_sign_in(c, {"sub": "g-2", "email": "x@example.com", "email_verified": False}, monkeypatch)
    assert r.status_code == 400 and "verified" in r.text
    r = c.get("/auth/google/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 400 and c.get("/api/manage/me").status_code == 401
    r = google_sign_in(c, {"sub": "g-3", "email": "y@example.com", "email_verified": True}, monkeypatch,
                       token_status=400)
    assert r.status_code == 400


def test_config_and_landing(app, monkeypatch):
    c, *_ = app
    assert c.get("/api/manage/config").json() == {"google": False, "password_login": True}
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    assert c.get("/api/manage/config").json()["google"] is True
    landing = c.get("/")
    assert landing.status_code == 200 and "Get started with Google" in landing.text


# ---- creating a business ------------------------------------------------------------------------


def test_create_business_starts_a_draft_owned_by_the_creator(app):
    c, store, *_ = app
    sign_in(c, store)
    created = new_business(c)
    assert created["template"]["id"] == "retail" and created["catalog"]["settings"]["name"] == "Rosa's Bakery"
    assert created["catalog"]["items"] == [] and created["live_catalog_id"] is None
    me = c.get("/api/manage/me").json()
    assert [(b["name"], b["role"], b["active"]) for b in me["businesses"]] == [("Rosa's Bakery", "owner", False)]
    assert store.business(created["id"]).slug == "rosa-s-bakery"
    # Can't publish an empty catalog without tax and items.
    errors = created["check"]["errors"]
    assert any("Sales tax" in e for e in errors) or any("no items" in e for e in errors)


def test_create_business_rules(app, monkeypatch):
    c, store, *_ = app
    sign_in(c, store)
    assert c.post("/api/manage/businesses", json={"name": "Hair", "template": "salon"}, headers=W).status_code == 400
    assert c.post("/api/manage/businesses", json={"name": "", "template": "retail"}, headers=W).status_code == 400
    monkeypatch.setenv("MAX_BUSINESSES_PER_ACCOUNT", "2")
    new_business(c, "One")
    new_business(c, "One")  # same name: gets its own slug
    r = c.post("/api/manage/businesses", json={"name": "Three", "template": "retail"}, headers=W)
    assert r.status_code == 400 and "up to 2" in r.json()["detail"]
    assert sorted(b.slug for b in store.businesses()) == ["one", "one-2"]


# ---- reading a menu ---------------------------------------------------------------------------


EXTRACTED = {
    "items": [
        {"name": "Croissant", "category": "Pastry", "price": 3.5, "price_was_printed": True,
         "description": "Butter. Bestseller.", "options": [], "extras": [{"name": "jam", "price": 0.5}]},
        {"name": "Cake", "category": "Cakes", "price": 30, "price_was_printed": False, "description": "",
         "options": [{"name": "Size", "required": True,
                      "choices": [{"name": "6 inch", "added_price": 0}, {"name": "8 inch", "added_price": 10}]}],
         "extras": []},
    ],
    "business": {"name": "Rosa's", "address": "1 Main St", "hours": None},
    "notes": ["Cakes need a day's notice."],
    "unreadable": ["bottom of page 2"],
}


def test_import_reads_photos_into_the_draft(app):
    c, store, fake, _ = app
    sign_in(c, store)
    b = new_business(c)
    fake.script = [message(text_block(json.dumps(EXTRACTED)))]
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": b["draft"]["revision"]},
               files=[("files", ("menu.jpg", b"\xff\xd8fakejpeg", "image/jpeg"))], headers=W)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["added"] == 2 and out["unreadable"] == ["bottom of page 2"]
    items = {i["id"]: i for i in out["catalog"]["items"]}
    assert items["croissant"]["extras"] == {"jam": 0.5}
    assert items["cake"]["options"] == {"size": {"choices": {"6 inch": 0, "8 inch": 10}, "required": True}}
    assert items["cake"]["placeholder_price"] is True
    settings = out["catalog"]["settings"]
    assert settings["address"] == "1 Main St" and "Cakes need a day's notice." in settings["notes"]
    assert settings["name"] == "Rosa's Bakery"  # what the owner typed wins
    call = fake.requests[-1]
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert call["output_config"]["format"]["type"] == "json_schema"
    # Unsupported file type.
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": out["draft"]["revision"]},
               files=[("files", ("menu.heic", b"x", "image/heic"))], headers=W)
    assert r.status_code == 400 and "JPEG" in r.json()["detail"]


MENU_PAGE = """<html><head><title>Rosa's</title><script>var price = "$999.99";</script></head><body>
<nav>Home | Order | Other location</nav><h2>Pastry</h2>
<div>Croissant <span>$3.50</span></div><div>Cake <span>$30.00</span></div><div>Tea <span>$2.00</span></div>
</body></html>"""


def test_import_reads_a_menu_link(app):
    c, store, fake, _ = app
    sign_in(c, store)
    b = new_business(c)
    seen = []

    def site(req):
        seen.append(str(req.url))
        if req.url.path == "/menu":
            return httpx.Response(301, headers={"location": "/food-menu"})
        return httpx.Response(200, text=MENU_PAGE, headers={"content-type": "text/html; charset=utf-8"})

    server.app.state.menu_http = httpx.AsyncClient(transport=httpx.MockTransport(site))

    async def resolve(host, port):
        return {"rosas.example": ["93.184.216.34"], "inside.example": ["10.0.0.7"]}[host]

    server.app.state.menu_resolve = resolve
    fake.script = [message(text_block(json.dumps(EXTRACTED)))]
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": b["draft"]["revision"]},
               data={"url": "rosas.example/menu"}, headers=W)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2
    assert seen == ["https://rosas.example/menu", "https://rosas.example/food-menu"]  # https added, redirect followed
    doc = fake.requests[-1]["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["type"] == "text"
    assert "Croissant $3.50" in doc["source"]["data"] and "$999.99" not in doc["source"]["data"]  # no scripts
    rev = r.json()["draft"]["revision"]
    # A link to a private address is refused before anything is fetched.
    n = len(seen)
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": rev},
               data={"url": "https://inside.example/menu"}, headers=W)
    assert r.status_code == 400 and "public" in r.json()["detail"] and len(seen) == n
    # Link and files together, or neither.
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": rev}, data={"url": "rosas.example"},
               files=[("files", ("m.png", b"png", "image/png"))], headers=W)
    assert r.status_code == 400
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": rev}, data={"url": " "}, headers=W)
    assert r.status_code == 400


def test_import_limits_per_day(app, monkeypatch):
    c, store, fake, _ = app
    sign_in(c, store)
    b = new_business(c)
    monkeypatch.setenv("IMPORTS_PER_BUSINESS_PER_DAY", "1")
    fake.script = [message(text_block(json.dumps({**EXTRACTED, "items": []})))]
    rev = b["draft"]["revision"]
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": rev},
               files=[("files", ("m.png", b"png", "image/png"))], headers=W)
    assert r.status_code == 200
    r = c.post(f"/api/manage/businesses/{b['id']}/import", params={"revision": r.json()["draft"]["revision"]},
               files=[("files", ("m.png", b"png", "image/png"))], headers=W)
    assert r.status_code == 429


def test_merge_items_keeps_ids_and_skips_duplicates():
    existing = [{"id": "croissant", "name": "Croissant", "category": "P", "price": 3.0}]
    new = [to_catalog_item(i) for i in EXTRACTED["items"]]
    appended = merge_items(existing, new, replace=False)
    assert [i["id"] for i in appended] == ["croissant", "cake"] and appended[0]["price"] == 3.0
    replaced = merge_items(existing, new, replace=True)
    assert [(i["id"], i["price"]) for i in replaced] == [("croissant", 3.5), ("cake", 30.0)]


# ---- test chat ----------------------------------------------------------------------------------


def test_test_chat_talks_to_the_draft_without_saving(app):
    c, store, fake, _ = app
    sign_in(c, store)
    b = new_business(c)
    fill_and_publish(c, b["id"])
    fake.script = [message(text_block("Hi! One croissant?")), message(text_block("Done."))]
    r = c.post(f"/api/manage/businesses/{b['id']}/test-chat", json={"text": "I'd like a croissant"}, headers=W)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["reply"] == "Hi! One croissant?" and first["greeting"].startswith("Thanks for calling Rosa's")
    r = c.post(f"/api/manage/businesses/{b['id']}/test-chat", json={"text": "yes", "session": first["session"]},
               headers=W)
    assert r.json()["session"] == first["session"]
    assert "Rosa's Bakery" in fake.requests[0]["system"][0]["text"]
    assert len(fake.requests[1]["messages"]) == 3  # same conversation continued
    assert store.recent() == [] and store.recent_calls() == []


# ---- going live -------------------------------------------------------------------------------


def test_go_live_buys_a_number_within_the_free_limit(app):
    c, store, fake, numbers = app
    sign_in(c, store)
    b = new_business(c)
    r = c.post(f"/api/manage/businesses/{b['id']}/go-live", json={"area_code": "913"}, headers=W)
    assert r.status_code == 400 and "publish" in r.json()["detail"]
    fill_and_publish(c, b["id"])
    r = c.post(f"/api/manage/businesses/{b['id']}/go-live", json={"area_code": "913"}, headers=W)
    assert r.json() == {"status": "live", "phone_number": "+19135550001"}
    assert numbers.bought == [("+19135550001", "Rosa's Bakery", "https://agent.example/voice")]
    twiml = c.post("/voice", data={"To": "+19135550001"}).text
    assert "Thanks for calling Rosa's Bakery" in twiml
    # Pause and resume.
    c.post(f"/api/manage/businesses/{b['id']}/pause", headers=W)
    assert "isn't taking orders" in c.post("/voice", data={"To": "+19135550001"}).text
    c.post(f"/api/manage/businesses/{b['id']}/resume", headers=W)
    assert "Rosa's Bakery" in c.post("/voice", data={"To": "+19135550001"}).text
    line = c.get(f"/api/manage/businesses/{b['id']}/line").json()
    assert line["phone_number"] == "+19135550001" and line["active"] and not line["pending"]


def test_go_live_past_the_free_limit_waits_for_the_operator(app):
    c, store, fake, numbers = app
    sign_in(c, store)
    first, second = new_business(c, "First"), new_business(c, "Second")
    for b in (first, second):
        fill_and_publish(c, b["id"])
    assert c.post(f"/api/manage/businesses/{first['id']}/go-live", json={}, headers=W).json()["status"] == "live"
    r = c.post(f"/api/manage/businesses/{second['id']}/go-live", json={"area_code": "816"}, headers=W)
    assert r.json() == {"status": "pending"} and len(numbers.bought) == 1
    assert c.get(f"/api/manage/businesses/{second['id']}/line").json()["pending"] is True
    assert c.get("/api/manage/admin/requests").status_code == 403  # not an operator

    sign_in(c, store, "boss@example.com")  # OPERATOR_EMAILS
    [req] = c.get("/api/manage/admin/requests").json()
    assert req["name"] == "Second" and req["area_code"] == "816"
    r = c.post(f"/api/manage/admin/requests/{req['id']}/approve", headers=W)
    assert r.json()["status"] == "live" and r.json()["phone_number"].startswith("+1816")
    assert c.get("/api/manage/admin/requests").json() == []


def test_daily_number_limit_across_everyone(app, monkeypatch):
    c, store, fake, numbers = app
    monkeypatch.setenv("NUMBERS_PER_DAY", "1")
    for email in ("a@example.com", "b@example.com"):
        sign_in(c, store, email)
        b = new_business(c, email)
        fill_and_publish(c, b["id"])
        c.post(f"/api/manage/businesses/{b['id']}/go-live", json={}, headers=W)
    assert len(numbers.bought) == 1 and len(store.go_live_requests("pending")) == 1


def test_go_live_rules(app):
    c, store, fake, numbers = app
    owner = sign_in(c, store)
    b = new_business(c)
    fill_and_publish(c, b["id"])
    assert c.post(f"/api/manage/businesses/{b['id']}/go-live", json={"area_code": "12"}, headers=W).status_code == 400
    numbers.fail = "No numbers are available in area code 913. Try a nearby one."
    r = c.post(f"/api/manage/businesses/{b['id']}/go-live", json={"area_code": "913"}, headers=W)
    assert r.status_code == 400 and "nearby" in r.json()["detail"]
    # A manager can't take the line live or change the team.
    uid = store.add_user("staff@example.com")
    store.grant(uid, b["id"], "manager")
    sign_in(c, store, "staff@example.com")
    assert c.post(f"/api/manage/businesses/{b['id']}/go-live", json={}, headers=W).status_code == 403
    assert c.post(f"/api/manage/businesses/{b['id']}/team", json={"email": "x@example.com"}, headers=W).status_code == 403
    assert owner


# ---- team, orders, calls ------------------------------------------------------------------------


def test_team_management(app):
    c, store, *_ = app
    sign_in(c, store)
    b = new_business(c)
    team = c.post(f"/api/manage/businesses/{b['id']}/team", json={"email": "Raj@Example.com"}, headers=W).json()
    assert [(m["email"], m["role"], m["joined"]) for m in team] == [
        ("ana@example.com", "owner", False), ("raj@example.com", "manager", False)]
    me_id = team[0]["user_id"]
    r = c.delete(f"/api/manage/businesses/{b['id']}/team/{me_id}", headers=W)
    assert r.status_code == 400 and "owner" in r.json()["detail"]
    raj = team[1]["user_id"]
    assert len(c.delete(f"/api/manage/businesses/{b['id']}/team/{raj}", headers=W).json()) == 1


def test_orders_and_calls_are_scoped_to_the_business(app):
    c, store, *_ = app
    other = seed_business(store)
    store.start_call("CAother", "+15550001111", other)
    sign_in(c, store)
    b = new_business(c)
    assert c.get(f"/api/manage/businesses/{b['id']}/orders").json() == []
    assert c.get(f"/api/manage/businesses/{b['id']}/calls").json() == []
    assert c.get(f"/api/manage/businesses/{b['id']}/calls/CAother").status_code == 404
    assert c.get(f"/api/manage/businesses/{other}/orders").status_code == 404
