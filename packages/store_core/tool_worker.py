"""Durable DEMO-only consumer for approved typed tool commands."""
from __future__ import annotations

from datetime import timedelta

from .domain import AttemptState, Capability, OutboxState
from .errors import ConflictError, NotFoundError
from .execution import DemoExecutionControlPlane
from .synthetic_provider import DurableSyntheticProvider


class DemoToolCommandWorker:
    """Drive one durable outbox item through the synthetic execution ledger.

    This class deliberately accepts the exact synthetic provider type through
    ``DemoExecutionControlPlane``.  It cannot dispatch a production adapter.
    """

    def __init__(self, service: DemoExecutionControlPlane,
                 provider: DurableSyntheticProvider, worker_id: str,
                 *, lease_seconds: int = 60) -> None:
        if type(service) is not DemoExecutionControlPlane:
            raise ConflictError("DEMO execution control plane required")
        service._inbound_identifier(worker_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ConflictError("invalid worker lease")
        self.service = service
        self.provider = provider
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def process(self, context, event_id: str):
        self.service.require(context, Capability.TENANT_ADMIN)
        now = self.service._clock()
        event = next((row for row in self.service.repo.outbox_for(context.tenant_id)
                      if row.id == event_id), None)
        if event is None:
            raise NotFoundError("outbox event not found")
        if event.topic != "tool.command" or event.state == OutboxState.COMPLETED:
            raise ConflictError("processable tool command event required")
        event = self.service.repo.claim_outbox(
            context.tenant_id, event.id, self.worker_id, now,
            now + timedelta(seconds=self.lease_seconds))
        command_id = event.payload.get("command_id")
        command = self.service.repo.get_tool_command(context.tenant_id, command_id)
        if (event.aggregate_ref != command.id or command.state != "accepted"
                or command.mode != "DEMO" or not command.approval_command_id
                or not command.intent_digest
                or event.payload.get("mode") != "DEMO"
                or event.payload.get("state") != "accepted"
                or event.payload.get("intent_digest") != command.intent_digest):
            raise ConflictError("tool command event binding mismatch")
        intent = self.service.repo.get_approval_intent(
            context.tenant_id, command.approval_command_id)
        if intent is None or intent.canonical_digest != command.intent_digest:
            raise ConflictError("approved command intent missing or changed")
        attempt, _ = self.service.prepare_attempt(
            context, command.approval_command_id, command.requested_policy_version,
            intent.target_version, "synthetic-v1")
        if attempt.state == AttemptState.PREPARED:
            leased = self.service.claim_attempt(
                context, attempt.id, self.worker_id, attempt.version, self.lease_seconds)
            attempt = self.service.dispatch_demo(
                context, leased.id, self.worker_id, leased.fencing_token, self.provider)
        elif attempt.state == AttemptState.UNKNOWN:
            leased = self.service.claim_attempt(
                context, attempt.id, self.worker_id, attempt.version, self.lease_seconds)
            attempt = self.service.reconcile_attempt(
                context, leased.id, self.worker_id, leased.fencing_token, self.provider)
        terminal = attempt.state in {
            AttemptState.VERIFIED_SUCCESS, AttemptState.VERIFIED_FAILURE,
            AttemptState.MANUAL_REVIEW,
        }
        self.service.repo.checkpoint_outbox(
            context.tenant_id, event.id, self.worker_id, event.fencing_token,
            {"attempt_id": attempt.id, "state": attempt.state.value},
            self.service._clock(), completed=terminal)
        return attempt
