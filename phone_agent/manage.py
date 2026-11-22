"""The /manage web interface: business staff edit their catalog and settings.

Edits go to a draft (one per business, saved automatically by the page) and reach
callers only when someone publishes; publishing adds a catalog version, which the next
call uses. Every save and publish runs the catalog checks (catalog_check.py): errors block
publishing, blockers and warnings are shown. Old versions can be restored into the
draft.

Who can do what:
  - Staff sign in with email and password (accounts.py) and see only their businesses.
  - The operator can use the ADMIN_API_TOKEN as a Bearer token and sees every business.

CSRF: the session cookie is SameSite=Lax, and every write needs the X-Manage header
(which a cross-site form can't send) plus, if the browser sends Origin, a matching one.
"""

from __future__ import annotations

import hmac
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .accounts import Accounts, LoginLimiter, check_password_rules
from .catalog_check import check_catalog
from . import brand
from .settings_schema import settings_for
from .templates import TEMPLATES, get_template

log = logging.getLogger("phone_agent.manage")

router = APIRouter()
COOKIE = "oa_session"
MAX_BODY = 2_000_000
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
    return HTMLResponse(brand.page("manage.html"), headers=PAGE_HEADERS)


# ---- who is asking -----------------------------------------------------------------------


class Who:
    def __init__(self, label: str, business_ids: list[int] | None, user: dict | None = None, store=None):
        self.label = label  # recorded on drafts and published versions
        self.business_ids = business_ids  # None: every business (operator)
        self.user = user
        self.store = store

    @property
    def operator(self) -> bool:
        return self.business_ids is None

    def can(self, business_id: int) -> bool:
        return self.business_ids is None or business_id in self.business_ids

    def role(self, business_id: int) -> str | None:
        """"owner" (phone line, team, POS, everything) or "manager" (catalog, settings,
        orders and calls). The operator counts as an owner everywhere."""
        if self.operator:
            return "owner"
        return self.store.user_role(self.user["id"], business_id) if self.user else None


def operator_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("OPERATOR_EMAILS", "").split(",") if e.strip()}


def _accounts(request: Request) -> Accounts:
    return Accounts(request.app.state.store)


