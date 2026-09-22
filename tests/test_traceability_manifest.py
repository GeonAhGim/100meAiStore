from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.validate_traceability import DEFAULT_MANIFEST, ROOT, source_requirements, validate_manifest


class TraceabilityManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))

    def validate_mutation(self, mutate):
        document = copy.deepcopy(self.document)
        mutate(document)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            return validate_manifest(path, ROOT)

    def test_manifest_covers_every_source_bullet_and_is_valid(self):
        self.assertEqual(43, len(source_requirements()))
        self.assertEqual([], validate_manifest())

    def test_missing_and_duplicate_ids_fail_closed(self):
        missing = self.validate_mutation(lambda doc: doc["requirements"].pop())
        self.assertTrue(any("missing requirement ids" in error for error in missing))

        def duplicate(doc):
            doc["requirements"][-1]["id"] = doc["requirements"][0]["id"]
        duplicated = self.validate_mutation(duplicate)
        self.assertTrue(any("duplicate requirement id" in error for error in duplicated))

    def test_invalid_path_and_status_fail_closed(self):
        def invalid(doc):
            doc["requirements"][0]["architecture"] = ["docs/does-not-exist.md"]
            doc["requirements"][0]["status"] = "done"
        errors = self.validate_mutation(invalid)
        self.assertTrue(any("allowed repository regular file" in error for error in errors))
        self.assertTrue(any("invalid status" in error for error in errors))

    def test_completed_requires_code_and_test_evidence(self):
        def unsupported_completion(doc):
            item = doc["requirements"][1]
            item["status"] = "completed"
            item["code_service"] = ["N/A: architecture-only claim"]
            item["tests"] = ["N/A: no acceptance test"]
        errors = self.validate_mutation(unsupported_completion)
        self.assertTrue(any("completed without code_service" in error for error in errors))
        self.assertTrue(any("completed without tests" in error for error in errors))

    def test_completed_external_gate_and_inconsistent_type_fail_closed(self):
        def gated_runtime(doc):
            item = doc["requirements"][1]
            item["status"] = "completed"
            item["external_gate"] = "G1: credentials still required"
        errors = self.validate_mutation(gated_runtime)
        self.assertTrue(any("external_gate is not N/A" in error for error in errors))

        def inconsistent_architecture_only(doc):
            item = doc["requirements"][0]
            item["status"] = "completed"
            item["tests"] = ["tests/test_traceability_manifest.py"]
        errors = self.validate_mutation(inconsistent_architecture_only)
        self.assertTrue(any("inconsistent runtime evidence" in error for error in errors))

    def test_reference_prefix_and_symlink_are_rejected(self):
        def wrong_namespace(doc):
            doc["requirements"][0]["architecture"] = ["tests/test_traceability_manifest.py"]
        errors = self.validate_mutation(wrong_namespace)
        self.assertTrue(any("allowed repository regular file" in error for error in errors))

        def lookalike_exact_file(doc):
            doc["requirements"][-1]["repository"] = [".gitignore-pretender"]
        errors = self.validate_mutation(lookalike_exact_file)
        self.assertTrue(any("allowed repository regular file" in error for error in errors))

        # Mocking is portable to Windows hosts where creating a symlink may need
        # a developer-mode privilege unavailable in CI.
        with mock.patch("pathlib.Path.is_symlink", return_value=True):
            errors = validate_manifest(DEFAULT_MANIFEST, ROOT)
        self.assertTrue(any("allowed repository regular file" in error for error in errors))

    def test_metadata_directory_and_fixed_source_count_fail_closed(self):
        def invalid_metadata(doc):
            doc["schema_version"] = 2
            doc["source"] = "docs/other.md"
            doc["policy"] = ""
            doc["requirements"][0]["architecture"] = ["docs"]
        errors = self.validate_mutation(invalid_metadata)
        self.assertTrue(any("schema_version" in error for error in errors))
        self.assertTrue(any("canonical requirements" in error for error in errors))
        self.assertTrue(any("policy" in error for error in errors))
        self.assertTrue(any("allowed repository regular file" in error for error in errors))

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "docs" / "100meAiStore-requirements-v1.md"
            source.parent.mkdir(parents=True)
            source.write_text("## one\n- only\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "source":
                "docs/100meAiStore-requirements-v1.md", "policy": "strict",
                "requirements": []}), encoding="utf-8")
            self.assertTrue(any("fixed baseline 43" in error for error in validate_manifest(manifest, root)))


if __name__ == "__main__":
    unittest.main()
