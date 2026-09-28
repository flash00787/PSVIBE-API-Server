#!/usr/bin/env python
"""Live probe for item-type split (owner 2026-09-28): resale vs made dishes.

Mints admin + staff tokens, then verifies against the running API (127.0.0.1:8000):
  * GET /api/menu/costs?type=resale  -> packaged items costed from purchase price
  * GET /api/menu/costs?type=dish     -> made dishes (recipe-based)
  * GET /api/menu/costs?type=all      -> both, with item_type + cost_source
  * create a demo RESALE item + a demo DISH item, verify each, then CLEAN UP
  * staff token -> 403 on every /api/menu/* endpoint
  * food-cost-report includes a dish/resale split
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")

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


def get_items(t):
    s, r = call("GET", f"/api/menu/costs?type={t}", ADMIN)
    return s, (r.get("data") if isinstance(r, dict) else None) or []


ts = str(int(time.time()))
RESALE_NAME = f"DEMO typesplit resale {ts}"
DISH_NAME = f"DEMO typesplit dish {ts}"
created = {"inventory": [], "recipes": [], "stock_in": []}

try:
    # ── 1. resale list ──────────────────────────────────────────────
    s, resale = get_items("resale")
    check("GET costs?type=resale 200", s == 200, str(s))
    names = {i["item_name"] for i in resale}
    check("Amico Cake in resale", "Amico Cake" in names)
    check("Birdy 3 in 1 Coffee in resale", "Birdy 3 in 1 Coffee" in names)
    bad = [i["item_name"] for i in resale if i.get("item_type") != "resale"]
    check("all resale rows typed 'resale'", not bad, f"bad={bad[:5]}")
    bad_src = [i["item_name"] for i in resale if i.get("cost_source") != "purchase"]
    check("all resale rows cost_source 'purchase'", not bad_src, f"bad={bad_src[:5]}")
    amico = next((i for i in resale if i["item_name"] == "Amico Cake"), None)
    if amico:
        check("Amico Cake has purchase cost", amico.get("unit_cost") is not None,
              f"unit_cost={amico.get('unit_cost')} status={amico.get('status')}")
        check("Amico Cake food-cost % present", amico.get("food_cost_pct") is not None,
              f"pct={amico.get('food_cost_pct')}")
    nohist = [i["item_name"] for i in resale if i.get("no_purchase_yet")]
    check("no-purchase rows flagged (never 0-cost)", all(
        i["food_cost_pct"] is None and i["unit_cost"] is None for i in resale if i.get("no_purchase_yet")),
        f"no-purchase count={len(nohist)} e.g. {nohist[:5]}")

    # ── 2. dish list ─────────────────────────────────────────────────
    s, dishes = get_items("dish")
    check("GET costs?type=dish 200", s == 200, str(s))
    bad = [i["item_name"] for i in dishes if i.get("item_type") != "dish"]
    check("all dish rows typed 'dish'", not bad, f"bad={bad[:5]}")

    # ── 3. all ───────────────────────────────────────────────────────
    s, allitems = get_items("all")
    check("GET costs?type=all 200", s == 200, str(s))
    check("all items carry item_type + cost_source", all(
        (i.get("item_type") in ("resale", "dish") and i.get("cost_source") in ("recipe", "purchase"))
        for i in allitems))

    # ── 4. create demo resale item ───────────────────────────────────
    s, r = call("POST", "/api/menu/item", ADMIN,
                {"item_name": RESALE_NAME, "category": "Snacks", "unit_price": 2000, "item_type": "resale"})
    check("create demo resale 200", s == 200 and (r.get("data") or {}).get("item_type") == "resale", str(s))
    created["inventory"].append(RESALE_NAME)
    s, r2 = call("PATCH", "/api/menu/item", ADMIN, {"item_name": RESALE_NAME, "item_type": "dish"})
    check("PATCH resale -> dish", s == 200 and (r2.get("data") or {}).get("item_type") == "dish", str(s))
    s, r3 = call("PATCH", "/api/menu/item", ADMIN, {"item_name": RESALE_NAME, "item_type": "resale"})
    check("PATCH dish -> resale", s == 200 and (r3.get("data") or {}).get("item_type") == "resale", str(s))

    # ── 5. create demo dish item (recipe-based) ──────────────────────
    s, r = call("POST", "/api/menu/item", ADMIN,
                {"item_name": DISH_NAME, "category": "Food", "unit_price": 3000, "item_type": "dish"})
    check("create demo dish 200", s == 200 and (r.get("data") or {}).get("item_type") == "dish", str(s))
    created["inventory"].append(DISH_NAME)
    s, dlist = get_items("dish")
    check("demo dish appears in type=dish", any(i["item_name"] == DISH_NAME for i in dlist))
    demo_dish = next((i for i in dlist if i["item_name"] == DISH_NAME), None)
    check("demo dish cost_source 'recipe'", demo_dish is not None and demo_dish.get("cost_source") == "recipe")

    # ── 6. food-cost-report split ────────────────────────────────────
    s, rep = call("GET", "/api/menu/food-cost-report?month=" + time.strftime("%Y-%m", time.gmtime()), ADMIN)
    repd = (rep or {}).get("data") or {}
    check("food-cost-report 200 + has split", s == 200 and "split" in repd, str(s))
    if "split" in repd:
        check("split has dish + resale", set(repd["split"].keys()) == {"dish", "resale"})

    # ── 7. staff 403 ─────────────────────────────────────────────────
    s, _ = call("GET", "/api/menu/costs", STAFF)
    check("staff 403 on GET /api/menu/costs", s == 403, str(s))
    s, _ = call("GET", "/api/menu/costs?type=resale", STAFF)
    check("staff 403 on GET /api/menu/costs?type=resale", s == 403, str(s))
    s, _ = call("POST", "/api/menu/item", STAFF, {"item_name": "staff-blocked", "unit_price": 1})
    check("staff 403 on POST /api/menu/item", s == 403, str(s))
    s, _ = call("PATCH", "/api/menu/item", STAFF, {"item_name": RESALE_NAME, "item_type": "dish"})
    check("staff 403 on PATCH /api/menu/item", s == 403, str(s))

finally:
    # ── CLEANUP (demo rows only) ─────────────────────────────────────
    removed = {"inventory": [], "recipes": [], "stock_in": []}
    import pymysql
    conn = pymysql.connect(host="127.0.0.1", port=3306, user=SEC["MYSQL_USER"], password=SEC["MYSQL_PASSWORD"],
                           database=SEC["MYSQL_DATABASE"], charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            for n in created["inventory"]:
                cur.execute("DELETE FROM menu_recipes WHERE menu_item=%s", (n,))
                removed["recipes"].append(n)
                cur.execute("DELETE FROM stock_in WHERE item_name=%s", (n,))
                removed["stock_in"].append(n)
                cur.execute("DELETE FROM inventory WHERE item_name=%s", (n,))
                removed["inventory"].append(n)
        conn.commit()
    finally:
        conn.close()
    print("\n── CLEANUP ──")
    print(json.dumps(removed, ensure_ascii=False))

print(f"\n==== SUMMARY: {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
