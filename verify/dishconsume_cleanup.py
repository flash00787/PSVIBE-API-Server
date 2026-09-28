"""Cleanup for the dish-consume probe — removes ONLY the PROBE demo rows."""
import os
import sys

sys.path.insert(0, "/opt/psvibe/api")

from mysql_db import query as _q, delete_rows as _del

# The probe used prefix "PROBE" + timestamp; the two ingredient/dish names are
# discovered by prefix, stock_in by source='probe', stock_out by notes='probe'.
P = "PROBE"
removed = {}


def main():
    # 1. ingredient_consumption (by dish_item prefix)
    rows = _q("SELECT id, dish_item, ingredient FROM ingredient_consumption WHERE dish_item LIKE %s", (P + "%",))
    removed["ingredient_consumption"] = len(rows)
    for r in rows:
        _del("DELETE FROM ingredient_consumption WHERE id=%s", (r["id"],))

    # 2. stock_layers for the demo stock_in rows (source='probe')
    layers = _q(
        "SELECT sl.id FROM stock_layers sl JOIN stock_in si ON si.id = sl.stock_in_id WHERE si.source='probe'"
    )
    removed["stock_layers"] = len(layers)
    for r in layers:
        _del("DELETE FROM stock_layers WHERE id=%s", (r["id"],))

    # 3. stock_out (notes='probe')
    so = _q("SELECT id FROM stock_out WHERE notes='probe'")
    removed["stock_out"] = len(so)
    for r in so:
        _del("DELETE FROM stock_out WHERE id=%s", (r["id"],))

    # 4. stock_in (source='probe')
    si = _q("SELECT id FROM stock_in WHERE source='probe'")
    removed["stock_in"] = len(si)
    for r in si:
        _del("DELETE FROM stock_in WHERE id=%s", (r["id"],))

    # 5. menu_recipes (menu_item prefix)
    mr = _q("SELECT id FROM menu_recipes WHERE menu_item LIKE %s", (P + "%",))
    removed["menu_recipes"] = len(mr)
    for r in mr:
        _del("DELETE FROM menu_recipes WHERE id=%s", (r["id"],))

    # 6. inventory (item_name prefix)
    inv = _q("SELECT id, item_name FROM inventory WHERE item_name LIKE %s", (P + "%",))
    removed["inventory"] = len(inv)
    for r in inv:
        _del("DELETE FROM inventory WHERE id=%s", (r["id"],))

    # 7. ingredients (name prefix)
    ing = _q("SELECT id, name FROM ingredients WHERE name LIKE %s", (P + "%",))
    removed["ingredients"] = len(ing)
    for r in ing:
        _del("DELETE FROM ingredients WHERE id=%s", (r["id"],))

    print("REMOVED:", removed)

    # verify nothing PROBE remains
    leftovers = {}
    for tbl, col in (("inventory", "item_name"), ("ingredients", "name"), ("menu_recipes", "menu_item"),
                     ("stock_in", "source"), ("stock_out", "notes"), ("ingredient_consumption", "dish_item")):
        if col in ("source", "notes"):
            leftovers[tbl] = _q(f"SELECT COUNT(*) c FROM {tbl} WHERE {col}='probe'")[0]["c"]
        else:
            leftovers[tbl] = _q(f"SELECT COUNT(*) c FROM {tbl} WHERE {col} LIKE %s", (P + "%",))[0]["c"]
    leftovers["stock_layers"] = _q(
        "SELECT COUNT(*) c FROM stock_layers sl JOIN stock_in si ON si.id=sl.stock_in_id WHERE si.source='probe'"
    )[0]["c"]
    print("LEFTOVER (should all be 0):", leftovers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
