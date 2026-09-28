"""Unit tests for item-type split: resale vs made dishes (owner 2026-09-28).

PURE tests only — no database, no writes. They exercise the new pure helpers in
routes/menu_cost.py:
  * resale_unit_cost      — purchase cost from stock_in (weighted average, NO
                            base-unit conversion)
  * classify_item_type    — auto-classification (has recipe -> dish, else resale)
  * normalize_item_type   — alias parsing + validation
  * filter_items_by_type  — ?type=resale|dish|all filtering
  * "no purchase yet"     — resale with no stock_in history must be flagged, never 0
  * food_cost thresholds  — unchanged bands (ok/warn/high)

Run:  /opt/psvibe/api/venv/bin/python tests/test_menu_type.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_menu_type.py -q
"""
import sys
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from routes.menu_cost import (
    resale_unit_cost,
    classify_item_type,
    normalize_item_type,
    filter_items_by_type,
    food_cost_status,
    food_cost_metrics,
)

D = Decimal


def main():
    passed = failed = 0
    results = []

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        results.append((name, ok, got, expected))
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    # ── resale cost from stock_in (weighted average, no conversion) ──────────
    check("resale: single purchase", resale_unit_cost([(10, 1000)]), D("100.0000"))
    check("resale: mixed quantities", resale_unit_cost([(10, 1000), (20, 3000)]), D("133.3333"))
    check("resale: rounds to 4dp", resale_unit_cost([(1, 100), (2, 300)]), D("133.3333"))
    check("resale: skips qty<=0 rows", resale_unit_cost([(0, 500), (-3, 900), (5, 500)]), D("100.0000"))
    check("resale: empty -> None (no purchase yet)", resale_unit_cost([]), None)
    check("resale: all zero qty -> None", resale_unit_cost([(0, 100)]), None)

    # ── "no purchase yet" must NOT be reported as 0-cost ─────────────────────
    uc = resale_unit_cost([])
    check("no purchase: unit cost None (not 0)", uc, None)
    m = food_cost_metrics(uc, D("1500")) if uc is not None else None
    check("no purchase: metrics None (no 0-cost revenue)", m, None)

    # ── auto-classification ──────────────────────────────────────────────────
    check("classify: has recipe -> dish", classify_item_type("resale", True), "dish")
    check("classify: explicit dish -> dish", classify_item_type("dish", False), "dish")
    check("classify: explicit dish + recipe -> dish", classify_item_type("dish", True), "dish")
    check("classify: resale no recipe -> resale", classify_item_type("resale", False), "resale")
    check("classify: None type no recipe -> resale", classify_item_type(None, False), "resale")
    check("classify: '' type no recipe -> resale", classify_item_type("", False), "resale")
    check("classify: invalid type no recipe -> resale", classify_item_type("banana", False), "resale")
    check("classify: invalid type + recipe -> dish", classify_item_type("banana", True), "dish")

    # ── normalize_item_type aliases ──────────────────────────────────────────
    check("norm: 'dish'", normalize_item_type("dish"), "dish")
    check("norm: 'Made dish'", normalize_item_type("Made dish"), "dish")
    check("norm: 'made-dish'", normalize_item_type("made-dish"), "dish")
    check("norm: 'resale'", normalize_item_type("resale"), "resale")
    check("norm: 'Ready-made'", normalize_item_type("Ready-made"), "resale")
    check("norm: 'packaged'", normalize_item_type("packaged"), "resale")
    check("norm: 'DISH' (case)", normalize_item_type("DISH"), "dish")
    check("norm: invalid -> None", normalize_item_type("xyz"), None)
    check("norm: empty -> None", normalize_item_type(""), None)
    check("norm: None -> None", normalize_item_type(None), None)

    # ── type filtering ───────────────────────────────────────────────────────
    items = [
        {"item_name": "Coca Cola", "item_type": "resale"},
        {"item_name": "ကြေးအိုးထမင်း", "item_type": "dish"},
        {"item_name": "Water", "item_type": "resale"},
    ]
    check("filter: resale", [i["item_name"] for i in filter_items_by_type(items, "resale")],
          ["Coca Cola", "Water"])
    check("filter: dish", [i["item_name"] for i in filter_items_by_type(items, "dish")],
          ["ကြေးအိုးထမင်း"])
    check("filter: all", [i["item_name"] for i in filter_items_by_type(items, "all")],
          ["Coca Cola", "ကြေးအိုးထမင်း", "Water"])
    check("filter: None (no filter)", [i["item_name"] for i in filter_items_by_type(items, None)],
          ["Coca Cola", "ကြေးအိုးထမင်း", "Water"])
    check("filter: unknown value -> all", [i["item_name"] for i in filter_items_by_type(items, "bogus")],
          ["Coca Cola", "ကြေးအိုးထမင်း", "Water"])

    # ── thresholds (unchanged bands) ─────────────────────────────────────────
    check("status: 35.0 -> ok", food_cost_status(D("35.0")), "ok")
    check("status: 35.1 -> warn", food_cost_status(D("35.1")), "warn")
    check("status: 40.0 -> warn", food_cost_status(D("40.0")), "warn")
    check("status: 40.1 -> high", food_cost_status(D("40.1")), "high")
    check("status: None -> unknown", food_cost_status(None), "unknown")

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
