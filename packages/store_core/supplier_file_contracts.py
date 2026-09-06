"""Versioned local CSV/manual fixture contracts; no supplier submission path."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field, replace

from .channel_order_contracts import ContractQuarantine, _identifier, _stamp, canonical_json


SCHEMA = "local-supplier-csv-v1"
HEADERS = ("supplier_sku", "barcode", "model", "brand", "spec", "color", "configuration",
           "quantity", "unit_cost_minor", "currency", "observed_at")
MAX_FILE_BYTES = 1024 * 1024
MAX_ROWS = 1000
_UNSIGNED = re.compile(r"(?:0|[1-9][0-9]{0,14})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class SupplierFileRow:
    record_number: int
    first_line: int
    last_line: int
    supplier_sku: str
    identity: tuple[tuple[str, str], ...]
    quantity: int
    unit_cost_minor: int
    currency: str
    observed_at: str
    row_digest: str


@dataclass(frozen=True)
class SupplierRowError:
    record_number: int
    first_line: int
    last_line: int
    code: str


@dataclass(frozen=True)
class SupplierImportReport:
    supplier_ref: str
    source_digest: str
    rows: tuple[SupplierFileRow, ...] = field(repr=False)
    errors: tuple[SupplierRowError, ...]
    schema: str = field(default=SCHEMA, init=False)
    imported: bool = field(default=False, init=False)

    @property
    def ready(self) -> bool:
        return bool(self.rows) and not self.errors


def _cell(value: str) -> str:
    if not value.strip() or len(value) > 1024:
        raise ContractQuarantine("invalid_cell_length")
    if any(ord(char) < 32 and char not in "\r\n" for char in value) or "\x7f" in value:
        raise ContractQuarantine("control_character")
    if value.lstrip().startswith(("=", "+", "-", "@")):
        raise ContractQuarantine("formula_cell")
    return value


def _number(value: str) -> int:
    if not _UNSIGNED.fullmatch(value):
        raise ContractQuarantine("unsigned_integer_required")
    return int(value)


def parse_supplier_csv(payload: bytes, *, supplier_ref: str, file_format: str = "CSV") -> SupplierImportReport:
    """Parse our local schema only. Any row error prevents a ready report."""
    _identifier(supplier_ref)
    if file_format != "CSV":
        raise ContractQuarantine("unsupported_file_format")
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_FILE_BYTES:
        raise ContractQuarantine("invalid_file_size")
    source_digest = hashlib.sha256(payload).hexdigest()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeError:
        raise ContractQuarantine("utf8_required") from None
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows, errors, seen = [], [], set()
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise ContractQuarantine("invalid_header") from None
    if len(header) != len(HEADERS) or set(header) != set(HEADERS):
        raise ContractQuarantine("header_schema_mismatch")
    record_number = 0
    while True:
        first_line = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error:
            errors.append(SupplierRowError(record_number + 1, first_line, reader.line_num, "malformed_csv"))
            break
        record_number += 1
        if record_number > MAX_ROWS:
            errors.append(SupplierRowError(record_number, first_line, reader.line_num, "row_limit_exceeded"))
            break
        try:
            if len(cells) != len(header):
                raise ContractQuarantine("column_count_mismatch")
            for cell in cells:
                _cell(cell)
            values = dict(zip(header, cells))
            sku = _identifier(values["supplier_sku"])
            if sku in seen:
                raise ContractQuarantine("duplicate_supplier_sku")
            seen.add(sku)
            if values["currency"] != "KRW":
                raise ContractQuarantine("unsupported_currency")
            quantity = _number(values["quantity"])
            cost = _number(values["unit_cost_minor"])
            observed_at = _stamp(values["observed_at"])
            identity = tuple((key, values[key]) for key in HEADERS[1:7])
            row_digest = hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()
            rows.append(SupplierFileRow(record_number, first_line, reader.line_num, sku, identity,
                                        quantity, cost, "KRW", observed_at, row_digest))
        except ContractQuarantine as error:
            errors.append(SupplierRowError(record_number, first_line, reader.line_num, str(error)))
    if not rows and not errors:
        errors.append(SupplierRowError(0, 1, reader.line_num, "empty_catalog"))
    return SupplierImportReport(supplier_ref, source_digest, tuple(rows), tuple(errors))


@dataclass(frozen=True)
class ManualSupplierTask:
    task_id: str
    tenant_ref: str
    supplier_ref: str
    request_digest: str
    status: str = "AWAITING_HUMAN"
    version: int = 0
    evidence_digest: str | None = None
    provider_confirmed: bool = field(default=False, init=False)
    external_write_performed: bool = field(default=False, init=False)


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ContractQuarantine("sha256_digest_required")
    return value


def create_manual_supplier_task(*, tenant_ref: str, supplier_ref: str,
                                idempotency_key: str, request_digest: str) -> ManualSupplierTask:
    for value in (tenant_ref, supplier_ref, idempotency_key):
        _identifier(value)
    _digest(request_digest)
    task_id = hashlib.sha256(canonical_json(
        [tenant_ref, supplier_ref, idempotency_key, request_digest]).encode("utf-8")).hexdigest()
    return ManualSupplierTask(task_id, tenant_ref, supplier_ref, request_digest)


def record_manual_confirmation(task: ManualSupplierTask, *, tenant_ref: str,
                               evidence_digest: str, expected_version: int) -> ManualSupplierTask:
    if not isinstance(task, ManualSupplierTask) or tenant_ref != task.tenant_ref:
        raise ContractQuarantine("manual_task_scope_mismatch")
    _digest(evidence_digest)
    if type(expected_version) is not int or expected_version < 0:
        raise ContractQuarantine("invalid_version")
    if task.status == "HUMAN_REPORTED" and task.evidence_digest == evidence_digest:
        return task
    if task.status != "AWAITING_HUMAN" or task.version != expected_version:
        raise ContractQuarantine("manual_task_version_conflict")
    return replace(task, status="HUMAN_REPORTED", version=task.version + 1, evidence_digest=evidence_digest)
