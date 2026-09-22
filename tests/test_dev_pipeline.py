"""dev.task pipeline: worker-verified, resumable, bounded, guarded.

Codex is replaced by a scripted fake; git and the test command run for real
inside a throwaway repository.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from smart_store_aios.config import ProfitPolicy, Settings
from smart_store_aios.db import StoreDB
from smart_store_aios.dev_pipeline import DevPipeline, default_runner
from smart_store_aios.worker import Worker

PASSING_TEST = '''import unittest
class T(unittest.TestCase):
    def test_demo_01_and_demo_02(self):
        """DEMO-01 DEMO-02"""
        from pkg.feature import answer
        self.assertEqual(42, answer())
'''
FAILING_TEST = PASSING_TEST.replace("42", "41")


class FakeCodex:
    """Scripted stand-in for `codex exec`. Each call pops the next action."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    def __call__(self, command, cwd, timeout):
        if not Path(command[0]).name.lower().startswith("codex"):
            return default_runner(command, cwd, timeout)
        label = Path(command[command.index("--output-last-message") + 1]).stem
        self.calls.append(label)
        action = self.actions.pop(0)
        if action == "throttle":
            return subprocess.CompletedProcess(command, 1, "", "error: rate limit reached")
        if action == "throttle-exit0":  # what codex 0.154 actually does
            return subprocess.CompletedProcess(
                command, 0, "", "ERROR: You've hit your usage limit. ... or try again at Sep 26th, 2099 11:48 PM.")
        if action == "crash":
            raise RuntimeError("simulated worker crash")
        action(Path(cwd))
        return subprocess.CompletedProcess(command, 0, "done", "")


def write_spec(task="demo"):
    def act(cwd):
        p = cwd / "docs" / "implementation" / f"{task}-l4.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        t = task.upper()
        p.write_text("# Demo — L4 packet\n\nStatus: implementation packet.\n\n## Acceptance evidence\n\n"
                     f"| ID | Acceptance criterion |\n|---|---|\n| {t}-01 | returns 42 |\n| {t}-02 | has a test |\n",
                     encoding="utf-8")
    return act


