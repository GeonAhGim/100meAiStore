from dataclasses import replace
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane
from packages.store_core.repository import InMemoryRepository
from packages.store_core.domain import DemoAgentRun, DemoBudgetLedgerEntry, DemoBudgetRequest
from packages.store_core import sqlite_repository

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class BudgetRepositoryTests(unittest.TestCase):
    def test_request_replay_monthly_sum_reservation_and_rollback_parity(self):
        with tempfile.TemporaryDirectory() as folder:
            for repo in (InMemoryRepository(), SQLiteRepository(Path(folder) / 'budget.db')):
                with self.subTest(repository=type(repo).__name__):
                    app = StoreControlPlane(repo, lambda: NOW)
                    ctx = app.bootstrap_tenant('Budget', 'budget@example.test')
                    run = DemoAgentRun('run', ctx.tenant_id, 'agent', 'inspect', 1, 'economy', 'p1',
                                       'a' * 64, '{}', 'high', 0, None, 23999, 23999, 'RECORDED', NOW)
                    entry = DemoBudgetLedgerEntry('entry', ctx.tenant_id, run.id, 23999, NOW, 'key')
                    request = DemoBudgetRequest(ctx.tenant_id, 'key', 'b' * 64, run.id, 'RECORDED', NOW)
                    with repo.transaction():
                        repo.save_agent_run(run)
                        repo.reserve_budget_entry(entry)
                        repo.save_budget_request(request)
                    self.assertEqual(request, repo.get_budget_request(ctx.tenant_id, 'key', 'b' * 64))
                    with self.assertRaises(ConflictError):
                        repo.get_budget_request(ctx.tenant_id, 'key', 'c' * 64)
                    self.assertEqual(23999, repo.platform_monthly_budget_total(NOW))
                    self.assertEqual(0, repo.platform_monthly_budget_total(NOW.replace(month=10)))
                    with self.assertRaises(ConflictError):
                        repo.reserve_budget_entry(replace(entry, id='entry2', idempotency_key='key2', amount_minor=6001))
                    with self.assertRaisesRegex(RuntimeError, 'rollback'):
                        with repo.transaction():
                            repo.save_budget_request(replace(request, idempotency_key='blocked', outcome='BLOCKED_BUDGET'))
                            raise RuntimeError('rollback')
                    self.assertIsNone(repo.get_budget_request(ctx.tenant_id, 'blocked', 'b' * 64))
                    if isinstance(repo, SQLiteRepository):
                        repo.close()
                        reopened = SQLiteRepository(Path(folder) / 'budget.db')
                        self.assertEqual(request, reopened.get_budget_request(ctx.tenant_id, 'key', 'b' * 64))
                        reopened.close()

    def test_v21_legacy_keys_fail_closed_and_upgrade_is_forward_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.db'
            with patch.object(sqlite_repository, 'MIGRATIONS', sqlite_repository.MIGRATIONS[:21]), patch.object(sqlite_repository, 'LATEST_SCHEMA_VERSION', 21):
                repo = SQLiteRepository(path)
                app = StoreControlPlane(repo, lambda: NOW)
                ctx = app.bootstrap_tenant('Legacy', 'legacy@example.test')
                run = DemoAgentRun('run', ctx.tenant_id, 'agent', 'inspect', 1, 'economy', 'p1',
                                   'a' * 64, '{}', 'high', 0, None, 5, 5, 'RECORDED', NOW)
                repo.save_agent_run(run)
                repo.save_budget_entry(DemoBudgetLedgerEntry('entry', ctx.tenant_id, run.id, 5, NOW, 'legacy'))
                repo.close()
            repo = SQLiteRepository(path)
            self.assertEqual(22, repo.readiness()['schema_version'])
            with self.assertRaisesRegex(ConflictError, 'legacy'):
                repo.get_budget_request(ctx.tenant_id, 'legacy', 'a' * 64)
            self.assertEqual(5, repo.platform_monthly_budget_total(NOW))
            repo.close()

    def test_v22_failed_migration_rolls_back_ddl_and_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollback.db'
            migrations = sqlite_repository.MIGRATIONS
            with patch.object(sqlite_repository, 'MIGRATIONS', migrations[:21]), patch.object(sqlite_repository, 'LATEST_SCHEMA_VERSION', 21):
                SQLiteRepository(path).close()
            broken = migrations[:21] + ((22, migrations[21][1] + '\nSELECT missing_column FROM missing_table;'),)
            with patch.object(sqlite_repository, 'MIGRATIONS', broken):
                with self.assertRaises(sqlite3.DatabaseError):
                    SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(21, db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0])
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='demo_budget_requests'").fetchone())
            SQLiteRepository(path).close()


if __name__ == '__main__':
    unittest.main()
