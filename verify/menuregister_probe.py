#!/usr/bin/env python
"""Live probe for the Menu Register + Stock-In cleanup (owner 2026-09-28 Osmo).

Mints admin + staff tokens, then against the RUNNING API (127.0.0.1:8000):
  * registers a demo ready-made item (price) and a demo ingredient (no price);
  * checks they land in /api/menu/register with the right type + the
    price_equals_cost warning after a purchase at cost;
  * checks fetch_food_menu: ready-made appears + is sellable, ingredient does NOT
    appear (even when it has an inventory row → excluded by kind=ingredient);
  * checks Stock-In never changes an existing item's unit_price;
  * checks staff -> 403 on the register endpoints;
then CLEANS UP every demo row it created (and restores the stock-guard quantity).
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

PASS, FAIL = [], []


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


import pymysql


def db():
    return pymysql.connect(host="127.0.0.1", port=3306, user=SEC["MYSQL_USER"],
                           password=SEC["MYSQL_PASSWORD"], database=SEC["MYSQL_DATABASE"],
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


TS = str(int(time.time()))
RESALE = f"DEMO MENUREG Coke {TS}"
INGREDIENT = f"DEMO MENUREG Rice {TS}"

created = {"inventory": [], "ingredients": [], "stock_in": []}
guard = {"item_name": None, "orig_qty": None, "stock_in_ids": []}

try:
    # ── 1. register ready-made (price) + ingredient (no price) ──────────────
    s, r = call("POST", "/api/menu/register", ADMIN, {"item_name": RESALE, "category": "Food", "type": "resale", "unit_price": 1500})
    check("register ready-made 200", s == 200, str(s))
    check("ready-made type resale", (r.get("data") or {}).get("type") == "resale", json.dumps(r.get("data"), ensure_ascii=False))

    s, r = call("POST", "/api/menu/register", ADMIN, {"item_name": INGREDIENT, "type": "ingredient"})
    check("register ingredient 200", s == 200, str(s))
    check("ingredient type ingredient", (r.get("data") or {}).get("type") == "ingredient", json.dumps(r.get("data"), ensure_ascii=False))
    check("ingredient selling price None", (r.get("data") or {}).get("selling_price") is None, str((r.get("data") or {}).get("selling_price")))

    # validation: ready-made without price → 422, ingredient with price → 422
    s, _ = call("POST", "/api/menu/register", ADMIN, {"item_name": "DEMO noprice " + TS, "type": "resale"})
    check("ready-made without price -> 422", s == 422, str(s))
    s, _ = call("POST", "/api/menu/register", ADMIN, {"item_name": "DEMO ingprice " + TS, "type": "ingredient", "unit_price": 500})
    check("ingredient with price -> 422", s == 422, str(s))

    # ── 2. register list shows them ─────────────────────────────────────────
    s, reg = call("GET", "/api/menu/register", ADMIN)
    items = (reg.get("data") or [])
    by = {i["item_name"]: i for i in items if isinstance(i, dict)}
    check("register list 200", s == 200, str(s))
    check("ready-made in register list", RESALE in by, "missing" if RESALE not in by else "ok")
    check("ingredient in register list", INGREDIENT in by, "missing" if INGREDIENT not in by else "ok")
    check("ingredient has no selling price in list", by.get(INGREDIENT, {}).get("selling_price") is None, str(by.get(INGREDIENT, {}).get("selling_price")))

    # ── 3. stock-in the ready-made at COST == price → price_equals_cost warn ─
    s, r = call("POST", "/api/stock/in", ADMIN, {"item_name": RESALE, "quantity": 5, "unit_cost": 1500.0, "payment_method": ""})
    check("stock-in ready-made 200", s == 200, str(s))
    created["stock_in"].append(RESALE)

    s, reg2 = call("GET", "/api/menu/register", ADMIN)
    by2 = {i["item_name"]: i for i in (reg2.get("data") or []) if isinstance(i, dict)}
    warn = (by2.get(RESALE) or {}).get("warnings") or []
    check("ready-made surfaces price_equals_cost warning", "price_equals_cost" in warn, str(warn))

    # ── 4. POS visibility: ready-made appears, ingredient does NOT ──────────
    s, menu = call("GET", "/api/fetch_food_menu", ADMIN)
    grouped = menu.get("data") if isinstance(menu, dict) else menu
    grouped = grouped if isinstance(grouped, dict) else {}
    flat = {}
    for cat, d in grouped.items():
        if isinstance(d, dict):
            flat.update(d)
    check("ready-made appears in fetch_food_menu (sellable)", RESALE in flat, f"price={flat.get(RESALE)}")
    check("ingredient NOT in fetch_food_menu", INGREDIENT not in flat, "present!" if INGREDIENT in flat else "ok")

    # robustness: give the ingredient an inventory row, it must STILL be excluded
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO inventory (branch_id, item_name, category, quantity, unit_price, reorder_level, item_type) VALUES (1,%s,'Food',1,999,0,'resale')", (INGREDIENT,))
        conn.commit()
        created["inventory"].append(INGREDIENT)
    finally:
        conn.close()
    s, menu3 = call("GET", "/api/fetch_food_menu?all=1", ADMIN)
    g3 = menu3.get("data") if isinstance(menu3, dict) else menu3
    g3 = g3 if isinstance(g3, dict) else {}
    flat3 = {}
    for cat, d in g3.items():
        if isinstance(d, dict):
            flat3.update(d)
    check("ingredient excluded even with an inventory row", INGREDIENT not in flat3, "leaked!" if INGREDIENT in flat3 else "ok")

    # ── 5. Stock-In never changes unit_price of an existing item ────────────
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT item_name, quantity, unit_price FROM inventory WHERE item_type='resale' AND unit_price > 0 AND item_name NOT LIKE 'DEMO %' ORDER BY id LIMIT 1")
            row = cur.fetchone()
        guard["item_name"] = row["item_name"]
        guard["orig_qty"] = int(row["quantity"] or 0)
        before_price = float(row["unit_price"] or 0)
    finally:
        conn.close()
    s, r = call("POST", "/api/stock/in", ADMIN, {"item_name": guard["item_name"], "quantity": 1, "unit_cost": 7777.0, "payment_method": ""})
    check("stock-in existing item 200", s == 200, str(s))
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT quantity, unit_price FROM inventory WHERE item_name=%s", (guard["item_name"],))
            r2 = cur.fetchone()
            cur.execute("SELECT id FROM stock_in WHERE item_name=%s AND unit_cost=7777.0 ORDER BY id DESC LIMIT 1", (guard["item_name"],))
            si = cur.fetchone()
            if si:
                guard["stock_in_ids"].append(si["id"])
    finally:
        conn.close()
    check("existing item unit_price UNCHANGED after stock-in", float(r2["unit_price"] or 0) == before_price, f"{float(r2['unit_price'] or 0)} vs {before_price}")
    check("existing item quantity +1", int(r2["quantity"] or 0) == guard["orig_qty"] + 1, f"{r2['quantity']} vs {guard['orig_qty']+1}")

    # ── 6. staff -> 403 on register endpoints ───────────────────────────────
    s, _ = call("GET", "/api/menu/register", STAFF)
    check("staff 403 GET /register", s == 403, str(s))
    s, _ = call("POST", "/api/menu/register", STAFF, {"item_name": "x", "type": "resale", "unit_price": 1})
    check("staff 403 POST /register", s == 403, str(s))
    s, _ = call("PATCH", "/api/menu/register", STAFF, {"item_name": "x", "type": "dish", "unit_price": 1})
    check("staff 403 PATCH /register", s == 403, str(s))
    s, _ = call("GET", "/api/stock/pick-items", STAFF)
    check("staff 200 GET /stock/pick-items (picker)", s == 200, str(s))

finally:
    # ── CLEANUP ──
    removed = {"inventory": [], "ingredients": [], "stock_in": [], "guard_stock_in": []}
    conn = db()
    try:
        with conn.cursor() as cur:
            for n in [RESALE, INGREDIENT]:
                cur.execute("DELETE FROM stock_in WHERE item_name=%s", (n,))
                removed["stock_in"].append(n)
                cur.execute("DELETE FROM inventory WHERE item_name=%s", (n,))
                removed["inventory"].append(n)
                cur.execute("DELETE FROM ingredients WHERE name=%s", (n,))
                removed["ingredients"].append(n)
            # restore the stock-guard item quantity + remove its probe stock_in row
            if guard["stock_in_ids"]:
                for sid in guard["stock_in_ids"]:
                    cur.execute("DELETE FROM stock_in WHERE id=%s", (sid,))
                    removed["guard_stock_in"].append(sid)
            if guard["item_name"] is not None and guard["orig_qty"] is not None:
                cur.execute("UPDATE inventory SET quantity=%s WHERE item_name=%s", (guard["orig_qty"], guard["item_name"]))
        conn.commit()
    finally:
        conn.close()
    print("\n── CLEANUP ──")
    print(json.dumps(removed, ensure_ascii=False))
    if guard["item_name"]:
        print(f"restored quantity of '{guard['item_name']}' -> {guard['orig_qty']}")

print(f"\n==== SUMMARY: {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
