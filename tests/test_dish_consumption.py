"""Unit tests for dish → ingredient FIFO stock consumption (owner 2026-09-28 Osmo).

Covers two layers:

  1. PURE (no DB) — routes.menu_cost.ledger_layers / fifo_consume_split /
     blended_unit_cost: FIFO ordering, per-layer cost split, waste, insufficient
     stock (short_by), exact exhaustion, empty ledger.

  2. MOCKED DB — dish_consumption.consume_dish_recipe + backfill_stock_layers:
     idempotent backfill (second run creates nothing), idempotent sale (same ref
     never deducts twice), FIFO across two purchase layers with the correct
     per-layer split + blended unit cost.

Run:  /opt/psvibe/api/venv/bin/python tests/test_dish_consumption.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_dish_consumption.py -q
"""
import sys
import os
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from routes.menu_cost import (
    ledger_layers,
    fifo_consume_split,
    blended_unit_cost,
)

D = Decimal


# ── PURE section ──────────────────────────────────────────────────────────────
def test_pure_fifo():
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    # ledger_layers — (qty_remaining, unit_cost, purchased_at, stock_in_id)
    rows = [
        (30, D("700.0000"), "2026-09-27 04:52:30", 416),
        (9, D("500.0000"), "2026-06-07 08:42:34", 16),
        (24, D("650.0000"), "2026-09-12 05:15:15", 381),
        (0, D("100.0000"), "2026-09-06 05:13:46", 999),   # qty<=0 -> skipped
    ]
    layers = ledger_layers(rows)
    check("ledger: qty<=0 skipped", len(layers), 3)
    check("ledger: oldest first", layers[0]["created_at"], "2026-06-07 08:42:34")
    check("ledger: newest last", layers[-1]["created_at"], "2026-09-27 04:52:30")
    check("ledger: unit_cost as-is (no division)", layers[0]["unit_cost"], D("500.0000"))
    check("ledger: empty -> []", ledger_layers([]), [])

    # fifo_consume_split — single layer partial
    one = ledger_layers([(10, D("500.0000"), "2026-06-08", 1)])
    r = fifo_consume_split(one, D("3"))
    check("split: single layer 3/10", r["consumed"], D("3"))
    check("split: single cost 3*500", r["total_cost"], D("1500.00"))
    check("split: one layer used", [u["layer_id"] for u in r["layers_used"]], [1])
    check("split: qty_taken 3", r["layers_used"][0]["qty_taken"], D("3"))
    check("split: no shortfall", r["short_by"], D("0"))

    # fifo_consume_split — spanning two layers (per-layer FIFO split)
    # layers: 9 @ 500 (Jun 7), 24 @ 650 (Sep 12), 30 @ 700 (Sep 27)
    # consume 20 -> 9 @ 500 + 11 @ 650
    two = ledger_layers([
        (9, D("500.0000"), "2026-06-07 08:42:34", 16),
        (24, D("650.0000"), "2026-09-12 05:15:15", 381),
        (30, D("700.0000"), "2026-09-27 04:52:30", 416),
    ])
    r2 = fifo_consume_split(two, D("20"))
    check("split: spanning consumed 20", r2["consumed"], D("20"))
    check("split: two layers used", len(r2["layers_used"]), 2)
    check("split: layer1 = 9 @ 500", (r2["layers_used"][0]["layer_id"], r2["layers_used"][0]["qty_taken"], r2["layers_used"][0]["unit_cost"]), (16, D("9"), D("500.0000")))
    check("split: layer2 = 11 @ 650", (r2["layers_used"][1]["layer_id"], r2["layers_used"][1]["qty_taken"], r2["layers_used"][1]["unit_cost"]), (381, D("11"), D("650.0000")))
    check("split: total cost 9*500+11*650", r2["total_cost"], D("11650.00"))
    check("split: no shortfall", r2["short_by"], D("0"))

    # blended unit cost across the two layers = 11650 / 20 = 582.5
    check("blended: 11650/20 -> 582.5000", blended_unit_cost(r2), D("582.5000"))

    # waste% — caller multiplies needed before calling (mirrors consume_dish_recipe):
    # qty_per_portion 2, sold 3, waste 10% -> 2*3*1.10 = 6.6 base units
    needed = D("2") * D("3") * (D("1") + D("10") / D("100"))
    check("waste: needed 6.6", needed, D("6.6"))
    rw = fifo_consume_split(ledger_layers([(10, D("500.0000"), "2026-06-08", 1)]), needed)
    check("waste: consumed 6.6", rw["consumed"], D("6.6"))
    check("waste: cost 6.6*500", rw["total_cost"], D("3300.00"))

    # insufficient stock -> short_by (never blocks; consume what remains)
    small = ledger_layers([(4, D("500.0000"), "2026-06-08", 1)])
    rs = fifo_consume_split(small, D("10"))
    check("short: consumed capped 4", rs["consumed"], D("4"))
    check("short: short_by 6", rs["short_by"], D("6"))
    check("short: cost 4*500", rs["total_cost"], D("2000.00"))
    check("short: one layer used", len(rs["layers_used"]), 1)

    # empty ledger
    re_ = fifo_consume_split([], D("5"))
    check("empty: consumed 0", re_["consumed"], D("0"))
    check("empty: short_by 5", re_["short_by"], D("5"))
    check("empty: cost 0", re_["total_cost"], D("0.00"))
    check("empty: no layers used", re_["layers_used"], [])

    # exact exhaustion
    rx = fifo_consume_split(ledger_layers([(4, D("500.0000"), "2026-06-08", 1)]), D("4"))
    check("exact: consumed 4, short_by 0", (rx["consumed"], rx["short_by"]), (D("4"), D("0")))

    print(f"[pure] TOTAL: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} pure checks failed"


# ── MOCKED DB section ─────────────────────────────────────────────────────────
import dish_consumption as dc


class FakeStore:
    """In-memory model of the tables dish_consumption touches."""

    def __init__(self):
        self.stock_in = []        # list of dict(id, branch_id, item_name, quantity, unit_cost, created_at)
        self.layers = []          # list of dict(id, stock_in_id, item_name, qty_remaining, unit_cost, purchased_at)
        self.consumption = []     # list of dict(dish_item, ingredient, qty, unit_cost, cost, ref)
        self.recipes = []         # list of dict(menu_item, ingredient, qty_per_portion, waste_pct)
        self.inventory = {}       # name -> Decimal quantity
        self.ingredients = {}     # name -> {"base_per_purchase": Decimal}
        self._layer_id = 1

    # --- helpers used by the fake SQL dispatch ---
    def next_layer_id(self):
        i = self._layer_id
        self._layer_id += 1
        return i


def _make_fake(store):
    """Monkeypatch dish_consumption's DB access onto the in-memory store."""

    def q(sql, args=()):
        s = sql.upper()
        a = list(args)
        if "FROM STOCK_IN" in s:
            if "WHERE QUANTITY > 0" in s:
                return [dict(r) for r in store.stock_in if r["quantity"] > 0]
            return [dict(r) for r in store.stock_in]
        if "FROM MENU_RECIPES" in s:
            menu_item = a[0]
            return [dict(r) for r in store.recipes if r["menu_item"] == menu_item]
        if "FROM STOCK_LAYERS" in s:
            # consumed path: WHERE item_name IN (...) AND qty_remaining > 0
            return [
                {"id": l["id"], "qty_remaining": l["qty_remaining"], "unit_cost": l["unit_cost"]}
                for l in store.layers if l["qty_remaining"] > 0
            ]
        if "FROM INGREDIENT_VARIANTS" in s:
            return []
        return []

    def q1(sql, args=()):
        s = sql.upper()
        a = list(args)
        if "FROM STOCK_IN" in s:
            sid = a[0]
            for r in store.stock_in:
                if r["id"] == sid:
                    return dict(r)
            return None
        if "FROM STOCK_LAYERS" in s:
            sid = a[0]
            for l in store.layers:
                if l["stock_in_id"] == sid:
                    return {"id": l["id"]}
            return None
        if "FROM INVENTORY" in s:
            name = a[0]
            if name in store.inventory:
                return {"item_type": "resale"}
            return None
        if "FROM MENU_RECIPES" in s:
            # is_dish_item LIMIT 1 check
            menu_item = a[0]
            for r in store.recipes:
                if r["menu_item"] == menu_item:
                    return {"id": 1}
            return None
        if "FROM INGREDIENT_CONSUMPTION" in s:
            dish, ref, ing = a[0], a[1], a[2]
            for c in store.consumption:
                if c["dish_item"] == dish and c["ref"] == ref and c["ingredient"] == ing:
                    return {"id": 1}
            return None
        if "FROM INGREDIENTS" in s:
            name = a[0]
            ing = store.ingredients.get(name)
            if ing is None:
                return None
            return {"base_per_purchase": ing["base_per_purchase"]}
        if "FROM INGREDIENT_VARIANTS" in s:
            return None
        return None

    def ex(sql, args=()):
        s = sql.upper()
        a = list(args)
        if s.startswith("INSERT") and "STOCK_LAYERS" in s:
            # (branch_id, item_name, qty_in, qty_remaining, unit_cost, stock_in_id, purchased_at)
            store.layers.append({
                "id": store.next_layer_id(),
                "stock_in_id": a[5],
                "item_name": a[1],
                "qty_remaining": Decimal(str(a[3])),
                "unit_cost": Decimal(str(a[4])),
            })
        return 1

    class _Cur:
        def __init__(self):
            self.lastrowid = 0
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, args=()):
            s = sql.upper()
            a = list(args)
            if "UPDATE STOCK_LAYERS" in s:
                # qty_remaining = qty_remaining - %s WHERE id=%s
                take, lid = Decimal(str(a[0])), a[1]
                for l in store.layers:
                    if l["id"] == lid:
                        l["qty_remaining"] -= take
            elif "UPDATE INVENTORY" in s:
                take, name = Decimal(str(a[0])), a[1]
                if name in store.inventory:
                    store.inventory[name] = max(Decimal("0"), store.inventory[name] - take)
            elif "INSERT INTO INGREDIENT_CONSUMPTION" in s:
                # (branch_id, dish_item, ingredient, qty, unit_cost, cost, layer_breakdown, short_by, ref, voucher_no)
                store.consumption.append({
                    "dish_item": a[1], "ingredient": a[2], "qty": Decimal(str(a[3])),
                    "unit_cost": Decimal(str(a[4])), "cost": Decimal(str(a[5])),
                    "short_by": Decimal(str(a[7])), "ref": a[8],
                })

    class _Conn:
        def cursor(self):
            return _Cur()
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    dc._q = q
    dc._q1 = q1
    dc._ex = ex
    dc._get_conn = lambda: _Conn()
    dc._READY = True  # skip CREATE TABLE (already covered by startup)


