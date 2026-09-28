import sys, os
sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "x")
from decimal import Decimal
import routes.menu_routes as mr
from routes.menu_cost import fifo_consume
from mysql_db import query as _q

# variant spellings + canon map + ingredients
print("=== stock_in for recipe ingredients ===")
for r in _q("SELECT si.item_name, SUM(si.quantity) q FROM stock_in si WHERE si.item_name IN (SELECT ingredient FROM menu_recipes) GROUP BY si.item_name"):
    print("  ", r)
print("=== stock_out for recipe ingredients ===")
for r in _q("SELECT so.item_name, SUM(so.quantity) q FROM stock_out so WHERE so.item_name IN (SELECT ingredient FROM menu_recipes) GROUP BY so.item_name"):
    print("  ", r)
print("=== ingredient_variants ===")
for r in _q("SELECT * FROM ingredient_variants"):
    print("  ", r)
print("=== ingredients ===")
for r in _q("SELECT name, kind, base_per_purchase, cost_basis FROM ingredients"):
    print("  ", r)

canon = mr._variant_canonical(1)
print("canon map:", canon)

# OLD estimate
est_layers = mr._canonical_fifo_layers(1, canon)
consumption, ref = mr._fifo_consumption(1, canon)
print("=== ESTIMATE consumption (canonical) ===")
for k, v in consumption.items():
    print("  ", repr(k), str(v))
print("=== ESTIMATE draw (canonical) ===")
for cname in sorted(set(est_layers) | set(consumption)):
    f = fifo_consume(est_layers.get(cname, []), consumption.get(cname, Decimal("0")))
    print("  ", repr(cname), "draw", f["unit_cost"], "remaining", f["fifo_remaining"], "onhand", f["on_hand_est"])

# LEDGER
ledger = mr._ledger_fifo_layers(1, canon)
print("=== LEDGER layers (canonical) ===")
for cname, layers in ledger.items():
    tot = sum(L["qty"] for L in layers)
    print("  ", repr(cname), "n_layers", len(layers), "total_remaining", tot, "oldest", layers[0]["unit_cost"] if layers else None)
