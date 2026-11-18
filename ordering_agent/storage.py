"""Persistence for placed orders and call transcripts.

SQLite keeps the demo dependency-free. For multiple server instances, swap this
for a shared database (Postgres, etc.) or POST orders to your POS. Only placed
orders need shared storage: live call state stays on the call's WebSocket.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .order import Order

TRANSCRIPT_MAX_CHARS = 4000  # per transcript line; tool results can be long

# Order status: "confirmed" (kitchen can cook), "awaiting_payment" (restaurant requires
# payment first), "cancelled" (payment link expired while awaiting payment).
# Payment status: "not_requested" (pay at pickup), "unpaid", "paid", "expired".
_EXTRA_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'confirmed'",
    "payment_status": "TEXT NOT NULL DEFAULT 'not_requested'",
    "payment_id": "TEXT",
    "payment_url": "TEXT",
    "paid_at": "REAL",
}


class OrderStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
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
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(orders)")}
        for col, decl in _EXTRA_COLUMNS.items():
            if col not in have:  # migrate databases created before payments existed
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS orders_payment_id ON orders(payment_id)")
        # One row per phone call, placed order or not, plus what was said on it.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                ended_at REAL,
                caller_phone TEXT,
                order_number INTEGER
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                at REAL NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS call_events_call ON call_events(call_id, id)")
        self._conn.commit()

    def save(self, order: Order, call_id: str | None = None, status: str = "confirmed",
             payment_status: str = "not_requested") -> int:
        s = order.summary()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO orders (created_at, call_id, customer_name, customer_phone, total, payload,"
                " status, payment_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), call_id, s["customer_name"], s["customer_phone"], s["total"], json.dumps(s),
                 status, payment_status),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_payment_link(self, order_number: int, payment_id: str, url: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET payment_id = ?, payment_url = ? WHERE id = ?",
                               (payment_id, url, order_number))
            self._conn.commit()

    def update_status(self, order_number: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_number))
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
            "SELECT id, customer_phone, status, payment_status FROM orders WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        if row is None:
            return None
        return {"order_number": row[0], "customer_phone": row[1], "status": row[2],
                "payment_status": row[3]}

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, call_id, payload, status, payment_status, payment_url"
                " FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"order_number": r[0], "created_at": r[1], "call_id": r[2], **json.loads(r[3]),
             "status": r[4], "payment_status": r[5], "payment_url": r[6]}
            for r in rows
        ]

    # ---- calls and transcripts ----------------------------------------------------

    def start_call(self, call_id: str, caller_phone: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO calls (call_id, started_at, caller_phone) VALUES (?, ?, ?)",
                (call_id, time.time(), caller_phone),
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

    def recent_calls(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.call_id, c.started_at, c.ended_at, c.caller_phone, c.order_number,"
                " (SELECT COUNT(*) FROM call_events e WHERE e.call_id = c.call_id AND e.role = 'caller')"
                " FROM calls c ORDER BY c.started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"call_id": r[0], "started_at": r[1], "ended_at": r[2], "caller_phone": r[3],
                 "order_number": r[4], "caller_turns": r[5]} for r in rows]

    def transcript(self, call_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, role, text FROM call_events WHERE call_id = ? ORDER BY id", (call_id,)
            ).fetchall()
        return [{"at": r[0], "role": r[1], "text": r[2]} for r in rows]
