"""P2-10 DEMO Discovery over synthetic fixtures. See the L4 packet
``docs/implementation/p2-10-demo-discovery-l4.md``.

Fail-closed: the run refuses to start unless every required gate has a DEMO
approval record. It reads only local fixtures and returns a report object; it
never performs an external read or write.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .channel_order_contracts import (OfflineOrderPage, OfflineOrderSnapshot, canonical_json,
                                      parse_coupang_day_page, parse_naver_details)
from .supplier_file_contracts import SupplierImportReport, parse_supplier_csv

REQUIRED_GATES = ("G1", "G2", "G3")
REQUIRED_FIELDS = {
    "naver": ("order_id", "line_id", "product_id", "status", "initial_quantity", "remaining_quantity"),
    "coupang": ("order_id", "line_id", "product_id", "shipment_id", "status", "initial_quantity", "remaining_quantity"),
    "supplier": ("supplier_sku", "quantity", "unit_cost_minor", "currency", "observed_at"),
}
# Fields present in fixtures that the DEMO records do not authorize us to keep.
UNAUTHORIZED_FIELDS = {"naver": ("ordererName",), "coupang": ("receiver.name", "receiver.addr1")}
_MODE = re.compile(r"^-\s*모드:\s*\*{0,2}([A-Z]+)\*{0,2}\s*$", re.MULTILINE)


class DiscoveryBlocked(PermissionError):
    """No usable approval record for a required gate."""


@dataclass(frozen=True)
class SourceCoverage:
    source: str
    rows: int
    required: tuple[str, ...]
    present: tuple[str, ...]
    absent: tuple[str, ...]

    @property
    def covered(self) -> bool:
        return self.rows > 0 and not self.absent


@dataclass(frozen=True)
class DiscoveryReport:
    mode: str
    gates: tuple[tuple[str, str], ...]
    coverage: tuple[SourceCoverage, ...]
    external_ids: int
    field_loss: tuple[str, ...]
    permission_gaps: tuple[str, ...]
    digest: str = field(default="", init=False)

    def __post_init__(self) -> None:
        body = asdict(self)
        body.pop("digest")
        object.__setattr__(self, "digest", hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest())

    @property
    def ready(self) -> bool:
        return all(c.covered for c in self.coverage) and not self.field_loss

    def canonical_payload(self) -> str:
        return canonical_json(asdict(self))


def approval_modes(approvals_dir: Path) -> dict[str, str]:
    """Gate -> mode from ``<gate>.md`` records. Unreadable or mode-less records are omitted."""
    modes: dict[str, str] = {}
    for gate in REQUIRED_GATES:
        path = Path(approvals_dir) / f"{gate}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = _MODE.search(text)
        if match:
            modes[gate] = match.group(1)
    return modes


def require_demo_gates(approvals_dir: Path) -> tuple[tuple[str, str], ...]:
    modes = approval_modes(approvals_dir)
    missing = [g for g in REQUIRED_GATES if modes.get(g) != "DEMO"]
    if missing:
        raise DiscoveryBlocked("no DEMO approval record for " + ", ".join(missing))
    return tuple((g, modes[g]) for g in REQUIRED_GATES)


def project_external_ids(snapshot: OfflineOrderSnapshot) -> tuple[tuple[str, str, str], ...]:
    """Canonical (provider, kind, value) triples. Values are strings so 2^53+ IDs survive."""
    ids = [("order", snapshot.order_id), ("line", snapshot.line_id), ("product", snapshot.product_id)]
    if snapshot.shipment_id is not None:
        ids.append(("shipment", snapshot.shipment_id))
    return tuple((snapshot.provider, kind, str(value)) for kind, value in ids)


def roundtrip_losses(snapshot: OfflineOrderSnapshot) -> tuple[str, ...]:
    """Project and map back; report any key whose value did not survive."""
    back = {kind: value for provider, kind, value in project_external_ids(snapshot) if provider == snapshot.provider}
    expected = {"order": snapshot.order_id, "line": snapshot.line_id, "product": snapshot.product_id}
    if snapshot.shipment_id is not None:
        expected["shipment"] = snapshot.shipment_id
    return tuple(f"{snapshot.provider}.{kind}" for kind, value in expected.items()
                 if back.get(kind) != str(value) or not value)


def _coverage(source: str, rows: list[dict[str, Any]]) -> SourceCoverage:
    required = REQUIRED_FIELDS[source]
    present = tuple(f for f in required if rows and all(r.get(f) not in (None, "") for r in rows))
    absent = tuple(f for f in required if f not in present)
    return SourceCoverage(source, len(rows), required, present, absent)


def _permission_gaps(source: str, body: Any) -> tuple[str, ...]:
    text = canonical_json(body)
    return tuple(f"{source}.{f}" for f in UNAUTHORIZED_FIELDS.get(source, ()) if f.split(".")[-1] in text)


def run_demo_discovery(*, approvals_dir: Path, channel_orders: dict[str, Any], supplier_csv: bytes,
                       requested_naver_ids: tuple[str, ...], supplier_ref: str = "demo-supplier-1") -> DiscoveryReport:
    gates = require_demo_gates(approvals_dir)
    naver: OfflineOrderPage = parse_naver_details(channel_orders["naver"], requested_ids=requested_naver_ids)
    coupang: OfflineOrderPage = parse_coupang_day_page(channel_orders["coupang"])
    supplier: SupplierImportReport = parse_supplier_csv(supplier_csv, supplier_ref=supplier_ref)

    coverage = (
        _coverage("naver", [asdict(s) for s in naver.snapshots]),
        _coverage("coupang", [asdict(s) for s in coupang.snapshots]),
        _coverage("supplier", [asdict(r) for r in supplier.rows]),
    )
    losses: list[str] = []
    count = 0
    for page in (naver, coupang):
        for snapshot in page.snapshots:
            count += len(project_external_ids(snapshot))
            losses.extend(roundtrip_losses(snapshot))
    gaps = _permission_gaps("naver", channel_orders["naver"]) + _permission_gaps("coupang", channel_orders["coupang"])
    return DiscoveryReport("DEMO", gates, coverage, count, tuple(losses), gaps)


def load_channel_orders(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
