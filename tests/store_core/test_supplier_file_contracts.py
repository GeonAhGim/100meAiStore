import csv
import hashlib
import io
import unittest
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.supplier_file_contracts import (
    HEADERS, MAX_FILE_BYTES, create_manual_supplier_task, parse_supplier_csv,
    record_manual_confirmation,
)


class SupplierFileContractTest(unittest.TestCase):
    def setUp(self):
        self.row = ["000123", "0012345678905", "fixture-model", "fixture-brand",
                    "size, weight\r\nsecond line", "blue", "single", "12", "3400", "KRW",
                    "2026-09-06T10:00:00+09:00"]

    def payload(self, rows=None, headers=HEADERS):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows([self.row] if rows is None else rows)
        return stream.getvalue().encode("utf-8")

    def test_csv_bom_quotes_newlines_identity_and_source_hash(self):
        payload = b"\xef\xbb\xbf" + self.payload()
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            report = parse_supplier_csv(payload, supplier_ref="fixture-supplier")
        self.assertTrue(report.ready)
        self.assertFalse(report.imported)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), report.source_digest)
        row = report.rows[0]
        self.assertEqual("000123", row.supplier_sku)
        self.assertEqual("0012345678905", dict(row.identity)["barcode"])
        self.assertEqual("size, weight\r\nsecond line", dict(row.identity)["spec"])
        self.assertEqual((1, 2, 3, 12, 3400), (row.record_number, row.first_line, row.last_line,
                                            row.quantity, row.unit_cost_minor))
        self.assertEqual(report, parse_supplier_csv(payload, supplier_ref="fixture-supplier"))

    def test_duplicate_sku_blocks_whole_report_and_masked_errors(self):
        report = parse_supplier_csv(self.payload([self.row, self.row]), supplier_ref="fixture-supplier")
        self.assertFalse(report.ready)
        self.assertEqual("duplicate_supplier_sku", report.errors[0].code)
        self.assertNotIn("fixture-model", repr(report))
        self.assertNotIn("000123", repr(report))

    def test_formula_currency_quantity_and_timezone_errors_never_import(self):
        changes = [(2, '=HYPERLINK("SYNTHETIC PRIVATE VALUE")'), (2, " \t@danger"),
                   (2, " "), (7, "-1"), (7, "1.2"), (8, "1e3"), (9, "USD"),
                   (10, "2026-09-06T10:00:00")]
        for column, value in changes:
            row = self.row.copy()
            row[column] = value
            report = parse_supplier_csv(self.payload([row]), supplier_ref="fixture-supplier")
            with self.subTest(column=column, value=value):
                self.assertFalse(report.ready)
                self.assertFalse(report.imported)
                self.assertEqual(1, len(report.errors))
                self.assertNotIn("PRIVATE", repr(report))
        nul_payload = self.payload().replace(b"fixture-model", b"control\x00value")
        report = parse_supplier_csv(nul_payload, supplier_ref="fixture-supplier")
        self.assertFalse(report.ready)

    def test_schema_encoding_and_format_boundaries(self):
        for payload, fmt in ((self.payload(headers=HEADERS[:-1] + ("quantity",)), "CSV"),
                             (b"\xff", "CSV"), (b"x" * (MAX_FILE_BYTES + 1), "CSV"),
                             (self.payload(), "EXCEL"), (self.payload(), "XML")):
            with self.subTest(format=fmt), self.assertRaises(ContractQuarantine):
                parse_supplier_csv(payload, supplier_ref="fixture-supplier", file_format=fmt)

    def test_empty_malformed_and_row_limit_fail_without_raw_data(self):
        header = self.payload(rows=[])
        self.assertEqual("empty_catalog", parse_supplier_csv(header, supplier_ref="s").errors[0].code)
        malformed = header + b'"SYNTHETIC PRIVATE VALUE'
        report = parse_supplier_csv(malformed, supplier_ref="s")
        self.assertEqual("malformed_csv", report.errors[0].code)
        self.assertNotIn("PRIVATE", repr(report))
        rows = []
        for index in range(1001):
            row = self.row.copy()
            row[0] = str(index)
            rows.append(row)
        report = parse_supplier_csv(self.payload(rows), supplier_ref="s")
        self.assertFalse(report.ready)
        self.assertEqual("row_limit_exceeded", report.errors[-1].code)

    def test_reordered_header_maps_by_name(self):
        report = parse_supplier_csv(self.payload([list(reversed(self.row))], tuple(reversed(HEADERS))), supplier_ref="s")
        self.assertEqual(3400, report.rows[0].unit_cost_minor)

    def test_manual_confirmation_is_human_report_not_provider_success(self):
        arguments = dict(tenant_ref="t1", supplier_ref="s1", idempotency_key="manual1", request_digest="a" * 64)
        task = create_manual_supplier_task(**arguments)
        self.assertEqual(task, create_manual_supplier_task(**arguments))
        self.assertEqual("AWAITING_HUMAN", task.status)
        self.assertFalse(task.provider_confirmed)
        confirmed = record_manual_confirmation(task, tenant_ref="t1", evidence_digest="b" * 64, expected_version=0)
        self.assertEqual(("HUMAN_REPORTED", 1), (confirmed.status, confirmed.version))
        self.assertFalse(confirmed.external_write_performed)
        self.assertFalse(confirmed.provider_confirmed)
        self.assertEqual(confirmed, record_manual_confirmation(confirmed, tenant_ref="t1", evidence_digest="b" * 64, expected_version=0))

    def test_manual_conflicting_evidence_version_and_scope_are_rejected(self):
        task = create_manual_supplier_task(tenant_ref="t1", supplier_ref="s1", idempotency_key="m1", request_digest="a" * 64)
        for overrides in ({"tenant_ref": "t2"}, {"expected_version": 1},
                          {"evidence_digest": "PRIVATE RAW RECEIPT"}, {"expected_version": True}):
            args = {"tenant_ref": "t1", "evidence_digest": "b" * 64, "expected_version": 0, **overrides}
            with self.assertRaises(ContractQuarantine): record_manual_confirmation(task, **args)
        confirmed = record_manual_confirmation(task, tenant_ref="t1", evidence_digest="b" * 64, expected_version=0)
        with self.assertRaises(ContractQuarantine):
            record_manual_confirmation(confirmed, tenant_ref="t1", evidence_digest="c" * 64, expected_version=1)


if __name__ == "__main__":
    unittest.main()
