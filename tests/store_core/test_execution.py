from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from packages.store_core.domain import AdapterCapabilityManifest, AdapterCapability, AttemptState, ApprovalKind, Role
from packages.store_core.errors import AuthorizationError, ConflictError, NotFoundError
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.repository import InMemoryRepository
from packages.store_core.sqlite_repository import SQLiteRepository, MIGRATIONS, LATEST_SCHEMA_VERSION
from packages.store_core.synthetic_provider import DurableSyntheticProvider


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'store.sqlite3'
        self.provider_path = Path(self.temp.name) / 'provider.sqlite3'
        self.repo = SQLiteRepository(self.path)
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.app = DemoExecutionControlPlane(self.repo, lambda: self.now)
        self.ctx, self.command, self.attempt = self.setup_app(self.app)
        self.provider = DurableSyntheticProvider(self.provider_path)

    def tearDown(self):
        self.provider.close()
        self.repo.close()
        self.temp.cleanup()

    def setup_app(self, app):
        ctx = app.bootstrap_tenant('demo', 'master@example.test')
        command, _ = app.request_approval(ctx, ApprovalKind.PURCHASE, 'po:demo', {'amount_minor': 1000}, 'request', 1, 1)
        app.decide(ctx, command.id, True, 'demo')
        app.set_demo_control(ctx, command.id, 1, 1)
        app.register_adapter_manifest(ctx, AdapterCapabilityManifest(ctx.tenant_id, 'synthetic', 'demo', 'synthetic-v1',
            frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}), frozenset(), self.now))
        attempt, _ = app.prepare_attempt(ctx, command.id, 1, 1, 'synthetic-v1')
        return ctx, command, attempt

    def claim(self, worker='worker'):
        value = self.app.get_attempt(self.ctx, self.attempt.id)
        return self.app.claim_attempt(self.ctx, value.id, worker, value.version)

    def reconcile(self):
        self.now += timedelta(minutes=6)
        value = self.claim()
        return self.app.reconcile_attempt(self.ctx, value.id, 'worker', value.fencing_token, self.provider)

    def test_ex01_success_and_no_repeat(self):
        value = self.claim()
        result = self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
        self.assertEqual(1, self.provider.effect_count(self.ctx.tenant_id))
        with self.assertRaises(ConflictError):
            self.claim()
        self.assertTrue(self.app.prepare_attempt(self.ctx, self.command.id, 1, 1, 'synthetic-v1')[1])
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))

    def test_ex02_timeout_after_effect_lookup_success(self):
        self.provider.mode = 'timeout_after'
        value = self.claim()
        result = self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(AttemptState.UNKNOWN, result.state)
        self.assertEqual(AttemptState.VERIFIED_SUCCESS, self.reconcile().state)
        self.assertEqual(1, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex04_effect_survives_local_observation_failure(self):
        value = self.claim()
        with patch.object(self.repo, 'append_observation', side_effect=RuntimeError('commit failed')):
            with self.assertRaises(RuntimeError):
                self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(1, self.provider.effect_count(self.ctx.tenant_id))
        self.assertEqual(AttemptState.DISPATCHING, self.app.get_attempt(self.ctx, value.id).state)
        self.assertEqual(AttemptState.VERIFIED_SUCCESS, self.reconcile().state)
        self.assertEqual(1, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex05_stale_lease_and_fencing(self):
        first = self.claim('old')
        self.app.begin_dispatch(self.ctx, first.id, 'old', first.fencing_token)
        self.now += timedelta(minutes=2)
        second = self.claim('new')
        self.assertGreater(second.fencing_token, first.fencing_token)
        self.assertEqual(AttemptState.UNKNOWN, second.state)
        with self.assertRaises(ConflictError):
            self.app.record_observation(self.ctx, first.id, 'old', first.fencing_token, self.app._unknown())
        with self.assertRaises(ConflictError):
            self.app.begin_dispatch(self.ctx, second.id, 'new', second.fencing_token)

    def test_ex06_non_authoritative_absence_never_resends(self):
        self.provider.mode = 'timeout_before'
        value = self.claim()
        self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(AttemptState.MANUAL_REVIEW, self.reconcile().state)
        self.assertEqual(0, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex06_authoritative_absence_rechecks_then_same_key_retry(self):
        self.provider.mode = 'timeout_before'
        self.provider.authoritative_absence = True
        value = self.claim()
        self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(AttemptState.PREPARED, self.reconcile().state)
        self.provider.mode = 'success'
        value = self.claim()
        result = self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(self.attempt.operation_key, result.operation_key)
        self.assertEqual(1, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex07_stop_or_changed_policy_prevents_dispatch(self):
        value = self.claim()
        for policy, stopped in ((1, True), (2, False)):
            self.app.set_demo_control(self.ctx, self.command.id, policy, 1, stopped)
            with self.assertRaises(ConflictError):
                self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(0, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex07_expiry_and_revoked_approver_block_dispatch(self):
        member = self.app.add_member(self.ctx, 'funds@example.test', [Role.FUNDS])
        command, _ = self.app.request_approval(self.ctx, ApprovalKind.PURCHASE, 'po:second', {}, 'second', 1, 1)
        self.app.decide(member, command.id, True, 'demo')
        self.app.set_demo_control(self.ctx, command.id, 1, 1)
        attempt, _ = self.app.prepare_attempt(self.ctx, command.id, 1, 1, 'synthetic-v1')
        leased = self.app.claim_attempt(self.ctx, attempt.id, 'worker', attempt.version)
        self.app.revoke_member(self.ctx, member.user_id)
        with self.assertRaises(AuthorizationError):
            self.app.dispatch_demo(self.ctx, attempt.id, 'worker', leased.fencing_token, self.provider)
        self.now += timedelta(hours=25)
        leased = self.claim()
        with self.assertRaises(ConflictError):
            self.app.dispatch_demo(self.ctx, leased.id, 'worker', leased.fencing_token, self.provider)
        self.assertEqual(0, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex06_ex07_absence_with_stop_is_manual(self):
        self.provider.mode, self.provider.authoritative_absence = 'timeout_before', True
        value = self.claim()
        self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.app.set_demo_control(self.ctx, self.command.id, 1, 1, True)
        self.assertEqual(AttemptState.MANUAL_REVIEW, self.reconcile().state)

    def test_ex06_lookup_unavailable_bounded_manual_and_duplicate_observation(self):
        self.provider.mode = 'lookup_unavailable'
        value = self.claim()
        self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        with self.assertRaises(ConflictError):
            self.app.record_observation(self.ctx, value.id, 'worker', value.fencing_token, self.app._unknown())
        for _ in range(4):
            result = self.reconcile()
        self.assertEqual(AttemptState.MANUAL_REVIEW, result.state)
        self.assertEqual(5, len(self.repo.observations_for(self.ctx.tenant_id, value.id)))
        self.assertEqual(1, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex01_explicit_manifest_capabilities_required(self):
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(self.ctx.tenant_id, 'synthetic', 'demo', 'synthetic-v1',
            frozenset({AdapterCapability.ORDERS_WRITE}), frozenset(), self.now))
        value = self.claim()
        with self.assertRaises(ConflictError):
            self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, self.provider)
        self.assertEqual(0, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex08_concurrent_claims(self):
        def claim(index):
            repo = SQLiteRepository(self.path)
            try:
                app = DemoExecutionControlPlane(repo, lambda: self.now)
                try:
                    app.claim_attempt(self.ctx, self.attempt.id, f'worker{index}', 1)
                    return True
                except ConflictError:
                    return False
            finally:
                repo.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(1, sum(pool.map(claim, range(8))))

    def test_ex08_observation_atomic_both_backends(self):
        for repo in (self.repo, InMemoryRepository()):
            app = DemoExecutionControlPlane(repo, lambda: self.now)
            ctx, _, value = (self.ctx, self.command, self.attempt) if repo is self.repo else self.setup_app(app)
            leased = app.claim_attempt(ctx, value.id, 'worker', 1)
            dispatching = app.begin_dispatch(ctx, value.id, 'worker', leased.fencing_token)
            baseline = len(repo.audits_for(ctx.tenant_id)), len(repo.outbox_for(ctx.tenant_id))
            for method in ('append_audit', 'append_outbox', 'append_observation'):
                with patch.object(repo, method, side_effect=RuntimeError('injected')):
                    with self.assertRaises(RuntimeError):
                        app.record_observation(ctx, value.id, 'worker', leased.fencing_token, app._unknown())
                self.assertEqual(dispatching, app.get_attempt(ctx, value.id))
                self.assertEqual((), repo.observations_for(ctx.tenant_id, value.id))
                self.assertEqual(baseline, (len(repo.audits_for(ctx.tenant_id)), len(repo.outbox_for(ctx.tenant_id))))

    def test_ex09_tenant_and_non_demo_adapter_denied(self):
        other = self.app.bootstrap_tenant('other', 'other@example.test')
        with self.assertRaises(NotFoundError):
            self.app.get_attempt(other, self.attempt.id)
        value = self.claim()
        with self.assertRaises(ConflictError):
            self.app.dispatch_demo(self.ctx, value.id, 'worker', value.fencing_token, object())

    def _killed_dispatch(self, mode):
        script = '''
import os,sys
from datetime import datetime,timezone
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
r=SQLiteRepository(sys.argv[1]); a=DemoExecutionControlPlane(r,lambda:datetime(2026,9,6,tzinfo=timezone.utc))
c=a.context_for(sys.argv[3],sys.argv[4]); v=a.get_attempt(c,sys.argv[5])
v=a.claim_attempt(c,v.id,'killed',v.version)
v=a.begin_dispatch(c,v.id,'killed',v.fencing_token)
if sys.argv[6]=='after':
 p=DurableSyntheticProvider(sys.argv[2]); p.execute(v.tenant_id,v.operation_key,v.intent_digest)
os._exit(77)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path), str(self.provider_path),
            self.ctx.tenant_id, self.ctx.user_id, self.attempt.id, mode], timeout=30)
        self.assertEqual(77, result.returncode)
        self.assertEqual(AttemptState.DISPATCHING, self.app.get_attempt(self.ctx, self.attempt.id).state)
        self.assertEqual(AttemptState.VERIFIED_SUCCESS if mode == 'after' else AttemptState.MANUAL_REVIEW, self.reconcile().state)
        self.assertEqual(1 if mode == 'after' else 0, self.provider.effect_count(self.ctx.tenant_id))

    def test_ex03_kill_before_fake_effect(self):
        self._killed_dispatch('before')

    def test_ex03_kill_after_fake_effect(self):
        self._killed_dispatch('after')

    def test_ex10_v6_upgrade_preserves_approval_and_preparation(self):
        path = Path(self.temp.name) / 'v6.sqlite3'
        with patch('packages.store_core.sqlite_repository.MIGRATIONS', MIGRATIONS[:6]):
            repo = SQLiteRepository(path)
            app = DemoExecutionControlPlane(repo, lambda: self.now)
            ctx = app.bootstrap_tenant('legacy', 'legacy@example.test')
            cmd, _ = app.request_approval(ctx, ApprovalKind.PURCHASE, 'po', {}, 'key', 1, 1)
            app.decide(ctx, cmd.id, True, 'demo')
            preparation, _ = app.prepare_execution(ctx, cmd.id, 1, 1)
            repo.close()
        upgraded = SQLiteRepository(path)
        try:
            self.assertEqual(LATEST_SCHEMA_VERSION, upgraded.readiness()['schema_version'])
            self.assertEqual(preparation, upgraded.get_execution_preparation(ctx.tenant_id, cmd.id))
            self.assertEqual(preparation.canonical_digest, upgraded.get_approval_intent(ctx.tenant_id, cmd.id).canonical_digest)
        finally:
            upgraded.close()
