import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core.channel_claim_contracts import parse_coupang_claim_page
from packages.store_core.channel_order_contracts import parse_coupang_day_page
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, Role
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.offline_split_tracking import build_followup_split_review, build_initial_split_review
from packages.store_core.split_tracking_gateway import request_split_tracking_approval, submit_approved_split_tracking
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker
from tests.store_core import test_offline_tracking_contracts as tracking_fixtures


class SplitTrackingGatewayTests(unittest.TestCase):
    def test_initial_split_review_reaches_one_demo_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = tracking_fixtures.OfflineTrackingContractTest()
            fixture.setUp()
            body = fixture.body
            body["nextToken"] = ""
            first = body["data"][0]["orderItems"][0]
            body["data"][0]["orderItems"].append(dict(first, sequenceNo="002", vendorItemId=9007199254740998))
            now = fixture.now
            claims = parse_coupang_claim_page({"code": 200, "data": [], "nextToken": ""})
            repo = SQLiteRepository(Path(directory) / "store.sqlite3")
            service = DemoExecutionControlPlane(repo, lambda: now)
            master = service.bootstrap_tenant("Split demo", "master@example.test")
            funds = service.add_member(master, "funds@example.test", [Role.FUNDS])
            review = build_initial_split_review(
                parse_coupang_day_page(body), claims, tenant_ref=master.tenant_id,
                connection_ref="fixture-channel", order_id="9007199254740993",
                shipment_id="9007199254740995", shipping_item_id="9007199254740997",
                invoice_number="0000123", deferred_date="2026-09-08",
                preparation_digest="a" * 64, post_preparation_reviewed=True,
                observed_at=now, claims_observed_at=now, now=now,
                expires_at=now + timedelta(seconds=60))
            command, approval = request_split_tracking_approval(
                service, master, review, connection_ref="fixture-channel",
                idempotency_key="split-approval", policy_version=1, target_version=1)
            pending = submit_approved_split_tracking(
                service, master, review, connection_ref="fixture-channel",
                approval_id=approval.id, idempotency_key="split-pending", policy_version=1)
            self.assertEqual("blocked", pending["state"])
            service.decide(funds, command.id, True, "split tracking reviewed")
            service.set_demo_control(master, command.id, 1, 1)
            service.register_adapter_manifest(master, AdapterCapabilityManifest(
                master.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            accepted = submit_approved_split_tracking(
                service, master, review, connection_ref="fixture-channel",
                approval_id=approval.id, idempotency_key="split-command", policy_version=1)
            event = next(row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(Path(directory) / "provider.sqlite3")
            result = DemoToolCommandWorker(service, provider, "split-worker").process(master, event.id)
            self.assertEqual("accepted", accepted["state"])
            command_row = repo.get_tool_command(master.tenant_id, accepted["command_id"])
            self.assertEqual("shipment", command_row.target_type)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
            self.assertEqual(1, provider.effect_count(master.tenant_id))
            provider.close(); repo.close()

    def test_followup_split_review_reaches_remapped_shipment_target(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = tracking_fixtures.OfflineTrackingContractTest()
            fixture.setUp()
            body = fixture.body
            body["nextToken"] = ""
            first = body["data"][0]["orderItems"][0]
            body["data"][0]["orderItems"].append(dict(first, sequenceNo="002", vendorItemId=9007199254740998))
            now = fixture.now
            claims = parse_coupang_claim_page({"code": 200, "data": [], "nextToken": ""})
            repo = SQLiteRepository(Path(directory) / "store.sqlite3")
            service = DemoExecutionControlPlane(repo, lambda: now)
            master = service.bootstrap_tenant("Split followup demo", "master@example.test")
            funds = service.add_member(master, "funds@example.test", [Role.FUNDS])
            initial = build_initial_split_review(
                parse_coupang_day_page(body), claims, tenant_ref=master.tenant_id,
                connection_ref="fixture-channel", order_id="9007199254740993",
                shipment_id="9007199254740995", shipping_item_id="9007199254740997",
                invoice_number="0000123", deferred_date="2026-09-08",
                preparation_digest="a" * 64, post_preparation_reviewed=True,
                observed_at=now, claims_observed_at=now, now=now,
                expires_at=now + timedelta(seconds=60))
            remapped = copy.deepcopy(body)
            shipped = remapped["data"][0]
            waiting = copy.deepcopy(shipped)
            shipped["shipmentBoxId"] = 9007199254740999
            shipped["status"] = "DEPARTURE"
            shipped["orderItems"] = shipped["orderItems"][:1]
            waiting["orderItems"] = waiting["orderItems"][1:]
            remapped["data"].append(waiting)
            followup = build_followup_split_review(
                initial, parse_coupang_day_page(remapped), claims,
                prior_approval_digest=initial.approval_digest,
                tenant_ref=master.tenant_id, connection_ref="fixture-channel",
                invoice_number="0000456", preparation_digest="b" * 64,
                post_preparation_reviewed=True, observed_at=now,
                claims_observed_at=now, now=now, expires_at=now + timedelta(seconds=60))
            command, approval = request_split_tracking_approval(
                service, master, followup, connection_ref="fixture-channel",
                idempotency_key="followup-approval", policy_version=1, target_version=1)
            service.decide(funds, command.id, True, "followup tracking reviewed")
            service.set_demo_control(master, command.id, 1, 1)
            service.register_adapter_manifest(master, AdapterCapabilityManifest(
                master.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            accepted = submit_approved_split_tracking(
                service, master, followup, connection_ref="fixture-channel",
                approval_id=approval.id, idempotency_key="followup-command", policy_version=1)
            event = next(row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(Path(directory) / "provider.sqlite3")
            result = DemoToolCommandWorker(service, provider, "split-followup-worker").process(master, event.id)
            command_row = repo.get_tool_command(master.tenant_id, accepted["command_id"])
            self.assertEqual("accepted", accepted["state"])
            self.assertEqual("shipment", command_row.target_type)
            self.assertEqual("9007199254740995", command_row.target_id)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
            self.assertEqual(1, provider.effect_count(master.tenant_id))
            provider.close(); repo.close()


if __name__ == "__main__":
    unittest.main()
