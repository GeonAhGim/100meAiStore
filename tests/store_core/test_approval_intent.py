import subprocess
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from packages.store_core import ApprovalKind, AuthorizationError, ConflictError, Role, SQLiteRepository, StoreControlPlane
from packages.store_core.repository import InMemoryRepository


class ApprovalIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'intent.sqlite3'
        self.repo = SQLiteRepository(self.path)
        self.now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        self.ctx = self.app.bootstrap_tenant('test', 'master@example.test')

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def request(self, app=None, ctx=None):
        return (app or self.app).request_approval(ctx or self.ctx, ApprovalKind.PURCHASE, 'po:demo',
            {'amount_minor': 1000, 'currency': 'KRW'}, 'intent-v1', 1, 1, [{'ref': 'evidence-1'}])

    def approved(self):
        command, _ = self.request()
        self.app.decide(self.ctx, command.id, True, 'checked')
        return command

    def test_ap01_ap02_request_approve_prepare_replay(self):
        command = self.approved()
        result, replay = self.app.prepare_execution(self.ctx, command.id, 1, 1)
        self.assertFalse(replay)
        self.assertEqual(command.id, self.request()[0].id)
        self.assertEqual((result, True), self.app.prepare_execution(self.ctx, command.id, 1, 1))
        self.assertEqual(1, sum(e.topic == 'execution.prepared' for e in self.repo.outbox_for(self.ctx.tenant_id)))
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))

    def test_ap03_changed_material_requires_reapproval(self):
        command = self.approved()
        for policy, target in ((2, 1), (1, 2)):
            with self.assertRaises(ConflictError):
                self.app.prepare_execution(self.ctx, command.id, policy, target)
        with self.assertRaises(ConflictError):
            self.app.request_approval(self.ctx, ApprovalKind.PURCHASE, 'po:demo', command.payload, 'intent-v1', 1, 1, [{'ref': 'changed'}])
        command.payload = {'amount_minor': 9000}
        self.repo.save_command(command)
        with self.assertRaises(ConflictError):
            self.app.prepare_execution(self.ctx, command.id, 1, 1)
        self.assertIsNone(self.repo.get_execution_preparation(self.ctx.tenant_id, command.id))

    def test_ap03_invalid_json_rejected_before_any_write(self):
        baseline = len(self.repo.audits_for(self.ctx.tenant_id))
        for payload, evidence in (({'value': object()}, []), ({'value': float('nan')}, []),
                ({1: 'ambiguous'}, []), ({}, [{'value': float('inf')}])):
            with self.assertRaises(ConflictError):
                self.app.request_approval(self.ctx, ApprovalKind.PURCHASE, 'po', payload, 'bad', 1, 1, evidence)
            self.assertIsNone(self.repo.command_id_for_key(self.ctx.tenant_id, 'bad'))
            self.assertEqual(baseline, len(self.repo.audits_for(self.ctx.tenant_id)))
            self.assertEqual((), self.repo.outbox_for(self.ctx.tenant_id))

    def test_ap04_legacy_pending_rejected_expired_superseded(self):
        legacy, _ = self.app.create_command(self.ctx, ApprovalKind.PURCHASE, 'legacy', {}, 'legacy')
        self.app.decide(self.ctx, legacy.id, True, 'checked')
        with self.assertRaises(ConflictError):
            self.app.prepare_execution(self.ctx, legacy.id, 1, 1)
        command, _ = self.request()
        with self.assertRaises(ConflictError):
            self.app.prepare_execution(self.ctx, command.id, 1, 1)
        self.app.decide(self.ctx, command.id, True, 'checked')
        self.now += timedelta(hours=24)
        with self.assertRaises(ConflictError):
            self.app.prepare_execution(self.ctx, command.id, 1, 1)
        self.now -= timedelta(hours=24)
        self.app.supersede(self.ctx, command.id, {'amount_minor': 2000}, 'replacement')
        with self.assertRaises(ConflictError):
            self.app.prepare_execution(self.ctx, command.id, 1, 1)

    def test_ap05_ap06_authority_and_tenant(self):
        member = self.app.add_member(self.ctx, 'funds@example.test', [Role.FUNDS])
        command, _ = self.request()
        self.app.decide(member, command.id, True, 'checked')
        self.app.change_member_roles(self.ctx, member.user_id, [Role.CATALOG_CS])
        with self.assertRaises(AuthorizationError):
            self.app.prepare_execution(self.ctx, command.id, 1, 1)
        with self.assertRaises(AuthorizationError):
            self.app.prepare_execution(member, command.id, 1, 1)
        other = self.app.bootstrap_tenant('other', 'other@example.test')
        with self.assertRaises(AuthorizationError):
            self.app.prepare_execution(other, command.id, 1, 1)

    def test_ap03_ap04_reject_and_changed_evidence_at_decision(self):
        command, _ = self.request()
        self.app.decide(self.ctx, command.id, False, 'rejected')
        with self.assertRaises(ConflictError):
            self.app.prepare_execution(self.ctx, command.id, 1, 1)
        repo = InMemoryRepository()
        app = StoreControlPlane(repo)
        ctx = app.bootstrap_tenant('mem', 'mem@example.test')
        command, approval = self.request(app, ctx)
        approval.evidence = ({'ref': 'changed'},)
        with self.assertRaises(ConflictError):
            app.decide(ctx, command.id, True, 'checked')

    def test_ap10_v5_upgrade_preserves_membership_and_immutable_intent(self):
        from packages.store_core.sqlite_repository import MIGRATIONS, LATEST_SCHEMA_VERSION
        legacy_path = Path(self.temp.name) / 'v5.sqlite3'
        with patch('packages.store_core.sqlite_repository.MIGRATIONS', MIGRATIONS[:5]):
            legacy = SQLiteRepository(legacy_path)
            app = StoreControlPlane(legacy)
            ctx = app.bootstrap_tenant('legacy', 'legacy@example.test')
            legacy.close()
        upgraded = SQLiteRepository(legacy_path)
        try:
            app = StoreControlPlane(upgraded)
            self.assertEqual(ctx, app.context_for(ctx.tenant_id, ctx.user_id))
            self.assertEqual(LATEST_SCHEMA_VERSION, upgraded.readiness()['schema_version'])
            command, _ = self.request(app, ctx)
            with self.assertRaises(sqlite3.IntegrityError):
                upgraded.connection.execute('UPDATE approval_intents SET target_version=2 WHERE tenant_id=? AND command_id=?', (ctx.tenant_id, command.id))
            self.assertEqual(1, upgraded.get_approval_intent(ctx.tenant_id, command.id).target_version)
        finally:
            upgraded.close()

    def test_ap07_atomic_both_backends(self):
        for repo in (self.repo, InMemoryRepository()):
            app = StoreControlPlane(repo)
            ctx = self.ctx if repo is self.repo else app.bootstrap_tenant('mem', 'mem@example.test')
            command, _ = self.request(app, ctx)
            app.decide(ctx, command.id, True, 'checked')
            baseline = len(repo.audits_for(ctx.tenant_id))
            for method in ('append_audit', 'append_outbox'):
                with patch.object(repo, method, side_effect=RuntimeError('injected')):
                    with self.assertRaises(RuntimeError):
                        app.prepare_execution(ctx, command.id, 1, 1)
                self.assertIsNone(repo.get_execution_preparation(ctx.tenant_id, command.id))
                self.assertEqual(baseline, len(repo.audits_for(ctx.tenant_id)))

    def test_ap08_concurrent_prepare(self):
        command = self.approved()
        def run(_):
            repo = SQLiteRepository(self.path)
            try:
                return StoreControlPlane(repo, lambda: self.now).prepare_execution(self.ctx, command.id, 1, 1)
            finally:
                repo.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(8)))
        self.assertEqual(1, sum(not replay for _, replay in results))
        self.assertEqual(1, len({value.id for value, _ in results}))

    def test_ap09_death_before_and_after_commit(self):
        command = self.approved()
        script = '''
import os, sys
from datetime import datetime, timezone
from packages.store_core import SQLiteRepository, StoreControlPlane
r=SQLiteRepository(sys.argv[1]); a=StoreControlPlane(r,lambda:datetime(2026,9,5,tzinfo=timezone.utc))
c=a.context_for(sys.argv[2],sys.argv[3])
if sys.argv[5]=='before': r.append_outbox=lambda e:os._exit(75)
a.prepare_execution(c,sys.argv[4],1,1)
os._exit(76)
'''
        for mode, code in (('before', 75), ('after', 76)):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path), self.ctx.tenant_id,
                self.ctx.user_id, command.id, mode], timeout=30)
            self.assertEqual(code, result.returncode)
            self.assertEqual(mode == 'before', self.repo.get_execution_preparation(self.ctx.tenant_id, command.id) is None)
        self.assertTrue(self.app.prepare_execution(self.ctx, command.id, 1, 1)[1])
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))
