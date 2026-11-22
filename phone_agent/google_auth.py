"""Sign in with Google (OpenID Connect, authorization code flow with PKCE).

  GET /auth/google            -> redirect to Google's account chooser
  GET /auth/google/callback   -> exchange the code, read the verified email, sign in

The first sign-in creates the account. An email that was already added to a team (or
has an older password account) is linked to the Google account on first sign-in, so
teammates are invited by just adding their email. Only Google-verified emails are
accepted. The state and PKCE verifier live in a short-lived signed, HttpOnly cookie.

Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (a Google Cloud "Web application"
OAuth client whose redirect URI is <PUBLIC_BASE_URL>/auth/google/callback).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .accounts import Accounts

log = logging.getLogger("phone_agent.google_auth")
router = APIRouter()

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
STATE_COOKIE = "oa_google"
STATE_SECONDS = 600


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _key() -> bytes:
    return hashlib.sha256(b"google-oauth-state:" + os.environ.get("GOOGLE_CLIENT_SECRET", "").encode()).digest()


def _sign(value: str) -> str:
    return value + "." + hmac.new(_key(), value.encode(), hashlib.sha256).hexdigest()[:32]


def _unsign(signed: str) -> str | None:
    value, _, sig = signed.rpartition(".")
    want = hmac.new(_key(), value.encode(), hashlib.sha256).hexdigest()[:32]
    return value if value and hmac.compare_digest(want, sig) else None


def _base(request: Request) -> str:
    public = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if public:
        return public
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def _redirect_uri(request: Request) -> str:
    return _base(request) + "/auth/google/callback"


def _page(title: str, msg: str, status: int = 400) -> HTMLResponse:
    import html

    return HTMLResponse(
        f"<!doctype html><meta name=viewport content='width=device-width'><title>{html.escape(title)}</title>"
        f"<body style='font-family:system-ui,sans-serif;padding:32px;max-width:480px;margin:auto'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(msg)}</p><p><a href='/manage'>Back</a></p></body>",
        status_code=status)


@router.get("/auth/google")
async def start(request: Request):
    if not configured():
        return _page("Sign-in isn't set up", "Google sign-in isn't configured on this server yet.", 503)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": _redirect_uri(request),
        "response_type": "code", "scope": "openid email profile", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    }
    resp = RedirectResponse(f"{AUTHORIZE_URL}?{urlencode(params)}", status_code=302)
    resp.set_cookie(STATE_COOKIE, _sign(f"{state}.{verifier}.{int(time.time()) + STATE_SECONDS}"),
                    max_age=STATE_SECONDS, httponly=True, samesite="lax", path="/auth/google",
                    secure=_base(request).startswith("https://"))
    return resp


@router.get("/auth/google/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return _page("Not signed in", "Google sign-in was cancelled.")
    raw = _unsign(request.cookies.get(STATE_COOKIE, ""))
    try:
        want_state, verifier, expires = raw.split(".") if raw else ("", "", "0")
    except ValueError:
        want_state, verifier, expires = "", "", "0"
    if not raw or not hmac.compare_digest(want_state, state) or time.time() > int(expires):
        return _page("Not signed in", "That sign-in link expired. Please try again.")

    http = getattr(request.app.state, "google_http", None)
    close = http is None
    if http is None:
        import httpx

        http = httpx.AsyncClient(timeout=15)
    try:
        token = await http.post(TOKEN_URL, data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"], "redirect_uri": _redirect_uri(request),
            "grant_type": "authorization_code", "code_verifier": verifier})
        if token.status_code != 200:
            log.warning("Google token exchange failed: %s %s", token.status_code, token.text[:200])
            return _page("Not signed in", "Google didn't accept the sign-in. Please try again.")
        info = await http.get(USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"})
        profile = info.json() if info.status_code == 200 else {}
    finally:
        if close:
            await http.aclose()

    email = str(profile.get("email", "")).strip().lower()
    sub = str(profile.get("sub", ""))
    if not (email and sub and profile.get("email_verified") is True):
        return _page("Not signed in", "Your Google account needs a verified email address.")

    store = request.app.state.store
    user = store.user_by_google(sub)
    if user is None:
        user = store.user_by_email(email)
        if user and user.get("google_sub") and user["google_sub"] != sub:
            return _page("Not signed in", "This email is linked to a different Google account.")
        if user is None:
            store.add_user(email, str(profile.get("name", ""))[:80])
            user = store.user_by_email(email)
        store.link_google(user["id"], sub, str(profile.get("name", "")))
        user = store.user(user["id"])
    if not user["active"]:
        return _page("Account disabled", "This account has been disabled. Contact support.", 403)

    from .manage import COOKIE, secure_cookie

    resp = RedirectResponse("/manage", status_code=302)
    resp.set_cookie(COOKIE, Accounts(store).start_session(user["id"]), max_age=14 * 86400, httponly=True,
                    secure=secure_cookie(request), samesite="lax", path="/")
    resp.delete_cookie(STATE_COOKIE, path="/auth/google")
    log.info("%s signed in with Google", email)
    return resp
