"""Unit tests for the Menu Register (owner 2026-09-28 Osmo).

Covers the new 3-way menu type + selling-price validation, the register
create/edit endpoints, and the POS exclusion of ingredients — WITHOUT touching
live MySQL (mysql_db helpers are monkey-patched onto the route modules).

Run:  /opt/psvibe/api/venv/bin/python tests/test_menu_register.py
      or  python3 tests/test_menu_register.py
"""
import sys, os, asyncio
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes import menu_routes as mr
from routes.menu_cost import (
    normalize_menu_type,
    menu_type_requires_price,
    validate_menu_type_price,
    classify_menu_type,
)

D = Decimal


class FakeRequest:
    class _State:
        branch_id = 1
    state = _State()


class FakeDB:
    """In-memory inventory + ingredients + recipes with enough SQL for the
    register endpoints (create/update) and the POS food-menu exclusion."""
    def __init__(self):
        self.inventory = {}    # name -> dict
        self.ingredients = {}  # name -> dict
        self.recipes = []      # menu_recipes rows (empty = no recipes)
        self._inv_id = 1
        self._ing_id = 1
        self.executed = []     # list of (kind, sql, args) for assertions

    def q1(self, sql, args):
        s = sql.upper()
        if "FROM INVENTORY" in s:
            name = str(args[0])
            for n, r in self.inventory.items():
                if str(n).lower() == name.lower():
                    return dict(r)
            return None
        if "FROM INGREDIENTS" in s:
            name = str(args[-1])
            for n, r in self.ingredients.items():
                if str(n).lower() == name.lower():
                    return dict(r)
            return None
        return None

    def q(self, sql, args):
        if "FROM MENU_RECIPES" in sql.upper():
            return [dict(r) for r in self.recipes]
        return []

    def ex(self, sql, args):
        s = sql.upper().lstrip()
        self.executed.append(("exec", sql, args))
        if s.startswith("INSERT"):
            if "INVENTORY" in s:
                name = args[1]
                self.inventory[name] = {
                    "id": self._inv_id, "item_name": name, "category": args[2],
                    "quantity": args[3], "unit_price": args[4], "item_type": args[6],
                }
                self._inv_id += 1
                return self.inventory[name]["id"]
            if "INGREDIENTS" in s:
                name = args[1]
                kind = args[2] if len(args) >= 3 else "ingredient"
                self.ingredients[name] = {"id": self._ing_id, "name": name, "kind": kind}
                self._ing_id += 1
                return self.ingredients[name]["id"]
            return 0
        if s.startswith("UPDATE"):
            # UPDATE inventory SET ... WHERE id=%s  (id last) / UPDATE ingredients SET ... WHERE id=%s
            rid = args[-1]
            if "INVENTORY" in s:
                for n, r in list(self.inventory.items()):
                    if r["id"] == rid:
                        # apply the common column order: item_name, category, unit_price, item_type
                        if "ITEM_NAME" in s:
                            r["item_name"] = args[0]
                        if "CATEGORY" in s:
                            r["category"] = args[1]
                        if "UNIT_PRICE" in s:
                            r["unit_price"] = args[2]
                        if "ITEM_TYPE" in s:
                            r["item_type"] = args[3]
                        return 1
            elif "INGREDIENTS" in s:
                for n, r in list(self.ingredients.items()):
                    if r["id"] == rid:
                        if "KIND" in s:
                            r["kind"] = args[-2] if len(args) >= 2 else r["kind"]
                        if "NAME" in s and "SET NAME" in s:
                            r["name"] = args[0]
                        return 1
            return 0
        return 0