def write_impl(test_source=PASSING_TEST):
    def act(cwd):
        (cwd / "pkg").mkdir(exist_ok=True)
        (cwd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (cwd / "pkg" / "feature.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        (cwd / "tests").mkdir(exist_ok=True)
        (cwd / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (cwd / "tests" / "test_feature.py").write_text(test_source, encoding="utf-8")
    return act


def tamper_spec(cwd):
    p = cwd / "docs" / "implementation" / "demo-l4.md"
    p.write_text(p.read_text(encoding="utf-8").replace("returns 42", "returns anything"), encoding="utf-8")
    (cwd / "tests" / "test_feature.py").write_text(PASSING_TEST, encoding="utf-8")


def noop(cwd):
    pass


class DevPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.test"],
                     ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "root"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "docs" / "implementation").mkdir(parents=True)
        (self.root / ".gitignore").write_text("data/\n__pycache__/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "ignore"], check=True, capture_output=True)
        self.settings = Settings(database=self.root / "data" / "jobs.db", dry_run=False, profit=ProfitPolicy(),
                                 lease_seconds=60, max_attempts=3, codex_enabled=True, dev_max_fix_rounds=2)
        self.db = StoreDB(self.settings.database)
        self.db.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def _worker(self, actions):
        worker = Worker(self.settings)
        fake = FakeCodex(actions)
        worker.dev_runner = fake
        # pipeline resolves the repo from cwd; pin it for the test
        original = worker._dispatch

        def dispatch(job):
            if job["kind"] == "dev.task":
                return DevPipeline(self.settings, self.db, job, worker.worker_id, runner=fake, repo_root=self.root).run()
            return original(job)
        worker._dispatch = dispatch
        return worker, fake

    def _job(self, **extra):
        payload = {"task_id": "demo", "title": "Demo", "goal": "return 42", "acceptance": ["returns 42", "has a test"]}
        payload.update(extra)
        return self.db.enqueue("dev.task", payload)

    def _row(self, job_id):
        with self.db.connect() as c:
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def test_happy_path_publishes_spec_with_worker_verified_evidence(self):
        job = self._job()
        worker, fake = self._worker([write_spec(), write_impl()])
        self.assertTrue(worker.run_once())
        row = self._row(job)
        self.assertEqual("done", row["status"], row["last_error"])
        self.assertEqual(["spec", "implement"], fake.calls)
        log = subprocess.run(["git", "-C", str(self.root), "log", "--oneline", "worker/demo"],
                             capture_output=True, text=True).stdout
        self.assertIn("spec(demo)", log); self.assertIn("feat(demo)", log); self.assertIn("docs(demo)", log)
        spec = (self.root / "data" / "worktrees" / "demo" / "docs" / "implementation" / "demo-l4.md").read_text(encoding="utf-8")
        self.assertIn("## Evidence", spec)
        self.assertIn("1 ran, OK", spec)
        # main checkout untouched
        self.assertEqual("", subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"],
                                            capture_output=True, text=True).stdout.strip())

    def test_resumes_from_checkpoint_without_repeating_model_stages(self):
        job = self._job()
        worker, fake = self._worker([write_spec(), write_impl(), "crash"])
        # crash is consumed by the fix round after a failing implementation
        fake.actions = [write_spec(), write_impl(FAILING_TEST), "crash"]
        self.assertTrue(worker.run_once())
        row = self._row(job)
        self.assertEqual("queued", row["status"])
        self.assertIn("simulated worker crash", row["last_error"])
        self.assertIn('"stage": "verify"', row["checkpoint"])
        with self.db.connect() as c:
            c.execute("UPDATE jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job,))
        worker2, fake2 = self._worker([write_impl(PASSING_TEST)])
        self.assertTrue(worker2.run_once())
        self.assertEqual("done", self._row(job)["status"], self._row(job)["last_error"])
        self.assertEqual(["fix-2"], fake2.calls)  # no spec/implement re-run

    def test_identical_failure_twice_is_a_stall_and_dead_letters(self):
        job = self._job()
        worker, fake = self._worker([write_spec(), write_impl(FAILING_TEST), write_impl(FAILING_TEST)])
        # the fix round must leave a diff, so alter an unrelated tracked file each round
        def fix(cwd):
            (cwd / "tests" / "test_feature.py").write_text(FAILING_TEST + "\n# retry\n", encoding="utf-8")
        fake.actions = [write_spec(), write_impl(FAILING_TEST), fix]
        self.assertTrue(worker.run_once())
        row = self._row(job)
        self.assertEqual("dead", row["status"])
        self.assertIn("stalled", row["last_error"])

    def test_fix_budget_is_bounded(self):
        job = self._job(max_fix_rounds=0)
        worker, _ = self._worker([write_spec(), write_impl(FAILING_TEST)])
        worker.run_once()
        row = self._row(job)
        self.assertEqual("dead", row["status"])
        self.assertIn("fix budget exhausted after 0 rounds", row["last_error"])

    def test_throttle_defers_without_consuming_an_attempt(self):
        job = self._job()
        worker, _ = self._worker(["throttle"])
        worker.run_once()
        row = self._row(job)
        self.assertEqual("queued", row["status"])
        self.assertEqual(0, row["attempts"])
        self.assertIn("throttled", row["last_error"])

    def test_exit_zero_usage_limit_defers_until_the_stated_retry_time(self):
        job = self._job()
        worker, _ = self._worker(["throttle-exit0"])
        worker.run_once()
        row = self._row(job)
        self.assertEqual("queued", row["status"])
        self.assertEqual(0, row["attempts"])
        self.assertIn("retry in", row["last_error"])
        self.assertGreater(row["available_at"], "2099-09-26")  # deferred to the hint, not 30 min

    def test_retry_hint_parser(self):
        from smart_store_aios.dev_pipeline import _retry_at
        parsed = _retry_at("or try again at Sep 26th, 2026 11:48 PM.")
        self.assertIsNotNone(parsed)
        self.assertEqual(2026, parsed.year)
        self.assertIsNone(_retry_at("no hint here"))

    def test_commit_in_main_checkout_during_a_stage_is_not_a_violation(self):
        job = self._job()
        marker = self.root / "docs" / "note.md"
        marker.write_text("wip\n", encoding="utf-8")  # dirty before the stage

        def spec_and_commit_main(cwd):
            write_spec()(cwd)
            subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "human commit"], check=True, capture_output=True)
        worker, _ = self._worker([spec_and_commit_main, write_impl()])
        worker.run_once()
        self.assertEqual("done", self._row(job)["status"], self._row(job)["last_error"])

    def test_invalid_payload_is_dead_immediately(self):
        job = self.db.enqueue("dev.task", {"task_id": "x", "title": "t", "goal": "g", "acceptance": []})
        worker, _ = self._worker([])
        worker.run_once()
        row = self._row(job)
        self.assertEqual("dead", row["status"])
        self.assertEqual(1, row["attempts"])

    def test_acceptance_table_tamper_and_scope_violation_are_rejected(self):
        job = self._job(files=["pkg/"])
        worker, _ = self._worker([write_spec(), write_impl(FAILING_TEST), tamper_spec])
        worker.run_once()
        self.assertIn("acceptance table", self._row(job)["last_error"])

        job2 = self._job(task_id="scoped", files=["other/"])
        worker2, _ = self._worker([write_spec("scoped"), write_impl()])
        worker2.run_once()
        self.assertIn("outside the allowed file list", self._row(job2)["last_error"])

    def test_model_stage_with_no_diff_is_not_progress(self):
        job = self._job()
        worker, _ = self._worker([write_spec(), noop])
        worker.run_once()
        row = self._row(job)
        self.assertEqual("queued", row["status"])
        self.assertIn("changed nothing", row["last_error"])


if __name__ == "__main__":
    unittest.main()
