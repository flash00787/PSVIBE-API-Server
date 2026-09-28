"""Unit tests for FIFO cost basis (owner 2026-09-28 Osmo "1 FIFO နဲ့ တွက်ပါ").

PURE tests only — no database, no writes.  Exercises the new pure helpers in
routes/menu_cost.py:
  * normalize_cost_basis   — alias parsing now accepts 'fifo'
  * fifo_layers            — build the FIFO queue (oldest -> newest, qty>0 only)
  * fifo_consume           — consume a qty FIFO and report the draw layer
  * select_unit_cost       — fifo vs last vs average (with safe fallbacks)
  * aggregate_canonical_purchases — merged variants still aggregate for FIFO

Run:  /opt/psvibe/api/venv/bin/python tests/test_fifo_basis.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_fifo_basis.py -q
"""
import sys
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from routes.menu_cost import (
    normalize_cost_basis,
    fifo_layers,
    fifo_consume,
    fifo_cost,
    select_unit_cost,
    aggregate_canonical_purchases,
)

D = Decimal


def main():
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    # ── normalize_cost_basis accepts fifo ───────────────────────────────────
    check("basis: 'fifo'", normalize_cost_basis("fifo"), "fifo")
    check("basis: 'FIFO' (case)", normalize_cost_basis("FIFO"), "fifo")
    check("basis: 'first_in_first_out'", normalize_cost_basis("first_in_first_out"), "fifo")
    check("basis: 'first-in-first-out'", normalize_cost_basis("first-in-first-out"), "fifo")
    check("basis: ဝယ်စဉ်လိုက်", normalize_cost_basis("ဝယ်စဉ်လိုက်"), "fifo")
    check("basis: last still works", normalize_cost_basis("last"), "last")
    check("basis: average still works", normalize_cost_basis("average"), "average")
    check("basis: invalid -> None", normalize_cost_basis("banana"), None)

    # ── fifo_layers (multiple layers, ordering, qty<=0, unit cost) ─────────
    rows = [
        (30, 21000, "2026-09-27 04:52:30", 416),   # newest — last purchase 700
        (10, 5000, "2026-06-08 14:05:59", 36),      # oldest 500
        (9, 4500, "2026-06-07 08:42:34", 16),       # oldest 500
        (24, 15600, "2026-09-12 05:15:15", 381),    # 650
        (5, 0, "2026-09-06 05:13:46", 358),         # zero-cost (unit 0, qty>0)
        (0, 0, "2026-09-06 05:13:46", 999),         # qty<=0 -> skipped
    ]
    layers = fifo_layers(rows)
    check("layers: qty<=0 row skipped", len(layers), 5)
    check("layers: sorted oldest first", layers[0]["created_at"], "2026-06-07 08:42:34")
    check("layers: newest last", layers[-1]["created_at"], "2026-09-27 04:52:30")
    check("layers: oldest unit 500", layers[0]["unit_cost"], D("500.0000"))
    check("layers: 30 @ 21000 -> 700", layers[-1]["unit_cost"], D("700.0000"))
    check("layers: zero-cost 5 @ 0 -> 0", layers[2]["unit_cost"], D("0.0000"))
    check("layers: empty -> []", fifo_layers([]), [])

    # ── fifo_consume (partial consumption across layers) ────────────────────
    # layer list: 500(9), 500(10), 0(5), 650(24), 700(30)  (qty order after sort)
    # consumption 44 -> consume 9+10+5+20 of the 650 layer (24-20=4 remain)
    r = fifo_consume(layers, D("44"))
    check("consume: draw layer cost 650", r["unit_cost"], D("650.0000"))
    check("consume: draw layer date 12 Sep", r["fifo_layer_date"], "2026-09-12 05:15:15")
    check("consume: remaining in layer 4", r["fifo_remaining"], D("4"))
    check("consume: on_hand_est 34 (4+30)", r["on_hand_est"], D("34"))
    check("consume: consumed 44", r["consumed"], D("44"))
    check("consume: not out of stock", r["out_of_stock"], False)

    # single layer
    single = fifo_layers([(10, 5000, "2026-06-08", 1)])
    r2 = fifo_consume(single, D("3"))
    check("single: draw cost 500", r2["unit_cost"], D("500.0000"))
    check("single: remaining 7", r2["fifo_remaining"], D("7"))
    check("single: on_hand 7", r2["on_hand_est"], D("7"))

    # out-of-stock (consume everything)
    r3 = fifo_consume(single, D("10"))
    check("oos: unit_cost None", r3["unit_cost"], None)
    check("oos: on_hand 0", r3["on_hand_est"], D("0"))
    check("oos: out_of_stock True", r3["out_of_stock"], True)

    # no layers -> no purchase (out_of_stock False)
    r4 = fifo_consume([], D("0"))
    check("empty: unit_cost None", r4["unit_cost"], None)
    check("empty: out_of_stock False (no purchase)", r4["out_of_stock"], False)

    # consumption larger than total -> fully consumed, on_hand 0
    r5 = fifo_consume(single, D("999"))
    check("over: unit_cost None", r5["unit_cost"], None)
    check("over: consumed capped 10", r5["consumed"], D("10"))
    check("over: on_hand 0", r5["on_hand_est"], D("0"))

    # zero consumption -> oldest layer is the draw layer
    r6 = fifo_consume(layers, D("0"))
    check("zero: draw = oldest 500", r6["unit_cost"], D("500.0000"))
    check("zero: on_hand = total 78", r6["on_hand_est"], D("78"))  # 9+10+5+24+30

    # ── select_unit_cost fifo vs last vs average ────────────────────────────
    check("select: fifo -> fifo layer cost", select_unit_cost(D("578.634"), D("700.0000"), "fifo", D("650.0000")), D("650.0000"))
    check("select: fifo falls back to last when layer None", select_unit_cost(D("578.634"), D("700.0000"), "fifo", None), D("700.0000"))
    check("select: fifo falls back to avg when both missing", select_unit_cost(D("578.634"), None, "fifo", None), D("578.634"))
    check("select: last unchanged", select_unit_cost(D("578.634"), D("700.0000"), "last", D("650.0000")), D("700.0000"))
    check("select: average unchanged", select_unit_cost(D("578.634"), D("700.0000"), "average", D("650.0000")), D("578.634"))

    # ── merged variants in FIFO ─────────────────────────────────────────────
    # ကြက်ဥ + ကြက်ဉ + Egg all map to canonical ကြက်ဥ
    canon = {"ကြက်ဉ": "ကြက်ဥ", "Egg": "ကြက်ဥ"}
    merged_rows = [
        ("ကြက်ဥ", 30, 21000, "2026-09-27 04:52:30"),
        ("ကြက်ဥ", 9, 4500, "2026-06-07 08:42:34"),
        ("ကြက်ဉ", 30, 18000, "2026-09-03 10:22:59"),   # 600
        ("Egg", 5, 0, "2026-09-06 05:13:46"),            # zero cost
    ]
    agg = aggregate_canonical_purchases(merged_rows, canon)
    check("merge: single canonical", sorted(agg.keys()), ["ကြက်ဥ"])
    check("merge: total qty 74", agg["ကြက်ဥ"]["total_qty"], D("74"))
    # build FIFO layers from merged rows and consume 44
    merged_layers = fifo_layers([(r[1], r[2], r[3]) for r in merged_rows])
    # sorted: 9@500(Jun7), 5@0(Sep6), 30@600(Sep3)... wait sort by created_at:
    #   Jun 7 9@500, Sep 3 30@600, Sep 6 5@0, Sep 27 30@700
    mr = fifo_consume(merged_layers, D("14"))  # 9 + 5 of the 600 layer -> draw 600
    check("merge-fifo: draw layer 600 (ကြက်ဉ)", mr["unit_cost"], D("600.0000"))
    check("merge-fifo: remaining 25 in 600 layer", mr["fifo_remaining"], D("25"))

    # ── fifo_cost convenience wrapper ───────────────────────────────────────
    c, dt, rem, onh = fifo_cost(layers, D("44"))
    check("fifo_cost: unit 650", c, D("650.0000"))
    check("fifo_cost: date 12 Sep", dt, "2026-09-12 05:15:15")
    check("fifo_cost: remaining 4", rem, D("4"))
    check("fifo_cost: on_hand 34", onh, D("34"))

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
