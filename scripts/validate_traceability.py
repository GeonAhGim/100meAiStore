"""Fail-closed validator for the atomic product-requirement traceability ledger."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "implementation" / "requirements-traceability.json"
SOURCE = ROOT / "docs" / "100meAiStore-requirements-v1.md"
REQUIRED_FIELDS = (
    "architecture", "code_service", "repository", "api_ui", "worker_outbox",
    "recovery", "tests", "evidence", "status", "external_gate",
)
ALLOWED_STATUSES = {"completed", "partial", "planned", "approval_gated"}
ID_PATTERN = re.compile(r"^R(0[1-7])-(\d{2})$")
EXPECTED_COUNT = 43
FIELD_PREFIXES = {
    "architecture": ("docs/architecture/",),
    "code_service": ("packages/", "smart_store_aios/", "scripts/"),
    "repository": ("packages/", "smart_store_aios/", ".gitignore"),
    "api_ui": ("packages/", "smart_store_aios/"),
    "worker_outbox": ("packages/", "smart_store_aios/", "scripts/"),
    "recovery": ("docs/", "packages/", "smart_store_aios/", "scripts/"),
    "tests": ("tests/", ".github/workflows/"),
    "evidence": ("docs/implementation/", "docs/architecture/"),
}


def source_requirements(path: Path = SOURCE) -> list[tuple[str, str]]:
    """Return stable IDs and exact bullet text for the seven requirement sections."""
    section = 0
    item = 0
    found: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            section += 1
            item = 0
            if section > 7:  # architecture deliverables are not atomic product requirements
                break
        elif section and raw.startswith("- "):
            item += 1
            found.append((f"R{section:02d}-{item:02d}", raw[2:]))
    return found


def _references(value: Any, field: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        errors.append(f"{field} must be a non-empty string list")
        return []
    return value


def _safe_regular_file(root: Path, ref: str, prefixes: tuple[str, ...]) -> bool:
    """Accept only repository-relative regular files under the field's namespace.

    Resolving the path prevents ``..`` and symlink escapes.  Rejecting every
    symlink component also keeps evidence immutable with respect to link swaps.
    """
    candidate = Path(ref)
    portable = candidate.as_posix()
    prefix_allowed = any(
        portable.startswith(prefix) if prefix.endswith("/") else portable == prefix
        for prefix in prefixes
    )
    if candidate.is_absolute() or ".." in candidate.parts or not prefix_allowed:
        return False
    root_resolved = root.resolve()
    unresolved = root / candidate
    try:
        resolved = unresolved.resolve(strict=True)
        if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def validate_manifest(path: Path = DEFAULT_MANIFEST, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest cannot be read: {exc}"]
    items = document.get("requirements") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return ["requirements must be a list"]
    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if document.get("source") != "docs/100meAiStore-requirements-v1.md":
        errors.append("source must identify the canonical requirements file")
    if not isinstance(document.get("policy"), str) or not document["policy"].strip():
        errors.append("policy must be a non-empty string")

    expected = source_requirements(root / "docs" / "100meAiStore-requirements-v1.md")
    expected_by_id = dict(expected)
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"requirements[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        req_id = item.get("id")
        if not isinstance(req_id, str) or not ID_PATTERN.fullmatch(req_id):
            errors.append(f"{label}.id is invalid")
            continue
        if req_id in seen:
            errors.append(f"duplicate requirement id: {req_id}")
        seen.add(req_id)
        if req_id not in expected_by_id:
            errors.append(f"unknown requirement id: {req_id}")
        elif item.get("requirement") != expected_by_id[req_id]:
            errors.append(f"{req_id} text does not match the source requirement")
        for field in REQUIRED_FIELDS:
            if field not in item:
                errors.append(f"{req_id} missing field: {field}")
        status = item.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{req_id} has invalid status: {status!r}")

        refs: dict[str, list[str]] = {}
        for field in REQUIRED_FIELDS[:-2]:
            refs[field] = _references(item.get(field), f"{req_id}.{field}", errors)
            for ref in refs[field]:
                if ref.startswith("N/A:"):
                    if not ref[4:].strip():
                        errors.append(f"{req_id}.{field} has an empty N/A reason")
                    continue
                if not _safe_regular_file(root, ref, FIELD_PREFIXES[field]):
                    errors.append(f"{req_id}.{field} path is not an allowed repository regular file: {ref}")
        gate = item.get("external_gate")
        if not isinstance(gate, str) or not gate.strip():
            errors.append(f"{req_id}.external_gate must be a non-empty string")
        if status == "completed":
            # A completed runtime requirement needs executable code, acceptance
            # tests, architectural intent and an implementation evidence note.
            # Architecture-only requirements may omit runtime code/tests, but
            # must say so consistently in every runtime field.
            runtime_fields = ("code_service", "repository", "api_ui", "worker_outbox")
            runtime_requirement = any(
                any(not ref.startswith("N/A:") for ref in refs.get(field, []))
                for field in runtime_fields
            )
            required = ("architecture", "evidence") + (("code_service", "tests") if runtime_requirement else ())
            for field in required:
                if not any(not ref.startswith("N/A:") for ref in refs.get(field, [])):
                    errors.append(f"{req_id} completed without {field} evidence")
            if runtime_requirement and all(
                    all(ref.startswith("N/A:") for ref in refs.get(field, []))
                    for field in ("repository", "api_ui", "worker_outbox", "recovery")):
                errors.append(f"{req_id} completed runtime requirement has no durable/API/worker/recovery evidence")
            if not runtime_requirement and any(
                    any(not ref.startswith("N/A:") for ref in refs.get(field, []))
                    for field in ("tests", "recovery")):
                errors.append(f"{req_id} architecture-only completion has inconsistent runtime evidence")
            if not gate.startswith("N/A:"):
                errors.append(f"{req_id} completed while external_gate is not N/A")

    missing = [req_id for req_id, _ in expected if req_id not in seen]
    if missing:
        errors.append("missing requirement ids: " + ", ".join(missing))
    if len(expected) != EXPECTED_COUNT:
        errors.append(f"canonical source count is {len(expected)}, expected fixed baseline {EXPECTED_COUNT}")
    if len(items) != EXPECTED_COUNT:
        errors.append(f"requirement count is {len(items)}, expected {EXPECTED_COUNT}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    errors = validate_manifest(args.manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Traceability manifest valid: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
