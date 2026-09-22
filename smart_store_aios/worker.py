from __future__ import annotations

import logging
import subprocess
import time
import uuid
from pathlib import Path

from .config import Settings
from .db import LeaseError, StoreDB

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = StoreDB(settings.database)
        self.worker_id = str(uuid.uuid4())

    def run_once(self) -> bool:
        """Claim and process one job. Returns False when nothing was claimable.

        A job failure is recorded on the job (retry or dead) and never
        propagates, so a daemon loop survives bad jobs. A lost lease is logged
        and the job is left to whichever worker now owns it.
        """
        job = self.db.claim(self.worker_id, self.settings.lease_seconds)
        if job is None:
            return False
        try:
            result = self._dispatch(job["kind"], job["payload"])
        except Exception as exc:  # noqa: BLE001 - job isolation boundary
            try:
                status = self.db.fail(job["id"], self.worker_id, str(exc), self.settings.max_attempts)
            except LeaseError as lost:
                log.warning("job %s: %s", job["id"], lost)
            else:
                log.warning("job %s failed (%s): %s", job["id"], status, exc)
            return True
        try:
            self.db.complete(job["id"], self.worker_id, f"{job['kind']}.completed", result)
        except LeaseError as lost:
            log.warning("job %s: %s", job["id"], lost)
        return True

    def run_forever(self, poll_seconds: float = 2.0, stop=None) -> None:
        """Poll until ``stop()`` is truthy. Sleeps ``poll_seconds`` after an empty claim."""
        while not (stop and stop()):
            if not self.run_once():
                time.sleep(poll_seconds)

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
        timeout = max(1, self.settings.lease_seconds - 5)
        try:
            completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex exec exceeded {timeout}s lease budget") from exc
        if completed.returncode:
            raise RuntimeError(completed.stderr or completed.stdout)
        return {"returncode": completed.returncode, "output": str(output_path)}

