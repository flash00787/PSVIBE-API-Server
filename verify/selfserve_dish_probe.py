#!/usr/bin/env python
"""Live probe for self-serve dish endpoints (owner 2026-09-28).

Mints admin + staff tokens from the live JWT secret, then exercises the new
POST/PATCH /api/menu/item flow against the running API (127.0.0.1:8000), verifies
costing, food-cost% recompute, POS menu visibility, staff 403, then cleans up
every demo row it created.
"""
import json, os, sys, urllib.request, urllib.error

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")

# Load secrets (JWT + MySQL)
SEC = {}
with open("/etc/psvibe/secrets.env") as f:
    for ln in f:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        SEC[k.strip()] = v.strip().strip('"').strip("'")
os.environ["JWT_SECRET_KEY"] = SEC.get("JWT_SECRET_KEY", "")

from auth import create_access_token  # noqa: E402

BASE = "http://127.0.0.1:8000"
ADMIN = create_access_token({"sub": "admin"})
STAFF = create_access_token({"sub": "demo", "role": "staff", "scope": "staff-webapp"})

PASS = []
FAIL = []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))


def call(method, path, token, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


DISH = "DEMO selfserve ကြေးအိုးထမင်း " + str(int(__import__("time").time()))
DISH_NAME = DISH
ING = []  # (name, qty, waste, base_unit, purchase_unit, bpp)

# Track what we created for cleanup
created_ingredients = []
created_recipes = []

try:
    # 1. ingredient list (admin) → pick 2 real stock_in items
    s, inglist = call("GET", "/api/menu/ingredients", ADMIN)
    check("ingredients list 200", s == 200, str(s))
    sugg = (inglist.get("data") or {}).get("suggested") or []
    check("has stock-in suggested items", len(sugg) > 0, f"count={len(sugg)}")
    picked = []
    for name in ["ကြက်ဥ", "ကြက်အူချောင်း အသေး", "ကြက်အူချောင်း အကြီး"]:
        hit = next((x for x in sugg if x["name"] == name), None)
        if hit and hit.get("weighted_avg_cost") is not None:
            picked.append(hit)
    check("picked 2-3 real ingredients", len(picked) >= 2, f"n={len(picked)}")
    # use first two, one with waste
    for i, p in enumerate(picked[:3]):
        base_unit = "pcs"
        purchase_unit = "pcs"
        bpp = 1
        body = {"name": p["name"], "base_unit": base_unit, "purchase_unit": purchase_unit, "base_per_purchase": bpp}
        s, r = call("POST", "/api/menu/ingredients", ADMIN, body)
        ok = s == 200 and (r.get("success") is True or r.get("data"))
        check(f"ingredient upsert #{i+1} {p['name']}", ok, str(s))
        created_ingredients.append(p["name"])
        ING.append({"name": p["name"], "qty": 2 if i == 0 else 1, "waste": 0 if i < 2 else 10, "wavg": float(p["weighted_avg_cost"])})

    # 2. create dish
    PRICE = 3000
    s, r = call("POST", "/api/menu/item", ADMIN, {"item_name": DISH_NAME, "category": "Food", "unit_price": PRICE})
    check("create dish 200", s == 200, str(s))
    check("create returns selling_price", (r.get("data") or {}).get("selling_price") == PRICE, json.dumps(r.get("data"), ensure_ascii=False))

    # 3. appears in /api/menu/costs
    s, costs = call("GET", "/api/menu/costs", ADMIN)
    items = costs.get("data") or []
    hit = next((x for x in items if x["item_name"] == DISH_NAME), None)
    check("dish appears in /api/menu/costs", hit is not None, "found" if hit else "missing")

    # 4. add recipe rows
    for ing in ING:
        s, r = call("POST", "/api/menu/recipe", ADMIN, {
            "menu_item": DISH_NAME, "ingredient": ing["name"],
            "qty_per_portion": ing["qty"], "waste_pct": ing["waste"],
        })
        check(f"recipe row {ing['name']}", s == 200, str(s))
        created_recipes.append(ing["name"])

    # 5. recipe totals → verify portion cost + food-cost %
    s, rec = call("GET", "/api/menu/recipe?item=" + urllib.parse.quote(DISH_NAME), ADMIN)
    rec = rec.get("data") or {}
    expected = 0.0
    for ing in ING:
        expected += ing["qty"] * ing["wavg"] * (1 + ing["waste"] / 100.0)
    expected = round(expected, 2)
    got = rec.get("portion_cost")
    ok = got is not None and abs(float(got) - expected) < 0.06
    check("portion cost correct", ok, f"got={got} expected={expected}")
    expected_pct = round(expected / PRICE * 100, 1)
    check("food-cost % correct", abs(float(rec.get("food_cost_pct") or 0) - expected_pct) < 0.15, f"got={rec.get('food_cost_pct')} expected={expected_pct}")

    # 6. PATCH price → % recomputes
    NEW_PRICE = 4000
    s, r = call("PATCH", "/api/menu/item", ADMIN, {"item_name": DISH_NAME, "unit_price": NEW_PRICE})
    check("PATCH price 200", s == 200, str(s))
    check("PATCH returns new price", (r.get("data") or {}).get("selling_price") == NEW_PRICE, json.dumps(r.get("data"), ensure_ascii=False))
    s, rec2 = call("GET", "/api/menu/recipe?item=" + urllib.parse.quote(DISH_NAME), ADMIN)
    rec2 = rec2.get("data") or {}
    new_pct = round(expected / NEW_PRICE * 100, 1)
    check("food-cost % recomputed after PATCH", abs(float(rec2.get("food_cost_pct") or 0) - new_pct) < 0.15, f"got={rec2.get('food_cost_pct')} expected={new_pct}")

    # 7. staff token → 403 on /api/menu/item
    s, r = call("POST", "/api/menu/item", STAFF, {"item_name": "staff cannot create", "unit_price": 1})
    check("staff 403 on POST /api/menu/item", s == 403, str(s))
    s, r = call("GET", "/api/menu/costs", STAFF)
    check("staff 403 on GET /api/menu/costs", s == 403, str(s))

    # 8. POS menu visibility: stock-in the demo dish (SQL) then fetch_food_menu
    import pymysql
    conn = pymysql.connect(host="127.0.0.1", port=3306, user=SEC["MYSQL_USER"], password=SEC["MYSQL_PASSWORD"],
                           database=SEC["MYSQL_DATABASE"], charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM inventory WHERE item_name=%s", (DISH_NAME,))
            row = cur.fetchone()
            inv_id = row["id"]
            cur.execute("INSERT INTO stock_in (branch_id, batch_id, item_name, quantity, unit_cost, source) VALUES (1,%s,%s,1,%s,'demo-selfserve')", ("demo-selfserve-" + str(int(__import__("time").time())), DISH_NAME, NEW_PRICE))
            stock_in_id = cur.lastrowid
            cur.execute("UPDATE inventory SET quantity=1 WHERE id=%s", (inv_id,))
        conn.commit()
        # fetch_food_menu default (quantity > 0)
        s, menu = call("GET", "/api/fetch_food_menu", ADMIN)
        grouped = menu.get("data") if isinstance(menu, dict) else menu
        grouped = grouped if isinstance(grouped, dict) else {}
        flat = {}
        for cat, d in grouped.items():
            if isinstance(d, dict):
                flat.update(d)
        check("dish appears in /api/fetch_food_menu (after stock-in)", DISH_NAME in flat, f"price={flat.get(DISH_NAME)}")
    finally:
        conn.close()

finally:
    # ── CLEANUP ──
    removed = {"recipes": [], "ingredients": [], "stock_in": [], "inventory": []}
    import pymysql
    conn = pymysql.connect(host="127.0.0.1", port=3306, user=SEC["MYSQL_USER"], password=SEC["MYSQL_PASSWORD"],
                           database=SEC["MYSQL_DATABASE"], charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            for ing in created_ingredients:
                cur.execute("DELETE FROM ingredients WHERE name=%s", (ing,))
                removed["ingredients"].append(ing)
            cur.execute("DELETE FROM menu_recipes WHERE menu_item=%s", (DISH_NAME,))
            removed["recipes"] = [DISH_NAME]
            cur.execute("DELETE FROM stock_in WHERE item_name=%s AND source='demo-selfserve'", (DISH_NAME,))
            removed["stock_in"] = [DISH_NAME]
            cur.execute("DELETE FROM inventory WHERE item_name=%s", (DISH_NAME,))
            removed["inventory"] = [DISH_NAME]
        conn.commit()
    finally:
        conn.close()
    print("\n── CLEANUP ──")
    print(json.dumps(removed, ensure_ascii=False))

print(f"\n==== SUMMARY: {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