def test_consume_db():
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    # ── backfill idempotency ────────────────────────────────────────────
    st = FakeStore()
    st.stock_in = [
        {"id": 1, "branch_id": 1, "item_name": "ဆန်", "quantity": 10, "unit_cost": D("500.0000"), "created_at": "2026-06-01"},
        {"id": 2, "branch_id": 1, "item_name": "ဆန်", "quantity": 20, "unit_cost": D("700.0000"), "created_at": "2026-06-02"},
    ]
    _make_fake(st)
    b1 = dc.backfill_stock_layers()
    check("backfill: created 2", b1["created"], 2)
    b2 = dc.backfill_stock_layers()
    check("backfill: second run created 0 (idempotent)", b2["created"], 0)
    check("backfill: still 2 layers total", len(st.layers), 2)

    # ── consume_dish_recipe: FIFO across 2 layers + waste + idempotent ──
    st2 = FakeStore()
    # ingredient ဆန် (rice): two purchase layers 10 @ 500, 20 @ 700
    st2.stock_in = [
        {"id": 1, "branch_id": 1, "item_name": "ဆန်", "quantity": 10, "unit_cost": D("500.0000"), "created_at": "2026-06-01"},
        {"id": 2, "branch_id": 1, "item_name": "ဆန်", "quantity": 20, "unit_cost": D("700.0000"), "created_at": "2026-06-02"},
    ]
    st2.layers = [
        {"id": 1, "stock_in_id": 1, "item_name": "ဆန်", "qty_remaining": D("10"), "unit_cost": D("500.0000")},
        {"id": 2, "stock_in_id": 2, "item_name": "ဆန်", "qty_remaining": D("20"), "unit_cost": D("700.0000")},
    ]
    st2.ingredients["ဆန်"] = {"base_per_purchase": D("1")}
    # recipe: ကြေးအိုးထမင်း needs 12 units of ဆန် per portion (waste 0%)
    st2.recipes = [{"menu_item": "ကြေးအိုးထမင်း", "ingredient": "ဆန်", "qty_per_portion": D("12"), "waste_pct": D("0")}]
    _make_fake(st2)

    res = dc.consume_dish_recipe("ကြေးအိုးထမင်း", 1, "stock_out:100")
    check("consume: consumed True", res["consumed"], True)
    # needed = 12; FIFO: 10 @ 500 + 2 @ 700
    c1 = st2.consumption[0]
    check("consume: one consumption row", len(st2.consumption), 1)
    check("consume: qty 12", c1["qty"], D("12"))
    check("consume: cost 10*500+2*700 = 6400", c1["cost"], D("6400.00"))
    check("consume: blended unit 6400/12", c1["unit_cost"], D("533.3333"))
    check("consume: no shortfall", c1["short_by"], D("0"))
    # layer 1 fully consumed, layer 2 has 18 remaining
    check("consume: layer1 remaining 0", st2.layers[0]["qty_remaining"], D("0"))
    check("consume: layer2 remaining 18", st2.layers[1]["qty_remaining"], D("18"))

    # idempotent — same ref + dish must NOT deduct again
    res2 = dc.consume_dish_recipe("ကြေးအိုးထမင်း", 1, "stock_out:100")
    check("idempotent: second call consumed False", res2["consumed"], False)
    check("idempotent: still one consumption row", len(st2.consumption), 1)
    check("idempotent: layer2 still 18", st2.layers[1]["qty_remaining"], D("18"))

    # ── insufficient stock -> short_by recorded ────────────────────────
    st3 = FakeStore()
    st3.layers = [
        {"id": 1, "stock_in_id": 1, "item_name": "ဆန်", "qty_remaining": D("3"), "unit_cost": D("500.0000")},
    ]
    st3.ingredients["ဆန်"] = {"base_per_purchase": D("1")}
    st3.recipes = [{"menu_item": "ကြေးအိုးထမင်း", "ingredient": "ဆန်", "qty_per_portion": D("12"), "waste_pct": D("0")}]
    _make_fake(st3)
    res3 = dc.consume_dish_recipe("ကြေးအိုးထမင်း", 1, "stock_out:200")
    c3 = st3.consumption[0]
    check("short: qty capped 3", c3["qty"], D("3"))
    check("short: short_by 9", c3["short_by"], D("9"))
    check("short: layer remaining 0", st3.layers[0]["qty_remaining"], D("0"))

    # ── waste% ──────────────────────────────────────────────────────────
    st4 = FakeStore()
    st4.layers = [
        {"id": 1, "stock_in_id": 1, "item_name": "ဆန်", "qty_remaining": D("100"), "unit_cost": D("500.0000")},
    ]
    st4.ingredients["ဆန်"] = {"base_per_purchase": D("1")}
    st4.recipes = [{"menu_item": "ကြေးအိုးထမင်း", "ingredient": "ဆန်", "qty_per_portion": D("10"), "waste_pct": D("10")}]
    _make_fake(st4)
    dc.consume_dish_recipe("ကြေးအိုးထမင်း", 1, "stock_out:300")
    c4 = st4.consumption[0]
    # needed = 10 * 1 * 1.10 = 11
    check("waste: qty 11 (10% waste)", c4["qty"], D("11"))

    print(f"[db] TOTAL: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} db checks failed"


def main():
    test_pure_fifo()
    test_consume_db()
    print("\nALL DISH-CONSUMPTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