def install(fake):
    mr._tables_ready = True
    mr._q1 = fake.q1
    mr._q = fake.q
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
        from fastapi import HTTPException
        try:
            run(fn())
            return None
        except HTTPException as e:
            return e.status_code

    # ── Pure: normalize_menu_type aliases ────────────────────────────────────
    check("norm: 'dish'", normalize_menu_type("dish"), "dish")
    check("norm: 'Made dish'", normalize_menu_type("Made dish"), "dish")
    check("norm: 'resale'", normalize_menu_type("resale"), "resale")
    check("norm: 'Ready-made'", normalize_menu_type("Ready-made"), "resale")
    check("norm: 'product'", normalize_menu_type("product"), "resale")
    check("norm: 'ingredient'", normalize_menu_type("ingredient"), "ingredient")
    check("norm: 'ဟင်းခံ'", normalize_menu_type("ဟင်းခံ"), "ingredient")
    check("norm: invalid -> None", normalize_menu_type("xyz"), None)
    check("norm: None -> None", normalize_menu_type(None), None)

    # ── Pure: validate_menu_type_price per type ─────────────────────────────
    check("val: dish price 3000 -> ok", validate_menu_type_price("dish", 3000), None)
    check("val: resale price 3000 -> ok", validate_menu_type_price("resale", "3000"), None)
    check("val: dish no price -> err", validate_menu_type_price("dish", None) is not None, True)
    check("val: dish price 0 -> err", validate_menu_type_price("dish", 0) is not None, True)
    check("val: resale negative -> err", validate_menu_type_price("resale", -1) is not None, True)
    check("val: ingredient no price -> ok", validate_menu_type_price("ingredient", None), None)
    check("val: ingredient price 0 -> ok", validate_menu_type_price("ingredient", 0), None)
    check("val: ingredient price 500 -> err", validate_menu_type_price("ingredient", 500) is not None, True)
    check("val: invalid type -> err", validate_menu_type_price("bogus", 5) is not None, True)
    check("requires_price: dish True", menu_type_requires_price("dish"), True)
    check("requires_price: resale True", menu_type_requires_price("resale"), True)
    check("requires_price: ingredient False", menu_type_requires_price("ingredient"), False)

    # ── Pure: classify_menu_type (ingredient kind wins) ─────────────────────
    check("classify: explicit ingredient -> ingredient", classify_menu_type("resale", False, "ingredient"), "ingredient")
    check("classify: recipe -> dish", classify_menu_type("resale", True, None), "dish")
    check("classify: resale no recipe -> resale", classify_menu_type("resale", False, None), "resale")

    # ── POST /api/menu/register ─────────────────────────────────────────────
    fb = FakeDB(); install(fb)
    r = run(mr.create_register_item(body={"item_name": "ကြေးအိုးထမင်း", "type": "dish", "unit_price": 3000},
                                    request=FakeRequest(), user={"role": "admin"}))
    check("create dish: success", r.get("success"), True)
    check("create dish: type dish", r["data"]["type"], "dish")
    check("create dish: selling 3000", r["data"]["selling_price"], 3000.0)
    check("create dish: in inventory", "ကြေးအိုးထမင်း" in fb.inventory, True)
    check("create dish: item_type stored dish", fb.inventory["ကြေးအိုးထမင်း"]["item_type"], "dish")

    fb2 = FakeDB(); install(fb2)
    r2 = run(mr.create_register_item(body={"item_name": "Coke", "type": "resale", "unit_price": 1500},
                                     request=FakeRequest(), user={"role": "admin"}))
    check("create resale: success", r2.get("success"), True)
    check("create resale: type resale", r2["data"]["type"], "resale")

    fb3 = FakeDB(); install(fb3)
    r3 = run(mr.create_register_item(body={"item_name": "ကြက်ဥ", "type": "ingredient"},
                                     request=FakeRequest(), user={"role": "admin"}))
    check("create ingredient: success", r3.get("success"), True)
    check("create ingredient: type ingredient", r3["data"]["type"], "ingredient")
    check("create ingredient: selling None", r3["data"]["selling_price"], None)
    check("create ingredient: NOT in inventory", "ကြက်ဥ" in fb3.inventory, False)
    check("create ingredient: in ingredients kind=ingredient", fb3.ingredients["ကြက်ဥ"]["kind"], "ingredient")

    # validation failures
    check("create dish no price -> 422", code_of("d-noprice", lambda: mr.create_register_item(body={"item_name": "X", "type": "dish"}, request=FakeRequest(), user={"role": "admin"})), 422)
    check("create resale price 0 -> 422", code_of("r-zero", lambda: mr.create_register_item(body={"item_name": "X", "type": "resale", "unit_price": 0}, request=FakeRequest(), user={"role": "admin"})), 422)
    check("create ingredient with price -> 422", code_of("i-price", lambda: mr.create_register_item(body={"item_name": "X", "type": "ingredient", "unit_price": 500}, request=FakeRequest(), user={"role": "admin"})), 422)
    check("create invalid type -> 422", code_of("bad-type", lambda: mr.create_register_item(body={"item_name": "X", "type": "widget"}, request=FakeRequest(), user={"role": "admin"})), 422)

    fb4 = FakeDB(); install(fb4)
    run(mr.create_register_item(body={"item_name": "Dup", "type": "dish", "unit_price": 10}, request=FakeRequest(), user={"role": "admin"}))
    check("create duplicate name -> 409", code_of("dup", lambda: mr.create_register_item(body={"item_name": "dup", "type": "dish", "unit_price": 20}, request=FakeRequest(), user={"role": "admin"})), 409)

    # ── PATCH /api/menu/register (type switch) ──────────────────────────────
    fb5 = FakeDB(); install(fb5)
    run(mr.create_register_item(body={"item_name": "Coke", "type": "resale", "unit_price": 1500}, request=FakeRequest(), user={"role": "admin"}))
    r5 = run(mr.update_register_item(body={"item_name": "Coke", "type": "ingredient"}, request=FakeRequest(), user={"role": "admin"}))
    check("switch resale->ingredient: success", r5.get("success"), True)
    check("switch resale->ingredient: type ingredient", r5["data"]["type"], "ingredient")
    check("switch resale->ingredient: ingredients kind", fb5.ingredients["Coke"]["kind"], "ingredient")

    fb6 = FakeDB(); install(fb6)
    run(mr.create_register_item(body={"item_name": "ဆန်", "type": "ingredient"}, request=FakeRequest(), user={"role": "admin"}))
    # switch ingredient -> resale with a price
    r6 = run(mr.update_register_item(body={"item_name": "ဆန်", "type": "resale", "unit_price": 800}, request=FakeRequest(), user={"role": "admin"}))
    check("switch ingredient->resale: success", r6.get("success"), True)
    check("switch ingredient->resale: inventory created", "ဆန်" in fb6.inventory, True)
    check("switch ingredient->resale: price 800", fb6.inventory["ဆန်"]["unit_price"], D("800"))
    # switch ingredient -> dish WITHOUT price must fail
    fb7 = FakeDB(); install(fb7)
    run(mr.create_register_item(body={"item_name": "ဆန်", "type": "ingredient"}, request=FakeRequest(), user={"role": "admin"}))
    check("switch ingredient->dish no price -> 422", code_of("sw-noprice", lambda: mr.update_register_item(body={"item_name": "ဆန်", "type": "dish"}, request=FakeRequest(), user={"role": "admin"})), 422)

    # rename collision
    fb8 = FakeDB(); install(fb8)
    run(mr.create_register_item(body={"item_name": "A", "type": "dish", "unit_price": 1}, request=FakeRequest(), user={"role": "admin"}))
    run(mr.create_register_item(body={"item_name": "B", "type": "dish", "unit_price": 2}, request=FakeRequest(), user={"role": "admin"}))
    check("patch rename collision -> 409", code_of("clash", lambda: mr.update_register_item(body={"item_name": "A", "new_name": "B"}, request=FakeRequest(), user={"role": "admin"})), 409)
    check("patch not found -> 404", code_of("404", lambda: mr.update_register_item(body={"item_name": "nope", "unit_price": 5}, request=FakeRequest(), user={"role": "admin"})), 404)

    # ── Ingredient exclusion from the POS food-menu list ────────────────────
    import routes.food_game_routes as fg
    captured = {}
    def fake_query(sql, args=None):
        captured["sql"] = sql
        captured["args"] = args
        return []
    fg._mysql_query = fake_query
    run(fg.api_fetch_food_menu(all=1, auth="x"))
    check("fetch_food_menu(all=1) excludes ingredients", "kind='ingredient'" in captured["sql"] and "NOT IN" in captured["sql"], True)
    run(fg.api_fetch_food_menu(all=0, auth="x"))
    check("fetch_food_menu(default) excludes ingredients", "kind='ingredient'" in captured["sql"], True)
    run(fg.api_fetch_food_prices(auth="x"))
    check("fetch_food_prices excludes ingredients", "kind='ingredient'" in captured["sql"], True)

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
