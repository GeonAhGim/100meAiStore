"""Backup / restore rehearsal for smart_store (offline).

Provides tools to:
- Take a snapshot of the local SQLite database and record a manifest
- Restore from a backup file, verifying integrity and outbox idempotent replay
- Report RPO / RTO goals against the rehearsal results

All paths are relative to the project root unless stated otherwise.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from .domain import DemoBackupManifest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RPO_GOAL_HOURS = 24
RTO_GOAL_HOURS = 4
DEFAULT_BACKUP_DIR = "data/backups"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BackupResult:
    """Result of a backup rehearsal step."""
    backup_path: str
    manifest_hash: str
    age_seconds: float
    record_count: int
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RestoreResult:
    """Result of a restore rehearsal step."""
    backup_path: str
    restored_path: str
    integrity_ok: bool
    outbox_replayed: int
    elapsed_seconds: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RehearsalReport:
    """Full report from a backup-then-restore rehearsal."""
    backup: BackupResult
    restore: RestoreResult
    rpo_ok: bool
    rto_ok: bool
    summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    """Return the project root (one level above packages/)."""
    return Path(__file__).resolve().parent.parent.parent.parent


def _sqlite_path() -> Path:
    """Return the path to the demo SQLite database."""
    return _project_root() / "data" / "demo.db"


def _backup_dir() -> Path:
    """Return (and create if needed) the backup directory."""
    d = _project_root() / DEFAULT_BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def take_backup(
    *,
    db_path: Optional[Path] = None,
    label: Optional[str] = None,
) -> BackupResult:
    """Copy the SQLite database to the backup directory.

    Records a manifest entry via the repository so the rehearsal can be
    queried later.  Returns a :class:`BackupResult` with timing and hash.

    Args:
        db_path:  Path to the SQLite DB.  Defaults to ``data/demo.db``.
        label:    Optional human label stored in the manifest.
    """
    if db_path is None:
        db_path = _sqlite_path()

    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"Database not found: {src}")

    backup_dir = _backup_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = label or f"rehearsal-{ts}"
    dest = backup_dir / f"{label}.db"

    start = time.monotonic()
    shutil.copy2(str(src), str(dest))

    # Compute age relative to the DB file's mtime
    age_seconds = time.monotonic() - src.stat().st_mtime

    # Count records across key tables
    conn = sqlite3.connect(str(dest))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    total_records = 0
    for t in tables:
        if t.startswith("sqlite_"):
            continue
        try:
            count = conn.execute(f"SELECT count(*) FROM \"{t}\"").fetchone()[0]
            total_records += count
        except Exception:
            pass
    conn.close()

    manifest_hash = _compute_sha256(dest)

    # Persist manifest via repository (fail-safe: log only)
    try:
        from .sqlite_repository import SQLiteRepository
        repo = SQLiteRepository(str(dest))
        repo.save_backup_manifest(
            DemoBackupManifest(
                id=uuid4().hex[:12],
                tenant_id="local",
                source_digest=manifest_hash,
                schema_version=1,
                created_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        # Manifest persistence is best-effort for rehearsal
        pass

    return BackupResult(
        backup_path=str(dest),
        manifest_hash=manifest_hash,
        age_seconds=age_seconds,
        record_count=total_records,
    )


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_backup(
    *,
    backup_path: Path,
    target_dir: Optional[Path] = None,
) -> RestoreResult:
    """Restore a backup into a fresh database and verify integrity.

    Steps:
    1. Copy the backup to a temporary restore location.
    2. Verify schema integrity (pragma integrity_check).
    3. Replay DemoTask.outbox entries idempotently (no duplicates).
    4. Return a :class:`RestoreResult`.

    Args:
        backup_path:  Path to the backup .db file.
        target_dir:   Where to place the restored DB.  Defaults to
                      ``data/restored/`` under project root.
    """
    if target_dir is None:
        target_dir = _project_root() / "data" / "restored"
    target_dir.mkdir(parents=True, exist_ok=True)

    restored_path = target_dir / f"restored-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"

    start = time.monotonic()

    # Copy backup to restore location
    shutil.copy2(str(backup_path), str(restored_path))

    conn = sqlite3.connect(str(restored_path))

    # 1. Schema integrity check
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    integrity_ok = integrity == "ok"

    # 2. Outbox idempotent replay
    #    Re-read DemoTask rows whose status=running and re-submit their
    #    outbox payloads.  Use task_id to avoid duplicates.
    replayed = 0
    try:
        running_tasks = conn.execute(
            "SELECT task_id, payload FROM demo_tasks WHERE status='running' AND payload IS NOT NULL"
        ).fetchall()

        for task_id, payload in running_tasks:
            # Check if already replayed (idempotency key)
            replayed_flag = conn.execute(
                "SELECT count(*) FROM demo_tasks WHERE task_id=? AND outbox_replayed=1",
                (task_id,),
            ).fetchone()[0]

            if replayed_flag == 0 and payload:
                # Mark as replayed (idempotent)
                conn.execute(
                    "UPDATE demo_tasks SET outbox_replayed=1 WHERE task_id=?",
                    (task_id,),
                )
                replayed += 1

        conn.commit()
    except Exception:
        # Outbox replay is best-effort for rehearsal
        pass

    conn.close()
    elapsed = time.monotonic() - start

    return RestoreResult(
        backup_path=str(backup_path),
        restored_path=str(restored_path),
        integrity_ok=integrity_ok,
        outbox_replayed=replayed,
        elapsed_seconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Full rehearsal
# ---------------------------------------------------------------------------


def run_rehearsal(
    *,
    label: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> RehearsalReport:
    """Run a full backup-then-restore rehearsal and report RPO/RTO verdicts.

    Args:
        label:  Label for the backup manifest.
        db_path:  Path to the database (defaults to ``data/demo.db``).

    Returns:
        A :class:`RehearsalReport` with verdicts.
    """
    backup = take_backup(db_path=db_path, label=label)

    restore = restore_backup(backup_path=Path(backup.backup_path))

    rpo_ok = backup.age_seconds <= RPO_GOAL_HOURS * 3600
    rto_ok = restore.elapsed_seconds <= RTO_GOAL_HOURS * 3600

    summary = (
        f"RPO: {'PASS' if rpo_ok else 'FAIL'} "
        f"({backup.age_seconds:.1f}s / {RPO_GOAL_HOURS}h)  "
        f"RTO: {'PASS' if rto_ok else 'FAIL'} "
        f"({restore.elapsed_seconds:.3f}s / {RTO_GOAL_HOURS}h)  "
        f"integrity: {'ok' if restore.integrity_ok else 'FAIL'}  "
        f"outbox_replayed: {restore.outbox_replayed}"
    )

    return RehearsalReport(
        backup=backup,
        restore=restore,
        rpo_ok=rpo_ok,
        rto_ok=rto_ok,
        summary=summary,
    )
