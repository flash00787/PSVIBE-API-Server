"""Unit tests for the Recipe / Food-Cost cost engine (owner 2026-09-28).

PURE tests only — no database, no writes. They exercise the cost math in
routes/menu_cost.py: weighted-average unit cost, base-unit conversion
(ပိဿာ / ကျပ်သား / kg / litre), waste %, rounding, missing-ingredient handling and
food-cost status thresholds.

Run:  /opt/psvibe/api/venv/bin/python tests/test_menu_cost.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_menu_cost.py -q
"""
import sys, os
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from routes.menu_cost import (
    weighted_average_cost,
    cost_per_base_unit,
    portion_cost_from,
    food_cost_metrics,
    food_cost_status,
    lookup_conversion,
    BASE_UNIT_CONVERSIONS,
)

D = Decimal


def test_helper():
    results = []
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        results.append((name, ok, got, expected))
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    # ── weighted average ──────────────────────────────────────────────
    check("wavg: simple", weighted_average_cost([(10, 1000)]), D("100.0000"))
    check("wavg: mixed quantities", weighted_average_cost([(10, 1000), (20, 3000)]), D("133.3333"))
    check("wavg: rounds to 4dp (133.3333..)", weighted_average_cost([(1, 100), (2, 300)]), D("133.3333"))
    check("wavg: skips qty<=0 rows", weighted_average_cost([(0, 500), (-3, 900), (5, 500)]), D("100.0000"))
    check("wavg: empty -> None", weighted_average_cost([]), None)
    check("wavg: all zero qty -> None", weighted_average_cost([(0, 100)]), None)

    # ── base-unit conversion ──────────────────────────────────────────
    check("ပိဿာ = 1632 g", lookup_conversion("ပိဿာ"), D("1632"))
    check("ကျပ်သား = 10 g", lookup_conversion("ကျပ်သား"), D("10"))
    check("kg = 1000 g", lookup_conversion("kg"), D("1000"))
    check("litre = 1000 ml", lookup_conversion("litre"), D("1000"))
    check("gram = 1", lookup_conversion("gram"), D("1"))
    check("unknown unit -> None", lookup_conversion("ရည်ညွှန်း"), None)

    check("cost/base: ပိဿာ 1632 Kyat -> 1/g", cost_per_base_unit(D("1632"), D("1632")), D("1.000000"))
    check("cost/base: 5000/1632 -> 3.063725/g", cost_per_base_unit(D("5000"), D("1632")), D("3.063725"))
    check("cost/base: ကျပ်သား 200 -> 20/g", cost_per_base_unit(D("200"), D("10")), D("20.000000"))
    check("cost/base: kg 3000 -> 3/g", cost_per_base_unit(D("3000"), D("1000")), D("3.000000"))
    check("cost/base: default 1 (no conversion)", cost_per_base_unit(D("1500"), D("1")), D("1500.000000"))
    check("cost/base: bpp 0 -> None", cost_per_base_unit(D("1500"), D("0")), None)
    check("cost/base: wavg None -> None", cost_per_base_unit(None, D("1632")), None)

    # ── portion cost + waste ──────────────────────────────────────────
    check("portion: 200g @ 1/g no waste", portion_cost_from([(200, 0, "rice")], {"rice": D("1.000000")}), (D("200.00"), []))
    check("portion: waste 10% -> 220", portion_cost_from([(200, 10, "rice")], {"rice": D("1.000000")}), (D("220.00"), []))
    check("portion: two ingredients sum", portion_cost_from([(100, 0, "a"), (50, 0, "b")], {"a": D("2"), "b": D("4")}), (D("400.00"), []))
    check("portion: rounds money to 2dp", portion_cost_from([(333, 0, "x")], {"x": D("1.500000")}), (D("499.50"), []))
    check("portion: missing ingredient -> None + list", portion_cost_from([(200, 0, "rice"), (50, 0, "oil")], {"rice": D("1.0")}), (None, ["oil"]))
    check("portion: empty recipe -> 0 cost", portion_cost_from([], {}), (D("0.00"), []))

    # ── metrics + status thresholds ───────────────────────────────────
    m = food_cost_metrics(D("200.00"), D("1000.00"))
    check("metrics: 200/1000 -> 20% / 800 margin", (m["food_cost_pct"], m["margin"], m["margin_pct"]), (D("20.0"), D("800.00"), D("80.0")))
    check("metrics: selling 0 -> None", food_cost_metrics(D("200"), D("0")), None)
    check("metrics: portion None -> None", food_cost_metrics(None, D("1000")), None)

    check("status: 35.0 -> ok", food_cost_status(D("35.0")), "ok")
    check("status: 20 -> ok", food_cost_status(D("20")), "ok")
    check("status: 35.1 -> warn", food_cost_status(D("35.1")), "warn")
    check("status: 40.0 -> warn", food_cost_status(D("40.0")), "warn")
    check("status: 40.1 -> high", food_cost_status(D("40.1")), "high")
    check("status: 60 -> high", food_cost_status(D("60")), "high")
    check("status: None -> unknown", food_cost_status(None), "unknown")

    return passed, failed, results


def main():
    p, f, _ = test_helper()
    print(f"\nTOTAL: {p} passed, {f} failed")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
