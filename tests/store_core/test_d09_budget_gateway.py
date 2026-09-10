from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
import tempfile
import unittest
from unittest.mock import patch

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane
from packages.store_core.repository import InMemoryRepository
from tests.store_core.test_d09_budget_repository import NOW


def setup(repo, suffix=''):
    app = StoreControlPlane(repo, lambda: NOW)
    ctx = app.bootstrap_tenant('Budget' + suffix, 'budget' + suffix + '@example.test')
    app.set_demo_budget_policy(ctx, daily_limit_minor=1000000, monthly_limit_minor=1000000,
                               generation_limit=100, agent_run_limit=100, max_tokens=1000,
                               max_tool_calls=3, model_tier='economy')
    return app, ctx


def request(app, ctx, key='key', amount=1, **changes):
    values = dict(agent_id='agent', goal='inspect', policy_version=1, model='economy',
                  prompt_version='p1', input_value={'a': 1, 'b': 2}, decision={'state': 'proposed'},
                  confidence='high', tool_calls=1, estimated_cost_minor=amount, idempotency_key=key)
    values.update(changes)
    return app.record_demo_agent_run(ctx, **values)


class BudgetGatewayTests(unittest.TestCase):
    def test_boundaries_parity_and_tenant_cannot_raise_platform_cap(self):
        for amount, outcome in ((23999, 'RECORDED'), (24000, 'RECORDED_WARNING'),
                                (29999, 'RECORDED_WARNING'), (30000, 'BLOCKED_GLOBAL_HARD_CAP'),
                                (30001, 'BLOCKED_GLOBAL_HARD_CAP')):
            with tempfile.TemporaryDirectory() as folder:
                for repo in (InMemoryRepository(), SQLiteRepository(Path(folder) / 'budget.db')):
                    try:
                        with self.subTest(amount=amount, repository=type(repo).__name__):
                            app, ctx = setup(repo)
                            run = request(app, ctx, amount=amount)
                            self.assertEqual(outcome, run.outcome)
                            expected = amount if outcome.startswith('RECORDED') else 0
                            self.assertEqual(expected, repo.platform_monthly_budget_total(NOW))
                            self.assertEqual(amount if expected else None, run.charged_cost_minor)
                    finally:
                        if isinstance(repo, SQLiteRepository): repo.close()

    def test_cross_tenant_warning_growth_suspend_and_hard_block(self):
        repo = InMemoryRepository()
        app, ctx = setup(repo)
        _, other = setup(repo, 'other')
        request(app, ctx, amount=23999)
        self.assertEqual('BLOCKED_GLOBAL_WARNING', request(app, other, 'growth', 1, work_kind='growth').outcome)
        self.assertEqual('RECORDED_WARNING', request(app, other, 'ordinary', 1).outcome)
        self.assertEqual('BLOCKED_GLOBAL_HARD_CAP', request(app, other, 'hard', 6000).outcome)
        self.assertEqual(24000, repo.platform_monthly_budget_total(NOW))
        command = app.submit_demo_tool(ctx, actor_type='workflow', actor_id='safety', tool='reconcile',
                                      target_type='tenant', target_id=ctx.tenant_id, input_value={},
                                      idempotency_key='essential', requested_policy_version=1)
        self.assertEqual('accepted', command['state'])

    def test_all_outcomes_replay_after_restart_and_semantic_changes_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'budget.db'
            repo = SQLiteRepository(path)
            app, ctx = setup(repo)
            cases = [('accepted', 1), ('warning', 23999), ('blocked', 6000)]
            runs = [request(app, ctx, key, amount) for key, amount in cases]
            audits = len(repo.audits_for(ctx.tenant_id))
            repo.close()
            repo = SQLiteRepository(path)
            try:
                app = StoreControlPlane(repo, lambda: NOW.replace(month=10))
                for (key, amount), run in zip(cases, runs):
                    self.assertEqual(run, request(app, ctx, key, amount, input_value={'b': 2, 'a': 1}))
                    for changes in ({'agent_id': 'other'}, {'goal': 'other'}, {'model': 'other'},
                                    {'policy_version': 2}, {'prompt_version': 'p2'}, {'input_value': {}},
                                    {'decision': {}}, {'confidence': 'low'}, {'tool_calls': 2},
                                    {'estimated_cost_minor': amount + 1}, {'work_kind': 'growth'}):
                        with self.subTest(key=key, changes=changes), self.assertRaises(ConflictError):
                            request(app, ctx, key, amount, **changes)
                self.assertEqual(3, len(repo.agent_runs_for(ctx.tenant_id)))
                self.assertEqual(audits, len(repo.audits_for(ctx.tenant_id)))
            finally: repo.close()

    def test_stale_missing_invalid_or_failing_telemetry_blocks_optional_work(self):
        repo = InMemoryRepository()
        app, ctx = setup(repo)
        snapshots = [None, {}, {'reserved_minor': 0, 'computed_at': NOW - timedelta(minutes=2)},
                     {'reserved_minor': True, 'computed_at': NOW}, {'reserved_minor': 0, 'computed_at': NOW + timedelta(seconds=1)}]
        for index, snapshot in enumerate(snapshots):
            with patch.object(repo, 'platform_budget_snapshot', return_value=snapshot):
                self.assertEqual('BLOCKED_TELEMETRY', request(app, ctx, str(index)).outcome)
        with patch.object(repo, 'platform_budget_snapshot', side_effect=RuntimeError('unavailable')):
            self.assertEqual('BLOCKED_TELEMETRY', request(app, ctx, 'unavailable').outcome)
        self.assertEqual(0, repo.platform_monthly_budget_total(NOW))

    def test_fault_injection_rolls_back_run_request_reservation_and_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            for repo in (InMemoryRepository(), SQLiteRepository(Path(folder) / 'budget.db')):
                try:
                    app, ctx = setup(repo)
                    baseline = len(repo.audits_for(ctx.tenant_id))
                    for method in ('save_agent_run', 'save_budget_request', 'save_budget_entry', 'append_audit'):
                        original = getattr(repo, method)
                        def fail_after(*args, **kwargs):
                            original(*args, **kwargs)
                            raise RuntimeError('injected')
                        with self.subTest(repository=type(repo).__name__, method=method):
                            with patch.object(repo, method, side_effect=fail_after), self.assertRaisesRegex(RuntimeError, 'injected'):
                                request(app, ctx)
                            self.assertEqual((), repo.agent_runs_for(ctx.tenant_id))
                            self.assertIsNone(repo._get_budget_request(ctx.tenant_id, 'key'))
                            self.assertEqual(0, repo.platform_monthly_budget_total(NOW))
                            self.assertEqual(baseline, len(repo.audits_for(ctx.tenant_id)))
                    self.assertEqual('RECORDED', request(app, ctx).outcome)
                finally:
                    if isinstance(repo, SQLiteRepository): repo.close()

    def test_two_sqlite_connections_reserve_atomically_and_replay_once(self):
        for same_key in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'race.db'
                repo = SQLiteRepository(path)
                app, ctx = setup(repo)
                baseline = len(repo.audits_for(ctx.tenant_id))
                repo.close()
                barrier = Barrier(2)
                def worker(index):
                    local = SQLiteRepository(path)
                    try:
                        service = StoreControlPlane(local, lambda: NOW)
                        barrier.wait(timeout=10)
                        return request(service, ctx, 'same' if same_key else str(index), 16000)
                    finally: local.close()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(worker, (0, 1)))
                repo = SQLiteRepository(path)
                try:
                    self.assertEqual(16000, repo.platform_monthly_budget_total(NOW))
                    if same_key:
                        self.assertEqual(results[0], results[1])
                        self.assertEqual(baseline + 1, len(repo.audits_for(ctx.tenant_id)))
                    else:
                        self.assertEqual({'RECORDED', 'BLOCKED_GLOBAL_HARD_CAP'}, {r.outcome for r in results})
                finally: repo.close()


if __name__ == '__main__': unittest.main()
