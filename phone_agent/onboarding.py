"""Self-serve: everything a business does in the web app beyond editing its catalog.

  create a business   POST /api/manage/businesses
  read a menu         POST /api/manage/businesses/{id}/import        (photos, PDF or a link -> draft items;
                      GET .../import/{job} for progress: a background job)
  try the agent       POST /api/manage/businesses/{id}/test-chat     (typed conversation, nothing saved)
  go live             POST /api/manage/businesses/{id}/go-live       (buys a number, or queues a request)
  pause / resume      POST /api/manage/businesses/{id}/pause|resume
  start over          POST /api/manage/businesses/{id}/start-over    (before the first publish)
  team                GET/POST/DELETE /api/manage/businesses/{id}/team
  orders and calls    GET /api/manage/businesses/{id}/orders|calls|calls/{call_id}
  requests            GET /api/manage/businesses/{id}/inquiries, POST .../inquiries/{number}
  POS                 GET/POST/DELETE /api/manage/businesses/{id}/pos
  operator            GET /api/manage/admin/requests, POST .../{id}/approve|reject
  analytics           GET /api/manage/admin/analytics (operator), GET .../{id}/insights (a business)

Limits keep strangers from running up costs (all per day unless noted, all env-tunable):
businesses per account, numbers bought automatically per account (in total) and across
everyone, menu reads and test-chat turns per business. A go-live past the automatic
limits waits for the operator to approve it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from .catalog import CatalogError, parse_catalog
from .catalog_check import check_catalog
from . import analytics, menu_link
from .catalog_import import ImportError_, Progress, merge_items, read_catalog
from .manage import Who, _business, _capture, _check_write, _editor_state, _json, _who
from .numbers import NumberError
from .schedule import default_timezone, valid_timezone
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
    store.track("business_created", business_id, user_id=(who.user or {}).get("id"), template=template.id)
    _capture("business_created", {"template": template.id})
    store.save_draft(business_id, _fresh_catalog(template.id, name, str(data.get("timezone") or "")),
                     None, None, who.label)
    log.info("%s created business %s (%s)", who.label, business_id, template.id)
    return {"id": business_id, **_editor_state(request, store.business(business_id))}


def _fresh_catalog(template_id: str, name: str, tz: str | None = None) -> dict:
    """A new business's starting draft: just its name and default settings, no items."""
    return {
        "template": template_id,
        "settings": {"name": name, "timezone": tz if tz and valid_timezone(tz) else default_timezone(),
                     "order_types": ["pickup"], "pickup_minutes": 20,
                     "payment": {"required_before_cooking": False, "pay_within_minutes": 30},
                     "about": "", "faq": []},
        "items": [],
    }


@router.post("/api/manage/businesses/{business_id}/start-over")
async def start_over(request: Request, business_id: int) -> dict:
    """Before the first publish only: wipe the draft back to how it was when the business
    was created (keeping its name and type). After publishing, "Discard" does the job."""
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    if row.catalog_id is not None:
        raise HTTPException(status_code=400, detail="this business has been published; discard unpublished changes instead")
    if any(j["business_id"] == row.id and j["state"] == "running" for j in _import_jobs(request.app).values()):
        raise HTTPException(status_code=409, detail="wait for the menu reading to finish first")
    data = await _json(request)
    store = request.app.state.store
    draft = store.draft(row.id)
    if draft is None:
        raise HTTPException(status_code=404, detail="nothing to start over")
    catalog = draft["catalog"]
    old = catalog.get("settings", {})
    fresh = _fresh_catalog(catalog.get("template"), str(old.get("name") or row.slug), old.get("timezone"))
    if store.save_draft(row.id, fresh, None, data.get("revision"), who.label) is None:
        raise HTTPException(status_code=409, detail="someone else changed this business; reload first")
    log.info("%s started %s over", who.label, row.slug)
    store.track("start_over", row.id, items=len(catalog.get("items", [])))
    return _editor_state(request, store.business(row.id))


# ---- read a menu from photos or a PDF --------------------------------------------------------


IMPORT_JOB_SECONDS = 900  # finished jobs are kept this long for the page to collect


