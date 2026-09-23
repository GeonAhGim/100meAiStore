import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from packages.store_core.channel_claim_contracts import parse_coupang_claim_page
from packages.store_core.channel_order_contracts import parse_coupang_day_page
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, Role
from packages.store_core.errors import ConflictError
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.offline_split_tracking import build_followup_split_review, build_initial_split_review
from packages.store_core.split_tracking_gateway import request_split_tracking_approval, submit_approved_split_tracking
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker
from tests.store_core import test_offline_tracking_contracts as tracking_fixtures


class SplitTrackingGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.directory = Path(self.temp.name)
        fixture = tracking_fixtures.OfflineTrackingContractTest()
        fixture.setUp()
        self.body = fixture.body
        self.body["nextToken"] = ""
        first = self.body["data"][0]["orderItems"][0]
        self.body["data"][0]["orderItems"].append(dict(first, sequenceNo="002", vendorItemId=9007199254740998))
        self.now = fixture.now
        self.claims = parse_coupang_claim_page({"code": 200, "data": [], "nextToken": ""})
        self.open()
        self.master = self.service.bootstrap_tenant("Split demo", "master@example.test")
        self.funds = self.service.add_member(self.master, "funds@example.test", [Role.FUNDS])
        self.service.register_adapter_manifest(self.master, AdapterCapabilityManifest(
            self.master.tenant_id, "synthetic", "demo", "synthetic-v1",
            frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}), frozenset(), self.now))
        self.provider = DurableSyntheticProvider(self.directory / "provider.sqlite3")

    def tearDown(self):
        self.provider.close(); self.repo.close(); self.temp.cleanup()

    def open(self):
        self.repo = SQLiteRepository(self.directory / "store.sqlite3")
        self.service = DemoExecutionControlPlane(self.repo, lambda: self.now)

    def restart(self):
        self.repo.close()
        self.open()
        self.master = self.service.context_for(self.master.tenant_id, self.master.user_id)
        self.funds = self.service.context_for(self.funds.tenant_id, self.funds.user_id)

    def initial_review(self):
        return build_initial_split_review(
            parse_coupang_day_page(self.body), self.claims, tenant_ref=self.master.tenant_id,
            connection_ref="fixture-channel", order_id="9007199254740993",
            shipment_id="9007199254740995", shipping_item_id="9007199254740997",
            invoice_number="0000123", deferred_date="2026-09-08",
            preparation_digest="a" * 64, post_preparation_reviewed=True,
            observed_at=self.now, claims_observed_at=self.now, now=self.now,
            expires_at=self.now + timedelta(seconds=60))

    def followup_review(self, initial, invoice="0000456"):
        remapped = copy.deepcopy(self.body)
        shipped = remapped["data"][0]
        waiting = copy.deepcopy(shipped)
        shipped["shipmentBoxId"] = 9007199254740999
        shipped["status"] = "DEPARTURE"
        shipped["orderItems"] = shipped["orderItems"][:1]
        waiting["orderItems"] = waiting["orderItems"][1:]
        remapped["data"].append(waiting)
        return build_followup_split_review(
            initial, parse_coupang_day_page(remapped), self.claims,
            prior_approval_digest=initial.approval_digest,
            tenant_ref=self.master.tenant_id, connection_ref="fixture-channel",
            invoice_number=invoice, preparation_digest="b" * 64,
            post_preparation_reviewed=True, observed_at=self.now,
            claims_observed_at=self.now, now=self.now, expires_at=self.now + timedelta(seconds=60))

    def approve(self, review, key):
        command, approval = request_split_tracking_approval(
            self.service, self.master, review, connection_ref="fixture-channel",
            idempotency_key=f"{key}-approval", policy_version=1, target_version=1)
        self.service.decide(self.funds, command.id, True, f"{key} reviewed")
        self.service.set_demo_control(self.master, command.id, 1, 1)
        return approval

    def submit(self, review, approval, key):
        return submit_approved_split_tracking(
            self.service, self.master, review, connection_ref="fixture-channel",
            approval_id=approval.id, idempotency_key=f"{key}-command", policy_version=1)

    def execute(self, accepted):
        event = next(row for row in self.repo.outbox_for(self.master.tenant_id)
                     if row.topic == "tool.command" and row.payload.get("command_id") == accepted["command_id"])
        return DemoToolCommandWorker(self.service, self.provider, "split-worker").process(self.master, event.id)

    def dispatched_initial(self):
        initial = self.initial_review()
        result = self.execute(self.submit(initial, self.approve(initial, "initial"), "initial"))
        self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
        return initial

    def test_initial_split_review_needs_approval_then_reaches_one_demo_effect(self):
        review = self.initial_review()
        command, approval = request_split_tracking_approval(
            self.service, self.master, review, connection_ref="fixture-channel",
            idempotency_key="split-approval", policy_version=1, target_version=1)
        pending = self.submit(review, approval, "split-pending")
        self.assertEqual("blocked", pending["state"])
        self.service.decide(self.funds, command.id, True, "split tracking reviewed")
        self.service.set_demo_control(self.master, command.id, 1, 1)
        accepted = self.submit(review, approval, "split")
        result = self.execute(accepted)
        self.assertEqual("accepted", accepted["state"])
        self.assertEqual("shipment", self.repo.get_tool_command(self.master.tenant_id, accepted["command_id"]).target_type)
        self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
        self.assertEqual(1, self.provider.effect_count(self.master.tenant_id))

    def test_followup_continues_a_verified_initial_dispatch_across_a_restart(self):
        initial = self.dispatched_initial()
        self.restart()                                            # the durable DEMO record is the only link left
        followup = self.followup_review(initial)
        accepted = self.submit(followup, self.approve(followup, "followup"), "followup")
        result = self.execute(accepted)
        command_row = self.repo.get_tool_command(self.master.tenant_id, accepted["command_id"])
        self.assertEqual(("accepted", "shipment", "9007199254740995"),
                         (accepted["state"], command_row.target_type, command_row.target_id))
        self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
        self.assertEqual(2, self.provider.effect_count(self.master.tenant_id))   # one per half of the split

    def test_followup_is_refused_until_the_initial_half_is_verified(self):
        initial = self.initial_review()
        followup = self.followup_review(initial)
        with self.assertRaises(ConflictError):                    # never dispatched
            request_split_tracking_approval(self.service, self.master, followup, connection_ref="fixture-channel",
                                            idempotency_key="early", policy_version=1, target_version=1)
        self.submit(initial, self.approve(initial, "initial"), "initial")
        with self.assertRaises(ConflictError):                    # accepted but not executed yet
            request_split_tracking_approval(self.service, self.master, followup, connection_ref="fixture-channel",
                                            idempotency_key="unverified", policy_version=1, target_version=1)
        self.assertEqual(0, self.provider.effect_count(self.master.tenant_id))

    def test_a_split_gets_one_followup_only(self):
        initial = self.dispatched_initial()
        first = self.followup_review(initial)
        second = self.followup_review(initial, invoice="0000789")
        second_approval = self.approve(second, "second")          # approved while no follow-up had gone out
        self.execute(self.submit(first, self.approve(first, "first"), "first"))
        with self.assertRaises(ConflictError):
            request_split_tracking_approval(self.service, self.master, second, connection_ref="fixture-channel",
                                            idempotency_key="second-again", policy_version=1, target_version=1)
        with self.assertRaises(ConflictError):                    # submission re-checks the durable state
            self.submit(second, second_approval, "second")
        self.assertEqual(2, self.provider.effect_count(self.master.tenant_id))


if __name__ == "__main__":
    unittest.main()
