from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.listing_gateway import request_listing_approval, submit_approved_listing
from packages.store_core.offline_listing_contracts import FixtureListingReview
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker


class ListingGatewayTests(unittest.TestCase):
    def test_fixture_review_requires_exact_human_approval_then_runs_demo_only(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 9, 8, tzinfo=timezone.utc)
            repo = SQLiteRepository(Path(directory) / "store.sqlite3")
            service = DemoExecutionControlPlane(repo, lambda: now)
            context = service.bootstrap_tenant("listing", "listing@example.test")
            review = FixtureListingReview(
                context.tenant_id, "coupang-demo", "vendor", "supplier", "sku-1",
                "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64,
                12000, 3, now.isoformat(), (now + timedelta(minutes=5)).isoformat())
            command, approval = request_listing_approval(
                service, context, review, offer_id="offer-1", connection_ref="coupang-demo",
                idempotency_key="listing-approval", policy_version=1, target_version=1)
            pending = submit_approved_listing(
                service, context, review, offer_id="offer-1", connection_ref="coupang-demo",
                approval_id=approval.id, idempotency_key="listing-tool", policy_version=1)
            self.assertEqual("blocked", pending["state"])
            service.decide(context, command.id, True, "reviewed listing")
            changed = replace(review, quantity=4)
            changed_result = submit_approved_listing(
                service, context, changed, offer_id="offer-1", connection_ref="coupang-demo",
                approval_id=approval.id, idempotency_key="changed-listing", policy_version=1)
            self.assertEqual("blocked", changed_result["state"])
            accepted = submit_approved_listing(
                service, context, review, offer_id="offer-1", connection_ref="coupang-demo",
                approval_id=approval.id, idempotency_key="accepted-listing", policy_version=1)
            self.assertEqual("accepted", accepted["state"])
            service.set_demo_control(context, command.id, 1, 1)
            service.register_adapter_manifest(context, AdapterCapabilityManifest(
                context.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            event = next(row for row in repo.outbox_for(context.tenant_id)
                         if row.topic == "tool.command")
            provider = DurableSyntheticProvider(Path(directory) / "provider.sqlite3")
            result = DemoToolCommandWorker(service, provider, "listing-worker").process(context, event.id)
            self.assertEqual("verified_success", result.state.value)
            self.assertEqual(1, provider.effect_count(context.tenant_id))
            provider.close(); repo.close()


if __name__ == "__main__":
    unittest.main()
