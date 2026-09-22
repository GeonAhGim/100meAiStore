"""P2-12 DEMO scope-bounded release policy. See
``docs/implementation/p2-12-demo-bounded-l4.md``.

Decides whether a proposed DEMO write may proceed; never writes. Fail-closed:
refuses to start unless G4 and G5 have DEMO approval records.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .channel_order_contracts import canonical_json
from .demo_discovery import approval_modes

REQUIRED_GATES = ("G4", "G5")
REASONS = ("halted", "scope_mismatch", "digest_mismatch", "approval_expired", "amount_cap", "sku_cap", "daily_cap")


class BoundedBlocked(PermissionError):
    """No usable approval record for a required gate."""


def payload_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _positive(name: str, value: Any, *, zero_ok: bool = False) -> int:
    if type(value) is not int or value < 0 or (value == 0 and not zero_ok):
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class BoundedPolicy:
    version: int
    capability: str
    max_amount_minor: int
    max_skus: int
    max_writes_per_day: int
    max_age_seconds: int
    halted: bool = False

    def __post_init__(self) -> None:
        _positive("version", self.version)
        _positive("max_amount_minor", self.max_amount_minor, zero_ok=True)
        _positive("max_skus", self.max_skus)
        _positive("max_writes_per_day", self.max_writes_per_day)
        _positive("max_age_seconds", self.max_age_seconds)
        if not self.capability:
            raise ValueError("capability required")


@dataclass(frozen=True)
class ScopedApproval:
    capability: str
    target_ref: str
    payload_digest: str
    approved_at: datetime
    approver: str


@dataclass(frozen=True)
class ProposedWrite:
    capability: str
    target_ref: str
    payload: dict = field(repr=False)
    amount_minor: int
    sku_count: int
    proposed_at: datetime


@dataclass(frozen=True)
class AuditRecord:
    sequence: int
    kind: str  # decision | halt | restore
    decision: str | None
    reasons: tuple[str, ...]
    capability: str
    target_ref: str | None
    payload_digest: str | None
    policy_version: int
    at: str


class BoundedGate:
    def __init__(self, approvals_dir: Path, policy: BoundedPolicy) -> None:
        modes = approval_modes(approvals_dir)
        missing = [g for g in REQUIRED_GATES if modes.get(g) != "DEMO"]
        if missing:
            raise BoundedBlocked("no DEMO approval record for " + ", ".join(missing))
        self.gates = tuple((g, modes[g]) for g in REQUIRED_GATES)
        self.policy = policy
        self._ledger: list[AuditRecord] = []
        self._admitted_days: dict[str, int] = {}

    @property
    def ledger(self) -> tuple[AuditRecord, ...]:
        return tuple(self._ledger)

    def _append(self, kind: str, decision: str | None, reasons: tuple[str, ...], capability: str,
                target_ref: str | None, digest: str | None, at: datetime) -> AuditRecord:
        record = AuditRecord(len(self._ledger) + 1, kind, decision, reasons, capability, target_ref, digest,
                             self.policy.version, at.isoformat())
        self._ledger.append(record)
        return record

    def decide(self, write: ProposedWrite, approval: ScopedApproval | None) -> AuditRecord:
        if write.proposed_at.tzinfo is None:
            raise ValueError("proposed_at must be timezone-aware")
        digest = payload_digest(write.payload)
        reasons: list[str] = []
        if self.policy.halted:
            reasons.append("halted")
        if approval is None or (approval.capability, approval.target_ref) != (write.capability, write.target_ref) \
                or write.capability != self.policy.capability:
            reasons.append("scope_mismatch")
        elif approval.payload_digest != digest:
            reasons.append("digest_mismatch")
        elif (write.proposed_at - approval.approved_at).total_seconds() > self.policy.max_age_seconds \
                or write.proposed_at < approval.approved_at:
            reasons.append("approval_expired")
        if write.amount_minor > self.policy.max_amount_minor:
            reasons.append("amount_cap")
        if write.sku_count > self.policy.max_skus:
            reasons.append("sku_cap")
        day = write.proposed_at.date().isoformat()
        if self._admitted_days.get(day, 0) + 1 > self.policy.max_writes_per_day:
            reasons.append("daily_cap")
        ordered = tuple(r for r in REASONS if r in reasons)
        decision = "admit" if not ordered else "reject"
        if decision == "admit":
            self._admitted_days[day] = self._admitted_days.get(day, 0) + 1
        return self._append("decision", decision, ordered, write.capability, write.target_ref, digest, write.proposed_at)

    def halt(self, at: datetime, operator: str) -> AuditRecord:
        self.policy = replace(self.policy, halted=True)
        return self._append("halt", None, (f"operator:{operator}",), self.policy.capability, None, None, at)

    def restore(self, at: datetime, operator: str) -> AuditRecord:
        self.policy = replace(self.policy, halted=False)
        return self._append("restore", None, (f"operator:{operator}",), self.policy.capability, None, None, at)

    def evidence(self) -> str:
        counts = {"admit": 0, "reject": 0, "halt": 0, "restore": 0}
        for r in self._ledger:
            counts[r.decision or r.kind] += 1
        ledger_digest = hashlib.sha256(canonical_json([asdict(r) for r in self._ledger]).encode("utf-8")).hexdigest()
        return canonical_json({"mode": "DEMO", "gates": self.gates, "policy_version": self.policy.version,
                               "halted": self.policy.halted, "counts": counts, "ledger_digest": ledger_digest,
                               "records": len(self._ledger)})
