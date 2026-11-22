"""Self-serve: everything a business does in the web app beyond editing its catalog.

  create a business   POST /api/manage/businesses
  read a menu         POST /api/manage/businesses/{id}/import        (photos or PDF -> draft items)
  try the agent       POST /api/manage/businesses/{id}/test-chat     (typed conversation, nothing saved)
  go live             POST /api/manage/businesses/{id}/go-live       (buys a number, or queues a request)
  pause / resume      POST /api/manage/businesses/{id}/pause|resume
  team                GET/POST/DELETE /api/manage/businesses/{id}/team
  orders and calls    GET /api/manage/businesses/{id}/orders|calls|calls/{call_id}
  POS                 GET/POST/DELETE /api/manage/businesses/{id}/pos
  operator            GET /api/manage/admin/requests, POST .../{id}/approve|reject

Limits keep strangers from running up costs (all per day unless noted, all env-tunable):
businesses per account, numbers bought automatically per account (in total) and across
everyone, menu reads and test-chat turns per business. A go-live past the automatic
limits waits for the operator to approve it.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, UploadFile

from .catalog import CatalogError, parse_catalog
from .catalog_check import check_catalog
from .catalog_import import ImportError_, merge_items, read_catalog
from .manage import Who, _business, _check_write, _editor_state, _json, _who
from .numbers import NumberError
from .templates import TEMPLATES, get_template

log = logging.getLogger("phone_agent.onboarding")
router = APIRouter()


def _limit(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def limits() -> dict:
    return {
        "businesses_per_account": _limit("MAX_BUSINESSES_PER_ACCOUNT", 3),
        "free_numbers_per_account": _limit("FREE_NUMBERS_PER_ACCOUNT", 1),
        "numbers_per_day": _limit("NUMBERS_PER_DAY", 5),
        "imports_per_day": _limit("IMPORTS_PER_BUSINESS_PER_DAY", 10),
        "test_turns_per_day": _limit("TEST_TURNS_PER_BUSINESS_PER_DAY", 60),
    }


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _owner(who: Who, business_id: int) -> None:
    if who.role(business_id) != "owner":
        raise HTTPException(status_code=403, detail="only the business owner can do that")


def _slug(store, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "business"
    slug, n = base, 2
    while store.business_by_slug(slug):
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---- create ---------------------------------------------------------------------------------


@router.post("/api/manage/businesses")
async def create_business(request: Request) -> dict:
    who = _who(request)
    _check_write(request)
    data = await _json(request)
    name = str(data.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(status_code=400, detail="enter your business name")
    template = TEMPLATES.get(str(data.get("template", "")))
    if template is None or template.status != "available":
        raise HTTPException(status_code=400, detail="choose a type of business that's available")
    store = request.app.state.store
    if who.user and not who.operator:
        cap = limits()["businesses_per_account"]
        if store.owned_business_count(who.user["id"]) >= cap:
            raise HTTPException(status_code=400, detail=f"an account can have up to {cap} businesses")
    business_id = store.create_business(_slug(store, name))
    if who.user:
        store.grant(who.user["id"], business_id, role="owner")
    catalog = {
        "template": template.id,
        "settings": {"name": name, "order_types": ["pickup"], "pickup_minutes": 20,
                     "payment": {"required_before_cooking": False, "pay_within_minutes": 30}, "notes": []},
        "items": [],
    }
    store.save_draft(business_id, catalog, None, None, who.label)
    log.info("%s created business %s (%s)", who.label, business_id, template.id)
    return {"id": business_id, **_editor_state(request, store.business(business_id))}


# ---- read a menu from photos or a PDF --------------------------------------------------------


@router.post("/api/manage/businesses/{business_id}/import")
async def import_catalog(request: Request, business_id: int, files: list[UploadFile], mode: str = "append",
                         revision: int | None = None) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    store = request.app.state.store
    draft = store.draft(row.id)
    catalog = draft["catalog"] if draft else store.catalog_payload(row.catalog_id)
    if draft and draft["revision"] != revision:
        raise HTTPException(status_code=409, detail="someone else changed this catalog; reload first")
    if not store.take_usage(f"business:{row.id}", "import", _today(), limits()["imports_per_day"]):
        raise HTTPException(status_code=429, detail="you've read the most files allowed today; try again tomorrow")
    uploads = []
    for f in files:
        data = await f.read()
        uploads.append(((f.content_type or "").split(";")[0].strip().lower(), data))
    try:
        result = await read_catalog(request.app.state.client, uploads, get_template(catalog.get("template")),
                                    catalog["settings"])
    except ImportError_ as e:
        raise HTTPException(status_code=400, detail=str(e))
    catalog = {**catalog, "items": merge_items(catalog.get("items", []), result["items"], replace=mode == "replace")}
    settings = dict(catalog["settings"])
    for key in ("address", "hours"):  # fill blanks only; never overwrite what the owner typed
        if result["business"].get(key) and not settings.get(key):
            settings[key] = result["business"][key].strip()[:400]
    notes = list(settings.get("notes") or [])
    settings["notes"] = (notes + [n.strip()[:400] for n in result["notes"] if n.strip() and n not in notes])[:30]
    catalog["settings"] = settings
    base = draft["base_catalog_id"] if draft else row.catalog_id
    new_revision = store.save_draft(row.id, catalog, base, draft["revision"] if draft else None, who.label)
    if new_revision is None:
        raise HTTPException(status_code=409, detail="someone else changed this catalog; reload first")
    log.info("business %s: read %d items from %d files", row.slug, len(result["items"]), len(uploads))
    return {"added": len(result["items"]), "unreadable": result["unreadable"],
            **_editor_state(request, store.business(row.id))}


# ---- try the agent in the browser -------------------------------------------------------------

TEST_SESSION_SECONDS = 1800
MAX_TEST_SESSIONS = 300


@router.post("/api/manage/businesses/{business_id}/test-chat")
async def test_chat(request: Request, business_id: int) -> dict:
    """A typed conversation with the agent, using the draft (what callers will hear after
    publishing). Orders aren't saved and no texts are sent."""
    from .agent import PhoneAgent

    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    data = await _json(request)
    text = str(data.get("text", "")).strip()[:500]
    if not text:
        raise HTTPException(status_code=400, detail="say something")
    sessions = _sessions(request.app)
    now = time.time()
    for sid in [k for k, v in sessions.items() if now - v["last"] > TEST_SESSION_SECONDS]:
        sessions.pop(sid, None)
    sid = str(data.get("session") or "")
    session = sessions.get(sid)
    if session is None or session["business_id"] != row.id:
        store = request.app.state.store
        draft = store.draft(row.id)
        raw = draft["catalog"] if draft else store.catalog_payload(row.catalog_id)
        try:
            catalog = parse_catalog(raw or {}, business_id=row.id, slug=row.slug)
        except CatalogError as e:
            raise HTTPException(status_code=400, detail=f"fix the catalog first: {e}")
        if len(sessions) >= MAX_TEST_SESSIONS:
            raise HTTPException(status_code=503, detail="too many test chats right now; try again shortly")
        agent = PhoneAgent(catalog, client=request.app.state.client, caller_phone="+15550000000")
        sid = secrets.token_urlsafe(12)
        session = sessions[sid] = {"business_id": row.id, "agent": agent, "last": now, "events": []}
        session["greeting"] = agent.greeting
        agent.on_tool = lambda name, args, result, is_error: session["events"].append(
            {"tool": name, "args": args, "error": is_error, "result": result[:300]})
    if not request.app.state.store.take_usage(f"business:{row.id}", "test_turn", _today(),
                                               limits()["test_turns_per_day"]):
        raise HTTPException(status_code=429, detail="that's enough test messages for today; try again tomorrow")
    session["last"] = now
    session["events"] = []
    reply = "".join([chunk async for chunk in session["agent"].respond(text)])
    return {"session": sid, "greeting": session.get("greeting"), "reply": reply.strip(),
            "events": session["events"], "ended": session["agent"].end_requested}


