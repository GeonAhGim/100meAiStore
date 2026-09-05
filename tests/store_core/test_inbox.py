import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, InboxState, Role
from packages.store_core.errors import AuthorizationError, ConflictError, NotFoundError
from packages.store_core.repository import InMemoryRepository
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.service import StoreControlPlane


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'inbox.sqlite3'
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant('test', 'master@example.test')
        self.register(self.app, self.ctx)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def register(self, app, ctx, capabilities=None, versions=None):
        app.register_adapter_manifest(ctx, AdapterCapabilityManifest(ctx.tenant_id, 'demo', 'conn', '1.0',
            frozenset({AdapterCapability.INBOUND_EVENTS} if capabilities is None else capabilities),
            frozenset({1, 2} if versions is None else versions), datetime.now(timezone.utc)))

    def receive(self, app=None, ctx=None, digest='a' * 64, version=1):
        return (app or self.app).receive_inbound(ctx or self.ctx, 'demo', 'conn', 'event', version, digest, 'opaque-ref')

    def test_in01_in02_in03_receive_replay_conflict_restart(self):
        count = len(self.repo.audits_for(self.ctx.tenant_id))
        msg, replay = self.receive()
        self.assertFalse(replay)
        self.assertEqual(InboxState.RECEIVED, msg.state)
        self.assertEqual(count + 1, len(self.repo.audits_for(self.ctx.tenant_id)))
        self.assertEqual(['inbox.process_requested'], [e.topic for e in self.repo.outbox_for(self.ctx.tenant_id)])
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo)
        same, replay = self.receive()
        self.assertEqual(msg.id, same.id)
        self.assertTrue(replay)
        for kwargs in ({'digest': 'b' * 64}, {'version': 2}):
            with self.assertRaises(ConflictError):
                self.receive(**kwargs)
        self.assertEqual(count + 1, len(self.repo.audits_for(self.ctx.tenant_id)))

    def test_in04_tenant_isolation(self):
        msg, _ = self.receive()
        other = self.app.bootstrap_tenant('other', 'other@example.test')
        self.register(self.app, other)
        another, _ = self.receive(ctx=other)
        self.assertNotEqual(msg.id, another.id)
        with self.assertRaises(NotFoundError):
            self.app.get_inbox(other, msg.id)
        with self.assertRaises(NotFoundError):
            self.app.process_inbound(other, msg.id, 1)

    def test_in05_manifest_and_validation(self):
        with self.assertRaises(NotFoundError):
            self.app.receive_inbound(self.ctx, 'missing', 'conn', 'event', 1, 'a' * 64)
        for capabilities, versions in ((set(), {1}), ({AdapterCapability.INBOUND_EVENTS}, {2})):
            self.register(self.app, self.ctx, capabilities, versions)
            with self.assertRaises(ConflictError):
                self.receive()
        self.register(self.app, self.ctx)
        for raw in ('https://secret.test', 'C:/private', '../private'):
            with self.assertRaises(ConflictError):
                self.app.receive_inbound(self.ctx, 'demo', 'conn', 'event', 1, 'a' * 64, raw)
        self.assertEqual((), self.app.inbox_for(self.ctx))

    def test_in06_master_and_revoked(self):
        member = self.app.add_member(self.ctx, 'member@example.test', [Role.CATALOG_CS])
        with self.assertRaises(AuthorizationError):
            self.receive(ctx=member)
        self.app.revoke_member(self.ctx, member.user_id)
        with self.assertRaises(AuthorizationError):
            self.app.inbox_for(member)

    def test_in07_process_cas_and_replay(self):
        msg, _ = self.receive()
        with self.assertRaises(ConflictError):
            self.app.process_inbound(self.ctx, msg.id, 2)
        done, replay = self.app.process_inbound(self.ctx, msg.id, 1)
        self.assertFalse(replay)
        self.assertEqual((InboxState.PROCESSED, 2), (done.state, done.version))
        self.assertTrue(self.app.process_inbound(self.ctx, msg.id, 1)[1])
        self.assertEqual(2, len(self.repo.outbox_for(self.ctx.tenant_id)))
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))

    def test_in08_both_repositories_rollback_and_detached_reads(self):
        for repo in (self.repo, InMemoryRepository()):
            app = StoreControlPlane(repo)
            ctx = self.ctx if repo is self.repo else app.bootstrap_tenant('mem', 'mem@example.test')
            self.register(app, ctx)
            baseline = len(repo.audits_for(ctx.tenant_id))
            for method in ('append_audit', 'append_outbox'):
                with patch.object(repo, method, side_effect=RuntimeError('injected')):
                    with self.assertRaises(RuntimeError):
                        self.receive(app, ctx)
                self.assertEqual((), repo.inbox_for(ctx.tenant_id))
                self.assertEqual(baseline, len(repo.audits_for(ctx.tenant_id)))
                self.assertEqual((), repo.outbox_for(ctx.tenant_id))
            msg, _ = self.receive(app, ctx)
            msg.payload_digest = 'b' * 64
            self.assertEqual('a' * 64, repo.get_inbox(ctx.tenant_id, msg.id).payload_digest)
            for method in ('append_audit', 'append_outbox'):
                with patch.object(repo, method, side_effect=RuntimeError('injected')):
                    with self.assertRaises(RuntimeError):
                        app.process_inbound(ctx, msg.id, 1)
                self.assertEqual(InboxState.RECEIVED, repo.get_inbox(ctx.tenant_id, msg.id).state)
                self.assertEqual(1, len(repo.outbox_for(ctx.tenant_id)))

    def test_in09_independent_connections(self):
        def run(_):
            repo = SQLiteRepository(self.path)
            try:
                app = StoreControlPlane(repo)
                msg, replay = self.receive(app)
                _, processed_replay = app.process_inbound(self.ctx, msg.id, 1)
                return replay, processed_replay
            finally:
                repo.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(8)))
        self.assertEqual(1, sum(not r[0] for r in results))
        self.assertEqual(1, sum(not r[1] for r in results))
        self.assertEqual(2, len(self.repo.outbox_for(self.ctx.tenant_id)))

    def test_in10_process_crash_before_and_after_commit(self):
        script = '''
import os, sys
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.service import StoreControlPlane
r=SQLiteRepository(sys.argv[1]); a=StoreControlPlane(r)
c=a.context_for(sys.argv[2],sys.argv[3])
if sys.argv[4]=='before':
    r.append_outbox=lambda e: os._exit(71)
a.receive_inbound(c,'demo','conn','event',1,'a'*64)
os._exit(72)
'''
        for mode, code, count in (('before', 71, 0), ('after', 72, 1)):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path), self.ctx.tenant_id, self.ctx.user_id, mode], timeout=30)
            self.assertEqual(code, result.returncode)
            self.assertEqual(count, len(self.repo.inbox_for(self.ctx.tenant_id)))
            self.assertEqual(count, len(self.repo.outbox_for(self.ctx.tenant_id)))
        self.assertTrue(self.receive()[1])
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))

    def test_in10_processing_crash_rolls_back_or_replays(self):
        msg, _ = self.receive()
        script = '''
import os, sys
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.service import StoreControlPlane
r=SQLiteRepository(sys.argv[1]); a=StoreControlPlane(r)
c=a.context_for(sys.argv[2],sys.argv[3])
if sys.argv[5]=='before':
    r.append_audit=lambda e: os._exit(73)
a.process_inbound(c,sys.argv[4],1)
os._exit(74)
'''
        for mode, code, state, count in (('before', 73, InboxState.RECEIVED, 1), ('after', 74, InboxState.PROCESSED, 2)):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path), self.ctx.tenant_id,
                self.ctx.user_id, msg.id, mode], timeout=30)
            self.assertEqual(code, result.returncode)
            self.assertEqual(state, self.repo.get_inbox(self.ctx.tenant_id, msg.id).state)
            self.assertEqual(count, len(self.repo.outbox_for(self.ctx.tenant_id)))
        self.assertTrue(self.app.process_inbound(self.ctx, msg.id, 1)[1])
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))
