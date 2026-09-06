from datetime import datetime, timezone
from decimal import Decimal
import unittest

from packages.store_core import calculate_demo_price, observe_demo_inventory
from packages.store_core.errors import ConflictError


class B04Tests(unittest.TestCase):
    def test_inventory_rejects_negative_or_naive(self):
        value = observe_demo_inventory("sku", "supplier", 2, datetime.now(timezone.utc))
        self.assertEqual(2, value.quantity)
        with self.assertRaises(ConflictError): observe_demo_inventory("sku", "supplier", -1, value.observed_at)
        with self.assertRaises(ConflictError): observe_demo_inventory("sku", "supplier", 1, datetime.now())

    def test_price_guard_separates_projected_margin_and_blocks_below_ten_percent(self):
        ready = calculate_demo_price(1000, 700, fee_rate=Decimal("0.05"))
        blocked = calculate_demo_price(1000, 900, fee_rate=Decimal("0.05"))
        self.assertEqual("READY", ready.status)
        self.assertEqual("BLOCKED", blocked.status)
        self.assertEqual(Decimal("0.2500"), ready.projected_margin)

    def test_price_inputs_fail_closed(self):
        with self.assertRaises(ConflictError): calculate_demo_price(0, 1)
        with self.assertRaises(ConflictError): calculate_demo_price(100, 1, fee_rate=Decimal("1"))


if __name__ == "__main__": unittest.main()
