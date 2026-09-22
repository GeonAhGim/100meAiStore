from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from .config import Settings
from .db import StoreDB


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = StoreDB(settings.database)
        self.worker_id = str(uuid.uuid4())

    def run_once(self) -> bool:
        job = self.db.claim(self.worker_id, self.settings.lease_seconds)
        if job is None:
            return False
        try:
            result = self._dispatch(job["kind"], job["payload"])
            self.db.complete(job["id"], f"{job['kind']}.completed", result)
        except Exception as exc:
            self.db.fail(job["id"], str(exc), self.settings.max_attempts)
            raise
        return True

    def _dispatch(self, kind: str, payload: dict) -> dict:
        if kind == "codex.task":
            return self._run_codex(payload)
        if kind in {"catalog.scan", "listing.publish", "inventory.sync", "orders.sync", "profit.snapshot"}:
            return {"dry_run": self.settings.dry_run, "accepted": True, "payload": payload}
        raise ValueError(f"unsupported job kind: {kind}")

    def _run_codex(self, payload: dict) -> dict:
        if not self.settings.codex_enabled:
            return {"dry_run": True, "skipped": "codex.enabled=false"}
        prompt = str(payload["prompt"])
        output_path = Path(payload.get("output", "data/codex-last-message.txt")).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "codex", "exec", "-C", str(Path.cwd()), "--sandbox", self.settings.codex_sandbox,
            "--output-last-message", str(output_path), prompt,
        ]
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        if completed.returncode:
            raise RuntimeError(completed.stderr or completed.stdout)
        return {"returncode": completed.returncode, "output": str(output_path)}

