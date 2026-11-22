"""Persistence: restaurants and their menus, placed orders, and call transcripts.

Every order and call belongs to a restaurant (`restaurant_id`). Order numbers are
counted per restaurant, so each restaurant's first order is #1; the row's own `id` is
the internal key (payment links, webhooks).

SQLite keeps the demo dependency-free, and all SQL lives in this file so it can be
swapped for Postgres. Only saved data is shared: live call state stays on the call's
WebSocket.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .order import Order

TRANSCRIPT_MAX_CHARS = 4000  # per transcript line; tool results can be long

# Order status: "confirmed" (kitchen can cook), "awaiting_payment" (restaurant requires
# payment first), "cancelled" (payment link expired while awaiting payment).
# Payment status: "not_requested" (pay at pickup), "unpaid", "paid", "expired".
_ORDER_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'confirmed'",
    "payment_status": "TEXT NOT NULL DEFAULT 'not_requested'",
    "payment_id": "TEXT",
    "payment_url": "TEXT",
    "paid_at": "REAL",
    "restaurant_id": "INTEGER",
    "number": "INTEGER",  # per-restaurant order number, what callers and staff see
    "menu_id": "INTEGER",  # the menu version the order was taken from
    # Sending the order to the restaurant's point of sale (pos.py). pos_status: NULL (no POS
    # connected), "held" (waiting for payment), "pending", "sent", "failed".
    "pos_status": "TEXT",
    "pos_order_id": "TEXT",
    "pos_error": "TEXT",
    "pos_attempts": "INTEGER NOT NULL DEFAULT 0",
    "pos_paid": "INTEGER NOT NULL DEFAULT 0",  # the online payment has been recorded on the POS
}
_CALL_COLUMNS = {"restaurant_id": "INTEGER"}
_MENU_COLUMNS = {"created_by": "TEXT"}  # who published it: a user's email, or "operator"


@dataclass(frozen=True)
class SavedOrder:
    id: int  # internal row id
    number: int  # per-restaurant order number


@dataclass(frozen=True)
class RestaurantRow:
    id: int
    slug: str
    phone_number: str | None
    active: bool
    menu_id: int | None


class OrderStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        c = self._conn
        c.execute(
            """CREATE TABLE IF NOT EXISTS restaurants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                phone_number TEXT UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                menu_id INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS menus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
                created_at REAL NOT NULL,
                payload TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                call_id TEXT,
                customer_name TEXT,
                customer_phone TEXT,
                total REAL NOT NULL,
                payload TEXT NOT NULL
            )"""
        )
        # One row per phone call, placed order or not, plus what was said on it.
        c.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
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
        # A restaurant's link to its point of sale. `secret` holds the encrypted access and
        # refresh tokens (pos.py encrypts them with POS_SECRET_KEY); `settings` holds ids
        # picked at connect time (order type, tax rate).
        c.execute(
            """CREATE TABLE IF NOT EXISTS pos_connections (
                restaurant_id INTEGER PRIMARY KEY REFERENCES restaurants(id),
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
        # Restaurant staff who sign in to /manage. A user can belong to several restaurants.
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
            """CREATE TABLE IF NOT EXISTS user_restaurants (
                user_id INTEGER NOT NULL REFERENCES users(id),
                restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
                role TEXT NOT NULL DEFAULT 'manager',
                PRIMARY KEY (user_id, restaurant_id)
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
        # Unpublished menu edits, one per restaurant. base_menu_id is the live version the
        # draft started from, to catch someone else publishing in the meantime.
        c.execute(
            """CREATE TABLE IF NOT EXISTS menu_drafts (
                restaurant_id INTEGER PRIMARY KEY REFERENCES restaurants(id),
                payload TEXT NOT NULL,
                base_menu_id INTEGER,
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                updated_by TEXT
            )"""
        )
        # Databases created before payments / multiple restaurants get the new columns.
        self._add_columns("orders", _ORDER_COLUMNS)
        self._add_columns("calls", _CALL_COLUMNS)
        self._add_columns("menus", _MENU_COLUMNS)
        c.execute("UPDATE orders SET number = id WHERE number IS NULL")
        c.execute("CREATE INDEX IF NOT EXISTS orders_payment_id ON orders(payment_id)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_restaurant ON orders(restaurant_id, number)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_pos ON orders(pos_status, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS calls_restaurant ON calls(restaurant_id, started_at)")
        c.execute("CREATE INDEX IF NOT EXISTS call_events_call ON call_events(call_id, id)")
        c.commit()

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in columns.items():
            if col not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ---- restaurants and menus -------------------------------------------------------

    def add_restaurant(self, slug: str, phone_number: str | None, menu: dict) -> int:
        """Create a restaurant with its first menu. Returns the restaurant id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO restaurants (slug, phone_number, created_at) VALUES (?, ?, ?)",
                (slug, phone_number, time.time()),
            )
            rid = int(cur.lastrowid)
            self._insert_menu(rid, menu)
            self._conn.commit()
            return rid

    def set_menu(self, restaurant_id: int, menu: dict, created_by: str = "operator") -> int:
        """Publish a new menu version for the restaurant. Old versions are kept. Returns its id."""
        with self._lock:
            menu_id = self._insert_menu(restaurant_id, menu, created_by)
            self._conn.commit()
            return menu_id

    def _insert_menu(self, restaurant_id: int, menu: dict, created_by: str = "operator") -> int:
        cur = self._conn.execute(
            "INSERT INTO menus (restaurant_id, created_at, payload, created_by) VALUES (?, ?, ?, ?)",
            (restaurant_id, time.time(), json.dumps(menu, ensure_ascii=False), created_by),
        )
        menu_id = int(cur.lastrowid)
        self._conn.execute("UPDATE restaurants SET menu_id = ? WHERE id = ?", (menu_id, restaurant_id))
        return menu_id

    def update_restaurant(self, restaurant_id: int, *, phone_number: str | None = None,
                          clear_phone: bool = False, active: bool | None = None) -> None:
        with self._lock:
            if phone_number is not None or clear_phone:
                self._conn.execute("UPDATE restaurants SET phone_number = ? WHERE id = ?",
                                   (None if clear_phone else phone_number, restaurant_id))
            if active is not None:
                self._conn.execute("UPDATE restaurants SET active = ? WHERE id = ?",
                                   (int(active), restaurant_id))
            self._conn.commit()

    def adopt_unassigned(self, restaurant_id: int) -> tuple[int, int]:
        """Give orders and calls saved before restaurants existed to this restaurant."""
        with self._lock:
            o = self._conn.execute("UPDATE orders SET restaurant_id = ? WHERE restaurant_id IS NULL",
                                   (restaurant_id,)).rowcount
            c = self._conn.execute("UPDATE calls SET restaurant_id = ? WHERE restaurant_id IS NULL",
                                   (restaurant_id,)).rowcount
            self._conn.commit()
            return o, c

    _RESTAURANT_COLS = "id, slug, phone_number, active, menu_id"

    def _restaurant(self, where: str, value) -> RestaurantRow | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._RESTAURANT_COLS} FROM restaurants WHERE {where} = ?", (value,)
            ).fetchone()
        return RestaurantRow(row[0], row[1], row[2], bool(row[3]), row[4]) if row else None

    def restaurant(self, restaurant_id: int) -> RestaurantRow | None:
        return self._restaurant("id", restaurant_id)

    def restaurant_by_slug(self, slug: str) -> RestaurantRow | None:
        return self._restaurant("slug", slug)

    def restaurant_by_phone(self, phone_number: str) -> RestaurantRow | None:
        return self._restaurant("phone_number", phone_number)

    def restaurants(self) -> list[RestaurantRow]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._RESTAURANT_COLS} FROM restaurants ORDER BY id").fetchall()
        return [RestaurantRow(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]

    def menu_payload(self, menu_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM menus WHERE id = ?", (menu_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def menu_versions(self, restaurant_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, created_by, payload FROM menus WHERE restaurant_id = ?"
                " ORDER BY id DESC LIMIT ?", (restaurant_id, limit)).fetchall()
        return [{"id": r[0], "created_at": r[1], "created_by": r[2] or "operator",
                 "items": len(json.loads(r[3]).get("items", []))} for r in rows]

    def menu_version_payload(self, restaurant_id: int, menu_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM menus WHERE id = ? AND restaurant_id = ?",
                                     (menu_id, restaurant_id)).fetchone()
        return json.loads(row[0]) if row else None

    # ---- menu drafts -----------------------------------------------------------------

    def draft(self, restaurant_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, base_menu_id, revision, updated_at, updated_by FROM menu_drafts"
                " WHERE restaurant_id = ?", (restaurant_id,)).fetchone()
        if row is None:
            return None
        return {"menu": json.loads(row[0]), "base_menu_id": row[1], "revision": row[2],
                "updated_at": row[3], "updated_by": row[4]}

    def save_draft(self, restaurant_id: int, menu: dict, base_menu_id: int | None,
                   expected_revision: int | None, updated_by: str) -> int | None:
        """Save the draft if nobody else saved since `expected_revision` (None: there was
        no draft). Returns the new revision, or None on a conflict."""
        payload = json.dumps(menu, ensure_ascii=False)
        with self._lock:
            row = self._conn.execute("SELECT revision FROM menu_drafts WHERE restaurant_id = ?",
                                     (restaurant_id,)).fetchone()
            current = row[0] if row else None
            if current != expected_revision:
                return None
            if row is None:
                self._conn.execute(
                    "INSERT INTO menu_drafts (restaurant_id, payload, base_menu_id, revision, updated_at,"
                    " updated_by) VALUES (?, ?, ?, 1, ?, ?)",
                    (restaurant_id, payload, base_menu_id, time.time(), updated_by))
                new = 1
            else:
                new = current + 1
                self._conn.execute(
                    "UPDATE menu_drafts SET payload = ?, revision = ?, updated_at = ?, updated_by = ?"
                    " WHERE restaurant_id = ?", (payload, new, time.time(), updated_by, restaurant_id))
            self._conn.commit()
            return new

    def delete_draft(self, restaurant_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM menu_drafts WHERE restaurant_id = ?", (restaurant_id,))
            self._conn.commit()

    def publish_draft(self, restaurant_id: int, menu: dict, created_by: str) -> int:
        """Publish the menu and drop the draft, in one transaction."""
        with self._lock:
            menu_id = self._insert_menu(restaurant_id, menu, created_by)
            self._conn.execute("DELETE FROM menu_drafts WHERE restaurant_id = ?", (restaurant_id,))
            self._conn.commit()
            return menu_id

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
                f"SELECT id, email, name, password_hash, active FROM users WHERE {col} = ?", (value,)
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "email": row[1], "name": row[2], "password_hash": row[3], "active": bool(row[4])}

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

    def grant(self, user_id: int, restaurant_id: int, role: str = "manager") -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO user_restaurants (user_id, restaurant_id, role)"
                               " VALUES (?, ?, ?)", (user_id, restaurant_id, role))
            self._conn.commit()

    def revoke(self, user_id: int, restaurant_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM user_restaurants WHERE user_id = ? AND restaurant_id = ?",
                               (user_id, restaurant_id))
            self._conn.commit()

    def user_restaurant_ids(self, user_id: int) -> list[int]:
        with self._lock:
            rows = self._conn.execute("SELECT restaurant_id FROM user_restaurants WHERE user_id = ?"
                                      " ORDER BY restaurant_id", (user_id,)).fetchall()
        return [r[0] for r in rows]

    def users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.email, u.name, u.active, u.password_hash IS NOT NULL,"
                " GROUP_CONCAT(r.slug, ', ') FROM users u"
                " LEFT JOIN user_restaurants ur ON ur.user_id = u.id"
                " LEFT JOIN restaurants r ON r.id = ur.restaurant_id GROUP BY u.id ORDER BY u.id").fetchall()
        return [{"id": r[0], "email": r[1], "name": r[2], "active": bool(r[3]), "has_password": bool(r[4]),
                 "restaurants": r[5] or ""} for r in rows]

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
        rid = order.menu.restaurant_id
        with self._lock:
            (number,) = self._conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM orders WHERE restaurant_id IS ?", (rid,)
            ).fetchone()
            cur = self._conn.execute(
                "INSERT INTO orders (created_at, call_id, customer_name, customer_phone, total, payload,"
                " status, payment_status, restaurant_id, number, menu_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), call_id, s["customer_name"], s["customer_phone"], s["total"], json.dumps(s),
                 status, payment_status, rid, number, order.menu.menu_id),
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
            "SELECT id, number, restaurant_id, customer_phone, status, payment_status"
            " FROM orders WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        if row is None:
            return None
        return {"order_id": row[0], "order_number": row[1], "restaurant_id": row[2],
                "customer_phone": row[3], "status": row[4], "payment_status": row[5]}

    def recent(self, limit: int = 20, restaurant_id: int | None = None) -> list[dict]:
        """Newest orders first, for one restaurant or (restaurant_id None) all of them."""
        where, args = ("WHERE restaurant_id = ?", (restaurant_id,)) if restaurant_id is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, number, restaurant_id, created_at, call_id, payload, status, payment_status,"
                " payment_url, pos_status, pos_error FROM orders"
                f" {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
        out = []
        for r in rows:
            payload = json.loads(r[5])
            payload.pop("items", None)
            out.append({"id": r[0], "order_number": r[1], "restaurant_id": r[2], "created_at": r[3],
                        "call_id": r[4], **payload, "status": r[6], "payment_status": r[7],
                        "payment_url": r[8], "pos_status": r[9], "pos_error": r[10]})
        return out

    # ---- point of sale ----------------------------------------------------------

    def set_pos_connection(self, restaurant_id: int, provider: str, environment: str, secret: str,
                           merchant_id: str | None = None, location_id: str | None = None,
                           settings: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO pos_connections (restaurant_id, provider, environment, merchant_id,"
                " location_id, secret, settings, connected_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(restaurant_id) DO UPDATE SET provider = excluded.provider,"
                " environment = excluded.environment, merchant_id = excluded.merchant_id,"
                " location_id = excluded.location_id, secret = excluded.secret,"
                " settings = excluded.settings, connected_at = excluded.connected_at,"
                " updated_at = excluded.updated_at",
                (restaurant_id, provider, environment, merchant_id, location_id, secret,
                 json.dumps(settings or {}), now, now),
            )
            self._conn.commit()

    def update_pos_connection(self, restaurant_id: int, *, secret: str | None = None,
                              location_id: str | None = None, settings: dict | None = None) -> None:
        sets, args = ["updated_at = ?"], [time.time()]
        for col, val in (("secret", secret), ("location_id", location_id),
                         ("settings", None if settings is None else json.dumps(settings))):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        with self._lock:
            self._conn.execute(f"UPDATE pos_connections SET {', '.join(sets)} WHERE restaurant_id = ?",
                               (*args, restaurant_id))
            self._conn.commit()

    def pos_connection(self, restaurant_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT provider, environment, merchant_id, location_id, secret, settings, connected_at"
                " FROM pos_connections WHERE restaurant_id = ?", (restaurant_id,)
            ).fetchone()
        if row is None:
            return None
        return {"restaurant_id": restaurant_id, "provider": row[0], "environment": row[1],
                "merchant_id": row[2], "location_id": row[3], "secret": row[4],
                "settings": json.loads(row[5]), "connected_at": row[6]}

    def delete_pos_connection(self, restaurant_id: int) -> bool:
        with self._lock:
            n = self._conn.execute("DELETE FROM pos_connections WHERE restaurant_id = ?",
                                   (restaurant_id,)).rowcount
            self._conn.commit()
        return bool(n)

    def order_for_pos(self, order_id: int) -> dict | None:
        """Everything needed to send one order to a point of sale."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, number, restaurant_id, menu_id, created_at, customer_name, customer_phone,"
                " payload, status, payment_status, payment_id, pos_status, pos_order_id, pos_attempts,"
                " pos_paid FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "number": row[1], "restaurant_id": row[2], "menu_id": row[3],
                "created_at": row[4], "customer_name": row[5], "customer_phone": row[6],
                **{k: v for k, v in json.loads(row[7]).items()
                   if k in ("items", "subtotal", "tax", "total", "lines")},
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

    def orders_to_send(self, since: float, restaurant_id: int | None = None,
                       statuses: tuple[str, ...] = ("pending", "failed")) -> list[int]:
        """Ids of orders not yet on their POS, oldest first (for retrying after a restart)."""
        marks = ", ".join("?" * len(statuses))
        where = f"pos_status IN ({marks}) AND created_at >= ?"
        args: list = [*statuses, since]
        if restaurant_id is not None:
            where += " AND restaurant_id = ?"
            args.append(restaurant_id)
        with self._lock:
            rows = self._conn.execute(f"SELECT id FROM orders WHERE {where} ORDER BY id", args).fetchall()
        return [r[0] for r in rows]

    def order_id_by_number(self, restaurant_id: int, number: int) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM orders WHERE restaurant_id = ? AND number = ?",
                                     (restaurant_id, number)).fetchone()
        return row[0] if row else None

    # ---- calls and transcripts ----------------------------------------------------

    def start_call(self, call_id: str, caller_phone: str | None, restaurant_id: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO calls (call_id, started_at, caller_phone, restaurant_id)"
                " VALUES (?, ?, ?, ?)",
                (call_id, time.time(), caller_phone, restaurant_id),
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

    def recent_calls(self, limit: int = 50, restaurant_id: int | None = None) -> list[dict]:
        where, args = ("WHERE c.restaurant_id = ?", (restaurant_id,)) if restaurant_id is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.call_id, c.started_at, c.ended_at, c.caller_phone, c.order_number, c.restaurant_id,"
                " (SELECT COUNT(*) FROM call_events e WHERE e.call_id = c.call_id AND e.role = 'caller')"
                f" FROM calls c {where} ORDER BY c.started_at DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [{"call_id": r[0], "started_at": r[1], "ended_at": r[2], "caller_phone": r[3],
                 "order_number": r[4], "restaurant_id": r[5], "caller_turns": r[6]} for r in rows]

    def call_restaurant(self, call_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT restaurant_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return row[0] if row else None

    def transcript(self, call_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, role, text FROM call_events WHERE call_id = ? ORDER BY id", (call_id,)
            ).fetchall()
        return [{"at": r[0], "role": r[1], "text": r[2]} for r in rows]
