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
}
_CALL_COLUMNS = {"restaurant_id": "INTEGER"}


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
        # Databases created before payments / multiple restaurants get the new columns.
        self._add_columns("orders", _ORDER_COLUMNS)
        self._add_columns("calls", _CALL_COLUMNS)
        c.execute("UPDATE orders SET number = id WHERE number IS NULL")
        c.execute("CREATE INDEX IF NOT EXISTS orders_payment_id ON orders(payment_id)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_restaurant ON orders(restaurant_id, number)")
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

    def set_menu(self, restaurant_id: int, menu: dict) -> int:
        """Publish a new menu version for the restaurant. Old versions are kept. Returns its id."""
        with self._lock:
            menu_id = self._insert_menu(restaurant_id, menu)
            self._conn.commit()
            return menu_id

    def _insert_menu(self, restaurant_id: int, menu: dict) -> int:
        cur = self._conn.execute(
            "INSERT INTO menus (restaurant_id, created_at, payload) VALUES (?, ?, ?)",
            (restaurant_id, time.time(), json.dumps(menu, ensure_ascii=False)),
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

    # ---- orders ------------------------------------------------------------------

    def save(self, order: Order, call_id: str | None = None, status: str = "confirmed",
             payment_status: str = "not_requested") -> SavedOrder:
        s = order.summary()
        rid = order.menu.restaurant_id
        with self._lock:
            (number,) = self._conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM orders WHERE restaurant_id IS ?", (rid,)
            ).fetchone()
            cur = self._conn.execute(
                "INSERT INTO orders (created_at, call_id, customer_name, customer_phone, total, payload,"
                " status, payment_status, restaurant_id, number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), call_id, s["customer_name"], s["customer_phone"], s["total"], json.dumps(s),
                 status, payment_status, rid, number),
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
                f" payment_url FROM orders {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [
            {"id": r[0], "order_number": r[1], "restaurant_id": r[2], "created_at": r[3], "call_id": r[4],
             **json.loads(r[5]), "status": r[6], "payment_status": r[7], "payment_url": r[8]}
            for r in rows
        ]

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
