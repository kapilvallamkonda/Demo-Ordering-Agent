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
        self._conn.commit()

    def save(self, order: Order, call_id: str | None = None) -> int:
        s = order.summary()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO orders (created_at, call_id, customer_name, customer_phone, total, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), call_id, s["customer_name"], s["customer_phone"], s["total"], json.dumps(s)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, call_id, payload FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"order_number": r[0], "created_at": r[1], "call_id": r[2], **json.loads(r[3])} for r in rows
        ]
