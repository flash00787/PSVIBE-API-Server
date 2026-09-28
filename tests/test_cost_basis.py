"""Unit tests for cost basis (last vs average) + name-variant merge (owner 2026-09-28).

PURE tests only — no database, no writes.  Exercises the new pure helpers in
routes/menu_cost.py:
  * normalize_cost_basis     — alias parsing for 'last' / 'average'
  * last_purchase_cost       — unit cost of the MOST RECENT purchase (the real
                               egg price 700 on 27 Sep), 4 dp
  * select_unit_cost         — pick last vs average, with a safe fallback
  * normalize_variant_key    — ZWSP / space / underscore / punctuation / case /
                               Burmese ဥ↔ဉ normalisation
  * duplicate_variant_groups — grouping names that normalise to the same key
  * aggregate_canonical_purchases — summing stock_in across merged variant names
  * cost_per_base_unit + portion_cost_from — recipe costing honours the chosen
                               basis (last vs average)

Run:  /opt/psvibe/api/venv/bin/python tests/test_cost_basis.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_cost_basis.py -q
"""
import sys
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from routes.menu_cost import (
    normalize_cost_basis,
    last_purchase_cost,
    select_unit_cost,
    normalize_variant_key,
    duplicate_variant_groups,
    aggregate_canonical_purchases,
    cost_per_base_unit,
    portion_cost_from,
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

    # ── normalize_cost_basis aliases ─────────────────────────────────────────
    check("basis: 'last'", normalize_cost_basis("last"), "last")
    check("basis: 'latest'", normalize_cost_basis("latest"), "last")
    check("basis: 'LAST' (case)", normalize_cost_basis("LAST"), "last")
    check("basis: 'average'", normalize_cost_basis("average"), "average")
    check("basis: 'wavg'", normalize_cost_basis("wavg"), "average")
    check("basis: 'weighted_average'", normalize_cost_basis("weighted_average"), "average")
    check("basis: invalid -> None", normalize_cost_basis("banana"), None)
    check("basis: empty -> None", normalize_cost_basis(""), None)
    check("basis: None -> None", normalize_cost_basis(None), None)

    # ── last_purchase_cost (egg: last 700 on 27 Sep) ────────────────────────
    egg_purchases = [
        (20, 12000, "2026-06-26"), (30, 18000, "2026-09-03"),
        (24, 15600, "2026-09-12"), (30, 21000, "2026-09-27"),
    ]
    check("last: egg 700 (30 @ 21000 on 27 Sep)", last_purchase_cost(egg_purchases), D("700.0000"))
    check("last: single row 600", last_purchase_cost([(30, 18000, "2026-09-03")]), D("600.0000"))
    check("last: ignores qty<=0", last_purchase_cost([(0, 500, "2026-09-27"), (30, 21000, "2026-09-26")]), D("700.0000"))
    check("last: empty -> None (no purchase)", last_purchase_cost([]), None)
    check("last: all qty<=0 -> None", last_purchase_cost([(0, 100, "2026-09-27")]), None)
    check("last: rounds to 4dp", last_purchase_cost([(3, 1000, "2026-09-27")]), D("333.3333"))

    # ── select_unit_cost (last vs average) ───────────────────────────────────
    check("select: last -> 700", select_unit_cost(D("578.634"), D("700.0000"), "last"), D("700.0000"))
    check("select: average -> 578.634", select_unit_cost(D("578.634"), D("700.0000"), "average"), D("578.634"))
    check("select: last falls back to avg when last None", select_unit_cost(D("578.634"), None, "last"), D("578.634"))
    check("select: invalid basis -> average", select_unit_cost(D("578.634"), D("700.0000"), "bogus"), D("578.634"))

    # ── normalize_variant_key (ZWSP/space/underscore/case/Burmese ဥ↔ဉ) ───────
    check("variant: ကြက်ဥ == ကြက်ဉ", normalize_variant_key("ကြက်ဥ") == normalize_variant_key("ကြက်ဉ"), True)
    check("variant: underscores/spaces equal", normalize_variant_key("Birdy_3 in 1") == normalize_variant_key("Birdy 3 in 1"), True)
    check("variant: case-insensitive", normalize_variant_key("EGG") == normalize_variant_key("egg"), True)
    check("variant: ZWSP stripped", normalize_variant_key("ကြက်\u200bဥ") == normalize_variant_key("ကြက်ဥ"), True)
    check("variant: punctuation stripped", normalize_variant_key("a-b.c") == normalize_variant_key("a b c"), True)
    check("variant: different names differ", normalize_variant_key("ကြက်ဥ") != normalize_variant_key("Egg"), True)
    check("variant: blank -> empty key", normalize_variant_key("   "), "")

    # ── duplicate_variant_groups ─────────────────────────────────────────────
    groups = duplicate_variant_groups(["ကြက်ဥ", "ကြက်ဉ", "Egg", "ဆန်", "ဆီ", "Birdy 3 in 1", "Birdy_3 in 1"])
    check("dup: one Burmese egg group", len(groups), 2)  # egg + birdy
    egg_group = next((g for g in groups if "ကြက်ဥ" in g), None)
    check("dup: egg group has both spellings", sorted(egg_group or []), ["ကြက်ဉ", "ကြက်ဥ"])
    birdy_group = next((g for g in groups if "Birdy" in g[0]), None)
    check("dup: birdy group detected", sorted(birdy_group or []), ["Birdy 3 in 1", "Birdy_3 in 1"])
    check("dup: no groups for unique names", duplicate_variant_groups(["a", "b", "c"]), [])

    # ── aggregate_canonical_purchases (merge across variants) ───────────────
    canon = {"ကြက်ဉ": "ကြက်ဥ", "Egg": "ကြက်ဥ"}
    rows = [
        ("ကြက်ဥ", 541, 309400, "2026-09-15 08:34:54"),
        ("ကြက်ဥ", 30, 21000, "2026-09-27 04:52:30"),  # last purchase: 700/pc
        ("ကြက်ဉ", 30, 18000, "2026-09-03 10:22:59"),
        ("Egg", 5, 0, "2026-09-06 05:13:46"),
    ]
    agg = aggregate_canonical_purchases(rows, canon)
    check("merge: single canonical key", sorted(agg.keys()), ["ကြက်ဥ"])
    check("merge: total qty 606", agg["ကြက်ဥ"]["total_qty"], D("606"))
    check("merge: total cost 348400", agg["ကြက်ဥ"]["total_cost"], D("348400"))
    check("merge: weighted avg 574.9175", agg["ကြက်ဥ"]["weighted_avg"], D("574.9175"))
    check("merge: last purchase still 700", agg["ကြက်ဥ"]["last_purchase"], D("700.0000"))
    check("merge: last_in is the 27 Sep row", agg["ကြက်ဥ"]["last_in"], "2026-09-27 04:52:30")
    # identity map (no variants) keeps names separate
    agg2 = aggregate_canonical_purchases(rows, None)
    check("no-merge: 3 separate keys", sorted(agg2.keys()), ["Egg", "ကြက်ဉ", "ကြက်ဥ"])

    # ── recipe costing honours the chosen basis ─────────────────────────────
    # cost per base unit from last (700) vs average (578.634), bpp = 1 (pcs)
    cp_last = cost_per_base_unit(select_unit_cost(D("578.634"), D("700.0000"), "last"), D("1"))
    cp_avg = cost_per_base_unit(select_unit_cost(D("578.634"), D("700.0000"), "average"), D("1"))
    check("recipe: cost/base on last = 700/g", cp_last, D("700.000000"))
    check("recipe: cost/base on average = 578.634/g", cp_avg, D("578.634000"))
    # a dish using 1 egg: portion cost differs by basis
    pc_last, _ = portion_cost_from([(1, 0, "ကြက်ဥ")], {"ကြက်ဥ": cp_last})
    pc_avg, _ = portion_cost_from([(1, 0, "ကြက်ဥ")], {"ကြက်ဥ": cp_avg})
    check("recipe: 1 egg on last = 700.00", pc_last, D("700.00"))
    check("recipe: 1 egg on average = 578.63", pc_avg, D("578.63"))

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
