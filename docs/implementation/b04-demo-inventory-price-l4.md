# B04 local DEMO inventory and price guard — L4 packet

Only deterministic fixture observations are accepted. Inventory observations
are immutable snapshots; price calculation keeps integer minor units and
separates projected contribution from any later realized settlement. A sell
price with projected margin below 10% is `BLOCKED` and cannot be proposed.
No channel/supplier write, order, payment, or network call is made.

`observe_demo_inventory(sku, supplier_id, quantity, observed_at)` rejects
negative quantities and naive timestamps. `calculate_demo_price(selling_price,
supply_cost, variable_cost, fee_rate)` returns the projected contribution and
margin with a `READY`/`BLOCKED` guard; no automatic price mutation occurs.

Implementation evidence: `tests/store_core/test_b04_inventory_price.py` covers
inventory validation, projected contribution, and the 10% guard. Full
verification: `python -B -m pytest -q -p no:cacheprovider` — 100 passed on
2026-09-06; compileall, `git diff --check`, and repository secret-pattern scan
passed.
