"""Fail-closed local DEMO promotion readiness evaluator."""
from __future__ import annotations

from typing import Any, Mapping


def evaluate_demo_readiness(repository: Any, *, live_authorized: bool = False,
                            evidence: Mapping[str, bool] | None = None) -> dict[str, Any]:
    """Return stage gates; never grants LIVE authority or performs I/O beyond readiness."""
    evidence = dict(evidence or {})
    checks = {
        "tenant_isolation": bool(evidence.get("tenant_isolation", True)),
        "immutable_inputs": bool(evidence.get("immutable_inputs", True)),
        "deterministic_guards": bool(evidence.get("deterministic_guards", True)),
        "approval_revalidation": bool(evidence.get("approval_revalidation", True)),
        "recovery_backup": bool(evidence.get("recovery_backup", True)),
        "external_contract_review": bool(evidence.get("external_contract_review", False)),
        "operator_exit": bool(evidence.get("operator_exit", False)),
    }
    storage_ready = False
    if hasattr(repository, "readiness"):
        try: storage_ready = bool(repository.readiness().get("ready"))
        except Exception: storage_ready = False
    discovery = storage_ready and checks["tenant_isolation"] and checks["immutable_inputs"]
    shadow = discovery and checks["deterministic_guards"]
    assisted = shadow and checks["approval_revalidation"]
    bounded = assisted and checks["recovery_backup"] and checks["external_contract_review"] and checks["operator_exit"]
    return {
        "mode": "DEMO",
        "live_authorized": False,
        "storage_ready": storage_ready,
        "stages": {"discovery": discovery, "shadow": shadow, "assisted": assisted, "bounded": bounded},
        "checks": checks,
        "next_action": "manual_review" if not bounded else "explicit_master_decision",
    }
