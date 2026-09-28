"""Regression tests for ingredient/product reclassification (owner 2026-09-28).

Covers the endpoint behind the Staff App 🍚/🛒 toggle — ``POST /api/menu/ingredients/kind``
(set_ingredient_kind) — WITHOUT touching the live MySQL: the mysql_db helpers are
monkey-patched onto ``routes.menu_routes`` and ``_ensure_tables`` is short-circuited,
so the endpoint logic runs against an in-memory fake ingredients table.

The owner reported "Amico Cake ကို Ingredient ထဲက နေ ဖြုတ်တာ မရဘူး" (reclassify from
the UI didn't work).  The API itself is fine — these tests lock in the behaviour:
a sellable product (Amico Cake) reclassifies to product and STAYS out of the
ingredient list.

Run:  /opt/psvibe/api/venv/bin/python tests/test_ingredient_reclassify.py
"""
import sys, os, asyncio

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes import menu_routes as mr
from routes.menu_cost import normalize_kind, classify_item_kind, filter_items_by_kind


class FakeRequest:
    class _State:
        branch_id = 1
    state = _State()


class FakeDB:
    """In-memory ingredients table with just enough SQL for the kind endpoint."""
    def __init__(self, rows=None):
        self.rows = list(rows or [])   # list of dicts {id, name, kind}
        self.next_id = max([r.get("id", 0) for r in self.rows] or [0]) + 1

    def q1(self, sql, args):
        s = sql.upper()
        if "FROM INGREDIENTS WHERE BRANCH_ID=%S AND NAME=%S" in s:
            for r in self.rows:
                if r["name"] == args[1]:
                    return dict(r)
            return None
        return None

    def ex(self, sql, args):
        s = sql.upper().lstrip()
        if s.startswith("UPDATE"):
            rid = args[-1]
            for r in self.rows:
                if r["id"] == rid:
                    r["kind"] = args[0]
            return 1
        if s.startswith("INSERT"):
            rid = self.next_id
            self.next_id += 1
            # INSERT INTO ingredients (branch_id, name, kind) VALUES (%s,%s,%s)
            self.rows.append({"id": rid, "name": args[1], "kind": args[2]})
            return rid
        return 0


def install(fake):
    mr._tables_ready = True
    mr._q1 = fake.q1
    mr._ex = fake.ex


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

    def code_of(name, fn):
        from fastapi import HTTPException
        try:
            run(fn())
            return None
        except HTTPException as e:
            return e.status_code

    # ── Pure classification (Amico Cake = sellable product) ──────────────────
    check("norm: 'product'", normalize_kind("product"), "product")
    check("norm: 'ingredient'", normalize_kind("ingredient"), "ingredient")
    check("classify: sellable -> product (Amico Cake)", classify_item_kind(None, True), "product")
    check("classify: explicit ingredient overrides sellable", classify_item_kind("ingredient", True), "ingredient")
    check("filter: product list excludes ingredients", [i["name"] for i in filter_items_by_kind(
        [{"name": "Amico Cake", "kind": "product"}, {"name": "ဆန်", "kind": "ingredient"}], "ingredient")], ["ဆန်"])

    # ── POST /api/menu/ingredients/kind — create product row ─────────────────
    fb = FakeDB(); install(fb)
    r = run(mr.set_ingredient_kind(body={"name": "Amico Cake", "kind": "product"}, request=FakeRequest(), user={"role": "admin"}))
    check("reclassify: success", r.get("success"), True)
    check("reclassify: kind product persisted", r["data"]["kind"], "product")
    check("reclassify: inserted one row", len(fb.rows), 1)

    # reclassify back to ingredient (update path)
    r = run(mr.set_ingredient_kind(body={"name": "Amico Cake", "kind": "ingredient"}, request=FakeRequest(), user={"role": "admin"}))
    check("reclassify: flip to ingredient", r["data"]["kind"], "ingredient")
    check("reclassify: still one row (update, not insert)", len(fb.rows), 1)

    # reclassify again to product — the owner's exact action
    r = run(mr.set_ingredient_kind(body={"name": "Amico Cake", "kind": "product"}, request=FakeRequest(), user={"role": "admin"}))
    check("reclassify: back to product (owner case)", r["data"]["kind"], "product")
    check("reclassify: Amico Cake no longer ingredient", [x for x in fb.rows if x["name"] == "Amico Cake"][0]["kind"], "product")

    # ── validation ───────────────────────────────────────────────────────────
    check("reclassify: empty name -> 422", code_of("empty", lambda: mr.set_ingredient_kind(body={"name": "  ", "kind": "product"}, request=FakeRequest(), user={"role": "admin"})), 422)
    check("reclassify: invalid kind -> 422", code_of("bad-kind", lambda: mr.set_ingredient_kind(body={"name": "X", "kind": "banana"}, request=FakeRequest(), user={"role": "admin"})), 422)

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