@router.post("/api/manage/businesses/{business_id}/import")
async def import_catalog(request: Request, business_id: int, files: list[UploadFile] = File(default=[]),
                         url: str = Form(default=""), mode: str = "append", revision: int | None = None) -> dict:
    """Start reading photos or PDFs (`files`), or a link to the menu (`url`), never both.

    Reading a big menu takes minutes, so it runs as a background job: this returns
    {"job": id} and the page polls GET .../import/{id} for progress and the result. One job
    per business at a time; jobs live in memory (lost on restart, like test chats)."""
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    store = request.app.state.store
    draft = store.draft(row.id)
    catalog = draft["catalog"] if draft else store.catalog_payload(row.catalog_id)
    if draft and draft["revision"] != revision:
        raise HTTPException(status_code=409, detail="someone else changed this catalog; reload first")
    if url.strip() and files:
        raise HTTPException(status_code=400, detail="upload files or paste a link, not both")
    if not url.strip() and not files:
        raise HTTPException(status_code=400, detail="add at least one photo or PDF, or paste a link")
    jobs = _import_jobs(request.app)
    if any(j["business_id"] == row.id and j["state"] == "running" for j in jobs.values()):
        raise HTTPException(status_code=409, detail="already reading a menu for this business; wait for it to finish")
    if not store.take_usage(f"business:{row.id}", "import", _today(), limits()["imports_per_day"]):
        raise HTTPException(status_code=429, detail="you've read the most menus allowed today; try again tomorrow")
    uploads = []
    for f in files:
        data = await f.read()
        uploads.append(((f.content_type or "").split(";")[0].strip().lower(), data))
    source = "link" if url.strip() else "files"
    store.track("menu_import_started", row.id, source=source, files=len(uploads))
    _capture("menu_import_started", {"source": source, "file_count": len(uploads)})
    job_id = secrets.token_urlsafe(12)
    job = {"business_id": row.id, "state": "running", "stage": "fetching" if url.strip() else "reading",
           "started": time.time(), "finished": None, "progress": Progress(), "result": None, "error": None,
           "source": "a link" if url.strip() else f"{len(uploads)} files",
           "ai_session_id": f"menu-import-{job_id}"}
    jobs[job_id] = job
    job["task"] = asyncio.create_task(_run_import(request.app, row, job, uploads, url.strip(), mode, who))
    return {"job": job_id}


async def _run_import(app_, row, job: dict, uploads: list, url: str, mode: str, who: Who) -> None:
    store = app_.state.store
    try:
        if url:
            uploads = [await menu_link.fetch(url, getattr(app_.state, "menu_http", None),
                                             getattr(app_.state, "menu_resolve", None) or menu_link.resolve_host)]
            job["stage"] = "reading"
        draft = store.draft(row.id)
        start_catalog = draft["catalog"] if draft else store.catalog_payload(row.catalog_id)
        result = await read_catalog(app_.state.client, uploads, get_template(start_catalog.get("template")),
                                    start_catalog["settings"], job["progress"], ai_session_id=job["ai_session_id"],
                                    distinct_id=str(who.user["id"]) if who.user else None)
        job["stage"] = "saving"
        # Merge into the draft as it is now: the owner may have kept editing while it read.
        for _ in range(3):
            row_now = store.business(row.id)
            draft = store.draft(row.id)
            catalog = draft["catalog"] if draft else store.catalog_payload(row_now.catalog_id)
            catalog = {**catalog, "items": merge_items(catalog.get("items", []), result["items"], replace=mode == "replace")}
            settings = dict(catalog["settings"])
            for key in ("address", "hours"):  # fill blanks only; never overwrite what the owner typed
                if result["business"].get(key) and not settings.get(key):
                    settings[key] = result["business"][key].strip()[:400]
            if result["about"] and not str(settings.get("about") or "").strip():
                settings["about"] = result["about"]
                settings["placeholders"] = sorted(set(settings.get("placeholders") or []) | {"about"})
            faq = list(settings.get("faq") or [])
            asked = {str(f.get("q", "")).strip().lower() for f in faq}
            for f in result["faq"]:  # drafted answers, flagged until the owner checks them
                if f["q"].lower() not in asked and len(faq) < 40:
                    faq.append({**f, "from_menu": True})
                    asked.add(f["q"].lower())
            settings["faq"] = faq
            guides = list(settings.get("guides") or [])
            titled = {str(g.get("title", "")).strip().lower() for g in guides}
            for g in result.get("guides") or []:  # drafted, flagged like the questions
                if g["title"].lower() not in titled and len(guides) < 30:
                    guides.append({**g, "from_menu": True})
                    titled.add(g["title"].lower())
            settings["guides"] = guides
            catalog["settings"] = settings
            base = draft["base_catalog_id"] if draft else row_now.catalog_id
            if store.save_draft(row.id, catalog, base, draft["revision"] if draft else None, who.label) is not None:
                break
        else:
            raise ImportError_("Someone kept changing this catalog while it was read. Please try again.")
        log.info("business %s: read %d items from %s", row.slug, len(result["items"]), job["source"])
        store.track("menu_import_done", row.id, ok=True, items=len(result["items"]),
                    seconds=round(time.time() - job["started"], 1), source=job["source"],
                    rules=sum(1 for it in result["items"] if it.get("rules_from_menu")), questions=len(result["faq"]))
        job["result"] = {"added": len(result["items"]), "unreadable": result["unreadable"],
                         "left_out": result["left_out"], "questions": len(result["faq"]),
                         "rules": sum(1 for it in result["items"] if it.get("rules_from_menu"))}
        job["state"] = "done"
    except ImportError_ as e:
        job["state"], job["error"] = "error", str(e)
        store.track("menu_import_failed", row.id, ok=False, error=str(e)[:160], source=job["source"],
                    seconds=round(time.time() - job["started"], 1))
    except Exception:  # never leave the owner watching a spinner forever
        log.exception("business %s: menu import failed", row.slug)
        job["state"], job["error"] = "error", "Reading didn't work this time. Please try again."
        store.track("menu_import_failed", row.id, ok=False, error="crashed", source=job["source"],
                    seconds=round(time.time() - job["started"], 1))
    finally:
        job["finished"] = time.time()
        job.pop("task", None)


