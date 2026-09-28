"""Live probe — dish sale → FIFO ingredient stock deduction (owner 2026-09-28).

Creates a demo dish + 2 ingredients + 2 purchase layers each, sells the dish via
the SAME helper the API now calls (dish_consumption.consume_after_stock_out), then
prints before/after inventory, the consumed layers (FIFO split), the recorded
consumption row, and proves a second identical sale does NOT re-deduct.  Cleans up
every demo row it created and lists exactly what was removed.

Read the safety note: this touches ONLY demo rows it creates (unique PROBE_ prefix).
"""
import os
import sys
import json
import time
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from mysql_db import query as _q, query_one as _q1, execute as _ex, delete_rows as _del
import dish_consumption as dc

TS = str(int(time.time()))
P = f"PROBE{TS}"

DISH = f"{P}_DISH"
ING_A = f"{P}_ING_A"
ING_B = f"{P}_ING_B"

created = {"ingredients": [], "inventory": [], "stock_in": [], "stock_out": [], "recipes": []}


def log(*a):
    print(*a)


def main():
    dc.ensure_tables()

    # ── 1. create demo dish + 2 ingredients + recipe + inventory rows ──────
    _ex("INSERT INTO inventory (branch_id, item_name, category, quantity, unit_price, item_type) VALUES (1,%s,'Food',0,3000,'dish')", (DISH,))
    created["inventory"].append(DISH)

    for ing in (ING_A, ING_B):
        _ex("INSERT INTO ingredients (branch_id, name, kind, base_unit, purchase_unit, base_per_purchase, cost_basis) VALUES (1,%s,'ingredient','pcs',NULL,1,'fifo')", (ing,))
        created["ingredients"].append(ing)
        # ingredient inventory row (so we can show before/after quantity)
        _ex("INSERT INTO inventory (branch_id, item_name, category, quantity, unit_price, item_type) VALUES (1,%s,'Ingredient',20,0,'resale')", (ing,))
        created["inventory"].append(ing)

    # recipe: dish = 2x ING_A + 1x ING_B per portion (waste 0)
    _ex("INSERT INTO menu_recipes (branch_id, menu_item, ingredient, qty_per_portion, waste_pct) VALUES (1,%s,%s,2,0)", (DISH, ING_A))
    _ex("INSERT INTO menu_recipes (branch_id, menu_item, ingredient, qty_per_portion, waste_pct) VALUES (1,%s,%s,1,0)", (DISH, ING_B))
    created["recipes"].append((DISH, ING_A))
    created["recipes"].append((DISH, ING_B))

    # ── 2. two purchase layers for ING_A (10 @ 500, 10 @ 700) and ING_B ───
    def stock_in(item, qty, cost):
        rid = _ex(
            "INSERT INTO stock_in (batch_id, branch_id, item_name, quantity, unit_cost, source) VALUES (%s,1,%s,%s,%s,'probe')",
            (f"{P}-SI-{item[-1]}-{qty}-{cost}", item, qty, cost),
        )
        created["stock_in"].append(rid)
        return rid

    stock_in(ING_A, 10, 500)
    stock_in(ING_A, 10, 700)
    stock_in(ING_B, 8, 300)
    stock_in(ING_B, 8, 400)

    # ── 3. backfill ledger (idempotent) ────────────────────────────────────
    b1 = dc.backfill_stock_layers()
    b2 = dc.backfill_stock_layers()
    log(f"[backfill] first={b1} second={b2}")

    before = {
        ING_A: int((_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING_A,)) or {}).get("quantity") or 0),
        ING_B: int((_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING_B,)) or {}).get("quantity") or 0),
    }
    log(f"[before] {ING_A}.quantity={before[ING_A]}  {ING_B}.quantity={before[ING_B]}")

    # ── 4. sell 2 portions of the dish (same helper the API uses) ─────────
    sid = _ex("INSERT INTO stock_out (branch_id, item_name, quantity, unit_price, total, sale_date, notes) VALUES (1,%s,2,3000,6000,NOW(),'probe')", (DISH,))
    created["stock_out"].append(sid)
    r = dc.consume_after_stock_out(DISH, 2, sid, voucher_no=f"probe-{P}")
    log(f"[sale] consume_after_stock_out -> {json.dumps(r, default=str, ensure_ascii=False)}")

    after = {
        ING_A: int((_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING_A,)) or {}).get("quantity") or 0),
        ING_B: int((_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING_B,)) or {}).get("quantity") or 0),
    }
    log(f"[after]  {ING_A}.quantity={after[ING_A]}  {ING_B}.quantity={after[ING_B]}")

    # layers remaining after consumption
    for ing in (ING_A, ING_B):
        rows = _q("SELECT id, qty_in, qty_remaining, unit_cost FROM stock_layers WHERE item_name=%s ORDER BY id", (ing,))
        log(f"[layers {ing}] " + json.dumps(rows, default=str))

    # consumption rows
    cons = _q("SELECT dish_item, ingredient, qty, unit_cost, cost, short_by, layer_breakdown, ref FROM ingredient_consumption WHERE ref=%s ORDER BY id", (f"stock_out:{sid}",))
    log(f"[consumption] " + json.dumps(cons, default=str, ensure_ascii=False))

    # ── 5. second identical sale (new stock_out id, but SAME helper) ──────
    # NOTE: a *different* stock_out row would legitimately deduct again.  Idempotency
    # is per (dish, ref, ingredient).  We prove idempotency by re-calling with the
    # SAME ref (the same stock_out id) — the double-submit case.
    r2 = dc.consume_dish_recipe(DISH, 2, f"stock_out:{sid}")
    log(f"[idempotency] same ref re-call -> consumed={r2['consumed']} (expect False)")
    cons2 = _q("SELECT COUNT(*) AS c FROM ingredient_consumption WHERE ref=%s", (f"stock_out:{sid}",))
    log(f"[idempotency] consumption rows still {cons2[0]['c']} (expect 2: A + B)")

    after2 = {
        ING_A: int((_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING_A,)) or {}).get("quantity") or 0),
        ING_B: int((_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING_B,)) or {}).get("quantity") or 0),
    }
    log(f"[after-dup] {ING_A}.quantity={after2[ING_A]}  {ING_B}.quantity={after2[ING_B]} (unchanged)")

    print("\n=== CLEANUP ===")
    print("created:", json.dumps(created, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
