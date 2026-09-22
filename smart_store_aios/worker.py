from __future__ import annotations

import logging
import subprocess
import time
import uuid
from pathlib import Path

from .config import Settings
from .db import LeaseError, StoreDB
from .blocked_triage import BlockedTriage
from .dev_pipeline import DevPipeline, PermanentFailure, UsageLimited, codex_executable

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = StoreDB(settings.database)
        self.worker_id = str(uuid.uuid4())
        self.dev_runner = None  # tests inject a fake subprocess runner
        self.repo_root = Path.cwd()

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
            result = self._dispatch(job)
        except UsageLimited as exc:
            try:
                self.db.defer(job["id"], self.worker_id, self.settings.dev_usage_limit_delay_seconds, str(exc))
            except LeaseError as lost:
                log.warning("job %s: %s", job["id"], lost)
            else:
                log.warning("job %s deferred: %s", job["id"], exc)
            return True
        except Exception as exc:  # noqa: BLE001 - job isolation boundary
            permanent = isinstance(exc, (PermanentFailure, ValueError, KeyError, TypeError))
            try:
                status = self.db.fail(job["id"], self.worker_id, str(exc), self.settings.max_attempts,
                                      permanent=permanent)
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
        """Poll until ``stop()`` is truthy. Sleeps ``poll_seconds`` after an empty claim.

        When ``dev.triage_interval_seconds`` is positive and the queue is idle, a
        ``blocked.triage`` job is enqueued at that interval so blocked progress
        items keep receiving offline preparation work without an operator.
        """
        last_triage = 0.0
        while not (stop and stop()):
            if self.run_once():
                continue
            interval = self.settings.dev_triage_interval_seconds
            if interval > 0 and time.monotonic() - last_triage >= interval:
                last_triage = time.monotonic()
                live = self.db.find_job("blocked.triage", "blocked-triage")
                if not live or live["status"] in {"done", "dead"}:
                    self.db.enqueue("blocked.triage", {"task_id": "blocked-triage", "auto": True})
                    continue
            time.sleep(poll_seconds)

    def _dispatch(self, job: dict) -> dict:
        kind, payload = job["kind"], job["payload"]
        if kind == "blocked.triage":
            return BlockedTriage(self.db, self.repo_root).run()
        if kind == "dev.task":
            kwargs = {"runner": self.dev_runner} if self.dev_runner else {}
            return DevPipeline(self.settings, self.db, job, self.worker_id, **kwargs).run()
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
            codex_executable(), "exec", "-C", str(Path.cwd()), "--sandbox", self.settings.codex_sandbox,
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

