"""Persistence for placed orders.

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
