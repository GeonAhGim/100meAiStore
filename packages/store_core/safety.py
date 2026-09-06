"""Safe local DEMO stop controls and temporary SQLite backup verification."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .domain import Capability, DemoBackupManifest, DemoStopControl
from .errors import ConflictError


def set_demo_stop(service: Any, context: Any, scope_type: str, scope_ref: str, stopped: bool, reason: str) -> DemoStopControl:
    service.require(context, Capability.TENANT_ADMIN)
    if scope_type not in {"global", "tenant", "connection"} or not isinstance(scope_ref, str) or not scope_ref or type(stopped) is not bool or not isinstance(reason, str) or not reason.strip():
        raise ConflictError("invalid DEMO stop control")
    if scope_type == "global" and scope_ref != "global": raise ConflictError("global scope ref must be global")
    with service.repo.transaction():
        prior = next((row for row in service.repo.stop_controls_for(context.tenant_id) if (row.scope_type, row.scope_ref) == (scope_type, scope_ref)), None)
        value = service.repo.save_stop_control(DemoStopControl(context.tenant_id, scope_type, scope_ref, stopped, reason.strip(), prior.version + 1 if prior else 1, service._clock()))
        service._audit(context.tenant_id, context.user_id, "stop.control_changed", f"{scope_type}:{scope_ref}", "succeeded", {"stopped": stopped})
        return value


def backup_demo_sqlite(repo: Any, destination: str | Path, tenant_id: str) -> DemoBackupManifest:
    """Copy a SQLite DEMO ledger to a new path and verify it before recording evidence."""
    if not hasattr(repo, "connection") or not isinstance(destination, (str, Path)) or not isinstance(tenant_id, str) or not tenant_id:
        raise ConflictError("SQLite DEMO backup requires a local repository and tenant")
    target = Path(destination)
    if target.exists(): raise ConflictError("backup destination must be new")
    try:
        dest = sqlite3.connect(str(target))
        repo.connection.backup(dest)
        integrity = dest.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok": raise RuntimeError("integrity check failed")
        source_digest = hashlib.sha256("\n".join(repo.connection.iterdump()).encode()).hexdigest()
        dest.close()
    except Exception as exc:
        try: dest.close()
        except Exception: pass
        raise ConflictError("DEMO backup verification failed") from exc
    manifest = DemoBackupManifest(str(uuid4()), tenant_id, source_digest, int(repo.readiness()["schema_version"]), datetime.now(timezone.utc))
    with repo.transaction(): repo.save_backup_manifest(manifest)
    return manifest
