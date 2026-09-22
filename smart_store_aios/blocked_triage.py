"""Turn blocked progress items into gate-free preparation work.

A progress item is ``blocked`` when it needs a human approval gate (G1..G5 in
``docs/implementation/live-phase2-backlog.md``). The worker pool cannot grant
a gate and must never pretend to. What it can do is prepare everything the
gate does not cover: draft the approval packet, and implement and test the
offline part against synthetic fixtures. This module enqueues one bounded
``dev.task`` per blocked item and records the link in
``data/blocked-triage.json`` so the development dashboard can show
"blocked, offline preparation in progress" with the real stage instead of a
bare "blocked".

The item's status in ``development-progress.json`` is never changed here.
Only a human moves a gated item out of ``blocked``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .db import StoreDB

GATE_PACKETS = {
    "G1": "channel access packet: selected seller/channel, API groups, exact read endpoints and interval, "
          "IP/hosting, data fields, expiry, masked evidence location",
    "G2": "supplier/finance packet: chosen business/contract, authorized sample field map, data rights, "
          "return/stock/cost terms, manual confirmation responsibilities",
    "G3": "legal/product packet: accountable owner, dated law, controller/processor roles, lawful purpose, "
          "retention/legal hold, cross-border flows, license",
    "G4": "write packet: exact capability, proposed payload digest, money/SKU/count/time limits, "
          "approval expiry, verification and compensation",
    "G5": "operations packet: selected deployment, quoted monthly cost, RPO/RTO, identities, "
          "restore evidence and rollback",
}
TRIAGE_FILE = Path("data") / "blocked-triage.json"
ALLOWED_PREP_PATHS = ["packages/store_core/", "docs/implementation/", "tests/"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prep_task_for(item: dict) -> dict:
    """Build the dev.task payload that prepares a blocked item without touching its gate."""
    item_id = str(item["id"])
    gates = [g.strip() for g in str(item.get("approval_gate") or "").split("/") if g.strip()]
    packets = [f"{g}: {GATE_PACKETS[g]}" for g in gates if g in GATE_PACKETS]
    title = str(item.get("title") or item_id)
    acceptance = [
        f"docs/implementation/{item_id.lower()}-approval-packet.md lists every required field of "
        f"{', '.join(gates) or 'the gate'} as filled from repository evidence or explicitly unresolved",
        f"The offline part of '{title}' is implemented against synthetic fixtures with no external read or write",
        "Tests prove the gated capability is fail-closed until an approval record exists",
    ]
    goal = (
        f"Prepare '{title}' ({item_id}) for its approval gate without exercising the gate. "
        f"Write the approval packet draft(s): {'; '.join(packets) or 'per live-phase2-backlog.md'}. "
        f"Implement only what needs no external access, using synthetic fixtures. "
        f"Never register credentials, call a marketplace, supplier, payment or hosting API, or change "
        f"the item's status in development-progress.json."
    )
    return {"task_id": f"prep-{item_id.lower()}", "title": f"Offline preparation for {item_id}",
            "goal": goal, "acceptance": acceptance, "files": ALLOWED_PREP_PATHS, "source_item": item_id}


class BlockedTriage:
    def __init__(self, db: StoreDB, project_root: Path) -> None:
        self.db = db
        self.project_root = Path(project_root)
        self.progress_path = self.project_root / "docs" / "implementation" / "development-progress.json"
        self.state_path = self.project_root / TRIAGE_FILE

    def blocked_items(self) -> list[dict]:
        try:
            items = json.loads(self.progress_path.read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            return []
        return [x for x in items if isinstance(x, dict) and x.get("status") == "blocked" and isinstance(x.get("id"), str)]

    def load_state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def run(self) -> dict:
        """Enqueue preparation for every blocked item that has no live prep job. Idempotent."""
        state = self.load_state()
        entries = state.get("items", {}) if isinstance(state.get("items"), dict) else {}
        enqueued, kept = [], []
        for item in self.blocked_items():
            payload = prep_task_for(item)
            live = self.db.find_job("dev.task", payload["task_id"])
            if live and live["status"] != "dead":
                entries[item["id"]] = {**entries.get(item["id"], {}), "job_id": live["id"], "task_id": payload["task_id"],
                                       "gate": item.get("approval_gate"), "updated_at": _now()}
                kept.append(item["id"])
                continue
            job_id = self.db.enqueue("dev.task", payload)
            entries[item["id"]] = {"job_id": job_id, "task_id": payload["task_id"], "gate": item.get("approval_gate"),
                                   "enqueued_at": _now(), "updated_at": _now(),
                                   "previous_dead_job": live["id"] if live else None}
            enqueued.append(item["id"])
        state = {"database": str(self.db.path), "updated_at": _now(), "items": entries,
                 "note": "Gates are never granted by the worker; only offline preparation is automated."}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return {"enqueued": enqueued, "kept": kept, "state": str(self.state_path)}


def offline_prep_status(project_root: Path) -> dict[str, dict]:
    """Read-only view for the dashboard: item id -> prep job status/stage. Never raises."""
    triage = BlockedTriage.__new__(BlockedTriage)
    triage.state_path = Path(project_root) / TRIAGE_FILE
    state = triage.load_state()
    entries = state.get("items", {}) if isinstance(state.get("items"), dict) else {}
    database = state.get("database")
    out: dict[str, dict] = {}
    db = StoreDB(database) if isinstance(database, str) and Path(database).exists() else None
    for item_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        view = {"task_id": entry.get("task_id"), "job_id": entry.get("job_id"), "status": "unknown", "stage": None,
                "commit": None, "spec": None, "last_error": None}
        job = db.job(entry["job_id"]) if db and isinstance(entry.get("job_id"), int) else None
        if job:
            checkpoint = job.get("checkpoint") or {}
            view.update({"status": job["status"], "stage": checkpoint.get("stage"), "commit": checkpoint.get("commit"),
                         "spec": checkpoint.get("spec"), "last_error": (job.get("last_error") or "")[:160] or None,
                         "attempts": job.get("attempts")})
        out[str(item_id)] = view
    return out
