"""Restaurant staff accounts for the /manage page.

People are invited, not given passwords: the operator runs `invite`, which prints a
one-time link (valid 72 hours) where the person sets their own password. A lost
password is fixed the same way. Passwords are hashed with scrypt; sessions and invites
are random tokens of which only a SHA-256 hash is stored.

Admin commands (run on the server, e.g. `fly ssh console -C "python -m ..."`):

    python -m ordering_agent.accounts invite EMAIL --restaurant SLUG [--name "Full Name"]
    python -m ordering_agent.accounts list
    python -m ordering_agent.accounts grant EMAIL --restaurant SLUG
    python -m ordering_agent.accounts revoke EMAIL --restaurant SLUG
    python -m ordering_agent.accounts disable EMAIL      (signs them out everywhere)
    python -m ordering_agent.accounts enable EMAIL
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time

from .storage import OrderStore

SESSION_DAYS = 14
INVITE_HOURS = 72
MIN_PASSWORD = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise ValueError("that doesn't look like an email address")
    return email


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=len(want),
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# A real-looking hash to check against when the email is unknown, so a wrong email and a
# wrong password take the same time.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_password_rules(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters")
    if len(password) > 200:
        raise ValueError("that password is too long")


class LoginLimiter:
    """At most `limit` failed sign-ins per email and per IP address in `window` seconds."""

    def __init__(self, limit: int = 8, window: float = 900):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, *keys: str) -> bool:
        now = time.time()
        with self._lock:
            for k in keys:
                recent = [t for t in self._fails.get(k, []) if now - t < self.window]
                self._fails[k] = recent
                if len(recent) >= self.limit:
                    return True
        return False

    def failed(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.setdefault(k, []).append(time.time())

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


class Accounts:
    def __init__(self, store: OrderStore):
        self.store = store

    def authenticate(self, email: str, password: str) -> dict | None:
        try:
            email = normalize_email(email)
        except ValueError:
            verify_password(password or "", _DUMMY_HASH)
            return None
        user = self.store.user_by_email(email)
        ok = verify_password(password or "", user["password_hash"] if user else _DUMMY_HASH)
        return user if ok and user and user["active"] else None

    def start_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self.store.add_token(_token_hash(token), "session", user_id, time.time() + SESSION_DAYS * 86400)
        return token

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        uid = self.store.token_user(_token_hash(token), "session")
        user = self.store.user(uid) if uid else None
        return user if user and user["active"] else None

    def end_session(self, token: str | None) -> None:
        if token:
            self.store.delete_token(_token_hash(token))

    def invite(self, email: str, restaurant_id: int | None, name: str = "") -> str:
        """Create the user if needed, give them the restaurant, and return an invite token."""
        email = normalize_email(email)
        user = self.store.user_by_email(email)
        uid = user["id"] if user else self.store.add_user(email, name)
        if restaurant_id is not None:
            self.store.grant(uid, restaurant_id)
        self.store.delete_user_tokens(uid, "invite")  # only the newest link works
        token = secrets.token_urlsafe(32)
        self.store.add_token(_token_hash(token), "invite", uid, time.time() + INVITE_HOURS * 3600)
        return token

    def accept_invite(self, token: str, password: str, name: str | None = None) -> dict:
        """Set the password from an invite link. Signs out the user's other sessions."""
        check_password_rules(password)
        uid = self.store.token_user(_token_hash(token or ""), "invite")
        user = self.store.user(uid) if uid else None
        if not user or not user["active"]:
            raise ValueError("this link has expired or was already used; ask for a new one")
        self.store.set_password_hash(uid, hash_password(password))
        self.store.delete_user_tokens(uid, "invite")
        self.store.delete_user_tokens(uid, "session")
        if name and name.strip() and not user["name"]:
            self.store.set_user_name(uid, name.strip()[:80])
        return self.store.user(uid)


def invite_url(base_url: str, token: str) -> str:
    # In the fragment, so the token isn't sent in requests, logged or leaked as a Referer.
    return f"{base_url.rstrip('/')}/manage#invite={token}"


def main(argv: list[str] | None = None, store: OrderStore | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ordering_agent.accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("invite", help="create a user (or reset a password) and print a set-password link")
    inv.add_argument("email")
    inv.add_argument("--restaurant", help="slug of a restaurant they manage")
    inv.add_argument("--name", default="")
    sub.add_parser("list")
    for name in ("grant", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("email")
        p.add_argument("--restaurant", required=True)
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("email")
    args = ap.parse_args(argv)

    store = store or OrderStore(os.environ.get("ORDERS_DB", "data/orders.db"))
    accounts = Accounts(store)

    def restaurant_id(slug: str | None) -> int | None:
        if slug is None:
            return None
        row = store.restaurant_by_slug(slug)
        if row is None:
            raise SystemExit(f"no restaurant {slug!r}")
        return row.id

    def user(email: str) -> dict:
        try:
            u = store.user_by_email(normalize_email(email))
        except ValueError as e:
            raise SystemExit(str(e))
        if u is None:
            raise SystemExit(f"no user {email!r}")
        return u

    if args.cmd == "invite":
        base = os.environ.get("PUBLIC_BASE_URL", "").strip()
        if not base:
            raise SystemExit("PUBLIC_BASE_URL isn't set")
        try:
            token = accounts.invite(args.email, restaurant_id(args.restaurant), args.name)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"Send this link to {args.email}; it works once, for {INVITE_HOURS} hours:")
        print(invite_url(base, token))
    elif args.cmd == "list":
        for u in store.users():
            state = "active" if u["active"] else "disabled"
            pw = "" if u["has_password"] else " (hasn't set a password yet)"
            print(f"{u['id']:>3}  {u['email']:<32} {state:<9} {u['restaurants'] or '-'}{pw}")
    elif args.cmd == "grant":
        store.grant(user(args.email)["id"], restaurant_id(args.restaurant))
        print("granted")
    elif args.cmd == "revoke":
        store.revoke(user(args.email)["id"], restaurant_id(args.restaurant))
        print("revoked")
    elif args.cmd in ("disable", "enable"):
        store.set_user_active(user(args.email)["id"], args.cmd == "enable")
        print(f"{args.cmd}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
