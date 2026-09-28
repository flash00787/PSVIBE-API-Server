"""End-to-end HTTP probe: dish sale via POST /api/inventory/stock-out triggers
ingredient FIFO consumption + records COGS.  Creates minimal demo, cleans up."""
import sys, os, time, json, urllib.request

sys.path.insert(0, "/opt/psvibe/api")

from mysql_db import query as _q, query_one as _q1, execute as _ex, delete_rows as _del
import dish_consumption as dc

TS = str(int(time.time()))
P = f"PROBE{TS}"
DISH = f"{P}_DISH"
ING = f"{P}_ING"

def main():
    key = os.environ.get("API_KEY", "")
    dc.ensure_tables()

    # demo dish + ingredient + 2 layers + inventory
    _ex("INSERT INTO inventory (branch_id,item_name,category,quantity,unit_price,item_type) VALUES (1,%s,'Food',0,3000,'dish')", (DISH,))
    _ex("INSERT INTO ingredients (branch_id,name,kind,base_unit,purchase_unit,base_per_purchase,cost_basis) VALUES (1,%s,'ingredient','pcs',NULL,1,'fifo')", (ING,))
    _ex("INSERT INTO inventory (branch_id,item_name,category,quantity,unit_price,item_type) VALUES (1,%s,'Ingredient',20,0,'resale')", (ING,))
    _ex("INSERT INTO menu_recipes (branch_id,menu_item,ingredient,qty_per_portion,waste_pct) VALUES (1,%s,%s,2,0)", (DISH, ING))
    s1 = _ex("INSERT INTO stock_in (batch_id,branch_id,item_name,quantity,unit_cost,source) VALUES (%s,1,%s,10,500,'probe')", (f"{P}-SI1", ING))
    s2 = _ex("INSERT INTO stock_in (batch_id,branch_id,item_name,quantity,unit_cost,source) VALUES (%s,1,%s,10,700,'probe')", (f"{P}-SI2", ING))
    dc.ensure_layer_for_stock_in(s1)
    dc.ensure_layer_for_stock_in(s2)

    before = int(_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING,))["quantity"])

    # POST /api/inventory/stock-out (the real dish-sale path)
    payload = json.dumps({"item_name": DISH, "qty": 2, "unit_price": 3000, "subtotal": 6000, "date": "2026-09-28", "voucher_no": f"probe-{P}"}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/api/inventory/stock-out", data=payload,
                                 headers={"Content-Type": "application/json", "x-api-key": key})
    resp = json.loads(urllib.request.urlopen(req).read())
    print("[http]", resp)

    after = int(_q1("SELECT quantity FROM inventory WHERE item_name=%s", (ING,))["quantity"])
    cons = _q("SELECT ingredient, qty, unit_cost, cost, short_by, layer_breakdown FROM ingredient_consumption WHERE dish_item=%s", (DISH,))
    layers = _q("SELECT qty_in, qty_remaining, unit_cost FROM stock_layers WHERE item_name=%s ORDER BY id", (ING,))
    print(f"[ingredient] before={before} after={after} (expect 20 -> 16)")
    print("[consumption]", json.dumps(cons, default=str, ensure_ascii=False))
    print("[layers]", json.dumps(layers, default=str))

    # cleanup
    _del("DELETE FROM ingredient_consumption WHERE dish_item=%s", (DISH,))
    _del("DELETE FROM stock_layers WHERE stock_in_id IN (%s,%s)", (s1, s2))
    _del("DELETE FROM stock_out WHERE notes LIKE %s", (f"Voucher: probe-{P}%",))
    _del("DELETE FROM stock_in WHERE source='probe'")
    _del("DELETE FROM menu_recipes WHERE menu_item=%s", (DISH,))
    _del("DELETE FROM inventory WHERE item_name IN (%s,%s)", (DISH, ING))
    _del("DELETE FROM ingredients WHERE name=%s", (ING,))
    print("[cleanup] done")
    return 0

if __name__ == "__main__":
    sys.exit(main())
