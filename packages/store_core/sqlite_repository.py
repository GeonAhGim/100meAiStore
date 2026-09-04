from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .domain import (
    Approval, ApprovalKind, ApprovalState, AuditEvent, Command, CommandState,
    Membership, OutboxEvent, OutboxState, Role, Tenant, User,
)
from .errors import ConflictError, NotFoundError, TenantBoundaryError


MIGRATIONS = ((1, """
CREATE TABLE tenants(id TEXT PRIMARY KEY, legal_name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE memberships(tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, roles_json TEXT NOT NULL,
 active INTEGER NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(tenant_id,user_id));
CREATE TABLE commands(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, target_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, supersedes_id TEXT, UNIQUE(tenant_id,idempotency_key));
CREATE INDEX commands_tenant_id_id ON commands(tenant_id,id);
CREATE TABLE approvals(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
 decided_by TEXT, decision_reason TEXT, UNIQUE(tenant_id,command_id));
CREATE INDEX approvals_tenant_command ON approvals(tenant_id,command_id);
CREATE TABLE audit_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL, actor_ref TEXT NOT NULL, action TEXT NOT NULL, target_ref TEXT NOT NULL, outcome TEXT NOT NULL,
 correlation_id TEXT NOT NULL, metadata_json TEXT NOT NULL, prev_hash TEXT, event_hash TEXT NOT NULL);
CREATE INDEX audit_tenant_sequence ON audit_events(tenant_id,sequence);
CREATE TABLE outbox(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, topic TEXT NOT NULL, aggregate_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 checkpoint_json TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 completed_at TEXT, UNIQUE(tenant_id,idempotency_key));
CREATE INDEX outbox_tenant_state ON outbox(tenant_id,state,created_at);
"""),(2, """
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN available_at TEXT;
ALTER TABLE outbox ADD COLUMN last_error TEXT;
CREATE INDEX outbox_claimable ON outbox(tenant_id,state,available_at,created_at);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
"""),(3, """
PRAGMA defer_foreign_keys=ON;
CREATE TABLE memberships_v3(
 tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, roles_json TEXT NOT NULL, active INTEGER NOT NULL, version INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,user_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT);
INSERT INTO memberships_v3 SELECT * FROM memberships;

CREATE TABLE commands_v3(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, target_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, supersedes_id TEXT,
 UNIQUE(tenant_id,idempotency_key), UNIQUE(tenant_id,id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,supersedes_id) REFERENCES commands_v3(tenant_id,id) DEFERRABLE INITIALLY DEFERRED);
INSERT INTO commands_v3 SELECT * FROM commands;

CREATE TABLE approvals_v3(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
 decided_by TEXT, decision_reason TEXT, UNIQUE(tenant_id,command_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,command_id) REFERENCES commands_v3(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(decided_by) REFERENCES users(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,decided_by) REFERENCES memberships_v3(tenant_id,user_id) ON DELETE RESTRICT);
INSERT INTO approvals_v3 SELECT * FROM approvals;

CREATE TABLE audit_events_v3(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL, actor_ref TEXT NOT NULL, action TEXT NOT NULL, target_ref TEXT NOT NULL, outcome TEXT NOT NULL,
 correlation_id TEXT NOT NULL, metadata_json TEXT NOT NULL, prev_hash TEXT, event_hash TEXT NOT NULL,
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
INSERT INTO audit_events_v3 SELECT * FROM audit_events;

CREATE TABLE outbox_v3(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, topic TEXT NOT NULL, aggregate_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 checkpoint_json TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 completed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT, last_error TEXT,
 UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
INSERT INTO outbox_v3(rowid,id,tenant_id,topic,aggregate_ref,payload_json,idempotency_key,state,created_at,
 checkpoint_json,lease_owner,lease_until,fencing_token,completed_at,attempts,available_at,last_error)
 SELECT rowid,id,tenant_id,topic,aggregate_ref,payload_json,idempotency_key,state,created_at,
 checkpoint_json,lease_owner,lease_until,fencing_token,completed_at,attempts,available_at,last_error FROM outbox;

DROP TABLE approvals;
DROP TABLE memberships;
DROP TABLE audit_events;
DROP TABLE outbox;
DROP TABLE commands;
ALTER TABLE commands_v3 RENAME TO commands;
ALTER TABLE memberships_v3 RENAME TO memberships;
ALTER TABLE approvals_v3 RENAME TO approvals;
ALTER TABLE audit_events_v3 RENAME TO audit_events;
ALTER TABLE outbox_v3 RENAME TO outbox;
CREATE INDEX commands_tenant_id_id ON commands(tenant_id,id);
CREATE INDEX approvals_tenant_command ON approvals(tenant_id,command_id);
CREATE INDEX audit_tenant_sequence ON audit_events(tenant_id,sequence);
CREATE INDEX outbox_tenant_state ON outbox(tenant_id,state,created_at);
CREATE INDEX outbox_claimable ON outbox(tenant_id,state,available_at,created_at);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
"""))
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]

