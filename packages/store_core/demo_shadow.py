"""P2-11 DEMO read-only Shadow comparison. See
``docs/implementation/p2-11-demo-shadow-l4.md``.

Read-only by construction: the run receives already-parsed snapshots and
returns a report. It never performs an external read or write. Fail-closed:
it refuses to start unless G1, G3 and G5 have DEMO approval records.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .channel_order_contracts import OfflineOrderPage, OfflineOrderSnapshot, canonical_json
from .demo_discovery import approval_modes

REQUIRED_GATES = ("G1", "G3", "G5")
MIN_WINDOW, MAX_WINDOW = 60, 24 * 3600


class ShadowBlocked(PermissionError):
    """No usable approval record for a required gate."""


@dataclass(frozen=True)
class LineComparison:
    provider: str
    line_id: str
    classes: tuple[str, ...]
    verified: bool

    @property
    def match(self) -> bool:
        return self.classes == ("match",)


@dataclass(frozen=True)
class OperatorException:
    kind: str
    provider: str
    line_id: str | None
    detail: str


@dataclass(frozen=True)
class ShadowReport:
    mode: str
    gates: tuple[tuple[str, str], ...]
    provider: str
    polling_window_seconds: int
    age_seconds: int
    stale: bool
    pages_compared: int
    operation_digest: str
    channel_digest: str
    lines: tuple[LineComparison, ...]
    exceptions: tuple[OperatorException, ...]
    decision: str
    rollback: str = "none_required"
    digest: str = field(default="", init=False)

    def __post_init__(self) -> None:
        body = asdict(self)
        body.pop("digest")
        object.__setattr__(self, "digest", hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest())

    def canonical_payload(self) -> str:
        return canonical_json(asdict(self))


def require_demo_gates(approvals_dir: Path) -> tuple[tuple[str, str], ...]:
    modes = approval_modes(approvals_dir)
    missing = [g for g in REQUIRED_GATES if modes.get(g) != "DEMO"]
    if missing:
        raise ShadowBlocked("no DEMO approval record for " + ", ".join(missing))
    return tuple((g, modes[g]) for g in REQUIRED_GATES)


def _key(snapshot: OfflineOrderSnapshot) -> tuple[str, str]:
    return snapshot.provider, snapshot.line_id


def _digest(snapshots: Iterable[OfflineOrderSnapshot]) -> str:
    return hashlib.sha256(canonical_json([asdict(s) for s in snapshots]).encode("utf-8")).hexdigest()


def compare_line(operation: OfflineOrderSnapshot, channel: OfflineOrderSnapshot) -> tuple[tuple[str, ...], tuple[OperatorException, ...]]:
    """Classify drift between the two sides of one line. Details name fields and values only."""
    classes: list[str] = []
    exceptions: list[OperatorException] = []
    provider, line_id = _key(channel)
    if operation.status != channel.status:
        classes.append("status_drift")
        exceptions.append(OperatorException("status_drift", provider, line_id, f"status {operation.status}->{channel.status}"))
    if (operation.initial_quantity, operation.remaining_quantity) != (channel.initial_quantity, channel.remaining_quantity):
        classes.append("quantity_drift")
        exceptions.append(OperatorException(
            "quantity_drift", provider, line_id,
            f"quantity {operation.initial_quantity}/{operation.remaining_quantity}->{channel.initial_quantity}/{channel.remaining_quantity}"))
    if dict(operation.amounts_krw) != dict(channel.amounts_krw):
        changed = sorted(k for k in set(dict(operation.amounts_krw)) | set(dict(channel.amounts_krw))
                         if dict(operation.amounts_krw).get(k) != dict(channel.amounts_krw).get(k))
        classes.append("amount_drift")
        exceptions.append(OperatorException("amount_drift", provider, line_id, "amounts " + ",".join(changed)))
    return tuple(classes) or ("match",), tuple(exceptions)


def run_demo_shadow(*, approvals_dir: Path, operation: Iterable[OfflineOrderSnapshot], channel: OfflineOrderPage,
                    observed_at: datetime, now: datetime, polling_window_seconds: int) -> ShadowReport:
    gates = require_demo_gates(approvals_dir)
    if type(polling_window_seconds) is not int or not MIN_WINDOW <= polling_window_seconds <= MAX_WINDOW:
        raise ValueError("polling window must be an int between 60 and 86400 seconds")
    if observed_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("observed_at and now must be timezone-aware")
    age = int((now - observed_at).total_seconds())
    if age < 0:
        raise ValueError("observed_at is in the future")
    stale = age > polling_window_seconds

    operation = tuple(operation)
    op_by_key = {_key(s): s for s in operation}
    ch_by_key = {_key(s): s for s in channel.snapshots}
    if len(op_by_key) != len(operation) or len(ch_by_key) != len(channel.snapshots):
        raise ValueError("duplicate line id on one side")

    lines: list[LineComparison] = []
    exceptions: list[OperatorException] = []
    if stale:
        exceptions.append(OperatorException("stale_page", channel.provider, None,
                                            f"age {age}s exceeds window {polling_window_seconds}s"))
    for key in sorted(set(op_by_key) | set(ch_by_key)):
        provider, line_id = key
        if key not in ch_by_key:
            lines.append(LineComparison(provider, line_id, ("missing_in_channel",), not stale))
            exceptions.append(OperatorException("missing_in_channel", provider, line_id, "line absent from channel page"))
            continue
        if key not in op_by_key:
            lines.append(LineComparison(provider, line_id, ("missing_in_operation",), not stale))
            exceptions.append(OperatorException("missing_in_operation", provider, line_id, "line absent from existing operation"))
            continue
        classes, found = compare_line(op_by_key[key], ch_by_key[key])
        lines.append(LineComparison(provider, line_id, classes, not stale))
        exceptions.extend(found)

    missing = any("missing_in_channel" in l.classes or "missing_in_operation" in l.classes for l in lines)
    if stale or missing:
        decision = "exit"
    elif exceptions:
        decision = "hold"
    else:
        decision = "continue"
    return ShadowReport("DEMO", gates, channel.provider, polling_window_seconds, age, stale, 1,
                        _digest(operation), channel.source_digest, tuple(lines), tuple(exceptions), decision)
