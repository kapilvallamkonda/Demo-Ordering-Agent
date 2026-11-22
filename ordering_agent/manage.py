"""The /manage web interface: restaurant staff edit their menu and settings.

Edits go to a draft (one per restaurant, saved automatically by the page) and reach
callers only when someone publishes; publishing adds a menu version, which the next
call uses. Every save and publish runs the menu checks (menu_check.py): errors block
publishing, blockers and warnings are shown. Old versions can be restored into the
draft.

Who can do what:
  - Staff sign in with email and password (accounts.py) and see only their restaurants.
  - The operator can use the ORDERS_API_TOKEN as a Bearer token and sees every restaurant.

CSRF: the session cookie is SameSite=Lax, and every write needs the X-Manage header
(which a cross-site form can't send) plus, if the browser sends Origin, a matching one.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .accounts import Accounts, LoginLimiter, check_password_rules
from .menu_check import check_menu
from .settings_schema import settings_for
from .templates import TEMPLATES, get_template

log = logging.getLogger("ordering_agent.manage")

router = APIRouter()
COOKIE = "oa_session"
MAX_BODY = 2_000_000
MANAGE_HTML = Path(__file__).parent / "static" / "manage.html"
limiter = LoginLimiter()

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


@router.get("/manage", response_class=HTMLResponse)
async def manage_page() -> HTMLResponse:
    return HTMLResponse(MANAGE_HTML.read_text(), headers=PAGE_HEADERS)


# ---- who is asking -----------------------------------------------------------------------


class Who:
    def __init__(self, label: str, restaurant_ids: list[int] | None, user: dict | None = None):
        self.label = label  # recorded on drafts and published versions
        self.restaurant_ids = restaurant_ids  # None: every restaurant (operator)
        self.user = user

    def can(self, restaurant_id: int) -> bool:
        return self.restaurant_ids is None or restaurant_id in self.restaurant_ids


def _accounts(request: Request) -> Accounts:
    return Accounts(request.app.state.store)


def _who(request: Request) -> Who:
    token = os.environ.get("ORDERS_API_TOKEN", "")
    auth = request.headers.get("authorization", "")
    if token and auth and hmac.compare_digest(auth, f"Bearer {token}"):
        return Who("operator", None)
    user = _accounts(request).session_user(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return Who(user["email"], request.app.state.store.user_restaurant_ids(user["id"]), user)


def _check_write(request: Request) -> None:
    if request.headers.get("x-manage") != "1":
        raise HTTPException(status_code=403, detail="missing X-Manage header")
    origin = request.headers.get("origin")
    if origin:
        allowed = {_base(request)}
        public = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if public:
            allowed.add(public)
        if origin.rstrip("/") not in allowed:
            raise HTTPException(status_code=403, detail="bad origin")


def _base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def _secure(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            or os.environ.get("PUBLIC_BASE_URL", "").startswith("https://"))


async def _json(request: Request) -> dict:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=413, detail="too large")
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="not JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="expected an object")
    return data


def _restaurant(request: Request, who: Who, restaurant_id: int):
    row = request.app.state.store.restaurant(restaurant_id)
    if row is None or not who.can(restaurant_id):
        raise HTTPException(status_code=404, detail="no such restaurant")
    return row


# ---- signing in ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "?")


def _signed_in(request: Request, user: dict) -> JSONResponse:
    token = _accounts(request).start_session(user["id"])
    resp = JSONResponse({"ok": True, "email": user["email"]})
    resp.set_cookie(COOKIE, token, max_age=14 * 86400, httponly=True, secure=_secure(request),
                    samesite="lax", path="/")
    return resp


@router.post("/api/manage/login")
async def login(request: Request) -> JSONResponse:
    _check_write(request)
    data = await _json(request)
    email = str(data.get("email", ""))[:200].strip().lower()
    keys = (f"email:{email}", f"ip:{_client_ip(request)}")
    if limiter.blocked(*keys):
        raise HTTPException(status_code=429, detail="too many attempts; try again in 15 minutes")
    user = _accounts(request).authenticate(email, str(data.get("password", "")))
    if user is None:
        limiter.failed(*keys)
        log.warning("failed sign-in for %s", email or "(blank)")
        raise HTTPException(status_code=401, detail="wrong email or password")
    limiter.reset(f"email:{email}")
    log.info("%s signed in", user["email"])
    return _signed_in(request, user)


@router.post("/api/manage/logout")
async def logout(request: Request) -> JSONResponse:
    _check_write(request)
    _accounts(request).end_session(request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.post("/api/manage/accept-invite")
async def accept_invite(request: Request) -> JSONResponse:
    _check_write(request)
    data = await _json(request)
    if limiter.blocked(f"ip:{_client_ip(request)}"):
        raise HTTPException(status_code=429, detail="too many attempts; try again in 15 minutes")
    try:
        check_password_rules(data.get("password"))
        user = _accounts(request).accept_invite(str(data.get("token", "")), data["password"], data.get("name"))
    except ValueError as e:
        if "expired" in str(e):
            limiter.failed(f"ip:{_client_ip(request)}")
        raise HTTPException(status_code=400, detail=str(e))
    log.info("%s set their password", user["email"])
    return _signed_in(request, user)


@router.get("/api/manage/me")
async def me(request: Request) -> dict:
    who = _who(request)
    directory = request.app.state.restaurants
    restaurants = [{"id": r["id"], "slug": r["slug"], "name": r["name"] or r["slug"], "active": r["active"],
                    "phone_number": r["phone_number"]}
                   for r in directory.listing() if who.can(r["id"])]
    return {"who": who.label, "name": who.user["name"] if who.user else "Operator",
            "operator": who.restaurant_ids is None, "restaurants": restaurants}


# ---- the editor -----------------------------------------------------------------------------


def _editor_state(request: Request, row) -> dict:
    store = request.app.state.store
    live = store.menu_payload(row.menu_id) if row.menu_id else None
    draft = store.draft(row.id)
    menu = draft["menu"] if draft else live
    pos = store.pos_connection(row.id)
    try:
        template = get_template((menu or {}).get("template"))
    except KeyError:
        template = get_template(None)
    return {
        "restaurant": {"id": row.id, "slug": row.slug, "phone_number": row.phone_number, "active": row.active},
        "schema": settings_for(template),
        "template": template.public(),
        "menu": menu,
        "live_menu_id": row.menu_id,
        "draft": None if not draft else {
            "revision": draft["revision"], "updated_at": draft["updated_at"], "updated_by": draft["updated_by"],
            "base_menu_id": draft["base_menu_id"], "live_changed": draft["base_menu_id"] != row.menu_id,
        },
        "check": check_menu(menu) if menu else None,
        "pos_provider": pos["provider"] if pos else None,
    }


@router.get("/api/manage/templates")
async def templates(request: Request) -> list[dict]:
    """Every kind of business, including ones coming soon (for onboarding screens)."""
    _who(request)
    return [t.public() for t in TEMPLATES.values()]


@router.get("/api/manage/restaurants/{restaurant_id}/editor")
async def editor(request: Request, restaurant_id: int) -> dict:
    who = _who(request)
    return _editor_state(request, _restaurant(request, who, restaurant_id))


@router.put("/api/manage/restaurants/{restaurant_id}/draft")
async def save_draft(request: Request, restaurant_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _restaurant(request, who, restaurant_id)
    data = await _json(request)
    menu = data.get("menu")
    if not isinstance(menu, dict) or not isinstance(menu.get("items"), list) \
            or not isinstance(menu.get("restaurant"), dict):
        raise HTTPException(status_code=400, detail="menu must have restaurant settings and items")
    store = request.app.state.store
    existing = store.draft(row.id)
    base = existing["base_menu_id"] if existing else row.menu_id
    revision = store.save_draft(row.id, menu, base, data.get("revision"), who.label)
    if revision is None:
        raise HTTPException(status_code=409, detail="someone else saved changes to this menu; reload to see them")
    return {"revision": revision, "check": check_menu(menu)}


@router.delete("/api/manage/restaurants/{restaurant_id}/draft")
async def discard_draft(request: Request, restaurant_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _restaurant(request, who, restaurant_id)
    request.app.state.store.delete_draft(row.id)
    log.info("%s discarded the draft menu of %s", who.label, row.slug)
    return _editor_state(request, row)


@router.post("/api/manage/restaurants/{restaurant_id}/publish")
async def publish(request: Request, restaurant_id: int) -> JSONResponse:
    who = _who(request)
    _check_write(request)
    row = _restaurant(request, who, restaurant_id)
    data = await _json(request)
    store = request.app.state.store
    draft = store.draft(row.id)
    if draft is None:
        raise HTTPException(status_code=400, detail="there are no changes to publish")
    if draft["revision"] != data.get("revision"):
        raise HTTPException(status_code=409, detail="the draft changed since you last saved; reload first")
    if draft["base_menu_id"] != row.menu_id and not data.get("overwrite"):
        return JSONResponse(status_code=409, content={
            "detail": "a newer menu was published since this draft was started", "code": "live_changed"})
    result = check_menu(draft["menu"])
    if result["errors"]:
        return JSONResponse(status_code=422, content={"detail": "fix the errors first", "check": result})
    menu_id = store.publish_draft(row.id, draft["menu"], who.label)
    log.info("%s published menu version %s for %s", who.label, menu_id, row.slug)
    return JSONResponse({"menu_id": menu_id, **_editor_state(request, store.restaurant(row.id))})


@router.get("/api/manage/restaurants/{restaurant_id}/versions")
async def versions(request: Request, restaurant_id: int) -> list[dict]:
    who = _who(request)
    row = _restaurant(request, who, restaurant_id)
    out = request.app.state.store.menu_versions(row.id)
    for v in out:
        v["live"] = v["id"] == row.menu_id
    return out


@router.post("/api/manage/restaurants/{restaurant_id}/versions/{menu_id}/restore")
async def restore(request: Request, restaurant_id: int, menu_id: int) -> dict:
    """Put an old version into the draft (not live until published)."""
    who = _who(request)
    _check_write(request)
    row = _restaurant(request, who, restaurant_id)
    data = await _json(request)
    store = request.app.state.store
    old = store.menu_version_payload(row.id, menu_id)
    if old is None:
        raise HTTPException(status_code=404, detail="no such version")
    existing = store.draft(row.id)
    if existing and data.get("revision") != existing["revision"]:
        raise HTTPException(status_code=409, detail="the draft changed; reload first")
    if existing:
        store.delete_draft(row.id)
    store.save_draft(row.id, old, row.menu_id, None, who.label)
    log.info("%s restored menu version %s of %s into the draft", who.label, menu_id, row.slug)
    return _editor_state(request, row)
