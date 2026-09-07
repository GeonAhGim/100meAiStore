import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from packages.store_core import AdapterCapability, AdapterCapabilityManifest, SQLiteRepository, StoreControlPlane
from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.contract_demo_bridge import ContractFixtureReadAdapter
from packages.store_core.domain import PurchaseOrderState, Role, SettlementStatus
from packages.store_core.errors import NotFoundError


class ContractDemoBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.fixtures = json.loads((Path(__file__).parents[1] / 'fixtures' / 'channel_orders.json').read_text(encoding='utf-8'))

    def clean(self, provider):
        body = copy.deepcopy(self.fixtures[provider])
        if provider == 'naver':
            row = body['data'][0]['productOrder']
            row.update(remainQuantity=3, initialPaymentAmount=3000, remainPaymentAmount=3000, claimStatus=None)
            bindings = {row['productId']: 'sku-naver'}
        else:
            body['nextToken'] = ''
            row = body['data'][0]
            zero = {'currencyCode': 'KRW', 'units': 0, 'nanos': 0}
            row['shippingPrice'], row['remotePrice'] = dict(zero), dict(zero)
            row['orderItems'][0].update(cancelCount=0, holdCountForCancel=0, discountPrice=dict(zero))
            bindings = {str(row['orderItems'][0]['vendorItemId']): 'sku-coupang'}
        return body, bindings

    def adapter(self, provider, body=None, bindings=None, **kwargs):
        clean, mapping = self.clean(provider)
        return ContractFixtureReadAdapter(provider, clean if body is None else body,
            sku_bindings=mapping if bindings is None else bindings, observed_at=self.now,
            clock=lambda: self.now, requested_ids=('fixture-product-order-1',),
            claim_body={'code': 200, 'data': [], 'nextToken': ''}, **kwargs)

    def test_both_channel_fixtures_reach_approval_readback_settlement_and_restart(self):
        with tempfile.TemporaryDirectory() as temp, patch('socket.socket', side_effect=AssertionError('no network')):
            path = Path(temp) / 'bridge.sqlite3'
            repo = SQLiteRepository(path)
            try:
                app = StoreControlPlane(repo, lambda: self.now)
                master = app.bootstrap_tenant('Fixture demo', 'master@example.test')
                funds = app.add_member(master, 'funds@example.test', [Role.FUNDS])
                app.add_member(master, 'catalog@example.test', [Role.CATALOG_CS])
                foreign = app.bootstrap_tenant('Foreign', 'foreign@example.test')
                order_ids = []
                for provider in ('naver', 'coupang'):
                    adapter = self.adapter(provider)
                    channel = adapter.provider
                    app.register_adapter_manifest(master, AdapterCapabilityManifest(master.tenant_id, channel, channel,
                        adapter.adapter_version, frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}),
                        frozenset({1}), self.now))
                    result = app.poll_demo_connection(master, channel, channel, 0, adapter)
                    order, replay = app.ingest_order(master, channel, result.payload_refs[0])
                    self.assertFalse(replay)
                    self.assertNotIn('PRIVATE', repo.get_normalized_payload(master.tenant_id, result.payload_refs[0]).payload_json)
                    with self.assertRaises(NotFoundError):
                        app.get_normalized_payload(foreign, result.payload_refs[0])
                    again = app.poll_demo_connection(master, channel, channel, 1, adapter)
                    self.assertEqual(1, again.replayed_count)
                    self.now += timedelta(minutes=16)
                    with self.assertRaises(ContractQuarantine):
                        app.poll_demo_connection(master, channel, channel, 2, adapter)
                    self.assertEqual(2, repo.get_poll_checkpoint(master.tenant_id, channel, channel).version)
                    self.now -= timedelta(minutes=16)
                    same, replay = app.ingest_order(master, channel, again.payload_refs[0])
                    self.assertTrue(replay); self.assertEqual(order.id, same.id)
                    lines = app.order_lines(master, order.id)
                    quotes = {line.sku: [{'supplier_id': 'fixture-supplier', 'unit_cost_minor': 500,
                                         'available_quantity': line.quantity}] for line in lines}
                    po = app.propose_routing(master, order.id, quotes)[0]
                    approval = repo.get_approval_for_command(master.tenant_id, po.approval_command_id)
                    app.decide_approval(funds, approval.id, True, 'synthetic purchase reviewed', 'fixture-nonce')
                    app.submit_demo_po(master, po.id)
                    acknowledged, _ = app.reconcile_demo_po(master, po.id,
                        {'status': 'ACKNOWLEDGED', 'provider_reference': channel + '-ack', 'observed_at': self.now})
                    self.assertEqual(PurchaseOrderState.ACKNOWLEDGED, acknowledged.status)
                    batch, _ = app.import_demo_settlement(master, channel, '2026-09', [
                        {'external_order_key': order.external_order_key, 'kind': 'SALE', 'amount_minor': order.total_minor,
                         'currency': 'KRW', 'source_row_ref': channel + '-sale'}], channel + '-settlement')
                    self.assertEqual(SettlementStatus.RECONCILED, batch.status)
                    order_ids.append(order.id)
                repo.close(); repo = SQLiteRepository(path); app = StoreControlPlane(repo, lambda: self.now)
                self.assertTrue(app.verify_audit_chain(master.tenant_id))
                for order_id in order_ids:
                    self.assertEqual(PurchaseOrderState.ACKNOWLEDGED, app.purchase_orders(master, order_id)[0].status)
            finally:
                repo.close()

    def test_partial_claim_discount_and_continuation_are_quarantined(self):
        for provider in ('naver', 'coupang'):
            with self.subTest(provider=provider), self.assertRaises(ContractQuarantine):
                self.adapter(provider, self.fixtures[provider])
            with self.assertRaises(ContractQuarantine):
                self.adapter(provider, bindings={})
        body, _ = self.clean('coupang')
        body['nextToken'] = 'more'
        with self.assertRaises(ContractQuarantine): self.adapter('coupang', body)
        body, _ = self.clean('naver')
        body['data'][0]['productOrder']['initialPaymentAmount'] = 2999
        body['data'][0]['productOrder']['remainPaymentAmount'] = 2999
        with self.assertRaises(ContractQuarantine): self.adapter('naver', body)

    def test_fixture_is_copied_and_freshness_is_rechecked_before_poll(self):
        body, mapping = self.clean('naver')
        adapter = self.adapter('naver', body, mapping)
        expected = adapter.list_changes(None).items
        body['data'].clear(); mapping.clear()
        returned = adapter.list_changes(None)
        returned.items[0]['lines'][0]['quantity'] = 99
        self.assertEqual(expected, adapter.list_changes(None).items)
        self.now += timedelta(minutes=16)
        with self.assertRaises(ContractQuarantine): adapter.list_changes(None)

    def test_missing_claim_feed_and_duplicate_sku_mapping_fail_closed(self):
        body, mapping = self.clean('coupang')
        with self.assertRaises(ContractQuarantine):
            ContractFixtureReadAdapter('coupang', body, sku_bindings=mapping,
                observed_at=self.now, clock=lambda: self.now)
        body, mapping = self.clean('naver')
        second = copy.deepcopy(body['data'][0])
        second['productOrder']['productOrderId'] = 'second-line'
        body['data'].append(second)
        with self.assertRaises(ContractQuarantine):
            ContractFixtureReadAdapter('naver', body, sku_bindings=mapping, observed_at=self.now,
                clock=lambda: self.now, requested_ids=('fixture-product-order-1', 'second-line'))


if __name__ == '__main__': unittest.main()
