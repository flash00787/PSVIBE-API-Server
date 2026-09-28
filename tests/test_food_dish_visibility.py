"""Unit tests for made-dish (ဟင်းပွဲ) visibility in the POS food lists (owner 2026-09-28 Osmo).

A newly created dish starts at quantity 0 but is made-to-order, so it must appear
in the Food Menu / Food & drink sale / session food note regardless of stock.
Resale (ready-made) items stay stock-gated (quantity > 0); ingredients stay excluded.

Pure + string-level only — no live MySQL.

Run:  /opt/psvibe/api/venv/bin/python tests/test_food_dish_visibility.py
      or  python3 tests/test_food_dish_visibility.py
"""
import sys, os, asyncio
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes.menu_cost import food_menu_include
import routes.food_game_routes as fg


def run(coro):
    return asyncio.run(coro)


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

    # ── Pure inclusion rule ──────────────────────────────────────────────────
    check("dish qty 0 -> included", food_menu_include("dish", 0), True)
    check("dish qty 5 -> included", food_menu_include("dish", 5), True)
    check("resale qty 5 -> included", food_menu_include("resale", 5), True)
    check("resale qty 0 -> excluded", food_menu_include("resale", 0), False)
    check("resale qty 0 (int) -> excluded", food_menu_include("resale", 0), False)
    check("resale qty 0 (Decimal) -> excluded", food_menu_include("resale", Decimal("0")), False)
    check("ingredient (kind=ingredient) -> excluded", food_menu_include("resale", 10, is_ingredient=True), False)
    check("ingredient even as 'dish' name -> excluded", food_menu_include("dish", 0, is_ingredient=True), False)
    check("alias 'Made dish' qty 0 -> included", food_menu_include("Made dish", 0), True)
    check("alias 'Ready-made' qty 0 -> excluded", food_menu_include("Ready-made", 0), False)
    check("None type qty 3 -> included (defaults resale in-stock)", food_menu_include(None, 3), True)
    check("None type qty 0 -> excluded", food_menu_include(None, 0), False)

    # ── SQL predicate (string-level) ────────────────────────────────────────
    captured = {}

    def fake_query(sql, args=None):
        captured["sql"] = sql
        captured["args"] = args
        return []

    fg._mysql_query = fake_query

    run(fg.api_fetch_food_prices(auth="x"))
    check("fetch_food_prices: dish un-gated", "item_type='dish'" in captured["sql"], True)
    check("fetch_food_prices: resale stock-gated", "quantity > 0" in captured["sql"], True)
    check("fetch_food_prices: ingredients excluded", "kind='ingredient'" in captured["sql"] and "NOT IN" in captured["sql"], True)

    run(fg.api_fetch_food_menu(all=0, auth="x"))
    check("fetch_food_menu(default): dish un-gated", "item_type='dish'" in captured["sql"], True)
    check("fetch_food_menu(default): resale stock-gated", "quantity > 0" in captured["sql"], True)
    check("fetch_food_menu(default): ingredients excluded", "kind='ingredient'" in captured["sql"], True)

    run(fg.api_fetch_food_costs(auth="x"))
    check("fetch_food_costs: dish un-gated", "item_type='dish'" in captured["sql"], True)
    check("fetch_food_costs: ingredients excluded", "kind='ingredient'" in captured["sql"], True)

    # ── Endpoint returns a dish row passed through the query (dict build) ────
    fg._mysql_query = lambda sql, args=None: [
        {"item_name": "ထမင်းဆီဆမ်း", "unit_price": Decimal("3000"), "category": "Food"},
    ]
    r = run(fg.api_fetch_food_menu(all=0, auth="x"))
    food = (r.get("data") or {}).get("Food") or {}
    check("fetch_food_menu returns dish with price 3000", food.get("ထမင်းဆီဆမ်း"), 3000)

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
