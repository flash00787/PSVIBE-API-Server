"""Unit tests for the Stock-In guard (owner 2026-09-28 Osmo).

Stock-In must ONLY record purchases of EXISTING items:
  * existing inventory item -> quantity bumped, unit_price NEVER touched;
  * raw ingredient (ingredients row) / prior stock_in history -> recorded,
    no inventory row created;
  * truly unknown name -> clear error, nothing written.

No live MySQL — mysql_db helpers are monkey-patched onto admin_sales_routes.

Run:  python3 tests/test_stockin_guard.py
"""
import sys, os, asyncio

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes import admin_sales_routes as ar


class FakeStockDB:
    """Tracks inventory / ingredients / stock_in history + captured SQL."""
    def __init__(self):
        self.inventory = {}      # name -> {"id", "quantity", "unit_price"}
        self.ingredients = {}    # name -> {"id"}
        self.stock_in_names = set()   # prior stock_in history (item_name)
        self.stock_in_rows = []       # inserted stock_in rows
        self.executed = []            # (sql, args) tuples
        self._inv_id = 1
        self._ing_id = 1

    def add_inventory(self, name, quantity, unit_price):
        self.inventory[name] = {"id": self._inv_id, "name": name, "quantity": quantity, "unit_price": unit_price}
        self._inv_id += 1

    def add_ingredient(self, name):
        self.ingredients[name] = {"id": self._ing_id}
        self._ing_id += 1

    def add_stockin_history(self, name):
        self.stock_in_names.add(name)

    def q1(self, sql, args):
        s = sql.upper()
        if "FROM INVENTORY" in s:
            # args = (item_name,)  — name is the first (only) positional
            name = args[0]
            r = self.inventory.get(name)
            return dict(r) if r else None
        if "FROM INGREDIENTS" in s:
            name = args[-1]
            r = self.ingredients.get(name)
            return dict(r) if r else None
        if "FROM STOCK_IN" in s:
            name = args[-1]
            return {"id": 999} if name in self.stock_in_names else None
        return None

    def ex(self, sql, args):
        s = sql.upper().lstrip()
        self.executed.append((sql, args))
        if s.startswith("UPDATE") and "INVENTORY" in s and "QUANTITY" in s:
            # UPDATE inventory SET quantity = quantity + %s ... WHERE id = %s
            qty = args[0]
            rid = args[-1]
            for r in self.inventory.values():
                if r["id"] == rid:
                    r["quantity"] += int(qty)
                    break
            return 1
        if s.startswith("INSERT") and "STOCK_IN" in s:
            # INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, ...) VALUES (...)
            self.stock_in_rows.append({"batch_id": args[0], "item_name": args[1], "quantity": args[2], "unit_cost": args[3]})
            return 1
        if s.startswith("INSERT") and "INVENTORY" in s:
            return 1  # must NOT happen — flag if it does
        return 0


def install(fake):
    ar._mysql_query_one = fake.q1
    ar._mysql_exec = fake.ex


def run(coro):
    return asyncio.run(coro)


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

    # ── existing inventory item: quantity bumped, unit_price untouched ──────
    db = FakeStockDB(); install(db)
    db.add_inventory("Coke", quantity=5, unit_price=200.0)
    r = run(ar.api_stock_in(req={"item_name": "Coke", "quantity": 3, "unit_cost": 50.0, "payment_method": ""}, auth="x"))
    check("existing item: success", r.get("success"), True)
    check("existing item: quantity 5+3=8", db.inventory["Coke"]["quantity"], 8)
    check("existing item: unit_price UNCHANGED (200.0)", db.inventory["Coke"]["unit_price"], 200.0)
    check("existing item: one stock_in row", len(db.stock_in_rows), 1)

    # ── raw ingredient (ingredients row, no inventory) → recorded, no inv row ─
    db2 = FakeStockDB(); install(db2)
    db2.add_ingredient("ကြက်ဥ")
    r2 = run(ar.api_stock_in(req={"item_name": "ကြက်ဥ", "quantity": 2, "unit_cost": 300.0, "payment_method": ""}, auth="x"))
    check("ingredient: success", r2.get("success"), True)
    check("ingredient: no inventory row created", "ကြက်ဥ" in db2.inventory, False)
    check("ingredient: one stock_in row recorded", len(db2.stock_in_rows), 1)

    # ── prior stock_in history only (stock-only item) → still purchasable ───
    db2b = FakeStockDB(); install(db2b)
    db2b.add_stockin_history("ဆန်")
    r2b = run(ar.api_stock_in(req={"item_name": "ဆန်", "quantity": 1, "unit_cost": 500.0, "payment_method": ""}, auth="x"))
    check("stock-only history: success", r2b.get("success"), True)
    check("stock-only history: no inventory row", "ဆန်" in db2b.inventory, False)
    check("stock-only history: stock_in recorded", len(db2b.stock_in_rows), 1)

    # ── truly unknown name → error, nothing written ─────────────────────────
    db3 = FakeStockDB(); install(db3)
    r3 = run(ar.api_stock_in(req={"item_name": "Brand New Item", "quantity": 2, "unit_cost": 10.0, "payment_method": ""}, auth="x"))
    check("unknown item: error (success False)", r3.get("success"), False)
    check("unknown item: error mentions Recipe & Cost", "Recipe & Cost" in r3.get("error", ""), True)
    check("unknown item: no stock_in row written", len(db3.stock_in_rows), 0)
    check("unknown item: no inventory row written", len(db3.inventory), 0)

    # ── batch: unknown item aborts the whole batch (no partial writes) ──────
    db4 = FakeStockDB(); install(db4)
    db4.add_inventory("Coke", quantity=5, unit_price=200.0)
    r4 = run(ar.api_stock_in_batch(req={
        "items": [
            {"item_name": "Coke", "quantity": 2, "unit_cost": 50.0},
            {"item_name": "Unknown New Thing", "quantity": 1, "unit_cost": 99.0},
        ],
        "payment_method": "",
    }, auth="x"))
    check("batch with unknown: error", r4.get("success"), False)
    check("batch with unknown: no stock_in rows (atomic)", len(db4.stock_in_rows), 0)
    check("batch with unknown: Coke quantity unchanged", db4.inventory["Coke"]["quantity"], 5)

    # ── batch: all-known → quantity bumped, unit_price untouched ────────────
    db5 = FakeStockDB(); install(db5)
    db5.add_inventory("Coke", quantity=5, unit_price=200.0)
    db5.add_ingredient("ကြက်ဥ")
    r5 = run(ar.api_stock_in_batch(req={
        "items": [
            {"item_name": "Coke", "quantity": 2, "unit_cost": 50.0},
            {"item_name": "ကြက်ဥ", "quantity": 10, "unit_cost": 300.0},
        ],
        "payment_method": "",
    }, auth="x"))
    check("batch all-known: success", r5.get("success"), True)
    check("batch all-known: Coke quantity 7", db5.inventory["Coke"]["quantity"], 7)
    check("batch all-known: Coke unit_price unchanged", db5.inventory["Coke"]["unit_price"], 200.0)
    check("batch all-known: 2 stock_in rows", len(db5.stock_in_rows), 2)

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
