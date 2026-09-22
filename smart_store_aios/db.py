from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  leased_until TEXT,
  worker_id TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
  ON jobs(status, available_at, leased_until);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT NOT NULL,
  entity_id TEXT,
  details TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class LeaseError(RuntimeError):
    """Raised when a worker acts on a job whose lease it no longer owns."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoreDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def enqueue(self, kind: str, payload: dict) -> int:
        now = utcnow().isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(kind,payload,available_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                (kind, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            return int(cursor.lastrowid)

    def claim(self, worker_id: str, lease_seconds: int) -> dict | None:
        now = utcnow()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM jobs
                WHERE available_at <= ? AND (status='queued' OR (status='running' AND leased_until < ?))
                ORDER BY id LIMIT 1""",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, leased_until=?, worker_id=?, updated_at=? WHERE id=?",
                (lease, worker_id, now.isoformat(), row["id"]),
            )
            connection.execute("COMMIT")
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def _owned_running(self, connection: sqlite3.Connection, job_id: int, worker_id: str, now: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            raise LeaseError(f"job {job_id} not found")
        if row["status"] != "running" or row["worker_id"] != worker_id or (row["leased_until"] or "") <= now:
            connection.execute("ROLLBACK")
            raise LeaseError(f"job {job_id} lease is stale or owned by another worker")
        return row

    def complete(self, job_id: int, worker_id: str, event: str, details: dict) -> None:
        now = utcnow().isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_running(connection, job_id, worker_id, now)
            connection.execute(
                "UPDATE jobs SET status='done', leased_until=NULL, updated_at=? WHERE id=?",
                (now, job_id),
            )
            connection.execute(
                "INSERT INTO audit_log(event,entity_id,details,created_at) VALUES(?,?,?,?)",
                (event, str(job_id), json.dumps(details, ensure_ascii=False), now),
            )
            connection.execute("COMMIT")

    def fail(self, job_id: int, worker_id: str, message: str, max_attempts: int) -> str:
        """Release a lease after a failure. Returns the new status ('queued' or 'dead')."""
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_running(connection, job_id, worker_id, now.isoformat())
            attempts = int(row["attempts"])
            status = "dead" if attempts >= max_attempts else "queued"
            delay = min(2 ** attempts, 60)
            available = now if status == "dead" else now + timedelta(seconds=delay)
            connection.execute(
                "UPDATE jobs SET status=?, available_at=?, leased_until=NULL, last_error=?, updated_at=? WHERE id=?",
                (status, available.isoformat(), message[:2000], now.isoformat(), job_id),
            )
            connection.execute(
                "INSERT INTO audit_log(event,entity_id,details,created_at) VALUES(?,?,?,?)",
                ("job.failed", str(job_id),
                 json.dumps({"status": status, "attempts": attempts, "error": message[:500]}, ensure_ascii=False),
                 now.isoformat()),
            )
            connection.execute("COMMIT")
            return status

    def stats(self) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")]