@router.get("/api/manage/businesses/{business_id}/import")
async def import_running(request: Request, business_id: int) -> dict:
    """The reading under way for this business, if any (so a reloaded page can follow it)."""
    who = _who(request)
    row = _business(request, who, business_id)
    running = [k for k, j in _import_jobs(request.app).items() if j["business_id"] == row.id and j["state"] == "running"]
    return {"job": running[0] if running else None}


@router.get("/api/manage/businesses/{business_id}/import/{job_id}")
async def import_status(request: Request, business_id: int, job_id: str) -> dict:
    who = _who(request)
    row = _business(request, who, business_id)
    job = _import_jobs(request.app).get(job_id)
    if job is None or job["business_id"] != row.id:  # another business's job looks like no job
        raise HTTPException(status_code=404, detail="that reading has expired; start again")
    out = {"state": job["state"], "stage": job["stage"], "elapsed": round(time.time() - job["started"]),
           **job["progress"].snapshot()}
    if job["state"] == "error":
        out["error"] = job["error"]
    elif job["state"] == "done":
        out.update(job["result"], **_editor_state(request, request.app.state.store.business(row.id)))
    return out


def _import_jobs(app_) -> dict:
    jobs = getattr(app_.state, "import_jobs", None)
    if jobs is None:
        jobs = app_.state.import_jobs = {}
    now = time.time()
    for jid in [k for k, j in jobs.items() if j["finished"] and now - j["finished"] > IMPORT_JOB_SECONDS]:
        jobs.pop(jid, None)
    return jobs


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
        sid = secrets.token_urlsafe(12)
        agent = PhoneAgent(catalog, client=request.app.state.client, call_id=sid,
                           caller_phone="+15550000000",
                           posthog_distinct_id=str(who.user["id"]) if who.user else None)
        session = sessions[sid] = {"business_id": row.id, "agent": agent, "last": now, "events": []}
        store.track("test_chat_started", row.id)
        _capture("test_chat_started")
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
        store.track("go_live_failed", row.id, error=str(e)[:160])
        raise HTTPException(status_code=400, detail=str(e))
    store.update_business(row.id, phone_number=bought.phone_number, active=True)
    store.add_go_live_request(row.id, user_id, area_code, "done", phone_number=bought.phone_number)
    log.info("business %s is live on %s", row.slug, bought.phone_number)
    store.track("went_live", row.id)
    _capture("business_went_live")
    return {"status": "live", "phone_number": bought.phone_number}


def _business_name(request: Request, row) -> str:
    return request.app.state.store.business_name(row.id) or row.slug


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
        store.track("go_live_pending", row.id)
        _capture("business_go_live_requested")
        return {"status": "pending"}
    return _buy(request, row, area_code, who.user["id"] if who.user else None)


