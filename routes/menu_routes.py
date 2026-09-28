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
    rows = _q("SELECT id, item_name, category, unit_price, quantity FROM inventory WHERE branch_id=%s", (branch_id,))
    return {r["item_name"]: r for r in rows}


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.get("/costs")
async def menu_costs(request: Request, recipe_only: int = Query(0),
                     user: dict = Depends(require_admin_user)):
    """Every inventory item with selling price, cost, food-cost % and margin."""
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
        missing = []
        portion_cost = None
        metrics = None
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
        items.append({
            "item_name": name,
            "category": iv.get("category"),
            "selling_price": _num(selling),
            "has_recipe": has_recipe,
            "portion_cost": _num(portion_cost),
            "food_cost_pct": _num(metrics["food_cost_pct"]) if metrics else None,
            "margin": _num(metrics["margin"]) if metrics else None,
            "margin_pct": _num(metrics["margin_pct"]) if metrics else None,
            "missing_cost_ingredients": missing,
            "status": status,
            "ingredient_count": len(rec),
        })

    if recipe_only:
        items = [i for i in items if i["has_recipe"]]

    items.sort(key=lambda i: (not i["has_recipe"], i["item_name"]))
    return {"success": True, "data": items}


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
    """Monthly food-cost report: theoretical ingredient cost vs food revenue.

    theoretical = SUM(stock_out.quantity × portion_cost of that menu item)
    revenue     = SUM(stock_out.total)
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

    rows = _q(
        "SELECT item_name, SUM(quantity) AS qty, SUM(total) AS revenue FROM stock_out "
        "WHERE branch_id=%s AND sale_date >= %s AND sale_date < %s GROUP BY item_name ORDER BY revenue DESC",
        (branch, start, end),
    )

    breakdown = []
    total_revenue = Decimal("0")
    total_theoretical = Decimal("0")
    uncosted_items = []
    for r in rows:
        name = r["item_name"]
        qty = Decimal(str(r["qty"] or 0))
        revenue = _d(r["revenue"])
        total_revenue += revenue
        rec = recipes.get(name) or []
        if not rec:
            uncosted_items.append({"item_name": name, "revenue": _num(revenue), "qty": _num(qty)})
            continue
        unit_costs = {c_name: (costs.get(c_name) or {}).get("cost_per_base_unit") for c_name in {x["ingredient"] for x in rec}}
        portion_cost, missing = portion_cost_from(
            [(x["qty_per_portion"], x["waste_pct"], x["ingredient"]) for x in rec], unit_costs)
        theoretical = portion_cost * qty if portion_cost is not None else None
        if theoretical is not None:
            total_theoretical += theoretical
        breakdown.append({
            "item_name": name,
            "qty_sold": _num(qty),
            "revenue": _num(revenue),
            "portion_cost": _num(portion_cost),
            "theoretical_cost": _num(theoretical),
            "missing_ingredients": missing,
        })

    food_cost_pct = None
    if total_revenue > 0 and total_theoretical is not None:
        food_cost_pct = (total_theoretical / total_revenue * Decimal("100")).quantize(Decimal("0.1"))
    return {
        "success": True,
        "data": {
            "month": month,
            "revenue": _num(total_revenue),
            "theoretical_cost": _num(total_theoretical),
            "food_cost_pct": _num(food_cost_pct),
            "status": food_cost_status(food_cost_pct),
            "gross_margin": _num(total_revenue - total_theoretical),
            "breakdown": breakdown,
            "uncosted_items": uncosted_items,
        },
    }
