"""P2-13 offline traceability, restart and readiness audit. See
``docs/implementation/p2-13-final-audit-l4.md``.

Reads repository files and a throwaway DEMO database only. Holds no gate
authority. Fail-closed: any exception yields ``ready=False``.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .channel_order_contracts import canonical_json
from .demo_discovery import approval_modes
from .domain import OutboxState
from .execution import DemoExecutionControlPlane
from .sqlite_repository import SQLiteRepository

ALL_GATES = ("G1", "G2", "G3", "G4", "G5")
_ID_ROW = re.compile(r"^\|\s*([A-Z][A-Z0-9-]*-\d{2,})\s*\|", re.MULTILINE)


@dataclass(frozen=True)
class ItemTrace:
    item_id: str
    status: str
    evidence: str | None
    acceptance_ids: tuple[str, ...]
    untested_ids: tuple[str, ...]
    gaps: tuple[str, ...]

    @property
    def traced(self) -> bool:
        return not self.gaps


@dataclass(frozen=True)
class RestartCheck:
    passed: bool
    events: int
    leased: int
    detail: str


@dataclass(frozen=True)
class Readiness:
    live_approved: bool
    gates: tuple[tuple[str, str], ...]
    not_live: tuple[str, ...]
    statement: str


@dataclass(frozen=True)
class AuditReport:
    generated_at: str
    items: tuple[ItemTrace, ...]
    restart: RestartCheck | None
    readiness: Readiness | None
    error: str | None
    digest: str = field(default="", init=False)

    def __post_init__(self) -> None:
        body = asdict(self)
        body.pop("digest")
        object.__setattr__(self, "digest", hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest())

    @property
    def gaps(self) -> tuple[ItemTrace, ...]:
        return tuple(i for i in self.items if not i.traced)

    @property
    def ready(self) -> bool:
        return (self.error is None and not self.gaps and self.restart is not None and self.restart.passed
                and self.readiness is not None)

    def canonical_payload(self) -> str:
        return canonical_json(asdict(self))


def acceptance_ids(document: Path) -> tuple[str, ...]:
    text = document.read_text(encoding="utf-8", errors="replace")
    return tuple(dict.fromkeys(m.group(1) for m in _ID_ROW.finditer(text) if m.group(1) != "ID"))


def _tests_corpus(root: Path) -> str:
    tests = root / "tests"
    if not tests.exists():
        return ""
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tests.rglob("*.py"))


def trace_items(root: Path, progress_path: Path | None = None) -> tuple[ItemTrace, ...]:
    progress = json.loads((progress_path or root / "docs" / "implementation" / "development-progress.json")
                          .read_text(encoding="utf-8"))
    corpus = _tests_corpus(root)
    out = []
    for item in progress.get("items", []):
        item_id, status = str(item.get("id")), str(item.get("status"))
        evidence = item.get("evidence")
        gaps: list[str] = []
        ids: tuple[str, ...] = ()
        untested: tuple[str, ...] = ()
        doc = root / evidence if isinstance(evidence, str) else None
        if doc is None or not doc.is_file():
            gaps.append("evidence_missing")
        else:
            ids = acceptance_ids(doc)
            if not ids:
                gaps.append("no_acceptance_table")
            else:
                untested = tuple(i for i in ids if i not in corpus)
                if untested:
                    gaps.append("untested_ids")
        if status == "completed" and gaps:
            gaps.append("status_overclaims")
        out.append(ItemTrace(item_id, status, evidence if isinstance(evidence, str) else None, ids, untested, tuple(gaps)))
    return tuple(out)


def restart_check() -> RestartCheck:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        path = Path(directory) / "audit.sqlite3"
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        repo = SQLiteRepository(path)
        service = DemoExecutionControlPlane(repo, lambda: now)
        context = service.bootstrap_tenant("audit", "audit@example.test")
        tenant = context.tenant_id
        repo.close()
        repo = SQLiteRepository(path)  # simulated worker restart on the same file
        events = repo.outbox_for(tenant)
        leased = sum(1 for e in events if e.state == OutboxState.LEASED)
        readable = service.__class__(repo, lambda: now).bootstrap_tenant  # class still constructible
        repo.close()
        detail = "reopened database has no leased outbox events" if leased == 0 else "leased events survived restart"
        return RestartCheck(leased == 0 and readable is not None, len(events), leased, detail)


def readiness(approvals_dir: Path) -> Readiness:
    modes = approval_modes(approvals_dir)
    gates = tuple((g, modes.get(g, "missing")) for g in ALL_GATES)
    not_live = tuple(g for g, m in gates if m != "LIVE")
    approved = not not_live
    statement = ("LIVE approved for all gates" if approved
                 else "LIVE is not approved: " + ", ".join(f"{g}={dict(gates)[g]}" for g in not_live))
    return Readiness(approved, gates, not_live, statement)


def run_final_audit(root: Path) -> AuditReport:
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        items = trace_items(root)
        restart = restart_check()
        ready = readiness(root / "docs" / "implementation" / "approvals")
        return AuditReport(stamp, items, restart, ready, None)
    except Exception as exc:  # noqa: BLE001 - fail closed with the error class
        return AuditReport(stamp, (), None, None, f"{type(exc).__name__}: {exc}"[:300])


def main(argv: list[str] | None = None) -> int:
    """``python -m packages.store_core.final_audit [root]``: exit 0 only when the audit is ready.

    Prints the remaining gaps one per line, so a caller (the control plane's
    milestone done check) can hand them to the next attempt.
    """
    import sys
    args = sys.argv[1:] if argv is None else argv
    report = run_final_audit(Path(args[0]) if args else Path("."))
    print(f"ready={report.ready} gaps={len(report.gaps)}/{len(report.items)}"
          + (f" error={report.error}" if report.error else ""))
    for item in report.gaps:
        detail = f" untested: {', '.join(item.untested_ids)}" if item.untested_ids else ""
        print(f"- {item.item_id} ({item.evidence}): {', '.join(item.gaps)}{detail}")
    if report.restart is not None and not report.restart.passed:
        print(f"- restart: {report.restart.detail}")
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
