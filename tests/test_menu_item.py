"""Unit tests for the self-serve dish endpoints (owner 2026-09-28).

Covers POST /api/menu/item (create) and PATCH /api/menu/item (rename / category /
selling-price) WITHOUT touching the live MySQL — the mysql_db helpers are monkey-
patched onto ``routes.menu_routes`` and ``_ensure_tables`` is short-circuited, so
the endpoint logic (validation, unique-name, price update) runs against an in-
memory fake inventory.

Run:  /opt/psvibe/api/venv/bin/python tests/test_menu_item.py
"""
import sys, os, asyncio
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes import menu_routes as mr
from routes.menu_cost import normalize_item_name, parse_selling_price, food_cost_metrics

D = Decimal


class FakeRequest:
    class _State:
        branch_id = 1
    state = _State()


class FakeDB:
    """In-memory inventory with just enough SQL support for the item endpoints."""
    def __init__(self):
        self.inventory = []   # list of dicts
        self.next_id = 1

    def q1(self, sql, args):
        s = sql.upper()
        if "AND ID<>%S" in s:
            name, exclude = args[0], args[1]
            for r in self.inventory:
                if r["item_name"].lower() == str(name).lower() and r["id"] != exclude:
                    return dict(r)
            return None
        if "WHERE ID=%S" in s:
            for r in self.inventory:
                if r["id"] == args[0]:
                    return dict(r)
            return None
        if "FROM INVENTORY WHERE ITEM_NAME=%S" in s:
            for r in self.inventory:
                if r["item_name"].lower() == str(args[0]).lower():
                    return dict(r)
            return None
        return None

    def ex(self, sql, args):
        s = sql.upper()
        if s.lstrip().startswith("INSERT"):
            rid = self.next_id
            self.next_id += 1
            self.inventory.append({
                "id": rid, "item_name": args[1], "category": args[2],
                "quantity": args[3], "unit_price": args[4],
            })
            return rid
        if s.lstrip().startswith("UPDATE"):
            rid = args[-1]
            for r in self.inventory:
                if r["id"] == rid:
                    r["item_name"] = args[0]
                    r["category"] = args[1]
                    r["unit_price"] = args[2]
            return 1
        return 0


def install(fake):
    """Point routes.menu_routes at the fake DB and short-circuit table creation."""
    mr._tables_ready = True
    mr._q1 = fake.q1
    mr._ex = fake.ex


def run(coro):
    return asyncio.run(coro)


def main():
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

    def code_of(name, fn):
        """Run fn, expect HTTPException, return its status_code."""
        from fastapi import HTTPException
        try:
            run(fn())
            return None  # no exception raised
        except HTTPException as e:
            return e.status_code

    # ── Pure validation helpers ──────────────────────────────────────────
    check("normalize: trims", normalize_item_name("  Rice  "), "Rice")
    check("normalize: empty string -> None", normalize_item_name(""), None)
    check("normalize: None -> None", normalize_item_name(None), None)
    check("normalize: whitespace -> None", normalize_item_name("   "), None)
    check("price: '3000' -> 3000", parse_selling_price("3000"), D("3000"))
    check("price: 0 allowed", parse_selling_price(0), D("0"))
    check("price: negative -> None", parse_selling_price(-5), None)
    check("price: None -> None", parse_selling_price(None), None)
    check("price: 'abc' -> None", parse_selling_price("abc"), None)
    # price-update → food-cost % recomputes (fixed portion cost 200)
    m1 = food_cost_metrics(D("200"), D("1000"))
    m2 = food_cost_metrics(D("200"), D("800"))
    check("recompute: 200/1000 -> 20.0%", m1["food_cost_pct"], D("20.0"))
    check("recompute: 200/800 -> 25.0%", m2["food_cost_pct"], D("25.0"))

    # ── POST /api/menu/item ──────────────────────────────────────────────
    fb = FakeDB(); install(fb)
    r = run(mr.create_menu_item(body={"item_name": "ကြေးအိုးထမင်း", "category": "Food", "unit_price": 3000},
                                request=FakeRequest(), user={"role": "admin"}))
    check("create: returns success + created row", r.get("success"), True)
    check("create: selling_price 3000", r["data"]["selling_price"], 3000.0)
    check("create: quantity 0", r["data"]["quantity"], 0)
    check("create: category Food", r["data"]["category"], "Food")
    check("create: inserted into inventory", len(fb.inventory), 1)

    fb2 = FakeDB(); install(fb2)
    check("create: empty name -> 422", code_of("empty-name", lambda: mr.create_menu_item(body={"item_name": "  "}, request=FakeRequest(), user={"role": "admin"})), 422)
    check("create: negative price -> 422", code_of("neg-price", lambda: mr.create_menu_item(body={"item_name": "X", "unit_price": -1}, request=FakeRequest(), user={"role": "admin"})), 422)
    run(mr.create_menu_item(body={"item_name": "Dup", "unit_price": 10}, request=FakeRequest(), user={"role": "admin"}))
    check("create: duplicate name -> 409", code_of("dup", lambda: mr.create_menu_item(body={"item_name": "dup", "unit_price": 20}, request=FakeRequest(), user={"role": "admin"})), 409)

    # ── PATCH /api/menu/item ─────────────────────────────────────────────
    fb3 = FakeDB(); install(fb3)
    run(mr.create_menu_item(body={"item_name": "လက်ဖက်ထမင်း", "unit_price": 2500}, request=FakeRequest(), user={"role": "admin"}))
    r = run(mr.update_menu_item(body={"item_name": "လက်ဖက်ထမင်း", "unit_price": 3000}, request=FakeRequest(), user={"role": "admin"}))
    check("patch: price update -> 3000", r["data"]["selling_price"], 3000.0)
    r = run(mr.update_menu_item(body={"item_name": "လက်ဖက်ထမင်း", "new_name": "လက်ဖက်ထမင်း အကြီး"}, request=FakeRequest(), user={"role": "admin"}))
    check("patch: rename", r["data"]["item_name"], "လက်ဖက်ထမင်း အကြီး")
    check("patch: not found -> 404", code_of("404", lambda: mr.update_menu_item(body={"item_name": "nope", "unit_price": 1}, request=FakeRequest(), user={"role": "admin"})), 404)
    check("patch: no update fields -> 422", code_of("no-upd", lambda: mr.update_menu_item(body={"item_name": "လက်ဖက်ထမင်း အကြီး"}, request=FakeRequest(), user={"role": "admin"})), 422)
    run(mr.create_menu_item(body={"item_name": "Other", "unit_price": 5}, request=FakeRequest(), user={"role": "admin"}))
    check("patch: rename collision -> 409", code_of("clash", lambda: mr.update_menu_item(body={"item_name": "လက်ဖက်ထမင်း အကြီး", "new_name": "Other"}, request=FakeRequest(), user={"role": "admin"})), 409)

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