def _sessions(app_) -> dict:
    if not hasattr(app_.state, "test_chats"):
        app_.state.test_chats = {}
    return app_.state.test_chats


# ---- go live ---------------------------------------------------------------------------------


def _voice_url(request: Request) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        base = f"{proto}://{request.headers.get('host', request.url.netloc)}"
    return base + "/voice"


def _buy(request: Request, row, area_code: str | None, user_id: int | None) -> dict:
    numbers = getattr(request.app.state, "numbers", None)
    if numbers is None:
        raise HTTPException(status_code=503, detail="phone numbers can't be bought on this server yet")
    store = request.app.state.store
    name = _business_name(request, row)
    try:
        bought = numbers.buy(area_code, name, _voice_url(request))
    except NumberError as e:
        store.add_go_live_request(row.id, user_id, area_code, "failed", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    store.update_business(row.id, phone_number=bought.phone_number, active=True)
    store.add_go_live_request(row.id, user_id, area_code, "done", phone_number=bought.phone_number)
    log.info("business %s is live on %s", row.slug, bought.phone_number)
    return {"status": "live", "phone_number": bought.phone_number}


def _business_name(request: Request, row) -> str:
    catalog = request.app.state.businesses.get(row.id)
    return (catalog.settings.get("name") if catalog else None) or row.slug


@router.post("/api/manage/businesses/{business_id}/go-live")
async def go_live(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    data = await _json(request)
    store = request.app.state.store
    if row.catalog_id is None:
        raise HTTPException(status_code=400, detail="publish your catalog first")
    live = check_catalog(store.catalog_payload(row.catalog_id))
    if live["errors"]:
        raise HTTPException(status_code=400, detail="the published catalog has errors; fix them and publish again")
    if row.phone_number:
        store.update_business(row.id, active=True)
        return {"status": "live", "phone_number": row.phone_number}
    area_code = str(data.get("area_code") or "").strip() or None
    if area_code and not re.fullmatch(r"[2-9]\d\d", area_code):
        raise HTTPException(status_code=400, detail="area code should be 3 digits, like 913")
    if store.go_live_requests("pending", row.id):
        return {"status": "pending"}
    caps = limits()
    within = who.operator or (
        store.live_business_count(who.user["id"]) < caps["free_numbers_per_account"]
        and store.take_usage("global", "number", _today(), caps["numbers_per_day"]))
    if not within:
        store.add_go_live_request(row.id, who.user["id"] if who.user else None, area_code, "pending")
        log.info("business %s: go-live request queued for approval", row.slug)
        return {"status": "pending"}
    return _buy(request, row, area_code, who.user["id"] if who.user else None)


@router.post("/api/manage/businesses/{business_id}/pause")
async def pause(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    request.app.state.store.update_business(row.id, active=False)
    return {"active": False}


@router.post("/api/manage/businesses/{business_id}/resume")
async def resume(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    if not row.phone_number or row.catalog_id is None:
        raise HTTPException(status_code=400, detail="go live first")
    request.app.state.store.update_business(row.id, active=True)
    return {"active": True}


@router.get("/api/manage/businesses/{business_id}/line")
async def line(request: Request, business_id: int) -> dict:
    """The phone line's state for the home page."""
    who = _who(request)
    row = _business(request, who, business_id)
    store = request.app.state.store
    requests_ = store.go_live_requests(business_id=row.id)
    return {"phone_number": row.phone_number, "active": row.active, "published": row.catalog_id is not None,
            "pending": any(r["status"] == "pending" for r in requests_),
            "last_error": next((r["error"] for r in requests_ if r["status"] == "failed"), None),
            "role": who.role(row.id), "limits": limits()}


# ---- team -----------------------------------------------------------------------------------


@router.get("/api/manage/businesses/{business_id}/team")
async def team(request: Request, business_id: int) -> list[dict]:
    who = _who(request)
    row = _business(request, who, business_id)
    return request.app.state.store.team(row.id)


@router.post("/api/manage/businesses/{business_id}/team")
async def add_teammate(request: Request, business_id: int) -> list[dict]:
    """Add someone by email. They sign in with Google using that email and get access."""
    from .accounts import normalize_email

    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    data = await _json(request)
    try:
        email = normalize_email(str(data.get("email", "")))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    role = "owner" if data.get("role") == "owner" else "manager"
    store = request.app.state.store
    if len(store.team(row.id)) >= 25:
        raise HTTPException(status_code=400, detail="a team can have up to 25 people")
    user = store.user_by_email(email)
    user_id = user["id"] if user else store.add_user(email)
    store.grant(user_id, row.id, role)
    return store.team(row.id)


@router.delete("/api/manage/businesses/{business_id}/team/{user_id}")
async def remove_teammate(request: Request, business_id: int, user_id: int) -> list[dict]:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    store = request.app.state.store
    members = store.team(row.id)
    owners = [m for m in members if m["role"] == "owner"]
    if any(m["user_id"] == user_id and m["role"] == "owner" for m in members) and len(owners) <= 1:
        raise HTTPException(status_code=400, detail="a business needs at least one owner")
    store.revoke(user_id, row.id)
    return store.team(row.id)


# ---- orders and calls -------------------------------------------------------------------------


@router.get("/api/manage/businesses/{business_id}/orders")
async def orders(request: Request, business_id: int, limit: int = 100) -> list[dict]:
    who = _who(request)
    row = _business(request, who, business_id)
    return request.app.state.store.recent(max(1, min(limit, 500)), business_id=row.id)


@router.get("/api/manage/businesses/{business_id}/calls")
async def calls(request: Request, business_id: int, limit: int = 100) -> list[dict]:
    who = _who(request)
    row = _business(request, who, business_id)
    return request.app.state.store.recent_calls(max(1, min(limit, 500)), business_id=row.id)


@router.get("/api/manage/businesses/{business_id}/calls/{call_id}")
async def call_transcript(request: Request, business_id: int, call_id: str) -> dict:
    who = _who(request)
    row = _business(request, who, business_id)
    store = request.app.state.store
    if store.call_business(call_id) != row.id:
        raise HTTPException(status_code=404, detail="no such call")
    return {"call_id": call_id, "events": store.transcript(call_id)}


# ---- POS ------------------------------------------------------------------------------------


@router.get("/api/manage/businesses/{business_id}/pos")
async def pos_status(request: Request, business_id: int) -> dict:
    who = _who(request)
    row = _business(request, who, business_id)
    hub = getattr(request.app.state, "pos", None)
    conn = request.app.state.store.pos_connection(row.id)
    return {"available": sorted(hub.providers) if hub else [],
            "connected": None if not conn else {"provider": conn["provider"], "environment": conn["environment"],
                                                "location_id": conn["location_id"]}}


@router.post("/api/manage/businesses/{business_id}/pos")
async def pos_connect(request: Request, business_id: int) -> dict:
    from .pos import PosError, connect_url

    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    data = await _json(request)
    hub = getattr(request.app.state, "pos", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="POS connections aren't set up on this server")
    base = os.environ.get("PUBLIC_BASE_URL", "") or str(request.base_url).rstrip("/")
    try:
        return {"url": connect_url(str(data.get("provider", "")), row.id, base, hub.providers)}
    except PosError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/manage/businesses/{business_id}/pos")
async def pos_disconnect(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    request.app.state.store.delete_pos_connection(row.id)
    return {"connected": None}


# ---- operator: approve go-live requests ---------------------------------------------------


def _operator(request: Request) -> Who:
    who = _who(request)
    if not who.operator:
        raise HTTPException(status_code=403, detail="operators only")
    return who


@router.get("/api/manage/admin/requests")
async def admin_requests(request: Request, status: str = "pending") -> list[dict]:
    _operator(request)
    out = request.app.state.store.go_live_requests(status or None)
    for r in out:
        r["name"] = _business_name(request, request.app.state.store.business(r["business_id"]))
    return out


@router.post("/api/manage/admin/requests/{request_id}/approve")
async def approve(request: Request, request_id: int) -> dict:
    who = _operator(request)
    _check_write(request)
    store = request.app.state.store
    req = next((r for r in store.go_live_requests("pending") if r["id"] == request_id), None)
    if req is None:
        raise HTTPException(status_code=404, detail="no such pending request")
    row = store.business(req["business_id"])
    store.update_go_live_request(request_id, "approved")
    log.info("%s approved go-live for %s", who.label, row.slug)
    return _buy(request, row, req["area_code"], req["user_id"])


@router.post("/api/manage/admin/requests/{request_id}/reject")
async def reject(request: Request, request_id: int) -> dict:
    _operator(request)
    _check_write(request)
    request.app.state.store.update_go_live_request(request_id, "rejected")
    return {"status": "rejected"}
