from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProfitPolicy:
    weekly_target_krw: int = 5_000_000
    minimum_contribution_margin_rate: float = 0.18
    return_reserve_rate: float = 0.05
    ad_cost_rate: float = 0.10
    marketplace_fee_rate: float = 0.06


@dataclass(frozen=True)
class Settings:
    database: Path
    dry_run: bool
    profit: ProfitPolicy
    lease_seconds: int = 300
    max_attempts: int = 3
    codex_enabled: bool = False
    codex_sandbox: str = "workspace-write"
    dev_max_fix_rounds: int = 3
    dev_stage_timeout_seconds: int = 1800
    dev_test_timeout_seconds: int = 900
    dev_test_command: tuple[str, ...] = ("python", "-m", "unittest", "discover", "-s", "tests", "-t", ".")
    dev_push: bool = False
    dev_usage_limit_delay_seconds: int = 1800
    dev_triage_interval_seconds: int = 0
    dev_model: str = "codex"  # "codex" runs the CLI; "manual" expects the worktree to be prepared by hand

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        config_path = Path(path).resolve()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        profit = ProfitPolicy(**raw.get("profit", {}))
        workers = raw.get("workers", {})
        codex = raw.get("codex", {})
        dev = raw.get("dev", {})
        database = Path(raw.get("database", "data/store.db"))
        if not database.is_absolute():
            database = config_path.parent / database
        return cls(
            database=database,
            dry_run=bool(raw.get("dry_run", True)),
            profit=profit,
            lease_seconds=int(workers.get("lease_seconds", 300)),
            max_attempts=int(workers.get("max_attempts", 3)),
            codex_enabled=bool(codex.get("enabled", False)),
            codex_sandbox=str(codex.get("sandbox", "workspace-write")),
            dev_max_fix_rounds=int(dev.get("max_fix_rounds", 3)),
            dev_stage_timeout_seconds=int(dev.get("stage_timeout_seconds", 1800)),
            dev_test_timeout_seconds=int(dev.get("test_timeout_seconds", 900)),
            dev_test_command=tuple(dev.get("test_command") or cls.dev_test_command),
            dev_push=bool(dev.get("push", False)),
            dev_usage_limit_delay_seconds=int(dev.get("usage_limit_delay_seconds", 1800)),
            dev_triage_interval_seconds=int(dev.get("triage_interval_seconds", 0)),
            dev_model=str(dev.get("model", "codex")),
        )