def _who(request: Request) -> Who:
    token = os.environ.get("ADMIN_API_TOKEN", "")
    auth = request.headers.get("authorization", "")
    if token and auth and hmac.compare_digest(auth, f"Bearer {token}"):
        return Who("operator", None, None, request.app.state.store)
    user = _accounts(request).session_user(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    store = request.app.state.store
    if user["email"] in operator_emails():
        return Who(user["email"], None, user, store)
    return Who(user["email"], store.user_business_ids(user["id"]), user, store)


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


def secure_cookie(request: Request) -> bool:
    return _secure(request)


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


def _business(request: Request, who: Who, business_id: int):
    row = request.app.state.store.business(business_id)
    if row is None or not who.can(business_id):
        raise HTTPException(status_code=404, detail="no such business")
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


@router.get("/api/manage/config")
async def config() -> dict:
    """What the sign-in page needs (no sign-in required)."""
    from .google_auth import configured

    return {"google": configured(), "password_login": not configured() or bool(os.environ.get("ALLOW_PASSWORD_LOGIN"))}


@router.get("/api/manage/me")
async def me(request: Request) -> dict:
    who = _who(request)
    store, directory = request.app.state.store, request.app.state.businesses
    businesses = []
    for r in directory.listing():
        if not who.can(r["id"]):
            continue
        name = r["name"]
        if not name:  # a new business that hasn't published yet: its draft has the name
            draft = store.draft(r["id"])
            name = ((draft or {}).get("catalog") or {}).get("settings", {}).get("name")
        businesses.append({"id": r["id"], "slug": r["slug"], "name": name or r["slug"], "active": r["active"],
                           "phone_number": r["phone_number"], "role": who.role(r["id"]),
                           "template": r["template"], "status": r["status"]})
    return {"who": who.label, "name": (who.user or {}).get("name") or ("Operator" if who.operator else ""),
            "email": (who.user or {}).get("email"), "operator": who.operator, "businesses": businesses}


# ---- the editor -----------------------------------------------------------------------------


def _editor_state(request: Request, row) -> dict:
    store = request.app.state.store
    live = store.catalog_payload(row.catalog_id) if row.catalog_id else None
    draft = store.draft(row.id)
    catalog = draft["catalog"] if draft else live
    pos = store.pos_connection(row.id)
    try:
        template = get_template((catalog or {}).get("template"))
    except KeyError:
        template = get_template(None)
    return {
        "business": {"id": row.id, "slug": row.slug, "phone_number": row.phone_number, "active": row.active},
        "schema": settings_for(template),
        "template": template.public(),
        "catalog": catalog,
        "live_catalog_id": row.catalog_id,
        "draft": None if not draft else {
            "revision": draft["revision"], "updated_at": draft["updated_at"], "updated_by": draft["updated_by"],
            "base_catalog_id": draft["base_catalog_id"], "live_changed": draft["base_catalog_id"] != row.catalog_id,
        },
        "check": check_catalog(catalog) if catalog else None,
        "pos_provider": pos["provider"] if pos else None,
    }


@router.get("/api/manage/templates")
async def templates(request: Request) -> list[dict]:
    """Every kind of business, including ones coming soon (for onboarding screens)."""
    _who(request)
    return [t.public() for t in TEMPLATES.values()]


@router.get("/api/manage/businesses/{business_id}/editor")
async def editor(request: Request, business_id: int) -> dict:
    who = _who(request)
    return _editor_state(request, _business(request, who, business_id))


@router.put("/api/manage/businesses/{business_id}/draft")
async def save_draft(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    data = await _json(request)
    catalog = data.get("catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list) \
            or not isinstance(catalog.get("settings"), dict):
        raise HTTPException(status_code=400, detail="catalog must have business settings and items")
    store = request.app.state.store
    existing = store.draft(row.id)
    base = existing["base_catalog_id"] if existing else row.catalog_id
    revision = store.save_draft(row.id, catalog, base, data.get("revision"), who.label)
    if revision is None:
        raise HTTPException(status_code=409, detail="someone else saved changes to this catalog; reload to see them")
    return {"revision": revision, "check": check_catalog(catalog)}


@router.delete("/api/manage/businesses/{business_id}/draft")
async def discard_draft(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    request.app.state.store.delete_draft(row.id)
    log.info("%s discarded the draft catalog of %s", who.label, row.slug)
    return _editor_state(request, row)


@router.post("/api/manage/businesses/{business_id}/publish")
async def publish(request: Request, business_id: int) -> JSONResponse:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    data = await _json(request)
    store = request.app.state.store
    draft = store.draft(row.id)
    if draft is None:
        raise HTTPException(status_code=400, detail="there are no changes to publish")
    if draft["revision"] != data.get("revision"):
        raise HTTPException(status_code=409, detail="the draft changed since you last saved; reload first")
    if draft["base_catalog_id"] != row.catalog_id and not data.get("overwrite"):
        return JSONResponse(status_code=409, content={
            "detail": "a newer catalog was published since this draft was started", "code": "live_changed"})
    result = check_catalog(draft["catalog"])
    if result["errors"]:
        return JSONResponse(status_code=422, content={"detail": "fix the errors first", "check": result})
    catalog_id = store.publish_draft(row.id, draft["catalog"], who.label)
    log.info("%s published catalog version %s for %s", who.label, catalog_id, row.slug)
    return JSONResponse({"catalog_id": catalog_id, **_editor_state(request, store.business(row.id))})


@router.get("/api/manage/businesses/{business_id}/versions")
async def versions(request: Request, business_id: int) -> list[dict]:
    who = _who(request)
    row = _business(request, who, business_id)
    out = request.app.state.store.catalog_versions(row.id)
    for v in out:
        v["live"] = v["id"] == row.catalog_id
    return out


@router.post("/api/manage/businesses/{business_id}/versions/{catalog_id}/restore")
async def restore(request: Request, business_id: int, catalog_id: int) -> dict:
    """Put an old version into the draft (not live until published)."""
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    data = await _json(request)
    store = request.app.state.store
    old = store.catalog_version_payload(row.id, catalog_id)
    if old is None:
        raise HTTPException(status_code=404, detail="no such version")
    existing = store.draft(row.id)
    if existing and data.get("revision") != existing["revision"]:
        raise HTTPException(status_code=409, detail="the draft changed; reload first")
    if existing:
        store.delete_draft(row.id)
    store.save_draft(row.id, old, row.catalog_id, None, who.label)
    log.info("%s restored catalog version %s of %s into the draft", who.label, catalog_id, row.slug)
    return _editor_state(request, row)