MAX_ERROR_LENGTH = 500
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token|password|secret)(\s*[:=]\s*)([^\s,;]+)")


def _safe_error(value: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)[:MAX_ERROR_LENGTH]


def _backoff_seconds(attempts: int) -> int:
    return min(3600, 30 * (2 ** max(0, attempts - 1)))


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteRepository:
    """Durable DEMO adapter. Every aggregate query includes a tenant predicate.

    One instance/connection is owned by one thread. Concurrency uses independent
    instances; ``_depth`` tracks savepoints only within that connection.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._depth = 0
        try:
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {r[0] for r in self.connection.execute("SELECT version FROM schema_migrations")}
        if applied and max(applied) > LATEST_SCHEMA_VERSION:
            raise ConflictError(
                f"database schema {max(applied)} is newer than supported {LATEST_SCHEMA_VERSION}"
            )
        for version, sql in MIGRATIONS:
            if version not in applied:
                # executescript commits implicitly unless transaction control is
                # embedded in the script. Keep DDL and its version marker atomic.
                applied_at = datetime.now().astimezone().isoformat().replace("'", "''")
                script = (
                    "BEGIN IMMEDIATE;\n" + sql + "\n"
                    f"INSERT INTO schema_migrations VALUES ({version},'{applied_at}');\n"
                    "COMMIT;"
                )
                try:
                    self.connection.executescript(script)
                except Exception:
                    if self.connection.in_transaction:
                        self.connection.execute("ROLLBACK")
                    raise

    def readiness(self, expected_schema_version: int = LATEST_SCHEMA_VERSION) -> dict[str, object]:
        """Fail-closed storage readiness check with no external side effects."""
        versions = [row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")]
        actual = max(versions, default=0)
        if actual != expected_schema_version or actual > LATEST_SCHEMA_VERSION:
            raise ConflictError(
                f"schema version mismatch: expected {expected_schema_version}, found {actual}"
            )
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ConflictError(f"sqlite integrity check failed: {integrity}")
        foreign_keys = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ConflictError("sqlite foreign key check failed")
        return {"ready": True, "schema_version": actual, "integrity": integrity}

    @contextmanager
    def transaction(self) -> Iterator[SQLiteRepository]:
        savepoint = f"uow_{self._depth}"
        if self._depth == 0:
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            self.connection.execute(f"SAVEPOINT {savepoint}")
        self._depth += 1
        try:
            yield self
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.connection.execute("ROLLBACK")
            else:
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                self.connection.execute(f"RELEASE {savepoint}")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.connection.execute("COMMIT")
            else:
                self.connection.execute(f"RELEASE {savepoint}")

    def add_tenant(self, value: Tenant) -> None:
        self.connection.execute("INSERT INTO tenants VALUES (?,?,?)", (value.id, value.legal_name, value.created_at.isoformat()))

    def add_user(self, value: User) -> None:
        try:
            self.connection.execute("INSERT INTO users VALUES (?,?,?)", (value.id, value.email, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("email already registered") from exc

    def find_user_by_email(self, email: str) -> User | None:
        row = self.connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return User(row["id"], row["email"], _dt(row["created_at"])) if row else None  # type: ignore[arg-type]

    def get_user(self, user_id: str) -> User:
        row = self.connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row: raise NotFoundError("user not found")
        return User(row["id"], row["email"], _dt(row["created_at"]))  # type: ignore[arg-type]

    def save_membership(self, value: Membership) -> None:
        self.connection.execute("""INSERT INTO memberships VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,user_id) DO UPDATE SET
 roles_json=excluded.roles_json,active=excluded.active,version=excluded.version""",
            (value.tenant_id,value.user_id,json.dumps(sorted(r.value for r in value.roles)),int(value.active),value.version))

    def get_membership(self, tenant_id: str, user_id: str) -> Membership:
        row = self.connection.execute("SELECT * FROM memberships WHERE tenant_id=? AND user_id=?", (tenant_id,user_id)).fetchone()
        if not row: raise NotFoundError("membership not found")
        return Membership(row["tenant_id"],row["user_id"],frozenset(Role(v) for v in json.loads(row["roles_json"])),bool(row["active"]),row["version"])

    def tenant_memberships(self, tenant_id: str):
        rows = self.connection.execute("SELECT user_id FROM memberships WHERE tenant_id=?", (tenant_id,)).fetchall()
        return tuple(self.get_membership(tenant_id,row["user_id"]) for row in rows)

    def save_command(self, c: Command) -> None:
        self.connection.execute("""INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
 state=excluded.state,supersedes_id=excluded.supersedes_id,payload_json=excluded.payload_json,payload_digest=excluded.payload_digest""",
            (c.id,c.tenant_id,c.kind.value,c.target_ref,json.dumps(c.payload,ensure_ascii=False,sort_keys=True),c.payload_digest,c.idempotency_key,c.state.value,c.created_at.isoformat(),c.supersedes_id))

    def get_command(self, tenant_id: str, command_id: str) -> Command:
        row = self.connection.execute("SELECT * FROM commands WHERE tenant_id=? AND id=?",(tenant_id,command_id)).fetchone()
        if not row:
            if self.connection.execute("SELECT 1 FROM commands WHERE id=?",(command_id,)).fetchone(): raise TenantBoundaryError("cross-tenant command access denied")
            raise NotFoundError("command not found")
        return Command(row["id"],row["tenant_id"],ApprovalKind(row["kind"]),row["target_ref"],json.loads(row["payload_json"]),row["payload_digest"],row["idempotency_key"],CommandState(row["state"]),_dt(row["created_at"]),row["supersedes_id"])  # type: ignore[arg-type]

    def command_id_for_key(self, tenant_id: str, key: str) -> str | None:
        row=self.connection.execute("SELECT id FROM commands WHERE tenant_id=? AND idempotency_key=?",(tenant_id,key)).fetchone()
        return row["id"] if row else None

    def bind_command_key(self, tenant_id: str, idempotency_key: str, command_id: str) -> None:
        # The unique binding is stored with the command row.
        if self.command_id_for_key(tenant_id,idempotency_key) != command_id: raise ConflictError("command idempotency binding mismatch")

    def save_approval(self, a: Approval) -> None:
        self.connection.execute("""INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
 state=excluded.state,decided_by=excluded.decided_by,decision_reason=excluded.decision_reason""",
            (a.id,a.tenant_id,a.command_id,a.kind.value,a.state.value,a.requested_at.isoformat(),a.expires_at.isoformat(),json.dumps(a.evidence,ensure_ascii=False,sort_keys=True),a.decided_by,a.decision_reason))

    def get_approval_for_command(self, tenant_id: str, command_id: str) -> Approval:
        self.get_command(tenant_id,command_id)
        r=self.connection.execute("SELECT * FROM approvals WHERE tenant_id=? AND command_id=?",(tenant_id,command_id)).fetchone()
        if not r: raise NotFoundError("approval not found")
        return Approval(r["id"],r["tenant_id"],r["command_id"],ApprovalKind(r["kind"]),ApprovalState(r["state"]),_dt(r["requested_at"]),_dt(r["expires_at"]),tuple(json.loads(r["evidence_json"])),r["decided_by"],r["decision_reason"])  # type: ignore[arg-type]

    def append_audit(self, e: AuditEvent) -> None:
        self.connection.execute("INSERT INTO audit_events(id,tenant_id,occurred_at,actor_ref,action,target_ref,outcome,correlation_id,metadata_json,prev_hash,event_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e.id,e.tenant_id,e.occurred_at.isoformat(),e.actor_ref,e.action,e.target_ref,e.outcome,e.correlation_id,json.dumps(e.metadata,ensure_ascii=False,sort_keys=True),e.prev_hash,e.event_hash))

    def audits_for(self, tenant_id: str) -> tuple[AuditEvent,...]:
        rows=self.connection.execute("SELECT * FROM audit_events WHERE tenant_id=? ORDER BY sequence",(tenant_id,)).fetchall()
        return tuple(AuditEvent(r["id"],r["tenant_id"],_dt(r["occurred_at"]),r["actor_ref"],r["action"],r["target_ref"],r["outcome"],r["correlation_id"],json.loads(r["metadata_json"]),r["prev_hash"],r["event_hash"]) for r in rows)  # type: ignore[arg-type]

    def append_outbox(self, e: OutboxEvent) -> None:
        try:
            self.connection.execute("""INSERT INTO outbox
            (id,tenant_id,topic,aggregate_ref,payload_json,idempotency_key,state,created_at,checkpoint_json,
             lease_owner,lease_until,fencing_token,completed_at,attempts,available_at,last_error)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(e.id,e.tenant_id,e.topic,e.aggregate_ref,json.dumps(e.payload,ensure_ascii=False,sort_keys=True),e.idempotency_key,e.state.value,e.created_at.isoformat(),json.dumps(e.checkpoint,ensure_ascii=False,sort_keys=True),e.lease_owner,e.lease_until.isoformat() if e.lease_until else None,e.fencing_token,e.completed_at.isoformat() if e.completed_at else None,e.attempts,(e.available_at or e.created_at).isoformat(),e.last_error))
        except sqlite3.IntegrityError as exc: raise ConflictError("outbox idempotency key already exists") from exc

    def _outbox(self, r: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(r["id"],r["tenant_id"],r["topic"],r["aggregate_ref"],json.loads(r["payload_json"]),r["idempotency_key"],OutboxState(r["state"]),_dt(r["created_at"]),json.loads(r["checkpoint_json"]),r["lease_owner"],_dt(r["lease_until"]),r["fencing_token"],_dt(r["completed_at"]),r["attempts"],_dt(r["available_at"]),r["last_error"])  # type: ignore[arg-type]

    def outbox_for(self, tenant_id: str) -> tuple[OutboxEvent,...]:
        return tuple(self._outbox(r) for r in self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? ORDER BY created_at,id",(tenant_id,)))

    def claim_outbox(self, tenant_id: str, event_id: str, worker_id: str, now: datetime, lease_until: datetime) -> OutboxEvent:
        with self.transaction():
            r=self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? AND id=?",(tenant_id,event_id)).fetchone()
            if not r:
                if self.connection.execute("SELECT 1 FROM outbox WHERE id=?",(event_id,)).fetchone(): raise TenantBoundaryError("cross-tenant outbox access denied")
                raise NotFoundError("outbox event not found")
            e=self._outbox(r)
            if e.state in {OutboxState.COMPLETED,OutboxState.DEAD}: raise ConflictError("outbox event is terminal")
            if e.state==OutboxState.RETRY and e.available_at and e.available_at>now: raise ConflictError("outbox event is not available")
            if e.lease_until and e.lease_until>now: raise ConflictError("outbox event already leased")
            token,attempts=e.fencing_token+1,e.attempts+1
            self.connection.execute("UPDATE outbox SET state=?,lease_owner=?,lease_until=?,fencing_token=?,attempts=?,last_error=NULL WHERE tenant_id=? AND id=?",(OutboxState.LEASED.value,worker_id,lease_until.isoformat(),token,attempts,tenant_id,event_id))
            e.state,e.lease_owner,e.lease_until,e.fencing_token,e.attempts,e.last_error=OutboxState.LEASED,worker_id,lease_until,token,attempts,None
            return e

    def claim_next_outbox(self, tenant_id: str, worker_id: str, now: datetime, lease_until: datetime) -> OutboxEvent | None:
        with self.transaction():
            row=self.connection.execute("""SELECT o.id FROM outbox o WHERE o.tenant_id=? AND
             ((o.state IN (?,?) AND COALESCE(o.available_at,o.created_at)<=?) OR (o.state=? AND o.lease_until<=?))
             AND NOT EXISTS (SELECT 1 FROM outbox prior WHERE prior.tenant_id=o.tenant_id
               AND prior.aggregate_ref=o.aggregate_ref AND prior.rowid<o.rowid AND prior.state NOT IN (?,?))
             ORDER BY o.rowid LIMIT 1""",
             (tenant_id,OutboxState.PENDING.value,OutboxState.RETRY.value,now.isoformat(),OutboxState.LEASED.value,now.isoformat(),OutboxState.COMPLETED.value,OutboxState.DEAD.value)).fetchone()
            return self.claim_outbox(tenant_id,row["id"],worker_id,now,lease_until) if row else None

    def checkpoint_outbox(self, tenant_id: str, event_id: str, worker_id: str, fencing_token: int, checkpoint: dict, now: datetime, completed: bool=False) -> OutboxEvent:
        with self.transaction():
            r=self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? AND id=?",(tenant_id,event_id)).fetchone()
            if not r: raise NotFoundError("outbox event not found")
            e=self._outbox(r)
            if e.state!=OutboxState.LEASED or e.lease_owner!=worker_id or e.fencing_token!=fencing_token or not e.lease_until or e.lease_until<=now: raise ConflictError("stale or expired outbox lease")
            state=OutboxState.COMPLETED if completed else OutboxState.LEASED
            self.connection.execute("UPDATE outbox SET checkpoint_json=?,state=?,completed_at=?,lease_owner=?,lease_until=? WHERE tenant_id=? AND id=?",(json.dumps(checkpoint,sort_keys=True),state.value,now.isoformat() if completed else None,None if completed else worker_id,None if completed else e.lease_until.isoformat(),tenant_id,event_id))
            e.checkpoint,e.state=dict(checkpoint),state
            if completed: e.completed_at,e.lease_owner,e.lease_until=now,None,None
            return e

    def fail_outbox(self, tenant_id: str, event_id: str, worker_id: str, fencing_token: int, error: str, now: datetime, max_attempts: int = 5) -> OutboxEvent:
        if max_attempts < 1: raise ValueError("max_attempts must be positive")
        with self.transaction():
            r=self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? AND id=?",(tenant_id,event_id)).fetchone()
            if not r: raise NotFoundError("outbox event not found")
            e=self._outbox(r)
            if e.state!=OutboxState.LEASED or e.lease_owner!=worker_id or e.fencing_token!=fencing_token or not e.lease_until or e.lease_until<=now: raise ConflictError("stale or expired outbox lease")
            state=OutboxState.DEAD if e.attempts>=max_attempts else OutboxState.RETRY
            from datetime import timedelta
            available=now if state==OutboxState.DEAD else now+timedelta(seconds=_backoff_seconds(e.attempts))
            safe=_safe_error(str(error))
            self.connection.execute("UPDATE outbox SET state=?,available_at=?,last_error=?,lease_owner=NULL,lease_until=NULL WHERE tenant_id=? AND id=?",(state.value,available.isoformat(),safe,tenant_id,event_id))
            e.state,e.available_at,e.last_error,e.lease_owner,e.lease_until=state,available,safe,None,None
            return e
