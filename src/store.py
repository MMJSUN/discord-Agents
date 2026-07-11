"""SQLite 持久化：sessions / tasks / costs / audit（SPEC §2 儲存層）。

批准狀態是任務層級（SPEC §5.1：不得跨任務沿用）；
costs 以本地日期記錄，配合每日預算 00:00 重置（SPEC §5.4）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "company.db"

AUDIT_SUMMARY_LIMIT = 200  # SPEC §5.3：參數摘要 ≤200 字

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    channel_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    plan TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS costs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    task_id INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    channel_id INTEGER,
    task_id INTEGER,
    agent TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    params_summary TEXT NOT NULL,
    decision TEXT NOT NULL
);
"""

# 任務狀態機：pending → approved/denied/expired；approved → running → done/failed
APPROVED_STATUSES = {"approved", "running"}


class Store:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = str(db_path or DB_PATH)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _today() -> str:
        return datetime.now().date().isoformat()

    # ---------- sessions（channel = 部門 = 一條 session） ----------

    def get_session(self, channel_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE channel_id=?", (channel_id,)
        ).fetchone()
        return row["session_id"] if row else None

    def set_session(self, channel_id: int, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions(channel_id, session_id, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET session_id=excluded.session_id, "
                "updated_at=excluded.updated_at",
                (channel_id, session_id, self._now()),
            )

    # ---------- tasks ----------

    def create_task(self, channel_id: int, description: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO tasks(channel_id, description, created_at) VALUES(?,?,?)",
                (channel_id, description, self._now()),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def set_plan(self, task_id: int, plan: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE tasks SET plan=? WHERE id=?", (plan, task_id))

    def set_status(self, task_id: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status=?, decided_at=? WHERE id=?",
                (status, self._now(), task_id),
            )

    def is_approved(self, task_id: int | None) -> bool:
        if task_id is None:
            return False
        row = self.get_task(task_id)
        return bool(row) and row["status"] in APPROVED_STATUSES

    def add_task_cost(self, task_id: int, amount_usd: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET cost_usd = cost_usd + ? WHERE id=?", (amount_usd, task_id)
            )

    # ---------- costs（每日預算的依據） ----------

    def record_cost(self, amount_usd: float, task_id: int | None = None, note: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO costs(date, amount_usd, task_id, note) VALUES(?,?,?,?)",
                (self._today(), amount_usd, task_id, note),
            )

    def cost_today(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM costs WHERE date=?",
            (self._today(),),
        ).fetchone()
        return float(row["total"])

    # ---------- audit ----------

    def add_audit(
        self,
        channel_id: int | None,
        task_id: int | None,
        agent: str,
        tool_name: str,
        params_summary: str,
        decision: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit(ts, channel_id, task_id, agent, tool_name, params_summary, decision) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    self._now(),
                    channel_id,
                    task_id,
                    agent,
                    tool_name,
                    params_summary[:AUDIT_SUMMARY_LIMIT],
                    decision,
                ),
            )

    def recent_audit(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
