"""Persistence: businesses and their catalogs, placed orders, and call transcripts.

Every order and call belongs to a business (`business_id`). Order numbers are
counted per business, so each business's first order is #1; the row's own `id` is
the internal key (payment links, webhooks).

SQLite keeps the demo dependency-free, and all SQL lives in this file so it can be
swapped for Postgres. Only saved data is shared: live call state stays on the call's
WebSocket.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .order import Order

TRANSCRIPT_MAX_CHARS = 4000  # per transcript line; tool results can be long

# Order status: "confirmed" (ready to prepare), "awaiting_payment" (the business requires
# payment first), "cancelled" (payment link expired while awaiting payment).
# Payment status: "not_requested" (pay at pickup), "unpaid", "paid", "expired".
# POS status: NULL (no POS connected), "held" (waiting for payment), "pending", "sent", "failed".


@dataclass(frozen=True)
class SavedOrder:
    id: int  # internal row id
    number: int  # per-business order number


@dataclass(frozen=True)
class BusinessRow:
    id: int
    slug: str
    phone_number: str | None
    active: bool
    catalog_id: int | None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        c = self._conn
        c.execute(
            """CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                phone_number TEXT UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                catalog_id INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS catalog_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER NOT NULL REFERENCES businesses(id),
                created_at REAL NOT NULL,
                created_by TEXT,  -- a user's email, or "operator"
                payload TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER REFERENCES businesses(id),
                number INTEGER,  -- per-business order number, what callers and staff see
                catalog_id INTEGER,  -- the catalog version the order was taken from
                created_at REAL NOT NULL,
                call_id TEXT,
                customer_name TEXT,
                customer_phone TEXT,
                total REAL NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                payment_status TEXT NOT NULL DEFAULT 'not_requested',
                payment_id TEXT,
                payment_url TEXT,
                paid_at REAL,
                pos_status TEXT,
                pos_order_id TEXT,
                pos_error TEXT,
                pos_attempts INTEGER NOT NULL DEFAULT 0,
                pos_paid INTEGER NOT NULL DEFAULT 0  -- the online payment is recorded on the POS
            )"""
        )
        # One row per phone call, placed order or not, plus what was said on it.
        c.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                business_id INTEGER REFERENCES businesses(id),
                started_at REAL NOT NULL,
                ended_at REAL,
                caller_phone TEXT,
                order_number INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                at REAL NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL
            )"""
        )
        # A business's link to its point of sale. `secret` holds the encrypted access and
        # refresh tokens (pos.py encrypts them with POS_SECRET_KEY); `settings` holds ids
        # picked at connect time (order type, tax rate).
        c.execute(
            """CREATE TABLE IF NOT EXISTS pos_connections (
                business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
                provider TEXT NOT NULL,
                environment TEXT NOT NULL,
                merchant_id TEXT,
                location_id TEXT,
                secret TEXT NOT NULL,
                settings TEXT NOT NULL DEFAULT '{}',
                connected_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        # Business staff who sign in to /manage. A user can belong to several businesses.
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                password_hash TEXT,
                created_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS user_businesses (
                user_id INTEGER NOT NULL REFERENCES users(id),
                business_id INTEGER NOT NULL REFERENCES businesses(id),
                role TEXT NOT NULL DEFAULT 'manager',
                PRIMARY KEY (user_id, business_id)
            )"""
        )
        # Sign-in sessions and set-password invites. Only SHA-256 hashes of the tokens are stored.
        c.execute(
            """CREATE TABLE IF NOT EXISTS auth_tokens (
                token_hash TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )"""
        )
        # Unpublished catalog edits, one per business. base_catalog_id is the live version the
        # draft started from, to catch someone else publishing in the meantime.
        c.execute(
            """CREATE TABLE IF NOT EXISTS catalog_drafts (
                business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
                payload TEXT NOT NULL,
                base_catalog_id INTEGER,
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                updated_by TEXT
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS orders_payment_id ON orders(payment_id)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_business ON orders(business_id, number)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_pos ON orders(pos_status, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS calls_business ON calls(business_id, started_at)")
        c.execute("CREATE INDEX IF NOT EXISTS call_events_call ON call_events(call_id, id)")
        c.commit()
        self._migrate()

    # ---- schema changes ----------------------------------------------------------------
    # The tables above are version 1. Each later change is a numbered step here, applied
    # once to an existing database (PRAGMA user_version records how far it has got). Never
    # edit a step that has shipped; add a new one.

    def _migrate(self) -> None:
        steps = [self._v2_self_serve, self._v3_analytics]
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0] or 1
            for number, step in enumerate(steps, start=2):
                if version < number:
                    step(self._conn)
                    self._conn.execute(f"PRAGMA user_version = {number}")
                    self._conn.commit()

    @staticmethod
    def _v3_analytics(c: sqlite3.Connection) -> None:
        """Analytics: an event log, measurements per call, and the AI review of each call."""
        # One row per thing worth counting (signed in, menu read, order placed, POS send
        # failed ...). props is small JSON, never transcripts; phone numbers are hashed.
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                kind TEXT NOT NULL,
                business_id INTEGER,
                user_id INTEGER,
                call_id TEXT,
                props TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind, at)")
        c.execute("CREATE INDEX IF NOT EXISTS events_business_at ON events(business_id, at)")
        cols = {row[1] for row in c.execute("PRAGMA table_info(calls)")}
        for name, kind in (("outcome", "TEXT"), ("metrics", "TEXT")):
            if name not in cols:
                c.execute(f"ALTER TABLE calls ADD COLUMN {name} {kind}")
        c.execute("CREATE INDEX IF NOT EXISTS calls_business_started ON calls(business_id, started_at)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS call_reviews (
                call_id TEXT PRIMARY KEY,
                business_id INTEGER,
                at REAL NOT NULL,
                model TEXT,
                review TEXT NOT NULL
            )"""
        )

    @staticmethod
    def _v2_self_serve(c: sqlite3.Connection) -> None:
        """Sign in with Google, go-live requests, and daily usage counters."""
        cols = {row[1] for row in c.execute("PRAGMA table_info(users)")}
        if "google_sub" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users(google_sub)")
        # A business asking for a phone number; "pending" ones wait for the operator.
        c.execute(
            """CREATE TABLE IF NOT EXISTS go_live_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER NOT NULL REFERENCES businesses(id),
                user_id INTEGER REFERENCES users(id),
                area_code TEXT,
                status TEXT NOT NULL,  -- pending, done, rejected, failed
                phone_number TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                decided_at REAL
            )"""
        )
        # Counters for limits: scope is "global", "user:<id>" or "business:<id>".
        c.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                scope TEXT NOT NULL,
                day TEXT NOT NULL,
                kind TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, day, kind)
            )"""
        )

    # ---- businesses and catalogs -------------------------------------------------------

    def create_business(self, slug: str) -> int:
        """A new self-serve business: no catalog, no number, not taking calls yet."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO businesses (slug, phone_number, active, created_at) VALUES (?, NULL, 0, ?)",
                (slug, time.time()))
            self._conn.commit()
            return int(cur.lastrowid)

    def add_business(self, slug: str, phone_number: str | None, catalog: dict) -> int:
        """Create a business with its first catalog. Returns the business id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO businesses (slug, phone_number, created_at) VALUES (?, ?, ?)",
                (slug, phone_number, time.time()),
            )
            rid = int(cur.lastrowid)
            self._insert_catalog(rid, catalog)
            self._conn.commit()
            return rid

    def set_catalog(self, business_id: int, catalog: dict, created_by: str = "operator") -> int:
        """Publish a new catalog version for the business. Old versions are kept. Returns its id."""
        with self._lock:
            catalog_id = self._insert_catalog(business_id, catalog, created_by)
            self._conn.commit()
            return catalog_id

    def _insert_catalog(self, business_id: int, catalog: dict, created_by: str = "operator") -> int:
        cur = self._conn.execute(
            "INSERT INTO catalog_versions (business_id, created_at, payload, created_by) VALUES (?, ?, ?, ?)",
            (business_id, time.time(), json.dumps(catalog, ensure_ascii=False), created_by),
        )
        catalog_id = int(cur.lastrowid)
        self._conn.execute("UPDATE businesses SET catalog_id = ? WHERE id = ?", (catalog_id, business_id))
        return catalog_id

    def update_business(self, business_id: int, *, phone_number: str | None = None,
                          clear_phone: bool = False, active: bool | None = None) -> None:
        with self._lock:
            if phone_number is not None or clear_phone:
                self._conn.execute("UPDATE businesses SET phone_number = ? WHERE id = ?",
                                   (None if clear_phone else phone_number, business_id))
            if active is not None:
                self._conn.execute("UPDATE businesses SET active = ? WHERE id = ?",
                                   (int(active), business_id))
            self._conn.commit()

    _BUSINESS_COLS = "id, slug, phone_number, active, catalog_id"

    def _business(self, where: str, value) -> BusinessRow | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._BUSINESS_COLS} FROM businesses WHERE {where} = ?", (value,)
            ).fetchone()
        return BusinessRow(row[0], row[1], row[2], bool(row[3]), row[4]) if row else None

    def business(self, business_id: int) -> BusinessRow | None:
        return self._business("id", business_id)

    def business_name(self, business_id: int) -> str | None:
        """The name the business goes by: its published catalog's, else its draft's (a business
        still setting up), else its slug. None if there's no such business."""
        row = self.business(business_id)
        if row is None:
            return None
        for payload in ((self.catalog_payload(row.catalog_id) if row.catalog_id else None),
                        (self.draft(business_id) or {}).get("catalog")):
            name = str(((payload or {}).get("settings") or {}).get("name") or "").strip()
            if name:
                return name
        return row.slug

    def business_by_slug(self, slug: str) -> BusinessRow | None:
        return self._business("slug", slug)

    def business_by_phone(self, phone_number: str) -> BusinessRow | None:
        return self._business("phone_number", phone_number)

    def businesses(self) -> list[BusinessRow]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._BUSINESS_COLS} FROM businesses ORDER BY id").fetchall()
        return [BusinessRow(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]

    def catalog_payload(self, catalog_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM catalog_versions WHERE id = ?", (catalog_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def catalog_versions(self, business_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, created_by, payload FROM catalog_versions WHERE business_id = ?"
                " ORDER BY id DESC LIMIT ?", (business_id, limit)).fetchall()
        return [{"id": r[0], "created_at": r[1], "created_by": r[2] or "operator",
                 "items": len(json.loads(r[3]).get("items", []))} for r in rows]

    def catalog_version_payload(self, business_id: int, catalog_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM catalog_versions WHERE id = ? AND business_id = ?",
                                     (catalog_id, business_id)).fetchone()
        return json.loads(row[0]) if row else None

    # ---- catalog drafts -----------------------------------------------------------------

    def draft(self, business_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, base_catalog_id, revision, updated_at, updated_by FROM catalog_drafts"
                " WHERE business_id = ?", (business_id,)).fetchone()
        if row is None:
            return None
        return {"catalog": json.loads(row[0]), "base_catalog_id": row[1], "revision": row[2],
                "updated_at": row[3], "updated_by": row[4]}

    def save_draft(self, business_id: int, catalog: dict, base_catalog_id: int | None,
                   expected_revision: int | None, updated_by: str) -> int | None:
        """Save the draft if nobody else saved since `expected_revision` (None: there was
        no draft). Returns the new revision, or None on a conflict."""
        payload = json.dumps(catalog, ensure_ascii=False)
        with self._lock:
            row = self._conn.execute("SELECT revision FROM catalog_drafts WHERE business_id = ?",
                                     (business_id,)).fetchone()
            current = row[0] if row else None
            if current != expected_revision:
                return None
            if row is None:
                self._conn.execute(
                    "INSERT INTO catalog_drafts (business_id, payload, base_catalog_id, revision, updated_at,"
                    " updated_by) VALUES (?, ?, ?, 1, ?, ?)",
                    (business_id, payload, base_catalog_id, time.time(), updated_by))
                new = 1
            else:
                new = current + 1
                self._conn.execute(
                    "UPDATE catalog_drafts SET payload = ?, revision = ?, updated_at = ?, updated_by = ?"
                    " WHERE business_id = ?", (payload, new, time.time(), updated_by, business_id))
            self._conn.commit()
            return new

    def delete_draft(self, business_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM catalog_drafts WHERE business_id = ?", (business_id,))
            self._conn.commit()

    def publish_draft(self, business_id: int, catalog: dict, created_by: str) -> int:
        """Publish the catalog and drop the draft, in one transaction."""
        with self._lock:
            catalog_id = self._insert_catalog(business_id, catalog, created_by)
            self._conn.execute("DELETE FROM catalog_drafts WHERE business_id = ?", (business_id,))
            self._conn.commit()
            return catalog_id

    # ---- users and sign-in --------------------------------------------------------------

    def add_user(self, email: str, name: str = "") -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO users (email, name, created_at) VALUES (?, ?, ?)",
                                     (email, name, time.time()))
            self._conn.commit()
            return int(cur.lastrowid)

    def user_by_email(self, email: str) -> dict | None:
        return self._user("email", email)

    def user(self, user_id: int) -> dict | None:
        return self._user("id", user_id)

    def _user(self, col: str, value) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT id, email, name, password_hash, active, google_sub FROM users WHERE {col} = ?", (value,)
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "email": row[1], "name": row[2], "password_hash": row[3], "active": bool(row[4]),
                "google_sub": row[5]}

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
            self._conn.commit()

    def set_user_name(self, user_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
            self._conn.commit()

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
            if not active:
                self._conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def grant(self, user_id: int, business_id: int, role: str = "manager") -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO user_businesses (user_id, business_id, role)"
                               " VALUES (?, ?, ?)", (user_id, business_id, role))
            self._conn.commit()

    def revoke(self, user_id: int, business_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM user_businesses WHERE user_id = ? AND business_id = ?",
                               (user_id, business_id))
            self._conn.commit()

    def user_role(self, user_id: int, business_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT role FROM user_businesses WHERE user_id = ? AND business_id = ?",
                                     (user_id, business_id)).fetchone()
        return row[0] if row else None

    def team(self, business_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.email, u.name, ub.role, u.google_sub IS NOT NULL OR u.password_hash IS NOT NULL"
                " FROM user_businesses ub JOIN users u ON u.id = ub.user_id WHERE ub.business_id = ?"
                " ORDER BY ub.role = 'owner' DESC, u.email", (business_id,)).fetchall()
        return [{"user_id": r[0], "email": r[1], "name": r[2], "role": r[3], "joined": bool(r[4])} for r in rows]

    def owned_business_count(self, user_id: int) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM user_businesses WHERE user_id = ? AND role = 'owner'",
                                      (user_id,)).fetchone()[0]

    def user_by_google(self, sub: str) -> dict | None:
        return self._user("google_sub", sub)

    def link_google(self, user_id: int, sub: str, name: str = "") -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET google_sub = ?, name = CASE WHEN name = '' THEN ? ELSE name END"
                               " WHERE id = ?", (sub, name[:80], user_id))
            self._conn.commit()

    def user_business_ids(self, user_id: int) -> list[int]:
        with self._lock:
            rows = self._conn.execute("SELECT business_id FROM user_businesses WHERE user_id = ?"
                                      " ORDER BY business_id", (user_id,)).fetchall()
        return [r[0] for r in rows]

    def users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.email, u.name, u.active, u.password_hash IS NOT NULL,"
                " GROUP_CONCAT(r.slug, ', ') FROM users u"
                " LEFT JOIN user_businesses ur ON ur.user_id = u.id"
                " LEFT JOIN businesses r ON r.id = ur.business_id GROUP BY u.id ORDER BY u.id").fetchall()
        return [{"id": r[0], "email": r[1], "name": r[2], "active": bool(r[3]), "has_password": bool(r[4]),
                 "businesses": r[5] or ""} for r in rows]

    def add_token(self, token_hash: str, kind: str, user_id: int, expires_at: float) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO auth_tokens (token_hash, kind, user_id, created_at, expires_at)"
                               " VALUES (?, ?, ?, ?, ?)", (token_hash, kind, user_id, time.time(), expires_at))
            self._conn.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (time.time(),))
            self._conn.commit()

    def token_user(self, token_hash: str, kind: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id FROM auth_tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (token_hash, kind, time.time())).fetchone()
        return row[0] if row else None

    def delete_token(self, token_hash: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
            self._conn.commit()

    def delete_user_tokens(self, user_id: int, kind: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM auth_tokens WHERE user_id = ? AND kind = ?", (user_id, kind))
            self._conn.commit()

    # ---- orders ------------------------------------------------------------------

    def save(self, order: Order, call_id: str | None = None, status: str = "confirmed",
             payment_status: str = "not_requested") -> SavedOrder:
        s = order.summary()
        # Structured lines, for sending the order to a point of sale later.
        s["items"] = [{"item_id": l.item_id, "name": l.name, "quantity": l.quantity,
                       "unit_price": l.unit_price, "options": l.options, "extras": l.extras,
                       "notes": l.notes} for l in order.lines]
        rid = order.catalog.business_id
        with self._lock:
            (number,) = self._conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM orders WHERE business_id IS ?", (rid,)
            ).fetchone()
            cur = self._conn.execute(
                "INSERT INTO orders (created_at, call_id, customer_name, customer_phone, total, payload,"
                " status, payment_status, business_id, number, catalog_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), call_id, s["customer_name"], s["customer_phone"], s["total"], json.dumps(s),
                 status, payment_status, rid, number, order.catalog.catalog_id),
            )
            self._conn.commit()
            return SavedOrder(id=int(cur.lastrowid), number=int(number))

    def set_payment_link(self, order_id: int, payment_id: str, url: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET payment_id = ?, payment_url = ? WHERE id = ?",
                               (payment_id, url, order_id))
            self._conn.commit()

    def update_status(self, order_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
            self._conn.commit()

    def mark_paid(self, payment_id: str) -> dict | None:
        """Mark the order paid (and confirmed). Returns the order only if this call changed it,
        so retried webhooks don't send duplicate texts."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orders SET payment_status = 'paid', paid_at = ?,"
                " status = CASE WHEN status = 'awaiting_payment' THEN 'confirmed' ELSE status END"
                " WHERE payment_id = ? AND payment_status != 'paid'",
                (time.time(), payment_id),
            )
            self._conn.commit()
            return self._by_payment_id(payment_id) if cur.rowcount else None

    def mark_expired(self, payment_id: str) -> dict | None:
        """The link expired unpaid. Orders that were waiting on payment are cancelled.
        Returns the order only if this call changed it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orders SET payment_status = 'expired',"
                " status = CASE WHEN status = 'awaiting_payment' THEN 'cancelled' ELSE status END"
                " WHERE payment_id = ? AND payment_status = 'unpaid'",
                (payment_id,),
            )
            self._conn.commit()
            return self._by_payment_id(payment_id) if cur.rowcount else None

    def _by_payment_id(self, payment_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, number, business_id, customer_phone, status, payment_status, payload"
            " FROM orders WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[6] or "{}")
        return {"order_id": row[0], "order_number": row[1], "business_id": row[2],
                "customer_phone": row[3], "status": row[4], "payment_status": row[5],
                # the scheduled pickup in words, or None for as soon as possible
                "pickup": payload.get("pickup") if payload.get("pickup_at") else None}

    def recent(self, limit: int = 20, business_id: int | None = None) -> list[dict]:
        """Newest orders first, for one business or (business_id None) all of them."""
        where, args = ("WHERE business_id = ?", (business_id,)) if business_id is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, number, business_id, created_at, call_id, payload, status, payment_status,"
                " payment_url, pos_status, pos_error FROM orders"
                f" {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
        out = []
        for r in rows:
            payload = json.loads(r[5])
            payload.pop("items", None)
            out.append({"id": r[0], "order_number": r[1], "business_id": r[2], "created_at": r[3],
                        "call_id": r[4], **payload, "status": r[6], "payment_status": r[7],
                        "payment_url": r[8], "pos_status": r[9], "pos_error": r[10]})
        return out

    # ---- point of sale ----------------------------------------------------------

    def set_pos_connection(self, business_id: int, provider: str, environment: str, secret: str,
                           merchant_id: str | None = None, location_id: str | None = None,
                           settings: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO pos_connections (business_id, provider, environment, merchant_id,"
                " location_id, secret, settings, connected_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(business_id) DO UPDATE SET provider = excluded.provider,"
                " environment = excluded.environment, merchant_id = excluded.merchant_id,"
                " location_id = excluded.location_id, secret = excluded.secret,"
                " settings = excluded.settings, connected_at = excluded.connected_at,"
                " updated_at = excluded.updated_at",
                (business_id, provider, environment, merchant_id, location_id, secret,
                 json.dumps(settings or {}), now, now),
            )
            self._conn.commit()

    def update_pos_connection(self, business_id: int, *, secret: str | None = None,
                              location_id: str | None = None, settings: dict | None = None) -> None:
        sets, args = ["updated_at = ?"], [time.time()]
        for col, val in (("secret", secret), ("location_id", location_id),
                         ("settings", None if settings is None else json.dumps(settings))):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        with self._lock:
            self._conn.execute(f"UPDATE pos_connections SET {', '.join(sets)} WHERE business_id = ?",
                               (*args, business_id))
            self._conn.commit()

    def pos_connection(self, business_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT provider, environment, merchant_id, location_id, secret, settings, connected_at"
                " FROM pos_connections WHERE business_id = ?", (business_id,)
            ).fetchone()
        if row is None:
            return None
        return {"business_id": business_id, "provider": row[0], "environment": row[1],
                "merchant_id": row[2], "location_id": row[3], "secret": row[4],
                "settings": json.loads(row[5]), "connected_at": row[6]}

    def delete_pos_connection(self, business_id: int) -> bool:
        with self._lock:
            n = self._conn.execute("DELETE FROM pos_connections WHERE business_id = ?",
                                   (business_id,)).rowcount
            self._conn.commit()
        return bool(n)

    def order_for_pos(self, order_id: int) -> dict | None:
        """Everything needed to send one order to a point of sale."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, number, business_id, catalog_id, created_at, customer_name, customer_phone,"
                " payload, status, payment_status, payment_id, pos_status, pos_order_id, pos_attempts,"
                " pos_paid FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "number": row[1], "business_id": row[2], "catalog_id": row[3],
                "created_at": row[4], "customer_name": row[5], "customer_phone": row[6],
                **{k: v for k, v in json.loads(row[7]).items()
                   if k in ("items", "subtotal", "tax", "total", "lines", "pickup_at", "pickup")},
                "status": row[8], "payment_status": row[9], "payment_id": row[10],
                "pos_status": row[11], "pos_order_id": row[12], "pos_attempts": row[13],
                "pos_paid": bool(row[14])}

    def set_pos_status(self, order_id: int, status: str, *, pos_order_id: str | None = None,
                       error: str | None = None, attempted: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET pos_status = ?, pos_error = ?,"
                " pos_order_id = COALESCE(?, pos_order_id),"
                " pos_attempts = pos_attempts + ? WHERE id = ?",
                (status, (error or "")[:500] or None, pos_order_id, int(attempted), order_id),
            )
            self._conn.commit()

    def clear_pos_order_id(self, order_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET pos_order_id = NULL WHERE id = ?", (order_id,))
            self._conn.commit()

    def set_pos_paid(self, order_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET pos_paid = 1 WHERE id = ?", (order_id,))
            self._conn.commit()

    def orders_awaiting_link(self, since: float) -> list[int]:
        """Orders waiting for a POS payment link (Square) to be paid, for watching after a restart."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM orders WHERE status = 'awaiting_payment' AND pos_status = 'held'"
                " AND payment_id IS NOT NULL AND pos_order_id IS NOT NULL AND created_at >= ? ORDER BY id",
                (since,)).fetchall()
        return [r[0] for r in rows]

    def orders_to_send(self, since: float, business_id: int | None = None,
                       statuses: tuple[str, ...] = ("pending", "failed")) -> list[int]:
        """Ids of orders not yet on their POS, oldest first (for retrying after a restart)."""
        marks = ", ".join("?" * len(statuses))
        where = f"pos_status IN ({marks}) AND created_at >= ?"
        args: list = [*statuses, since]
        if business_id is not None:
            where += " AND business_id = ?"
            args.append(business_id)
        with self._lock:
            rows = self._conn.execute(f"SELECT id FROM orders WHERE {where} ORDER BY id", args).fetchall()
        return [r[0] for r in rows]

    def order_id_by_number(self, business_id: int, number: int) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM orders WHERE business_id = ? AND number = ?",
                                     (business_id, number)).fetchone()
        return row[0] if row else None

    # ---- calls and transcripts ----------------------------------------------------

    def start_call(self, call_id: str, caller_phone: str | None, business_id: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO calls (call_id, started_at, caller_phone, business_id)"
                " VALUES (?, ?, ?, ?)",
                (call_id, time.time(), caller_phone, business_id),
            )
            self._conn.commit()

    def end_call(self, call_id: str, order_number: int | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE calls SET ended_at = ?, order_number = ? WHERE call_id = ?",
                               (time.time(), order_number, call_id))
            self._conn.commit()

    def log_event(self, call_id: str, role: str, text: str) -> None:
        """Append one transcript line. role: caller, agent, tool or event."""
        with self._lock:
            self._conn.execute("INSERT INTO call_events (call_id, at, role, text) VALUES (?, ?, ?, ?)",
                               (call_id, time.time(), role, text[:TRANSCRIPT_MAX_CHARS]))
            self._conn.commit()

    def recent_calls(self, limit: int = 50, business_id: int | None = None) -> list[dict]:
        where, args = ("WHERE c.business_id = ?", (business_id,)) if business_id is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.call_id, c.started_at, c.ended_at, c.caller_phone, c.order_number, c.business_id,"
                " (SELECT COUNT(*) FROM call_events e WHERE e.call_id = c.call_id AND e.role = 'caller'),"
                " c.outcome, r.review FROM calls c LEFT JOIN call_reviews r ON r.call_id = c.call_id"
                f" {where} ORDER BY c.started_at DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [{"call_id": r[0], "started_at": r[1], "ended_at": r[2], "caller_phone": r[3],
                 "order_number": r[4], "business_id": r[5], "caller_turns": r[6], "outcome": r[7],
                 "review": json.loads(r[8]) if r[8] else None} for r in rows]

    # ---- analytics --------------------------------------------------------------------

    def track(self, kind: str, business_id: int | None = None, *, user_id: int | None = None,
              call_id: str | None = None, **props) -> None:
        """Record one analytics event. Never raises: analytics must not break the app."""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events (at, kind, business_id, user_id, call_id, props) VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), kind, business_id, user_id, call_id,
                     json.dumps({k: v for k, v in props.items() if v is not None}, default=str)[:4000]))
                self._conn.commit()
        except Exception:
            logging.getLogger("phone_agent.storage").exception("couldn't record event %s", kind)

    def set_call_metrics(self, call_id: str, outcome: str, metrics: dict) -> None:
        with self._lock:
            self._conn.execute("UPDATE calls SET outcome = ?, metrics = ? WHERE call_id = ?",
                               (outcome, json.dumps(metrics), call_id))
            self._conn.commit()

    def save_call_review(self, call_id: str, business_id: int | None, review: dict, model: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO call_reviews (call_id, business_id, at, model, review)"
                               " VALUES (?, ?, ?, ?, ?)", (call_id, business_id, time.time(), model, json.dumps(review)))
            self._conn.commit()

    def call_transcript_text(self, call_id: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT role, text FROM call_events WHERE call_id = ? ORDER BY id",
                                      (call_id,)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def read(self, sql: str, args: tuple = ()) -> list[tuple]:
        """A read-only query, for the analytics reports (analytics.py)."""
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), "read() is for SELECT queries"
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ---- go-live requests and usage limits ------------------------------------------------

    def add_go_live_request(self, business_id: int, user_id: int | None, area_code: str | None,
                            status: str, phone_number: str | None = None, error: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO go_live_requests (business_id, user_id, area_code, status, phone_number, error,"
                " created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (business_id, user_id, area_code, status, phone_number, error, time.time(),
                 None if status == "pending" else time.time()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update_go_live_request(self, request_id: int, status: str, phone_number: str | None = None,
                               error: str | None = None) -> None:
        with self._lock:
            self._conn.execute("UPDATE go_live_requests SET status = ?, phone_number = ?, error = ?, decided_at = ?"
                               " WHERE id = ?", (status, phone_number, error, time.time(), request_id))
            self._conn.commit()

    def go_live_requests(self, status: str | None = None, business_id: int | None = None) -> list[dict]:
        where, args = [], []
        if status:
            where.append("r.status = ?"); args.append(status)
        if business_id is not None:
            where.append("r.business_id = ?"); args.append(business_id)
        sql = ("SELECT r.id, r.business_id, b.slug, r.user_id, u.email, r.area_code, r.status, r.phone_number,"
               " r.error, r.created_at, r.decided_at FROM go_live_requests r JOIN businesses b ON b.id = r.business_id"
               " LEFT JOIN users u ON u.id = r.user_id"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.id DESC")
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        keys = ("id", "business_id", "slug", "user_id", "email", "area_code", "status", "phone_number",
                "error", "created_at", "decided_at")
        return [dict(zip(keys, r)) for r in rows]

    def usage(self, scope: str, kind: str, day: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT count FROM usage WHERE scope = ? AND day = ? AND kind = ?",
                                     (scope, day, kind)).fetchone()
        return row[0] if row else 0

    def take_usage(self, scope: str, kind: str, day: str, limit: int) -> bool:
        """Count one use if that stays within the limit. Returns False (and counts
        nothing) when the limit is reached. Atomic, so concurrent requests can't overshoot."""
        with self._lock:
            row = self._conn.execute("SELECT count FROM usage WHERE scope = ? AND day = ? AND kind = ?",
                                     (scope, day, kind)).fetchone()
            if (row[0] if row else 0) >= limit:
                return False
            self._conn.execute(
                "INSERT INTO usage (scope, day, kind, count) VALUES (?, ?, ?, 1)"
                " ON CONFLICT(scope, day, kind) DO UPDATE SET count = count + 1", (scope, day, kind))
            self._conn.commit()
            return True

    def live_business_count(self, user_id: int) -> int:
        """Businesses this user owns that have a phone number."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM user_businesses ub JOIN businesses b ON b.id = ub.business_id"
                " WHERE ub.user_id = ? AND ub.role = 'owner' AND b.phone_number IS NOT NULL", (user_id,)).fetchone()[0]

    def call_business(self, call_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT business_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return row[0] if row else None

    def transcript(self, call_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, role, text FROM call_events WHERE call_id = ? ORDER BY id", (call_id,)
            ).fetchall()
        return [{"at": r[0], "role": r[1], "text": r[2]} for r in rows]