@router.post("/api/manage/businesses/{business_id}/pause")
async def pause(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    request.app.state.store.update_business(row.id, active=False)
    _capture("business_paused")
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
    _capture("business_resumed")
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
    _capture("team_member_added", {"role": role, "existing_account": user is not None})
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
    _capture("team_member_removed")
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


@router.get("/api/manage/businesses/{business_id}/inquiries")
async def inquiries(request: Request, business_id: int, status: str = "") -> list[dict]:
    """Custom requests callers left (modules/inquiries.py), newest first."""
    who = _who(request)
    row = _business(request, who, business_id)
    return request.app.state.store.inquiries(row.id, status=status or None)


@router.post("/api/manage/businesses/{business_id}/inquiries/{number}")
async def inquiry_status(request: Request, business_id: int, number: int) -> dict:
    """Mark a request contacted or closed (or new again)."""
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    status = str((await _json(request)).get("status", ""))
    store = request.app.state.store
    try:
        if not store.set_inquiry_status(row.id, number, status):
            raise HTTPException(status_code=404, detail="no such request")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _capture("inquiry_status_changed", {"status": status})
    return {"number": number, "status": status}


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
                                                "location_id": conn["location_id"],
                                                "merchant_id": conn["merchant_id"]}}


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
    provider = str(data.get("provider", ""))
    try:
        url = connect_url(provider, row.id, base, hub.providers, shop=str(data.get("shop") or ""))
    except PosError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _capture("pos_connection_started", {"provider": provider})
    return {"url": url}


@router.post("/api/manage/businesses/{business_id}/pos/import")
async def pos_import(request: Request, business_id: int) -> dict:
    """Replace the draft's items with the connected POS's catalog (keeping settings, and item
    ids of items imported before). The owner reviews it and publishes as usual."""
    from .pos import PosError, merge_import

    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    hub = getattr(request.app.state, "pos", None)
    store = request.app.state.store
    if hub is None or store.pos_connection(row.id) is None:
        raise HTTPException(status_code=400, detail="connect your POS or store first")
    provider = store.pos_connection(row.id)["provider"]
    try:
        conn = await asyncio.to_thread(hub.connection, row.id)
        imported = await asyncio.to_thread(hub.provider(provider).catalog, conn)
    except PosError as e:
        store.track("pos_import_failed", row.id, provider=provider, error=str(e)[:160])
        raise HTTPException(status_code=502, detail=f"Couldn't read your {provider.title()} catalog: {e}")
    if not imported:
        raise HTTPException(status_code=400, detail="no products with a price were found")
    for _ in range(3):
        row_now = store.business(row.id)
        draft = store.draft(row.id)
        current = draft["catalog"] if draft else store.catalog_payload(row_now.catalog_id)
        catalog, notes = merge_import(current, imported, conn.provider)
        base = draft["base_catalog_id"] if draft else row_now.catalog_id
        revision = store.save_draft(row.id, catalog, base, draft["revision"] if draft else None, who.label)
        if revision is not None:
            break
    else:
        raise HTTPException(status_code=409, detail="the catalog changed while importing; try again")
    store.track("pos_catalog_imported", row.id, provider=conn.provider, items=len(catalog["items"]))
    _capture("pos_catalog_imported", {"provider": conn.provider, "items": len(catalog["items"])})
    return {"items": len(catalog["items"]), "notes": notes, "revision": revision}


@router.delete("/api/manage/businesses/{business_id}/pos")
async def pos_disconnect(request: Request, business_id: int) -> dict:
    who = _who(request)
    _check_write(request)
    row = _business(request, who, business_id)
    _owner(who, row.id)
    request.app.state.store.delete_pos_connection(row.id)
    _capture("pos_disconnected")
    return {"connected": None}


# ---- analytics ---------------------------------------------------------------------------


@router.get("/api/manage/admin/analytics")
async def operator_analytics(request: Request, days: int = 7, tz: int = 0) -> dict:
    """Everything across all businesses (operator only). tz: the browser's offset in minutes."""
    _operator(request)
    return await asyncio.to_thread(analytics.report, request.app.state.store, max(1, min(days, 90)),
                                   None, None, tz)


@router.get("/api/manage/businesses/{business_id}/insights")
async def business_insights(request: Request, business_id: int, days: int = 7, tz: int = 0) -> dict:
    """One business's calls, orders and what callers struggled with (staff of that business)."""
    who = _who(request)
    row = _business(request, who, business_id)
    return await asyncio.to_thread(analytics.report, request.app.state.store, max(1, min(days, 90)),
                                   row.id, None, tz)


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
