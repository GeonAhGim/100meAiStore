import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_store_control import review


class ReviewTests(unittest.TestCase):
    def test_review_passes_explicit_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "task.patch"
            artifact.write_text("diff --git a/x b/x", encoding="utf-8")
            task = {"id": 1, "artifact": str(artifact)}
            with patch.object(review, "claim_review", lambda worker: task), patch.object(
                review, "complete", lambda *args, **kwargs: "검토 완료\nREVIEW: PASS"
            ), patch.object(review, "finish_review", lambda *args, **kwargs: {"status": "reviewed"}):
                self.assertEqual("reviewed", review.run_once()["status"])

    def test_missing_marker_fails_closed(self):
        task = {"id": 1, "artifact": "missing"}
        with patch.object(review, "claim_review", lambda worker: task), patch.object(
            review, "complete", lambda *args, **kwargs: "결론 불명확"
        ), patch.object(review, "finish_review", lambda task_id, decision, **kwargs: {"decision": decision}):
            self.assertEqual("fail", review.run_once()["decision"])


if __name__ == "__main__":
    unittest.main()
