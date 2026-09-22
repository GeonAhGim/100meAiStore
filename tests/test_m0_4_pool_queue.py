"""M0.4 PM·로컬 워커풀 큐: claims reflect AIOS available slots and the handoff state.

Exit criterion: "AIOS 가용 슬롯과 핸드오프 상태를 반영해 task를 claim함".
Each test drives the live probe and the runtime handoff and checks what the
queue lets a worker claim.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import pm


class M04PoolQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks.json"
        self.pools = root / "pools.json"
        self.runtime = root / "runtime.json"
        self.pools.write_text(json.dumps({"pools": {"local-impl": {"size": 2, "max_size": 2, "engine": "local-http"}}}), encoding="utf-8")
        self.tasks.write_text(json.dumps({"tasks": [
            {"id": 1, "role": "local-impl", "status": "ready", "priority": 90},
            {"id": 2, "role": "local-impl", "status": "ready", "priority": 80},
            {"id": 3, "role": "local-impl", "status": "ready", "priority": 70},
        ]}), encoding="utf-8")
        self.live = {"available_slots": 0}
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks), mock.patch.object(pm, "POOLS_PATH", self.pools),
                        mock.patch.object(pm, "CONTROL_DIR", root),
                        mock.patch.object(pm, "snapshot", lambda: {"aios_live": dict(self.live)})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _runtime(self, handoff, floor=0):
        self.runtime.write_text(json.dumps({"handoff_granted": handoff, "slots": {"operator_floor": floor}}), encoding="utf-8")

    def test_no_handoff_means_no_claim_even_with_free_slots(self):
        self._runtime(handoff=False)
        self.live["available_slots"] = 6
        self.assertEqual(0, pm.effective_capacity("local-impl")["effective"])
        self.assertIsNone(pm.claim("local-impl-1", "local-impl"))

    def test_aios_holding_every_slot_blocks_claims_unless_the_operator_set_a_floor(self):
        self._runtime(handoff=True, floor=0)
        self.live["available_slots"] = 0
        self.assertIsNone(pm.claim("local-impl-1", "local-impl"))
        self._runtime(handoff=True, floor=1)
        claimed = pm.claim("local-impl-1", "local-impl")
        self.assertEqual(1, claimed["id"])
        self.assertEqual({"aios_available": 0, "handoff_granted": True, "operator_floor": 1, "effective": 1},
                         {k: claimed["claim_basis"][k] for k in ("aios_available", "handoff_granted", "operator_floor", "effective")})
        self.assertIsNone(pm.claim("local-impl-2", "local-impl"))   # the floor is exactly one

    def test_claims_follow_the_live_slot_count_as_it_changes(self):
        self._runtime(handoff=True)
        self.live["available_slots"] = 1
        self.assertEqual(1, pm.claim("local-impl-1", "local-impl")["id"])
        self.assertIsNone(pm.claim("local-impl-2", "local-impl"))    # only one slot was free
        self.live["available_slots"] = 3                             # AIOS released slots
        second = pm.claim("local-impl-2", "local-impl")
        self.assertEqual(2, second["id"])
        self.assertEqual(1, second["claim_basis"]["active_before"])
        self.assertIsNone(pm.claim("local-impl-3", "local-impl"))    # pool size 2 is the ceiling

    def test_reviews_share_the_local_slots(self):
        self._runtime(handoff=True)
        self.live["available_slots"] = 1
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        data["tasks"].append({"id": 4, "role": "local-impl", "status": "reviewing", "reviewer": "local-review-1"})
        self.tasks.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(pm.claim("local-impl-1", "local-impl"))    # the one slot is busy reviewing


if __name__ == "__main__":
    unittest.main()
