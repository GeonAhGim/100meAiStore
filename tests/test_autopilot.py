import threading
import unittest
from unittest.mock import patch

from smart_store_control import autopilot, recovery


def _tick_env(tasks, **extra):
    patches = [patch.object(autopilot, "status", lambda: {"enabled": True, "local_first": True}),
               patch.object(autopilot, "pm_status", lambda: {"tasks": tasks, "attention": 0}),
               patch.object(autopilot, "recovery_status", lambda: {"status": "idle"}),
               patch.object(autopilot, "integrate_landed", lambda: []),
               patch.object(autopilot, "start_doctor", lambda: False)]
    patches += [patch.object(autopilot, name, value) for name, value in extra.items()]
    return patches


class AutopilotTests(unittest.TestCase):
    def run_tick(self, tasks, **extra):
        patches = _tick_env(tasks, **extra)
        for p in patches:
            p.start()
        try:
            return autopilot.tick()
        finally:
            for p in patches:
                p.stop()

    def test_waits_while_worker_is_active_and_nothing_is_ready(self):
        self.assertEqual("waiting", self.run_tick([{"id": 1, "status": "in_progress"}])["action"])

    def test_ready_work_is_dispatched_even_while_another_worker_runs(self):
        # The old flow returned "waiting" here and left the free slots idle.
        tasks = [{"id": 1, "status": "in_progress"}, {"id": 2, "status": "ready"}]
        result = self.run_tick(tasks, dispatch=lambda: ["local-2"])
        self.assertEqual(("dispatch", ["local-2"]), (result["action"], result["started"]))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        recovery._SLOTS.clear()
        self.release = threading.Event()
        self.calls = []

    def tearDown(self):
        self.release.set()
        recovery._SLOTS.clear()

    def test_each_free_slot_gets_one_worker_and_a_busy_slot_is_left_alone(self):
        def slow_slot(name):
            self.calls.append(name)
            self.release.wait(5)
        with patch.object(recovery, "slot_names", lambda: ["local-1", "local-2", "cursor-impl-1"]), \
                patch.object(recovery, "_slot", slow_slot):
            self.assertEqual(["local-1", "local-2", "cursor-impl-1"], recovery.dispatch())
            self.assertEqual([], recovery.dispatch())                  # all three still running
            self.release.set()
            for thread in list(recovery._SLOTS.values()):
                thread.join(5)
            self.assertEqual(["local-1", "local-2", "cursor-impl-1"], recovery.dispatch())  # refilled once done

    def test_a_local_slot_reviews_first_and_implements_only_when_no_review_is_waiting(self):
        with patch.object(recovery, "review_once", lambda worker: self.calls.append(worker) or {"status": "idle"}), \
                patch.object(recovery, "run_once", lambda worker: self.calls.append(worker) or {"status": "idle"}):
            recovery._slot("local-2")
            self.assertEqual(["local-review-2", "local-impl-2"], self.calls)
        self.calls.clear()
        with patch.object(recovery, "review_once", lambda worker: self.calls.append(worker) or {"status": "reviewed"}), \
                patch.object(recovery, "run_once", lambda worker: self.calls.append(worker)):
            recovery._slot("local-1")
            self.assertEqual(["local-review-1"], self.calls)
        self.calls.clear()
        with patch.object(recovery, "run_once", lambda worker: self.calls.append(worker)):
            recovery._slot("gemini-impl-1")
            self.assertEqual(["gemini-impl-1"], self.calls)


if __name__ == "__main__":
    unittest.main()
