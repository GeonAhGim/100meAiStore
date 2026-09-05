"""DEMO-only dispatch and reconciliation; no production provider is accepted."""
from datetime import timedelta
from pathlib import Path
import re
from uuid import uuid4

from .domain import (AdapterCapability, AttemptState, AttemptObservation, Capability,
                     DemoExecutionControl, ExecutionAttempt, OutboxEvent, OutboxState)
from .errors import AuthorizationError, ConflictError
from .service import StoreControlPlane, _strict_intent_digest
from .synthetic_provider import DurableSyntheticProvider


class DemoExecutionControlPlane(StoreControlPlane):
    def set_demo_control(self, context, command_id, policy_version, target_version, stopped=False):
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            self.repo.get_command(context.tenant_id, command_id)
            if any(type(v) is not int or v < 1 for v in (policy_version, target_version)) or type(stopped) is not bool:
                raise ConflictError('invalid demo control')
            self.repo.save_demo_control(DemoExecutionControl(context.tenant_id, command_id, policy_version, target_version, stopped))
            self._audit(context.tenant_id, context.user_id, 'demo.control_updated', command_id, 'accepted', {'stopped': stopped})

    def _gate(self, context, command_id):
        control = self.repo.get_demo_control(context.tenant_id, command_id)
        if control.stopped:
            raise ConflictError('demo execution stopped')
        return self.prepare_execution(context, command_id, control.policy_version, control.target_version)[0]

    def _manifest(self, context, provider, connection_id, adapter_version):
        manifest = self.repo.get_adapter_manifest(context.tenant_id, provider, connection_id)
        if manifest.adapter_version != adapter_version or not {AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP} <= manifest.capabilities:
            raise ConflictError('explicit matching DEMO execution/lookup manifest required')

    def prepare_attempt(self, context, command_id, policy_version, target_version, adapter_version,
                        provider='synthetic', connection_id='demo'):
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            for value in (provider, connection_id, adapter_version):
                self._inbound_identifier(value)
            self._manifest(context, provider, connection_id, adapter_version)
            preparation = self._gate(context, command_id)
            self.prepare_execution(context, command_id, policy_version, target_version)
            key = _strict_intent_digest({'tenant': context.tenant_id, 'command': command_id, 'operation_version': 1})
            prior = self.repo.attempt_for_key(context.tenant_id, key)
            if prior:
                if (prior.adapter_version, prior.provider, prior.connection_id) != (adapter_version, provider, connection_id):
                    raise ConflictError('logical operation already bound to another adapter')
                return prior, True
            value = ExecutionAttempt(str(uuid4()), context.tenant_id, command_id, preparation.id,
                key, preparation.canonical_digest, adapter_version, provider, connection_id)
            self.repo.insert_attempt(value)
            self._event(context, value, 'attempt.prepared')
            return value, False

    def _event(self, context, value, topic):
        self._audit(context.tenant_id, context.user_id, topic, value.id, 'accepted', {'state': value.state.value})
        self.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, topic, value.id,
            {'attempt_id': value.id, 'state': value.state.value}, f'{topic}:{value.id}:{value.version}',
            OutboxState.PENDING, self._clock()))

    def get_attempt(self, context, attempt_id):
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_attempt(context.tenant_id, attempt_id)

    def claim_attempt(self, context, attempt_id, worker_id, expected_version, lease_seconds=60):
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            self._inbound_identifier(worker_id)
            if type(expected_version) is not int or type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
                raise ConflictError('invalid lease/version')
            value = self.repo.get_attempt(context.tenant_id, attempt_id)
            now = self._clock()
            if value.version != expected_version:
                raise ConflictError('attempt version conflict')
            if value.state in {AttemptState.VERIFIED_SUCCESS, AttemptState.VERIFIED_FAILURE, AttemptState.MANUAL_REVIEW}:
                raise ConflictError('attempt terminal')
            if value.lease_until and value.lease_until > now:
                raise ConflictError('attempt leased')
            if value.next_check_at and value.next_check_at > now:
                raise ConflictError('reconciliation not yet due')
            recovered = value.state in {AttemptState.DISPATCHING, AttemptState.RECONCILING}
            if recovered:
                value.state = AttemptState.UNKNOWN
            value.version += 1
            value.fencing_token += 1
            value.lease_owner, value.lease_until = worker_id, now + timedelta(seconds=lease_seconds)
            self.repo.update_attempt(value, expected_version)
            if recovered:
                self._event(context, value, 'attempt.recovered_unknown')
            return value

    def _leased(self, context, attempt_id, worker_id, token):
        self.require(context, Capability.TENANT_ADMIN)
        value = self.repo.get_attempt(context.tenant_id, attempt_id)
        if value.lease_owner != worker_id or value.fencing_token != token or not value.lease_until or value.lease_until <= self._clock():
            raise ConflictError('stale or expired attempt lease')
        return value

    def begin_dispatch(self, context, attempt_id, worker_id, token):
        with self.repo.transaction():
            value = self._leased(context, attempt_id, worker_id, token)
            if value.state != AttemptState.PREPARED:
                raise ConflictError('dispatch requires prepared state')
            preparation = self._gate(context, value.command_id)
            if preparation.canonical_digest != value.intent_digest:
                raise ConflictError('attempt intent mismatch')
            self._manifest(context, value.provider, value.connection_id, value.adapter_version)
            version = value.version
            value.version += 1
            value.state = AttemptState.DISPATCHING
            self.repo.update_attempt(value, version)
            self._event(context, value, 'attempt.dispatching')
            return value

    def begin_reconciliation(self, context, attempt_id, worker_id, token):
        with self.repo.transaction():
            value = self._leased(context, attempt_id, worker_id, token)
            if value.state != AttemptState.UNKNOWN:
                raise ConflictError('reconciliation requires UNKNOWN')
            version = value.version
            value.version += 1
            value.state = AttemptState.RECONCILING
            self.repo.update_attempt(value, version)
            self._event(context, value, 'attempt.reconciling')
            return value

    def record_observation(self, context, attempt_id, worker_id, token, observation):
        with self.repo.transaction():
            value = self._leased(context, attempt_id, worker_id, token)
            if value.state not in {AttemptState.DISPATCHING, AttemptState.RECONCILING}:
                raise ConflictError('observation requires dispatch/reconciliation')
            _strict_intent_digest(observation)
            kind = observation.get('kind')
            digest = observation.get('response_digest')
            reference = observation.get('provider_reference')
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ConflictError('invalid response digest')
            if reference is not None:
                self._inbound_identifier(reference)
            if kind not in {'FOUND_SUCCESS', 'FOUND_FAILURE', 'ABSENT', 'INCONCLUSIVE'}:
                raise ConflictError('invalid observation kind')
            if kind == 'ABSENT' and value.state != AttemptState.RECONCILING:
                raise ConflictError('absence is lookup-only')
            state = {'FOUND_SUCCESS': AttemptState.VERIFIED_SUCCESS,
                     'FOUND_FAILURE': AttemptState.VERIFIED_FAILURE}.get(kind, AttemptState.UNKNOWN)
            if kind == 'ABSENT':
                state = AttemptState.MANUAL_REVIEW
                if observation.get('authoritative_absence') is True:
                    try:
                        self._gate(context, value.command_id)
                    except (AuthorizationError, ConflictError):
                        pass
                    else:
                        state = AttemptState.PREPARED
            prior_count = len(self.repo.observations_for(context.tenant_id, value.id))
            if state == AttemptState.UNKNOWN and prior_count >= 4:
                state = AttemptState.MANUAL_REVIEW
            version = value.version
            value.version += 1
            value.state = state
            value.provider_reference = reference or value.provider_reference
            value.last_observed_at = self._clock()
            value.next_check_at = self._clock() + timedelta(seconds=min(300, 30 * (2 ** min(prior_count, 4)))) if state == AttemptState.UNKNOWN else None
            value.lease_owner = value.lease_until = None
            self.repo.update_attempt(value, version)
            self.repo.append_observation(AttemptObservation(str(uuid4()), context.tenant_id, value.id,
                kind, digest, self._clock(), str(uuid4())))
            self._event(context, value, 'attempt.observed')
            return value

    def _provider(self, value, adapter):
        # Exact concrete type intentionally excludes production/subclass callbacks.
        if type(adapter) is not DurableSyntheticProvider or adapter.execution_mode != 'DEMO' or adapter.adapter_version != value.adapter_version:
            raise ConflictError('only durable synthetic DEMO provider allowed')
        if hasattr(self.repo, 'path') and Path(self.repo.path).resolve() == Path(adapter.path).resolve():
            raise ConflictError('fake provider ledger must be separate')

    @staticmethod
    def _unknown():
        return {'kind': 'INCONCLUSIVE', 'response_digest': _strict_intent_digest({'result': 'unknown'}), 'provider_reference': None}

    def dispatch_demo(self, context, attempt_id, worker_id, token, adapter):
        value = self.get_attempt(context, attempt_id)
        self._provider(value, adapter)
        value = self.begin_dispatch(context, attempt_id, worker_id, token)
        try:
            result = adapter.execute(value.tenant_id, value.operation_key, value.intent_digest)
        except TimeoutError:
            result = self._unknown()
        return self.record_observation(context, attempt_id, worker_id, token, result)

    def reconcile_attempt(self, context, attempt_id, worker_id, token, adapter):
        value = self.get_attempt(context, attempt_id)
        self._provider(value, adapter)
        value = self.begin_reconciliation(context, attempt_id, worker_id, token)
        try:
            result = adapter.lookup(value.tenant_id, value.operation_key, value.intent_digest)
        except TimeoutError:
            result = self._unknown()
        return self.record_observation(context, attempt_id, worker_id, token, result)
