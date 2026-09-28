"""PS VIBE API — Recipe / Food-Cost routes (owner 2026-09-28 Osmo "လုပ်ပေးပါ").

Adds made-dish costing (လက်ဖက်ထမင်း, ကြေးအိုးထမင်း, rice dishes …) ON TOP of the
existing inventory + stock_in + stock_out model. Nothing about sales / stock
behaviour changes; this is purely additive.

Data model (additive only):
  * ingredients   — maps a purchased stock_in.item_name to a base unit + conversion
                    (e.g. ပိဿာ = 1632 g).  base_per_purchase defaults to 1.
  * menu_recipes  — menu_item (matches inventory.item_name) -> ingredient (matches
                    stock_in.item_name / ingredients.name) with qty per portion in
                    the ingredient's BASE unit + waste %.

Endpoints (mounted at /api/menu):
  GET    /api/menu/costs                    — every inventory item, costed
  GET    /api/menu/recipe?item=<name>        — one menu item's recipe rows + totals
  POST   /api/menu/recipe                    — upsert one recipe row
  DELETE /api/menu/recipe?id=<id>            — remove a recipe row
  GET    /api/menu/ingredients               — ingredient master + computed unit cost
  POST   /api/menu/ingredients               — upsert one ingredient conversion
  GET    /api/menu/food-cost-report?month=YYYY-MM  — monthly theoretical vs revenue
"""
import logging
from decimal import Decimal
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from auth import get_staff_or_dashboard_user
from mysql_db import query as _q, query_one as _q1, execute as _ex, delete_rows as _del, get_connection as _get_conn
from routes.menu_cost import (
    weighted_average_cost,
    cost_per_base_unit,
    portion_cost_from,
    food_cost_metrics,
    food_cost_status,
    lookup_conversion,
    normalize_item_name,
    parse_selling_price,
    normalize_item_type,
    classify_item_type,
    resale_unit_cost,
    filter_items_by_type,
    BASE_UNIT_CONVERSIONS,
    _d,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/menu", tags=["menu"])

_tables_ready = False


def _branch(request: Request) -> int:
    """Current branch id from the branch-context middleware (defaults to 1)."""
    try:
        return int(getattr(request.state, "branch_id", 1))
    except (TypeError, ValueError):
        return 1


def _num(v):
    """Coerce Decimal/int to a JSON-safe float (None passes through)."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _is_admin(user: dict) -> bool:
    """Same role check as routes/account_routes._is_admin (owner 2026-09-28: admin only)."""
    return str((user or {}).get("role") or "").strip().lower() == "admin"


async def require_admin_user(user: dict = Depends(get_staff_or_dashboard_user)) -> dict:
    """ADMIN-only gate for the Recipe & Food-Cost feature (owner 2026-09-28).

    Reuses the existing get_staff_or_dashboard_user auth + the account_routes
    role-check pattern.  Non-admin (staff/cashier/etc.) get 403 on EVERY
    /api/menu/* endpoint — read endpoints included."""
    if not _is_admin(user):
        role = str((user or {}).get("role") or "staff").strip() or "staff"
        raise HTTPException(
            status_code=403,
            detail=f"Recipe & Cost is admin-only. Your role is '{role}'. Ask an owner / admin.",
        )
    return user


def _ensure_tables() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS + defensive ALTERs (same style as
    account_routes._ensure_daily_balances_table).  Never touches existing data."""
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingredients (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    branch_id INT NOT NULL DEFAULT 1,
                    name VARCHAR(200) NOT NULL,
                    base_unit VARCHAR(10) NOT NULL DEFAULT 'g',
                    purchase_unit VARCHAR(20) DEFAULT NULL,
                    base_per_purchase DECIMAL(14,4) NOT NULL DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_branch_name (branch_id, name),
                    KEY idx_branch (branch_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS menu_recipes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    branch_id INT NOT NULL DEFAULT 1,
                    menu_item VARCHAR(200) NOT NULL,
                    ingredient VARCHAR(200) NOT NULL,
                    qty_per_portion DECIMAL(12,3) NOT NULL DEFAULT 0,
                    waste_pct DECIMAL(5,2) NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_branch_menu_ing (branch_id, menu_item, ingredient),
                    KEY idx_branch_menu (branch_id, menu_item)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # Idempotent item_type column on inventory (owner 2026-09-28).
            # resale = packaged/ready-made (cost from purchase price), dish = made
            # dish (cost from recipe).  Additive only — never touches existing data.
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'inventory'
                  AND COLUMN_NAME = 'item_type'
                """
            )
            col = cur.fetchone()
            if not col or int(col["c"] or 0) == 0:
                cur.execute(
                    "ALTER TABLE inventory ADD COLUMN item_type VARCHAR(16) NOT NULL DEFAULT 'resale'"
                )
        conn.commit()
        _tables_ready = True
    finally:
        conn.close()


# ── Cost-engine DB helpers (SELECT-only on prod data) ─────────────────────────
def _stockin_wavg(branch_id: int):
    """Per-item moving weighted average from stock_in (all-time, quantity>0)."""
    rows = _q(
        "SELECT item_name, SUM(quantity) AS qty, SUM(total_cost) AS tot, "
        "MAX(created_at) AS last_in "
        "FROM stock_in WHERE branch_id=%s AND quantity > 0 GROUP BY item_name",
        (branch_id,),
    )
    out = {}
    for r in rows:
        w = weighted_average_cost([(r.get("qty"), r.get("tot"))])
        out[r["item_name"]] = {
            "weighted_avg": w,
            "total_qty": r.get("qty"),
            "total_cost": r.get("tot"),
            "last_in": r.get("last_in"),
        }
    return out


def _ingredient_costs(branch_id: int):
    """Cost per BASE unit for every ingredient (stock_in item) — one pass.

    Combines the stock_in weighted average with the ingredients conversion row
    (base_per_purchase).  An ingredient with no stock_in rows (or <=0 qty) has
    cost None (unknown — never silently 0)."""
    stock = _stockin_wavg(branch_id)
    ing_rows = _q("SELECT name, base_unit, purchase_unit, base_per_purchase FROM ingredients WHERE branch_id=%s", (branch_id,))
    conv = {r["name"]: r for r in ing_rows}
    out = {}
    for name, s in stock.items():
        cr = conv.get(name) or {}
        bpp = cr.get("base_per_purchase") if cr.get("base_per_purchase") is not None else Decimal("1")
        cp = cost_per_base_unit(s["weighted_avg"], bpp) if s["weighted_avg"] is not None else None
        out[name] = {
            "weighted_avg": s["weighted_avg"],
            "cost_per_base_unit": cp,
            "base_unit": cr.get("base_unit") or "g",
            "purchase_unit": cr.get("purchase_unit"),
            "base_per_purchase": _d(bpp),
            "last_in": s["last_in"],
            "total_qty": s["total_qty"],
        }
    return out, conv


def _recipes_by_menu(branch_id: int, menu_item: str = None):
    rows = _q(
        "SELECT id, menu_item, ingredient, qty_per_portion, waste_pct FROM menu_recipes "
        "WHERE branch_id=%s" + (" AND menu_item=%s" if menu_item else "") + " ORDER BY id",
        (branch_id, menu_item) if menu_item else (branch_id,),
    )
    out = {}
    for r in rows:
        out.setdefault(r["menu_item"], []).append(r)
    return out


def _inventory_map(branch_id: int):
    rows = _q("SELECT id, item_name, category, unit_price, quantity, item_type FROM inventory WHERE branch_id=%s", (branch_id,))
    return {r["item_name"]: r for r in rows}


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.get("/costs")
async def menu_costs(request: Request, recipe_only: int = Query(0),
                     type: str = Query("all"),
                     user: dict = Depends(require_admin_user)):
    """Every inventory item, costed — split by type (owner 2026-09-28).

    * dish   — costed from its menu_recipes recipe (cost_source 'recipe').
    * resale — costed from the stock_in weighted-average purchase price
               (cost_source 'purchase'); no recipe required.

    ``type`` filters the result: resale | dish | all (default).  ``recipe_only=1``
    is kept for backward compatibility (== dish items with a recipe).
    """
    _ensure_tables()
    branch = _branch(request)
    inv = _inventory_map(branch)
    costs, _conv = _ingredient_costs(branch)
    recipes = _recipes_by_menu(branch)

    items = []
    for name, iv in inv.items():
        rec = recipes.get(name) or []
        selling = _d(iv.get("unit_price"))
        has_recipe = bool(rec)
        stored_type = (iv.get("item_type") or "").strip().lower() or "resale"
        etype = classify_item_type(stored_type, has_recipe)
        missing = []
        portion_cost = None
        unit_cost = None
        metrics = None
        no_purchase_yet = False
        cost_source = "recipe" if etype == "dish" else "purchase"

        if etype == "dish":
            status = "no_recipe" if not has_recipe else "unknown"
            if has_recipe:
                unit_costs = {c_name: (costs.get(c_name) or {}).get("cost_per_base_unit") for c_name in
                              {r["ingredient"] for r in rec}}
                portion_cost, missing = portion_cost_from(
                    [(r["qty_per_portion"], r["waste_pct"], r["ingredient"]) for r in rec],
                    unit_costs,
                )
                if portion_cost is not None:
                    metrics = food_cost_metrics(portion_cost, selling)
                    status = food_cost_status(metrics["food_cost_pct"]) if metrics else "unknown"
                else:
                    status = "unknown"
            unit_cost = portion_cost
        else:
            # resale — purchase unit cost (no base-unit conversion)
            unit_cost = (costs.get(name) or {}).get("weighted_avg")
            no_purchase_yet = unit_cost is None
            if unit_cost is not None:
                metrics = food_cost_metrics(unit_cost, selling)
                status = food_cost_status(metrics["food_cost_pct"]) if metrics else "unknown"
            else:
                status = "no_purchase"

        items.append({
            "item_name": name,
            "item_type": etype,
            "stored_item_type": stored_type,
            "cost_source": cost_source,
            "category": iv.get("category"),
            "selling_price": _num(selling),
            "has_recipe": has_recipe,
            "unit_cost": _num(unit_cost),
            "portion_cost": _num(portion_cost),
            "food_cost_pct": _num(metrics["food_cost_pct"]) if metrics else None,
            "margin": _num(metrics["margin"]) if metrics else None,
            "margin_pct": _num(metrics["margin_pct"]) if metrics else None,
            "no_purchase_yet": no_purchase_yet,
            "missing_cost_ingredients": missing,
            "status": status,
            "ingredient_count": len(rec),
        })

    if recipe_only:
        items = [i for i in items if i["has_recipe"]]
    else:
        items = filter_items_by_type(items, type)

    items.sort(key=lambda i: (0 if i["item_type"] == "dish" else 1, i["item_name"]))
    return {"success": True, "data": items}


@router.post("/item")
async def create_menu_item(body: dict = Body(...), request: Request = None,
                           user: dict = Depends(require_admin_user)):
    """ADMIN-only: create a menu item = a new ``inventory`` row (a sellable dish).

    This is the missing piece for self-serve dish entry — today a dish must already
    exist in ``inventory`` before it can be costed.  Creates it with ``quantity 0``
    (it becomes sellable via the POS as soon as stock is received, exactly like any
    other inventory item) and ``unit_price`` = selling price.  Additive only.

    body: {item_name, category? (default 'Food'), unit_price? (selling price, ≥ 0)}
    """
    branch = _branch(request)
    _ensure_tables()
    name = normalize_item_name(body.get("item_name", body.get("name")))
    if not name:
        raise HTTPException(status_code=422, detail={"success": False, "error": "item_name is required", "code": 422})
    category = str(body.get("category") or "").strip() or "Food"
    price = parse_selling_price(body.get("unit_price", body.get("selling_price")))
    if price is None:
        raise HTTPException(status_code=422, detail={"success": False, "error": "unit_price (selling price) must be a number ≥ 0", "code": 422})
    item_type = normalize_item_type(body.get("item_type"))
    if item_type is None:
        item_type = "resale"  # auto-classify default (no recipe yet)

    existing = _q1("SELECT id FROM inventory WHERE item_name=%s", (name,))
    if existing:
        raise HTTPException(status_code=409, detail={"success": False, "error": f"'{name}' already exists in the menu", "code": 409})

    new_id = _ex(
        "INSERT INTO inventory (branch_id, item_name, category, quantity, unit_price, reorder_level, item_type) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (branch, name, category, 0, price, 0, item_type),
    )
    row = _q1("SELECT id, item_name, category, quantity, unit_price, item_type FROM inventory WHERE id=%s", (new_id,))
    return {"success": True, "data": {
        "id": row["id"],
        "item_name": row["item_name"],
        "category": row["category"],
        "quantity": _num(row["quantity"]),
        "selling_price": _num(row["unit_price"]),
        "item_type": row.get("item_type") or "resale",
    }}


@router.patch("/item")
async def update_menu_item(body: dict = Body(...), request: Request = None,
                           user: dict = Depends(require_admin_user)):
    """ADMIN-only: rename / change category / price / item_type of a menu item.

    ``item_name`` identifies the CURRENT row (case-insensitive via MySQL collation).
    Optional update fields: ``new_name`` (rename), ``category``, ``unit_price``
    (selling price), ``item_type`` (resale | dish).  At least one update field must
    be supplied.  Rename collision (with a *different* item) is rejected with 409.
    """
    branch = _branch(request)
    _ensure_tables()
    current = normalize_item_name(body.get("item_name"))
    if not current:
        raise HTTPException(status_code=422, detail={"success": False, "error": "item_name (current name) is required", "code": 422})

    existing = _q1("SELECT id, item_name, category, unit_price, item_type FROM inventory WHERE item_name=%s", (current,))
    if not existing:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"'{current}' not found in the menu", "code": 404})

    _new_raw = body.get("new_name")
    new_name = normalize_item_name(_new_raw) if _new_raw is not None else None
    _cat_raw = body.get("category")
    category = (str(_cat_raw).strip() or None) if _cat_raw is not None else None
    has_price = ("unit_price" in body) or ("selling_price" in body)
    price = parse_selling_price(body.get("unit_price", body.get("selling_price"))) if has_price else None
    if has_price and price is None:
        raise HTTPException(status_code=422, detail={"success": False, "error": "unit_price (selling price) must be a number ≥ 0", "code": 422})

    item_type = None
    if "item_type" in body:
        item_type = normalize_item_type(body.get("item_type"))
        if item_type is None:
            raise HTTPException(status_code=422, detail={"success": False, "error": "item_type must be 'resale' or 'dish'", "code": 422})

    if new_name is None and category is None and price is None and item_type is None:
        raise HTTPException(status_code=422, detail={"success": False, "error": "no update fields supplied (new_name / category / unit_price / item_type)", "code": 422})

    # Rename collision: only when the name actually changes (case-insensitive).
    if new_name and str(new_name).lower() != str(existing["item_name"]).lower():
        clash = _q1("SELECT id FROM inventory WHERE item_name=%s AND id<>%s", (new_name, existing["id"]))
        if clash:
            raise HTTPException(status_code=409, detail={"success": False, "error": f"'{new_name}' already exists in the menu", "code": 409})

    final_name = new_name or existing["item_name"]
    final_cat = category if category is not None else existing["category"]
    final_price = price if price is not None else existing["unit_price"]
    final_type = item_type if item_type is not None else (existing.get("item_type") or "resale")

    _ex("UPDATE inventory SET item_name=%s, category=%s, unit_price=%s, item_type=%s WHERE id=%s",
        (final_name, final_cat, final_price, final_type, existing["id"]))

    return {"success": True, "data": {
        "id": existing["id"],
        "item_name": final_name,
        "category": final_cat,
        "selling_price": _num(final_price),
        "item_type": final_type,
    }}


@router.get("/recipe")
async def menu_recipe(item: str = Query(...), request: Request = None,
                      user: dict = Depends(require_admin_user)):
    """One menu item's recipe rows with per-row qty, unit cost, waste, line cost + totals."""
    _ensure_tables()
    branch = _branch(request)
    costs, _conv = _ingredient_costs(branch)
    rows = _q(
        "SELECT id, menu_item, ingredient, qty_per_portion, waste_pct FROM menu_recipes "
        "WHERE branch_id=%s AND menu_item=%s ORDER BY id",
        (branch, item),
    )
    selling = _inventory_map(branch).get(item, {}).get("unit_price")
    lines = []
    total = Decimal("0")
    missing = []
    for r in rows:
        ci = costs.get(r["ingredient"]) or {}
        uc = ci.get("cost_per_base_unit")
        qty = _d(r["qty_per_portion"])
        waste = _d(r["waste_pct"])
        line_cost = None
        if uc is not None:
            line_cost = (qty * uc * (Decimal("1") + waste / Decimal("100"))).quantize(Decimal("0.01"))
            total += line_cost
        else:
            missing.append(r["ingredient"])
        lines.append({
            "id": r["id"],
            "ingredient": r["ingredient"],
            "qty_per_portion": _num(qty),
            "base_unit": ci.get("base_unit") or "g",
            "unit_cost_per_base": _num(uc),
            "waste_pct": _num(waste),
            "line_cost": _num(line_cost),
        })
    portion_cost = total if not missing else None
    metrics = food_cost_metrics(portion_cost, _d(selling)) if (portion_cost is not None and selling) else None
    return {
        "success": True,
        "data": {
            "menu_item": item,
            "selling_price": _num(_d(selling)),
            "portion_cost": _num(portion_cost),
            "food_cost_pct": _num(metrics["food_cost_pct"]) if metrics else None,
            "margin": _num(metrics["margin"]) if metrics else None,
            "margin_pct": _num(metrics["margin_pct"]) if metrics else None,
            "status": food_cost_status(metrics["food_cost_pct"]) if metrics else ("unknown" if missing else "no_recipe"),
            "missing_cost_ingredients": missing,
            "rows": lines,
        },
    }


@router.post("/recipe")
async def upsert_recipe(body: dict = Body(...), request: Request = None,
                        user: dict = Depends(require_admin_user)):
    """Upsert one recipe row {menu_item, ingredient, qty_per_portion, waste_pct}."""
    _ensure_tables()
    branch = _branch(request)
    menu_item = str(body.get("menu_item") or "").strip()
    ingredient = str(body.get("ingredient") or "").strip()
    if not menu_item or not ingredient:
        raise HTTPException(status_code=422, detail={"success": False, "error": "menu_item and ingredient are required", "code": 422})
    qty = _d(body.get("qty_per_portion") or 0)
    waste = _d(body.get("waste_pct") or 0)
    if qty <= 0:
        raise HTTPException(status_code=422, detail={"success": False, "error": "qty_per_portion must be > 0", "code": 422})
    if waste < 0:
        raise HTTPException(status_code=422, detail={"success": False, "error": "waste_pct cannot be negative", "code": 422})

    existing = _q1("SELECT id FROM menu_recipes WHERE branch_id=%s AND menu_item=%s AND ingredient=%s", (branch, menu_item, ingredient))
    if existing:
        _ex("UPDATE menu_recipes SET qty_per_portion=%s, waste_pct=%s WHERE id=%s", (qty, waste, existing["id"]))
        rid = existing["id"]
    else:
        rid = _ex(
            "INSERT INTO menu_recipes (branch_id, menu_item, ingredient, qty_per_portion, waste_pct) VALUES (%s,%s,%s,%s,%s)",
            (branch, menu_item, ingredient, qty, waste),
        )
    return {"success": True, "data": {"id": rid, "menu_item": menu_item, "ingredient": ingredient}}


@router.delete("/recipe")
async def delete_recipe(id: int = Query(...), request: Request = None,
                        user: dict = Depends(require_admin_user)):
    """Remove one recipe row by id."""
    _ensure_tables()
    branch = _branch(request)
    affected = _del("DELETE FROM menu_recipes WHERE id=%s AND branch_id=%s", (id, branch))
    if not affected:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Recipe row not found", "code": 404})
    return {"success": True, "data": {"deleted": affected}}


@router.get("/ingredients")
async def menu_ingredients(request: Request, user: dict = Depends(require_admin_user)):
    """Ingredient master + computed unit cost per base unit + a suggested list of
    stock_in item names that have no conversion row yet (so staff can set one)."""
    _ensure_tables()
    branch = _branch(request)
    costs, conv = _ingredient_costs(branch)
    ing_rows = _q("SELECT id, name, base_unit, purchase_unit, base_per_purchase, updated_at FROM ingredients WHERE branch_id=%s ORDER BY name", (branch,))

    # Ingredient master entries (existing conversions), merged with computed cost.
    master = []
    seen_names = set()
    for r in ing_rows:
        seen_names.add(r["name"])
        c = costs.get(r["name"]) or {}
        master.append({
            "id": r["id"],
            "name": r["name"],
            "base_unit": r["base_unit"] or "g",
            "purchase_unit": r["purchase_unit"],
            "base_per_purchase": _num(r["base_per_purchase"]),
            "cost_per_base_unit": _num(c.get("cost_per_base_unit")),
            "weighted_avg_cost": _num(c.get("weighted_avg")),
            "last_purchase": str(c.get("last_in")) if c.get("last_in") else None,
        })

    # Suggested: stock_in items that have purchases but no conversion row yet.
    suggested = []
    for name, c in sorted(costs.items(), key=lambda kv: kv[0]):
        if name in seen_names:
            continue
        suggested.append({
            "name": name,
            "weighted_avg_cost": _num(c.get("weighted_avg")),
            "cost_per_base_unit": _num(c.get("cost_per_base_unit")),
            "last_purchase": str(c.get("last_in")) if c.get("last_in") else None,
            "suggested_base_unit": "g",
            "suggested_conversion": None,
        })

    return {
        "success": True,
        "data": {
            "ingredients": master,
            "suggested": suggested,
            "known_units": [
                {"unit": k, "base_per_purchase": float(v)}
                for k, v in sorted(BASE_UNIT_CONVERSIONS.items(), key=lambda kv: str(kv[0]))
                if k
            ],
        },
    }


@router.post("/ingredients")
async def upsert_ingredient(body: dict = Body(...), request: Request = None,
                            user: dict = Depends(require_admin_user)):
    """Upsert {name, base_unit, purchase_unit, base_per_purchase}.

    When base_per_purchase is omitted/0, it is auto-filled from the known
    purchase-unit conversion table (ပိဿာ=1632, ကျပ်သား=10, kg=1000, litre=1000),
    else defaults to 1 (cost per purchased unit)."""
    _ensure_tables()
    branch = _branch(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail={"success": False, "error": "name is required", "code": 422})
    base_unit = str(body.get("base_unit") or "g").strip() or "g"
    purchase_unit = str(body.get("purchase_unit") or "").strip() or None
    bpp = body.get("base_per_purchase")
    if bpp is None or _d(bpp) <= 0:
        auto = lookup_conversion(purchase_unit) if purchase_unit else None
        bpp = auto if auto is not None else Decimal("1")
    else:
        bpp = _d(bpp)

    existing = _q1("SELECT id FROM ingredients WHERE branch_id=%s AND name=%s", (branch, name))
    if existing:
        _ex("UPDATE ingredients SET base_unit=%s, purchase_unit=%s, base_per_purchase=%s WHERE id=%s",
            (base_unit, purchase_unit, bpp, existing["id"]))
        iid = existing["id"]
    else:
        iid = _ex(
            "INSERT INTO ingredients (branch_id, name, base_unit, purchase_unit, base_per_purchase) VALUES (%s,%s,%s,%s,%s)",
            (branch, name, base_unit, purchase_unit, bpp),
        )
    return {"success": True, "data": {"id": iid, "name": name, "base_unit": base_unit, "purchase_unit": purchase_unit, "base_per_purchase": _num(bpp)}}


@router.get("/food-cost-report")
async def food_cost_report(month: str = Query(...), request: Request = None,
                           user: dict = Depends(require_admin_user)):
    """Monthly food-cost report: theoretical cost vs food revenue, split by type.

    dish   theoretical = SUM(stock_out.quantity × portion_cost from recipe)
    resale theoretical = SUM(stock_out.quantity × purchase unit cost from stock_in)
    revenue = SUM(stock_out.total)
    NOTE: stock_out.sale_date is stored in MMT (Asia/Yangon)."""
    _ensure_tables()
    branch = _branch(request)
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(status_code=422, detail={"success": False, "error": "month must be YYYY-MM", "code": 422})
    try:
        start = f"{month}-01"
        y, m = int(month[:4]), int(month[5:7])
        if not (1 <= m <= 12):
            raise ValueError("month out of range")
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = f"{ny:04d}-{nm:02d}-01"
    except ValueError:
        raise HTTPException(status_code=422, detail={"success": False, "error": "invalid month", "code": 422})

    costs, _conv = _ingredient_costs(branch)
    recipes = _recipes_by_menu(branch)
    inv = _inventory_map(branch)

    rows = _q(
        "SELECT item_name, SUM(quantity) AS qty, SUM(total) AS revenue FROM stock_out "
        "WHERE branch_id=%s AND sale_date >= %s AND sale_date < %s GROUP BY item_name ORDER BY revenue DESC",
        (branch, start, end),
    )

    breakdown = []
    total_revenue = Decimal("0")
    total_theoretical = Decimal("0")
    uncosted_items = []
    split = {
        "dish": {"revenue": Decimal("0"), "theoretical_cost": Decimal("0"), "items": 0},
        "resale": {"revenue": Decimal("0"), "theoretical_cost": Decimal("0"), "items": 0},
    }
    for r in rows:
        name = r["item_name"]
        qty = Decimal(str(r["qty"] or 0))
        revenue = _d(r["revenue"])
        total_revenue += revenue
        rec = recipes.get(name) or []
        stored_type = (inv.get(name) or {}).get("item_type") or "resale"
        etype = classify_item_type(stored_type, bool(rec))
        cost_source = "recipe" if etype == "dish" else "purchase"
        portion_cost = None
        unit_cost = None
        missing = []
        no_purchase = False
        uncosted_reason = None

        if etype == "dish":
            if not rec:
                uncosted_reason = "no_recipe"
            else:
                unit_costs = {c_name: (costs.get(c_name) or {}).get("cost_per_base_unit") for c_name in {x["ingredient"] for x in rec}}
                portion_cost, missing = portion_cost_from(
                    [(x["qty_per_portion"], x["waste_pct"], x["ingredient"]) for x in rec], unit_costs)
                if portion_cost is None:
                    uncosted_reason = "missing_ingredients"
            unit_cost = portion_cost
        else:
            # resale — purchase unit cost (no base-unit conversion)
            unit_cost = (costs.get(name) or {}).get("weighted_avg")
            if unit_cost is None:
                no_purchase = True
                uncosted_reason = "no_purchase"
            else:
                portion_cost = unit_cost

        theoretical = portion_cost * qty if portion_cost is not None else None
        if theoretical is not None:
            total_theoretical += theoretical
            split[etype]["theoretical_cost"] += theoretical
        split[etype]["revenue"] += revenue
        split[etype]["items"] += 1

        if uncosted_reason:
            uncosted_items.append({
                "item_name": name, "item_type": etype, "revenue": _num(revenue),
                "qty": _num(qty), "reason": uncosted_reason,
                "missing_ingredients": missing,
            })

        breakdown.append({
            "item_name": name,
            "item_type": etype,
            "cost_source": cost_source,
            "qty_sold": _num(qty),
            "revenue": _num(revenue),
            "unit_cost": _num(unit_cost),
            "portion_cost": _num(portion_cost),
            "theoretical_cost": _num(theoretical),
            "missing_ingredients": missing,
            "no_purchase_yet": no_purchase,
        })

    food_cost_pct = None
    if total_revenue > 0 and total_theoretical is not None:
        food_cost_pct = (total_theoretical / total_revenue * Decimal("100")).quantize(Decimal("0.1"))

    def _split_pct(rev, theo):
        if rev <= 0 or theo is None:
            return None
        return _num((theo / rev * Decimal("100")).quantize(Decimal("0.1")))

    return {
        "success": True,
        "data": {
            "month": month,
            "revenue": _num(total_revenue),
            "theoretical_cost": _num(total_theoretical),
            "food_cost_pct": _num(food_cost_pct),
            "status": food_cost_status(food_cost_pct),
            "gross_margin": _num(total_revenue - total_theoretical),
            "split": {
                "dish": {
                    "revenue": _num(split["dish"]["revenue"]),
                    "theoretical_cost": _num(split["dish"]["theoretical_cost"]),
                    "food_cost_pct": _split_pct(split["dish"]["revenue"], split["dish"]["theoretical_cost"]),
                    "items": split["dish"]["items"],
                },
                "resale": {
                    "revenue": _num(split["resale"]["revenue"]),
                    "theoretical_cost": _num(split["resale"]["theoretical_cost"]),
                    "food_cost_pct": _split_pct(split["resale"]["revenue"], split["resale"]["theoretical_cost"]),
                    "items": split["resale"]["items"],
                },
            },
            "breakdown": breakdown,
            "uncosted_items": uncosted_items,
        },
    }
