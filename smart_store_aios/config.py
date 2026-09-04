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

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        config_path = Path(path).resolve()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        profit = ProfitPolicy(**raw.get("profit", {}))
        workers = raw.get("workers", {})
        codex = raw.get("codex", {})
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
        )

