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
  checkpoint TEXT,
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
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "checkpoint" not in columns:  # databases created before dev.task support
                connection.execute("ALTER TABLE jobs ADD COLUMN checkpoint TEXT")

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
            try:
                result["payload"] = json.loads(result["payload"])
                result["checkpoint"] = json.loads(result["checkpoint"]) if result.get("checkpoint") else {}
            except json.JSONDecodeError as exc:
                # One corrupt row must not stop the queue: dead-letter it in place.
                connection.execute(
                    "UPDATE jobs SET status='dead', leased_until=NULL, last_error=?, updated_at=? WHERE id=?",
                    (f"corrupt job row: {exc}", now.isoformat(), row["id"]))
                return self.claim(worker_id, lease_seconds)
            return result

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int) -> None:
        """Extend the caller's lease. Raises LeaseError if the lease was lost."""
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_running(connection, job_id, worker_id, now.isoformat())
            connection.execute(
                "UPDATE jobs SET leased_until=?, updated_at=? WHERE id=?",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), job_id))
            connection.execute("COMMIT")

    def checkpoint(self, job_id: int, worker_id: str, data: dict) -> None:
        """Persist resumable progress for the job the caller currently leases."""
        now = utcnow().isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_running(connection, job_id, worker_id, now)
            connection.execute("UPDATE jobs SET checkpoint=?, updated_at=? WHERE id=?",
                               (json.dumps(data, ensure_ascii=False, sort_keys=True), now, job_id))
            connection.execute("COMMIT")

    def defer(self, job_id: int, worker_id: str, delay_seconds: int, reason: str) -> None:
        """Re-queue without consuming an attempt (backend throttled, not a job fault)."""
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_running(connection, job_id, worker_id, now.isoformat())
            connection.execute(
                "UPDATE jobs SET status='queued', attempts=attempts-1, leased_until=NULL, available_at=?, last_error=?, updated_at=? WHERE id=?",
                ((now + timedelta(seconds=delay_seconds)).isoformat(), reason[:2000], now.isoformat(), job_id))
            connection.execute("COMMIT")

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

    def fail(self, job_id: int, worker_id: str, message: str, max_attempts: int, *, permanent: bool = False) -> str:
        """Release a lease after a failure. Returns the new status ('queued' or 'dead').

        ``permanent`` dead-letters immediately (invalid payload, exhausted fix budget).
        """
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_running(connection, job_id, worker_id, now.isoformat())
            attempts = int(row["attempts"])
            status = "dead" if permanent or attempts >= max_attempts else "queued"
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

    def withdraw(self, job_id: int, reason: str) -> bool:
        """Dead-letter a job that no worker has claimed yet; False when it is already running or finished.

        The requester takes the work back (the control plane hands an unclaimed
        dev.task to Claude Code), so a worker started later must not run it too.
        """
        now = utcnow().isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE jobs SET status='dead', last_error=?, updated_at=? WHERE id=? AND status='queued'",
                (reason[:2000], now, job_id)).rowcount
            if changed:
                connection.execute("INSERT INTO audit_log(event,entity_id,details,created_at) VALUES(?,?,?,?)",
                                   ("job.withdrawn", str(job_id), json.dumps({"reason": reason[:500]}, ensure_ascii=False), now))
            connection.execute("COMMIT")
        return bool(changed)

    def job(self, job_id: int) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "checkpoint"):
            try:
                result[key] = json.loads(result[key]) if result.get(key) else {}
            except json.JSONDecodeError:
                result[key] = {}
        return result

    def find_job(self, kind: str, task_id: str) -> dict | None:
        """Most recent job of ``kind`` whose payload.task_id matches, or None."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM jobs WHERE kind=? AND json_extract(payload,'$.task_id')=? ORDER BY id DESC LIMIT 1",
                (kind, task_id)).fetchone()
        return self.job(int(row["id"])) if row else None

    def stats(self) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")]

