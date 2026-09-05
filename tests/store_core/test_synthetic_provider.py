import tempfile
import unittest
from pathlib import Path

from packages.store_core.errors import ConflictError
from packages.store_core.synthetic_provider import DurableSyntheticProvider


class SyntheticProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "provider.sqlite3"
        self.provider = DurableSyntheticProvider(self.path)

    def tearDown(self):
        self.provider.close()
        self.tmp.cleanup()

    def test_replay_restart_and_tenant_isolation(self):
        first = self.provider.execute("a", "key", "digest")
        self.assertEqual(first, self.provider.execute("a", "key", "digest"))
        with self.assertRaises(ConflictError):
            self.provider.execute("a", "key", "changed")
        self.assertEqual("ABSENT", self.provider.lookup("b", "key", "digest")["kind"])
        second = self.provider.execute("b", "key", "changed")
        self.assertNotEqual(first["provider_reference"], second["provider_reference"])
        self.provider.close()
        self.provider = DurableSyntheticProvider(self.path)
        self.assertEqual(first, self.provider.lookup("a", "key", "digest"))
        self.assertEqual(1, self.provider.effect_count("a"))
        self.assertEqual(64, len(first["response_digest"]))

    def test_timeouts_and_authoritative_absence(self):
        self.provider.mode = "timeout_before"
        with self.assertRaises(TimeoutError):
            self.provider.execute("a", "key", "digest")
        self.assertEqual(0, self.provider.effect_count("a"))
        self.assertFalse(self.provider.lookup("a", "key", "digest")["authoritative_absence"])
        self.provider.authoritative_absence = True
        self.assertTrue(self.provider.lookup("a", "key", "digest")["authoritative_absence"])
        self.provider.mode = "timeout_after"
        with self.assertRaises(TimeoutError):
            self.provider.execute("a", "key", "digest")
        self.assertEqual("FOUND_SUCCESS", self.provider.lookup("a", "key", "digest")["kind"])
        self.provider.mode = "success"
        self.provider.execute("a", "key", "digest")
        self.assertEqual(1, self.provider.effect_count("a"))

    def test_refusal_delayed_visibility_and_unavailable(self):
        self.provider.mode = "refusal"
        self.assertEqual("FOUND_FAILURE", self.provider.execute("a", "refused", "d")["kind"])
        self.assertEqual(0, self.provider.effect_count("a"))
        self.provider.mode = "delayed_lookup"
        with self.assertRaises(TimeoutError):
            self.provider.execute("a", "delayed", "d")
        self.provider.close()
        self.provider = DurableSyntheticProvider(self.path)
        self.assertEqual("INCONCLUSIVE", self.provider.lookup("a", "delayed", "d")["kind"])
        self.assertEqual("FOUND_SUCCESS", self.provider.lookup("a", "delayed", "d")["kind"])
        for mode in ("lookup_unavailable", "stale_response"):
            self.provider.mode = mode
            self.assertEqual("INCONCLUSIVE", self.provider.lookup("a", "delayed", "d")["kind"])
        self.assertEqual("INCONCLUSIVE", self.provider.execute("a", "delayed", "d")["kind"])
        self.assertEqual(1, self.provider.effect_count("a"))
        with self.assertRaises(ConflictError):
            self.provider.lookup("a", "delayed", "wrong")

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            self.provider.mode = "live"
