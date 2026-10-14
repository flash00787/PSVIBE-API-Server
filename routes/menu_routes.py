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
    normalize_kind,
    classify_item_kind,
    filter_items_by_kind,
    ingredient_display,
    normalize_cost_basis,
    select_unit_cost,
    last_purchase_cost,
    fifo_layers,
    ledger_layers,
    fifo_consume,
    normalize_variant_key,
    duplicate_variant_groups,
    aggregate_canonical_purchases,
    normalize_menu_type,
    menu_type_requires_price,
    validate_menu_type_price,
    classify_menu_type,
    COST_BASIS_LAST,
    COST_BASIS_AVERAGE,
    COST_BASIS_FIFO,
    BASE_UNIT_CONVERSIONS,
    _d,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/menu", tags=["menu"])

_tables_ready = False

# Global cost-basis setting (settings_config key) — "fifo" | "last" | "average" (default "fifo").
_COST_BASIS_KEY = "ingredient_cost_basis"


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
                    kind VARCHAR(12) NOT NULL DEFAULT 'ingredient',
                    base_unit VARCHAR(10) NOT NULL DEFAULT 'g',
                    purchase_unit VARCHAR(20) DEFAULT NULL,
                    base_per_purchase DECIMAL(14,4) NOT NULL DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_branch_name (branch_id, name),
                    KEY idx_branch (branch_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # Idempotent ``kind`` column on ingredients (owner 2026-09-28 Osmo:
            # ingredient vs product split).  Additive only — never touches data.
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ingredients'
                  AND COLUMN_NAME = 'kind'
                """
            )
            col = cur.fetchone()
            if not col or int(col["c"] or 0) == 0:
                cur.execute(
                    "ALTER TABLE ingredients ADD COLUMN kind VARCHAR(12) NOT NULL DEFAULT 'ingredient' AFTER name"
                )
            # Idempotent ``cost_basis`` column on ingredients (owner 2026-09-28 Osmo:
            # per-ingredient cost-basis override, NULL = use the global setting).
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ingredients'
                  AND COLUMN_NAME = 'cost_basis'
                """
            )
            col = cur.fetchone()
            if not col or int(col["c"] or 0) == 0:
                cur.execute(
                    "ALTER TABLE ingredients ADD COLUMN cost_basis VARCHAR(10) DEFAULT NULL AFTER kind"
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingredient_variants (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    branch_id INT NOT NULL DEFAULT 1,
                    canonical_name VARCHAR(200) NOT NULL,
                    variant_name VARCHAR(200) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_branch_variant (branch_id, variant_name),
                    KEY idx_branch_canonical (branch_id, canonical_name)
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
def _variant_canonical(branch_id: int):
    """dict: variant_name -> canonical_name (from ingredient_variants)."""
    rows = _q("SELECT canonical_name, variant_name FROM ingredient_variants WHERE branch_id=%s", (branch_id,))
    return {r["variant_name"]: r["canonical_name"] for r in rows}


def _stockin_rows(branch_id: int):
    """All valid stock_in rows as (item_name, quantity, total_cost, created_at, id),
    ordered by created_at ASC, id ASC so last-purchase ties resolve to the newest id
    and FIFO layers draw oldest-first deterministically."""
    rows = _q(
        "SELECT item_name, quantity, total_cost, created_at, id FROM stock_in "
        "WHERE branch_id=%s AND quantity > 0 ORDER BY created_at ASC, id ASC",
        (branch_id,),
    )
    return [
        (r["item_name"], r["quantity"], r["total_cost"],
         str(r["created_at"]) if r.get("created_at") else "", r["id"])
        for r in rows
    ]


def _canonical_stock(branch_id: int):
    """Per-canonical stock aggregates (wavg + last purchase + totals), merged
    across variant names.  Returns ``(by_canonical, canonical_of)``."""
    canon = _variant_canonical(branch_id)
    rows = _stockin_rows(branch_id)
    by_canon = aggregate_canonical_purchases(rows, canon)
    return by_canon, canon


def _canonical_fifo_layers(branch_id: int, canon: dict):
    """FIFO layers per canonical name: {canonical: [layer dict, …]} ordered
    oldest -> newest, merging across variant names.  Layer dicts come from
    ``routes.menu_cost.fifo_layers`` (qty, unit_cost, created_at, id)."""
    rows = _stockin_rows(branch_id)
    buckets = {}
    for name, qty, cost, created, rid in rows:
        cname = canon.get(name, name)
        buckets.setdefault(cname, []).append((qty, cost, created, rid))
    out = {}
    for cname, raw in buckets.items():
        out[cname] = fifo_layers(raw)
    return out


def _ledger_fifo_layers(branch_id: int, canon: dict):
    """FIFO layers from ``stock_layers.qty_remaining`` (ACTUAL remaining, already
    net of consumption) per canonical name, oldest -> newest.  Returns {} when the
    ledger table is absent or empty — callers then fall back to the estimate."""
    try:
        rows = _q(
            "SELECT item_name, qty_remaining, unit_cost, purchased_at, stock_in_id FROM stock_layers "
            "WHERE branch_id=%s AND qty_remaining > 0 ORDER BY purchased_at ASC, id ASC",
            (branch_id,),
        )
    except Exception:
        return {}
    buckets = {}
    for r in rows:
        cname = canon.get(r["item_name"], r["item_name"])
        buckets.setdefault(cname, []).append(
            (r["qty_remaining"], r["unit_cost"],
             str(r["purchased_at"]) if r.get("purchased_at") else "", r["stock_in_id"])
        )
    return {cname: ledger_layers(raw) for cname, raw in buckets.items()}


def _fifo_consumption(branch_id: int, canon: dict):
    """Compute FIFO consumption (Decimal) per canonical ingredient.

    consumption = direct stock_out (canonical + variant names) + recipe usage
    (dish sales x recipe qty x (1 + waste%)).  Returns
    ``(consumption, recipe_referenced)`` where ``recipe_referenced`` is the set of
    canonical names referenced by any recipe row (used to label "FIFO (est.)" when
    an ingredient has no recipe so dish usage is unknowable).  Read-only."""
    rows = _q(
        "SELECT item_name, SUM(quantity) AS qty FROM stock_out WHERE branch_id=%s GROUP BY item_name",
        (branch_id,),
    )
    direct = {}
    sales = {}
    for r in rows:
        sales[r["item_name"]] = _d(r["qty"])
        c = canon.get(r["item_name"], r["item_name"])
        direct[c] = direct.get(c, Decimal("0")) + _d(r["qty"])

    recipes = _recipes_by_menu(branch_id)
    recipe_usage = {}
    recipe_referenced = set()
    for menu_item, recs in recipes.items():
        sold = sales.get(menu_item, Decimal("0"))
        for rec in recs:
            c = canon.get(rec["ingredient"], rec["ingredient"])
            recipe_referenced.add(c)
            add = sold * _d(rec["qty_per_portion"]) * (Decimal("1") + _d(rec["waste_pct"]) / Decimal("100"))
            recipe_usage[c] = recipe_usage.get(c, Decimal("0")) + add

    consumption = {}
    for c in set(direct) | set(recipe_usage):
        consumption[c] = direct.get(c, Decimal("0")) + recipe_usage.get(c, Decimal("0"))
    return consumption, recipe_referenced


def _global_cost_basis():
    """Global cost basis from settings_config (key ``ingredient_cost_basis``),
    defaulting to ``fifo`` when unset / invalid (owner 2026-09-28 Osmo "1 FIFO နဲ့ တွက်ပါ")."""
    row = _q1("SELECT config_value FROM settings_config WHERE config_key=%s", (_COST_BASIS_KEY,))
    b = normalize_cost_basis(row["config_value"]) if row else None
    return b or COST_BASIS_FIFO


def _ingredient_costs(branch_id: int):
    """Cost per BASE unit for every canonical ingredient (stock_in item) — one pass.

    Combines the stock_in weighted average + last-purchase price (aggregated across
    merged variant names) with the ingredients conversion row (base_per_purchase),
    then picks the effective basis (per-ingredient override or the global setting).
    An ingredient with no stock_in rows (or <=0 qty) has cost None (never 0)."""
    by_canon, canon = _canonical_stock(branch_id)
    ing_rows = _q(
        "SELECT name, base_unit, purchase_unit, base_per_purchase, cost_basis FROM ingredients WHERE branch_id=%s",
        (branch_id,),
    )
    # Canonicalise conversion rows: prefer the canonical's own row, else fall back
    # to a variant's row (e.g. a conversion saved under `Egg` applies to `ကြက်ဥ`).
    conv = {}
    for r in ing_rows:
        cname = canon.get(r["name"], r["name"])
        if cname == r["name"]:
            conv[cname] = r
        elif cname not in conv:
            conv[cname] = r
    global_basis = _global_cost_basis()
    fifo_layers_map = _canonical_fifo_layers(branch_id, canon)
    ledger_map = _ledger_fifo_layers(branch_id, canon)
    consumption, recipe_referenced = _fifo_consumption(branch_id, canon)
    out = {}
    for name, s in by_canon.items():
        cr = conv.get(name) or {}
        bpp = cr.get("base_per_purchase") if cr.get("base_per_purchase") is not None else Decimal("1")
        basis = normalize_cost_basis(cr.get("cost_basis")) or global_basis
        has_ledger = name in ledger_map
        f = fifo_consume(
            ledger_map[name] if has_ledger else fifo_layers_map.get(name, []),
            Decimal("0") if has_ledger else consumption.get(name, Decimal("0")),
        )
        fifo_unit = f["unit_cost"]
        unit_cost = select_unit_cost(s["weighted_avg"], s["last_purchase"], basis, fifo_unit)
        cp = cost_per_base_unit(unit_cost, bpp) if unit_cost is not None else None
        out[name] = {
            "weighted_avg": s["weighted_avg"],
            "last_purchase": s["last_purchase"],
            "unit_cost": unit_cost,
            "cost_basis": basis,
            "cost_per_base_unit": cp,
            "base_unit": cr.get("base_unit") or "g",
            "purchase_unit": cr.get("purchase_unit"),
            "base_per_purchase": _d(bpp),
            "last_in": s["last_in"],
            "total_qty": s["total_qty"],
            "fifo_unit_cost": fifo_unit,
            "fifo_layer_cost": f["fifo_layer_cost"],
            "fifo_layer_date": f["fifo_layer_date"],
            "fifo_remaining": f["fifo_remaining"],
            "on_hand_est": f["on_hand_est"],
            "fifo_consumed": f["consumed"],
            "fifo_out_of_stock": f["out_of_stock"],
            "fifo_est": name not in recipe_referenced,
            "fifo_no_purchase": not fifo_layers_map.get(name),
            "fifo_ledger": has_ledger,
        }
    return out, conv, canon


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


def _sellable_inventory_names(branch_id: int):
    """Names of inventory items that are sellable PRODUCTS (not made dishes)."""
    return {
        name for name, r in _inventory_map(branch_id).items()
        if (str(r.get("item_type") or "resale").strip().lower()) != "dish"
    }


def _all_item_names(branch_id: int):
    """Union of every raw item name (stock_in + ingredients + inventory) — used by
    the duplicate-name candidate detector."""
    names = set()
    for r in _q("SELECT DISTINCT item_name FROM stock_in WHERE branch_id=%s", (branch_id,)):
        names.add(r["item_name"])
    for r in _q("SELECT name FROM ingredients WHERE branch_id=%s", (branch_id,)):
        names.add(r["name"])
    for r in _q("SELECT item_name FROM inventory WHERE branch_id=%s", (branch_id,)):
        names.add(r["item_name"])
    return names


def _stock_items(branch_id: int):
    """Full stock-item catalog (owner 2026-09-28): every purchased (stock_in) item
    + every explicit ``ingredients`` row, each with an effective kind
    (ingredient|product) and unit-cost display info.

    Variant-merge aware: items are keyed by CANONICAL name; stock_in aggregates
    (weighted average + last purchase) are summed across the canonical's variant
    names.  Kind is NOT inherited from variants (it stays on the canonical's own
    row or the sellable heuristic), but the base-unit conversion may fall back to
    a variant's row so costing stays correct.
    """
    by_canon, canon = _canonical_stock(branch_id)
    ing_rows = _q(
        "SELECT id, name, kind, base_unit, purchase_unit, base_per_purchase, cost_basis, updated_at "
        "FROM ingredients WHERE branch_id=%s",
        (branch_id,),
    )
    conv_raw = {r["name"]: r for r in ing_rows}
    inv_names = _sellable_inventory_names(branch_id)

    variants_by_canon = {}
    for v, c in canon.items():
        variants_by_canon.setdefault(c, []).append(v)

    def conv_for(cname):
        """Conversion info for a canonical (base_unit/purchase_unit/base_per_purchase),
        preferring its own row, else a variant's row."""
        if cname in conv_raw:
            return conv_raw[cname]
        for v in variants_by_canon.get(cname, []):
            if v in conv_raw:
                return conv_raw[v]
        return {}

    names = set(by_canon.keys()) | set(canon.values())
    for n in inv_names:
        names.add(canon.get(n, n))
    for n in conv_raw:
        names.add(canon.get(n, n))

    global_basis = _global_cost_basis()
    fifo_layers_map = _canonical_fifo_layers(branch_id, canon)
    ledger_map = _ledger_fifo_layers(branch_id, canon)
    consumption, recipe_referenced = _fifo_consumption(branch_id, canon)
    items = []
    for name in sorted(names):
        cr = conv_for(name)
        own = conv_raw.get(name) or {}
        s = by_canon.get(name) or {}
        explicit_kind = (str(own.get("kind") or "").strip() or None)
        is_sellable = name in inv_names or any(v in inv_names for v in variants_by_canon.get(name, []))
        kind = classify_item_kind(explicit_kind, is_sellable)
        basis = normalize_cost_basis(own.get("cost_basis")) or global_basis
        has_ledger = name in ledger_map
        f = fifo_consume(
            ledger_map[name] if has_ledger else fifo_layers_map.get(name, []),
            Decimal("0") if has_ledger else consumption.get(name, Decimal("0")),
        )
        fifo_unit = f["unit_cost"]
        unit_cost = select_unit_cost(s.get("weighted_avg"), s.get("last_purchase"), basis, fifo_unit)
        bpp = cr.get("base_per_purchase") if cr.get("base_per_purchase") is not None else None
        disp = ingredient_display(unit_cost, cr.get("purchase_unit"), cr.get("base_unit"), bpp)
        cpb = cost_per_base_unit(unit_cost, bpp) if (unit_cost is not None and bpp is not None and _d(bpp) > 0) else None
        items.append({
            "id": own.get("id"),
            "name": name,
            "kind": kind,
            "explicit_kind": explicit_kind,
            "is_sellable": is_sellable,
            "base_unit": cr.get("base_unit") or "g",
            "purchase_unit": cr.get("purchase_unit"),
            "base_per_purchase": _num(bpp if bpp is not None else Decimal("1")),
            "has_conversion": disp["has_conversion"],
            "unit_label": disp["unit_label"],
            "cost_basis": disp["cost_basis"],
            "display_cost": _num(disp["display_cost"]),
            "weighted_avg_cost": _num(s.get("weighted_avg")),
            "last_purchase_price": _num(s.get("last_purchase")),
            "last_purchase_date": str(s.get("last_in")) if s.get("last_in") else None,
            "unit_cost": _num(unit_cost),
            "effective_cost_basis": basis,
            "explicit_cost_basis": normalize_cost_basis(own.get("cost_basis")),
            "cost_per_base_unit": _num(cpb),
            "last_purchase": str(s.get("last_in")) if s.get("last_in") else None,
            "total_qty": _num(s.get("total_qty")),
            "fifo_unit_cost": _num(fifo_unit),
            "fifo_layer_cost": _num(f["fifo_layer_cost"]),
            "fifo_layer_date": f["fifo_layer_date"],
            "fifo_remaining": _num(f["fifo_remaining"]),
            "on_hand_est": _num(f["on_hand_est"]),
            "fifo_consumed": _num(f["consumed"]),
            "fifo_out_of_stock": f["out_of_stock"],
            "fifo_est": name not in recipe_referenced,
            "fifo_no_purchase": not fifo_layers_map.get(name),
            "fifo_ledger": has_ledger,
            "variants": sorted(variants_by_canon.get(name, [])),
        })
    return items, conv_raw


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
    costs, _conv, canon = _ingredient_costs(branch)
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
        unit_cost_basis = None
        last_price = None
        last_date = None
        cost_source = "recipe" if etype == "dish" else "purchase"

        if etype == "dish":
            status = "no_recipe" if not has_recipe else "unknown"
            if has_recipe:
                unit_costs = {c_name: (costs.get(canon.get(c_name, c_name)) or {}).get("cost_per_base_unit") for c_name in
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
            # resale — purchase unit cost (no base-unit conversion), honouring the
            # chosen cost basis (last purchase by default, weighted average optional).
            cname = canon.get(name, name)
            ci = costs.get(cname) or {}
            unit_cost = ci.get("unit_cost")
            no_purchase_yet = unit_cost is None
            unit_cost_basis = ci.get("cost_basis")
            last_price = ci.get("last_purchase")
            last_date = ci.get("last_in")
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
            "unit_cost_basis": unit_cost_basis if etype != "dish" else None,
            "last_purchase_price": _num(last_price) if etype != "dish" else None,
            "last_purchase_date": str(last_date) if (etype != "dish" and last_date) else None,
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


@router.get("/register")
async def menu_register_list(request: Request, user: dict = Depends(require_admin_user)):
    """ADMIN-only: Menu Register — every item with a unified type + pricing.

    Returns dish / resale / ingredient items with selling price, current cost,
    food-cost % and warnings (missing_price / price_equals_cost).  This is the
    screen where selling prices are SET (owner 2026-09-28: Stock-In no longer
    creates items, so prices are set here instead).
    """
    _ensure_tables()
    branch = _branch(request)
    inv = _inventory_map(branch)
    costs, _conv, canon = _ingredient_costs(branch)
    recipes = _recipes_by_menu(branch)
    stock_items, _ = _stock_items(branch)
    ing_names = {it["name"] for it in stock_items if it["kind"] == "ingredient"}

    items = []
    seen = set()

    def _resale_cost(name):
        return costs.get(canon.get(name, name)) or {}

    for name, iv in inv.items():
        has_recipe = bool(recipes.get(name))
        stored_type = (iv.get("item_type") or "resale").strip().lower() or "resale"
        mtype = "ingredient" if name in ing_names else classify_item_type(stored_type, has_recipe)
        selling = _d(iv.get("unit_price"))
        unit_cost = None
        portion_cost = None
        metrics = None
        status = "unknown"
        no_purchase_yet = False
        cost_basis = None
        last_price = None
        last_date = None
        if mtype == "dish":
            rec = recipes.get(name) or []
            if has_recipe:
                unit_costs = {c_name: (costs.get(canon.get(c_name, c_name)) or {}).get("cost_per_base_unit")
                              for c_name in {r["ingredient"] for r in rec}}
                portion_cost, missing = portion_cost_from(
                    [(r["qty_per_portion"], r["waste_pct"], r["ingredient"]) for r in rec], unit_costs)
                if portion_cost is not None:
                    metrics = food_cost_metrics(portion_cost, selling)
                    status = food_cost_status(metrics["food_cost_pct"]) if metrics else "unknown"
                else:
                    status = "unknown"
            else:
                status = "no_recipe"
            unit_cost = portion_cost
        elif mtype == "ingredient":
            ci = _resale_cost(name)
            unit_cost = ci.get("unit_cost")
            last_price = ci.get("last_purchase")
            last_date = ci.get("last_in")
            no_purchase_yet = unit_cost is None
            status = "ingredient"
        else:  # resale
            ci = _resale_cost(name)
            unit_cost = ci.get("unit_cost")
            cost_basis = ci.get("cost_basis")
            last_price = ci.get("last_purchase")
            last_date = ci.get("last_in")
            no_purchase_yet = unit_cost is None
            if unit_cost is not None:
                metrics = food_cost_metrics(unit_cost, selling)
                status = food_cost_status(metrics["food_cost_pct"]) if metrics else "unknown"
            else:
                status = "no_purchase"

        warnings = []
        if mtype in ("dish", "resale"):
            if selling is None or selling <= 0:
                warnings.append("missing_price")
            elif unit_cost is not None and abs(selling - unit_cost) < Decimal("0.005"):
                warnings.append("price_equals_cost")
        items.append({
            "item_name": name,
            "type": mtype,
            "stored_item_type": stored_type,
            "category": iv.get("category"),
            "selling_price": _num(selling),
            "unit_cost": _num(unit_cost),
            "portion_cost": _num(portion_cost),
            "food_cost_pct": _num(metrics["food_cost_pct"]) if metrics else None,
            "margin": _num(metrics["margin"]) if metrics else None,
            "margin_pct": _num(metrics["margin_pct"]) if metrics else None,
            "status": status,
            "no_purchase_yet": no_purchase_yet,
            "cost_basis": cost_basis,
            "last_purchase_price": _num(last_price),
            "last_purchase_date": str(last_date) if last_date else None,
            "warnings": warnings,
            "has_recipe": has_recipe,
        })
        seen.add(name)

    # Ingredients that are NOT inventory rows (raw stock-only items).
    for it in stock_items:
        name = it["name"]
        if name in seen or it["kind"] != "ingredient":
            continue
        unit_cost = it.get("unit_cost")
        items.append({
            "item_name": name,
            "type": "ingredient",
            "stored_item_type": None,
            "category": None,
            "selling_price": None,
            "unit_cost": _num(unit_cost),
            "portion_cost": None,
            "food_cost_pct": None,
            "margin": None,
            "margin_pct": None,
            "status": "ingredient",
            "no_purchase_yet": unit_cost is None,
            "cost_basis": it.get("effective_cost_basis"),
            "last_purchase_price": _num(it.get("last_purchase_price")),
            "last_purchase_date": str(it.get("last_purchase")) if it.get("last_purchase") else None,
            "warnings": [],
            "has_recipe": False,
        })
        seen.add(name)

    order = {"dish": 0, "resale": 1, "ingredient": 2}
    items.sort(key=lambda i: (order.get(i["type"], 9), i["item_name"]))
    return {"success": True, "data": items}


@router.post("/register")
async def create_register_item(body: dict = Body(...), request: Request = None,
                               user: dict = Depends(require_admin_user)):
    """ADMIN-only: create a menu item with a unified type + selling-price rules.

    type 'dish'/'resale' -> inventory row (sellable), selling price REQUIRED.
    type 'ingredient'    -> ingredients row (kind=ingredient), NO selling price.
    """
    _ensure_tables()
    branch = _branch(request)
    name = normalize_item_name(body.get("item_name", body.get("name")))
    if not name:
        raise HTTPException(status_code=422, detail={"success": False, "error": "item_name is required", "code": 422})
    mtype = normalize_menu_type(body.get("type", body.get("item_type")))
    price_raw = body.get("unit_price", body.get("selling_price"))
    price = parse_selling_price(price_raw) if price_raw is not None else None
    err = validate_menu_type_price(mtype, price)
    if err:
        raise HTTPException(status_code=422, detail={"success": False, "error": err, "code": 422})
    category = str(body.get("category") or "").strip() or "Food"

    if mtype == "ingredient":
        # must not collide with an existing sellable inventory row (reclassify instead)
        clash = _q1("SELECT id FROM inventory WHERE item_name=%s", (name,))
        if clash:
            raise HTTPException(status_code=409, detail={"success": False, "error": f"'{name}' already exists as a menu item — switch its type instead", "code": 409})
        existing = _q1("SELECT id FROM ingredients WHERE branch_id=%s AND name=%s", (branch, name))
        if existing:
            _ex("UPDATE ingredients SET kind='ingredient' WHERE id=%s", (existing["id"],))
            iid = existing["id"]
        else:
            iid = _ex("INSERT INTO ingredients (branch_id, name, kind) VALUES (%s,%s,'ingredient')", (branch, name))
        return {"success": True, "data": {"id": iid, "item_name": name, "type": "ingredient", "category": None, "selling_price": None}}

    # dish / resale -> inventory row
    existing = _q1("SELECT id FROM inventory WHERE item_name=%s", (name,))
    if existing:
        raise HTTPException(status_code=409, detail={"success": False, "error": f"'{name}' already exists in the menu", "code": 409})
    new_id = _ex(
        "INSERT INTO inventory (branch_id, item_name, category, quantity, unit_price, reorder_level, item_type) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (branch, name, category, 0, price, 0, mtype),
    )
    ing = _q1("SELECT id FROM ingredients WHERE branch_id=%s AND name=%s", (branch, name))
    if ing:
        _ex("UPDATE ingredients SET kind='product' WHERE id=%s", (ing["id"],))
    return {"success": True, "data": {"id": new_id, "item_name": name, "type": mtype, "category": category, "selling_price": _num(price)}}


@router.patch("/register")
async def update_register_item(body: dict = Body(...), request: Request = None,
                               user: dict = Depends(require_admin_user)):
    """ADMIN-only: edit a menu item — rename / category / type / selling price.

    ``item_name`` identifies the CURRENT item.  ``type`` may switch between
    dish / resale / ingredient.  Selling-price rules are enforced per type:
      * dish/resale -> selling_price > 0 required;
      * ingredient  -> no selling price.
    """
    _ensure_tables()
    branch = _branch(request)
    current = normalize_item_name(body.get("item_name"))
    if not current:
        raise HTTPException(status_code=422, detail={"success": False, "error": "item_name (current name) is required", "code": 422})

    inv = _q1("SELECT id, item_name, category, unit_price, item_type FROM inventory WHERE item_name=%s", (current,))
    ing = _q1("SELECT id, name, kind FROM ingredients WHERE branch_id=%s AND name=%s", (branch, current))
    if not inv and not ing:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"'{current}' not found", "code": 404})

    new_name = normalize_item_name(body.get("new_name")) if body.get("new_name") is not None else None
    category = str(body.get("category")).strip() if body.get("category") is not None else None

    has_type = ("type" in body) or ("item_type" in body)
    new_type = normalize_menu_type(body.get("type", body.get("item_type"))) if has_type else None

    has_price = ("unit_price" in body) or ("selling_price" in body)
    new_price = parse_selling_price(body.get("unit_price", body.get("selling_price"))) if has_price else None

    recipes = _recipes_by_menu(branch)
    stored_type = (inv.get("item_type") or "resale") if inv else "resale"
    has_recipe = bool(recipes.get(current))
    explicit_kind = ing.get("kind") if ing else None
    cur_type = classify_menu_type(stored_type, has_recipe, explicit_kind)

    target_type = new_type or cur_type
    if target_type is None:
        raise HTTPException(status_code=422, detail={"success": False, "error": "type must be 'dish', 'resale' or 'ingredient'", "code": 422})

    # Validate price against the TARGET type (an existing price is ignored when
    # switching TO ingredient; it is required when the target is dish/resale).
    if target_type == "ingredient":
        if new_price is not None and new_price > 0:
            raise HTTPException(status_code=422, detail={"success": False, "error": "ingredient items must not have a selling price", "code": 422})
        validate_price = None
    else:
        validate_price = new_price if new_price is not None else (parse_selling_price(inv.get("unit_price")) if inv else None)
        err = validate_menu_type_price(target_type, validate_price)
        if err:
            raise HTTPException(status_code=422, detail={"success": False, "error": err, "code": 422})

    final_name = new_name or current
    if final_name.lower() != current.lower():
        clash = _q1("SELECT id FROM inventory WHERE item_name=%s", (final_name,))
        clash2 = _q1("SELECT id FROM ingredients WHERE branch_id=%s AND name=%s", (branch, final_name))
        if clash or clash2:
            raise HTTPException(status_code=409, detail={"success": False, "error": f"'{final_name}' already exists", "code": 409})

    final_selling = None
    if target_type == "ingredient":
        if ing:
            _ex("UPDATE ingredients SET name=%s, kind='ingredient' WHERE id=%s", (final_name, ing["id"]))
        else:
            _ex("INSERT INTO ingredients (branch_id, name, kind) VALUES (%s,%s,'ingredient')", (branch, final_name))
        if inv:
            _ex("UPDATE inventory SET item_name=%s WHERE id=%s", (final_name, inv["id"]))
    else:
        final_cat = category if category is not None else ((inv.get("category") if inv else None) or "Food")
        if inv:
            _ex("UPDATE inventory SET item_name=%s, category=%s, unit_price=%s, item_type=%s WHERE id=%s",
                (final_name, final_cat, validate_price, target_type, inv["id"]))
        else:
            _ex("INSERT INTO inventory (branch_id, item_name, category, quantity, unit_price, reorder_level, item_type) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (branch, final_name, final_cat, 0, validate_price, 0, target_type))
        if ing:
            _ex("UPDATE ingredients SET kind='product' WHERE id=%s", (ing["id"],))
        final_selling = validate_price

    return {"success": True, "data": {"item_name": final_name, "type": target_type, "category": category if category is not None else (inv.get("category") if inv else None), "selling_price": _num(final_selling)}}


@router.get("/recipe")
async def menu_recipe(item: str = Query(...), request: Request = None,
                      user: dict = Depends(require_admin_user)):
    """One menu item's recipe rows with per-row qty, unit cost, waste, line cost + totals."""
    _ensure_tables()
    branch = _branch(request)
    costs, _conv, canon = _ingredient_costs(branch)
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
        ci = costs.get(canon.get(r["ingredient"], r["ingredient"])) or {}
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
            "cost_basis": ci.get("cost_basis"),
            "last_purchase_price": _num(ci.get("last_purchase")),
            "last_purchase_date": str(ci.get("last_in")) if ci.get("last_in") else None,
            "fifo_unit_cost": _num(ci.get("fifo_unit_cost")),
            "fifo_layer_date": ci.get("fifo_layer_date"),
            "fifo_remaining": _num(ci.get("fifo_remaining")),
            "on_hand_est": _num(ci.get("on_hand_est")),
            "fifo_est": ci.get("fifo_est"),
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
async def menu_ingredients(request: Request, kind: str = Query("all"),
                           user: dict = Depends(require_admin_user)):
    """Stock-item catalog with kind split + unit-cost display (owner 2026-09-28).

    ``kind`` filters the result: ingredient | product | all (default all).
    The recipe picker calls ``?kind=ingredient`` so packaged products are NEVER
    offered as recipe ingredients.

    Response data:
      * items      — full catalog filtered by ``kind``, each entry carries an
                     effective ``kind``, ``is_sellable``, ``unit_label`` +
                     ``display_cost`` (real purchase unit or per-base-unit when a
                     conversion is configured).
      * ingredients — explicit ``ingredients`` master rows (kind-aware).
      * suggested   — purchased stock_in items with no conversion row yet
                      (backward-compatible with the self-serve dish probe).
    """
    _ensure_tables()
    branch = _branch(request)
    items, _conv = _stock_items(branch)
    filtered = filter_items_by_kind(items, kind)
    master = [i for i in items if i.get("id") is not None]
    suggested = [i for i in items if i.get("id") is None and i.get("weighted_avg_cost") is not None]

    return {
        "success": True,
        "data": {
            "kind": normalize_kind(kind) or "all",
            "items": filtered,
            "ingredients": master,
            "suggested": suggested,
            "known_units": [
                {"unit": k, "base_per_purchase": float(v)}
                for k, v in sorted(BASE_UNIT_CONVERSIONS.items(), key=lambda kv: str(kv[0]))
                if k
            ],
        },
    }


@router.post("/ingredients/kind")
async def set_ingredient_kind(body: dict = Body(...), request: Request = None,
                              user: dict = Depends(require_admin_user)):
    """ADMIN-only: reclassify a stock item as ingredient vs product (persisted).

    body: {name, kind} where kind is 'ingredient' | 'product' (aliases accepted).
    Idempotent — creates a bare ``ingredients`` row (default conversion) when the
    item has no row yet, otherwise only flips ``kind`` (never clobbers a saved
    conversion)."""
    _ensure_tables()
    branch = _branch(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail={"success": False, "error": "name is required", "code": 422})
    k = normalize_kind(body.get("kind"))
    if not k:
        raise HTTPException(status_code=422, detail={"success": False, "error": "kind must be 'ingredient' or 'product'", "code": 422})

    existing = _q1("SELECT id FROM ingredients WHERE branch_id=%s AND name=%s", (branch, name))
    if existing:
        _ex("UPDATE ingredients SET kind=%s WHERE id=%s", (k, existing["id"]))
        iid = existing["id"]
    else:
        iid = _ex(
            "INSERT INTO ingredients (branch_id, name, kind) VALUES (%s,%s,%s)",
            (branch, name, k),
        )
    return {"success": True, "data": {"id": iid, "name": name, "kind": k}}


@router.post("/ingredients")
async def upsert_ingredient(body: dict = Body(...), request: Request = None,
                            user: dict = Depends(require_admin_user)):
    """Upsert {name, base_unit, purchase_unit, base_per_purchase, kind?}.

    When base_per_purchase is omitted/0, it is auto-filled from the known
    purchase-unit conversion table (ပိဿာ=1632, ကျပ်သား=10, kg=1000, litre=1000),
    else defaults to 1 (cost per purchased unit).  ``kind`` is optional; a new
    row without an explicit kind is auto-classified (sellable -> product, else
    ingredient), and an existing row keeps its kind unless one is supplied."""
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

    kind = normalize_kind(body.get("kind"))
    has_basis = "cost_basis" in body
    cost_basis = normalize_cost_basis(body.get("cost_basis")) if has_basis else None

    existing = _q1("SELECT id, kind FROM ingredients WHERE branch_id=%s AND name=%s", (branch, name))
    if existing:
        final_kind = kind or (str(existing.get("kind") or "").strip() or "ingredient")
        if has_basis:
            _ex("UPDATE ingredients SET base_unit=%s, purchase_unit=%s, base_per_purchase=%s, kind=%s, cost_basis=%s WHERE id=%s",
                (base_unit, purchase_unit, bpp, final_kind, cost_basis, existing["id"]))
        else:
            _ex("UPDATE ingredients SET base_unit=%s, purchase_unit=%s, base_per_purchase=%s, kind=%s WHERE id=%s",
                (base_unit, purchase_unit, bpp, final_kind, existing["id"]))
        iid = existing["id"]
    else:
        if kind is None:
            # default heuristic: sellable inventory item -> product, else ingredient
            kind = classify_item_kind(None, name in _sellable_inventory_names(branch))
        final_kind = kind
        iid = _ex(
            "INSERT INTO ingredients (branch_id, name, kind, base_unit, purchase_unit, base_per_purchase, cost_basis) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (branch, name, kind, base_unit, purchase_unit, bpp, cost_basis),
        )
    return {"success": True, "data": {"id": iid, "name": name, "kind": final_kind, "base_unit": base_unit, "purchase_unit": purchase_unit, "base_per_purchase": _num(bpp), "cost_basis": cost_basis}}


@router.get("/cost-basis")
async def get_cost_basis(request: Request, user: dict = Depends(require_admin_user)):
    """Read the effective cost basis: global setting + per-ingredient overrides."""
    _ensure_tables()
    branch = _branch(request)
    global_basis = _global_cost_basis()
    overrides = {}
    for r in _q(
        "SELECT name, cost_basis FROM ingredients WHERE branch_id=%s AND cost_basis IS NOT NULL AND cost_basis <> ''",
        (branch,),
    ):
        b = normalize_cost_basis(r["cost_basis"])
        if b:
            overrides[r["name"]] = b
    return {"success": True, "data": {"global": global_basis, "overrides": overrides}}


@router.post("/cost-basis")
async def set_cost_basis(body: dict = Body(...), request: Request = None,
                         user: dict = Depends(require_admin_user)):
    """ADMIN-only: set the cost basis.

    * global: {basis: "fifo"|"last"|"average"} — persisted in settings_config
      (key ``ingredient_cost_basis``).
    * per-ingredient: {name, basis} — persisted on ``ingredients.cost_basis``
      (NULL = follow the global setting).
    """
    _ensure_tables()
    branch = _branch(request)
    name = str(body.get("name") or "").strip() if body.get("name") is not None else ""
    basis = normalize_cost_basis(body.get("basis"))
    if not basis:
        raise HTTPException(status_code=422, detail={"success": False, "error": "basis must be 'fifo', 'last' or 'average'", "code": 422})

    if name:
        existing = _q1("SELECT id FROM ingredients WHERE branch_id=%s AND name=%s", (branch, name))
        if existing:
            _ex("UPDATE ingredients SET cost_basis=%s WHERE id=%s", (basis, existing["id"]))
        else:
            _ex("INSERT INTO ingredients (branch_id, name, cost_basis) VALUES (%s,%s,%s)", (branch, name, basis))
        return {"success": True, "data": {"name": name, "basis": basis}}

    existing = _q1("SELECT id FROM settings_config WHERE config_key=%s", (_COST_BASIS_KEY,))
    if existing:
        _ex("UPDATE settings_config SET config_value=%s, config_type='string' WHERE config_key=%s", (basis, _COST_BASIS_KEY))
    else:
        _ex(
            "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
            "VALUES (%s,%s,'string','menu','Ingredient cost basis: fifo | last | average')",
            (_COST_BASIS_KEY, basis),
        )
    return {"success": True, "data": {"global": basis}}


@router.get("/ingredient-variants")
async def list_ingredient_variants(request: Request, user: dict = Depends(require_admin_user)):
    """List current name-variant merges (canonical -> [variant, …])."""
    _ensure_tables()
    branch = _branch(request)
    rows = _q(
        "SELECT canonical_name, variant_name, created_at FROM ingredient_variants "
        "WHERE branch_id=%s ORDER BY canonical_name, variant_name",
        (branch,),
    )
    mappings = {}
    for r in rows:
        mappings.setdefault(r["canonical_name"], []).append({
            "variant_name": r["variant_name"],
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
        })
    return {"success": True, "data": {"mappings": mappings}}


@router.get("/ingredient-variants/candidates")
async def variant_candidates(request: Request, user: dict = Depends(require_admin_user)):
    """Auto-detect duplicate name variants (normalisation: ZWSP/space/punct/case +
    Burmese ဥ/ဉ) and suggest a canonical per group (sellable > most purchased)."""
    _ensure_tables()
    branch = _branch(request)
    names = sorted(_all_item_names(branch))
    raw_agg = aggregate_canonical_purchases(_stockin_rows(branch), {})
    sellable = _sellable_inventory_names(branch)
    canon = _variant_canonical(branch)

    groups = []
    for g in duplicate_variant_groups(names):
        # Skip groups already fully resolved: every member maps to the SAME
        # canonical via an existing merge (so we don't re-suggest the egg merge
        # forever after it has been merged).
        resolved = {canon.get(n, n) for n in g}
        if len(resolved) == 1:
            continue
        def qty(n):
            return float(raw_agg.get(n, {}).get("total_qty") or 0)
        suggested = None
        for n in g:
            if n in sellable:
                suggested = n
                break
        if suggested is None:
            suggested = sorted(g, key=lambda n: (-qty(n), len(str(n)), str(n)))[0]
        members = [{
            "name": n,
            "weighted_avg": _num(raw_agg.get(n, {}).get("weighted_avg")),
            "last_purchase": _num(raw_agg.get(n, {}).get("last_purchase")),
            "last_in": str(raw_agg.get(n, {}).get("last_in")) if raw_agg.get(n, {}).get("last_in") else None,
            "total_qty": _num(raw_agg.get(n, {}).get("total_qty")),
            "is_sellable": n in sellable,
        } for n in g]
        groups.append({"members": g, "suggested_canonical": suggested, "items": members})

    mappings = {c: sorted(v for v in canon if canon[v] == c) for c in sorted(set(canon.values()))}
    return {"success": True, "data": {"groups": groups, "mappings": mappings}}


@router.post("/ingredient-variants/merge")
async def merge_ingredient_variants(body: dict = Body(...), request: Request = None,
                                    user: dict = Depends(require_admin_user)):
    """ADMIN-only: merge variant names into ONE canonical ingredient (non-destructive).

    body: {canonical_name, variant_names: [...]}.  Creates / re-points
    ``ingredient_variants`` mapping rows only — stock_in rows are NEVER touched,
    so the merge is fully reversible via /ingredient-variants/unmerge.
    """
    _ensure_tables()
    branch = _branch(request)
    canonical = str(body.get("canonical_name") or "").strip()
    if not canonical:
        raise HTTPException(status_code=422, detail={"success": False, "error": "canonical_name is required", "code": 422})
    raw = body.get("variant_names", body.get("variants", []))
    if isinstance(raw, str):
        raw = [raw]
    variants = []
    for v in raw:
        v = str(v).strip()
        if v and v != canonical and v not in variants:
            variants.append(v)
    if not variants:
        raise HTTPException(status_code=422, detail={"success": False, "error": "variant_names must list at least one name different from canonical_name", "code": 422})

    created, updated, skipped = [], [], []
    for v in variants:
        existing = _q1("SELECT canonical_name FROM ingredient_variants WHERE branch_id=%s AND variant_name=%s", (branch, v))
        if existing:
            if existing["canonical_name"] == canonical:
                skipped.append(v)
            else:
                _ex("UPDATE ingredient_variants SET canonical_name=%s WHERE branch_id=%s AND variant_name=%s", (canonical, branch, v))
                updated.append(v)
        else:
            _ex("INSERT INTO ingredient_variants (branch_id, canonical_name, variant_name) VALUES (%s,%s,%s)", (branch, canonical, v))
            created.append(v)
    return {"success": True, "data": {
        "canonical_name": canonical, "variant_names": variants,
        "created": created, "updated": updated, "skipped": skipped,
    }}


@router.post("/ingredient-variants/unmerge")
async def unmerge_ingredient_variant(body: dict = Body(...), request: Request = None,
                                     user: dict = Depends(require_admin_user)):
    """ADMIN-only: undo a single variant mapping (reversible).  No stock_in rows touched."""
    _ensure_tables()
    branch = _branch(request)
    variant = str(body.get("variant_name") or "").strip()
    if not variant:
        raise HTTPException(status_code=422, detail={"success": False, "error": "variant_name is required", "code": 422})
    affected = _del("DELETE FROM ingredient_variants WHERE branch_id=%s AND variant_name=%s", (branch, variant))
    return {"success": True, "data": {"variant_name": variant, "removed": int(affected)}}


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

    costs, _conv, canon = _ingredient_costs(branch)
    recipes = _recipes_by_menu(branch)
    inv = _inventory_map(branch)

    rows = _q(
        "SELECT item_name, SUM(quantity) AS qty, SUM(total) AS revenue FROM stock_out "
        "WHERE branch_id=%s AND sale_date >= %s AND sale_date < %s GROUP BY item_name ORDER BY revenue DESC",
        (branch, start, end),
    )

    # Actual FIFO COGS recorded on dish sales (ingredient_consumption).  Timestamp is
    # server time (UTC) vs stock_out.sale_date (MMT) — approximate at month edges.
    actual_cogs_map = {}
    total_actual_cogs = Decimal("0")
    try:
        cogs_rows = _q(
            "SELECT dish_item, SUM(cost) AS cogs FROM ingredient_consumption "
            "WHERE branch_id=%s AND created_at >= %s AND created_at < %s GROUP BY dish_item",
            (branch, start, end),
        )
        for cr in cogs_rows:
            actual_cogs_map[cr["dish_item"]] = _d(cr["cogs"])
            total_actual_cogs += _d(cr["cogs"])
    except Exception:
        pass  # ingredient_consumption may not exist yet

    # 💰 FIFO cost per item — SAME basis as the P&L Food COGS (owner Osmo 2026-10-14:
    # "FIFO နဲ့ပဲ တွက်ပါ"): cum(month) − cum(prev month) of stock_fifo.calc_fifo.
    fifo_map = {}
    try:
        import os as _os, pymysql as _pmysql, stock_fifo as _sf
        _pw = _os.environ.get("MYSQL_ROOT_PASSWORD", "")
        def _fc(ym_):
            _cn = _pmysql.connect(host="127.0.0.1", user="root", password=_pw, database="psvibe_api")
            try:
                return _sf.calc_fifo(_cn, ym_, cumulative=True)
            finally:
                _cn.close()
        _cur = _fc(month)
        _y2, _m2 = int(month[:4]), int(month[5:7])
        _prev_ym = f"{_y2-1:04d}-12" if _m2 == 1 else f"{_y2:04d}-{_m2-1:02d}"
        _prv = _fc(_prev_ym)
        _pmap = {str(d.get("item") or "").strip(): float(d.get("cogs") or 0) for d in _prv.get("details", [])}
        for d in _cur.get("details", []):
            _nm = str(d.get("item") or "").strip()
            _v = float(d.get("cogs") or 0) - _pmap.get(_nm, 0.0)
            if abs(_v) > 0.5:
                fifo_map[_nm] = _v
    except Exception as _fe:
        logger.warning("food-cost-report FIFO calc failed: %s", _fe)

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
                unit_costs = {c_name: (costs.get(canon.get(c_name, c_name)) or {}).get("cost_per_base_unit") for c_name in {x["ingredient"] for x in rec}}
                portion_cost, missing = portion_cost_from(
                    [(x["qty_per_portion"], x["waste_pct"], x["ingredient"]) for x in rec], unit_costs)
                if portion_cost is None:
                    uncosted_reason = "missing_ingredients"
            unit_cost = portion_cost
        else:
            # resale — purchase unit cost (no base-unit conversion), basis-aware
            unit_cost = (costs.get(canon.get(name, name)) or {}).get("unit_cost")
            if unit_cost is None:
                no_purchase = True
                uncosted_reason = "no_purchase"
            else:
                portion_cost = unit_cost

        theoretical = portion_cost * qty if portion_cost is not None else None
        # Cost used for the report = FIFO (matches the P&L); fall back to the
        # recipe/current-price value when FIFO has no layer for this item.
        fifo_cost = fifo_map.get(name)
        if fifo_cost is None:
            # Dish: its ingredients ARE stocked and consumed FIFO, but the cost
            # lives in `ingredient_consumption` (not as a stock layer of the dish
            # itself). This IS the FIFO cost of the dish.
            _ic = actual_cogs_map.get(name)
            if _ic is not None:
                fifo_cost = float(_ic)
        if fifo_cost is not None:
            cost_used = Decimal(str(fifo_cost))
            cost_source = "fifo"
        else:
            cost_used = None
            if uncosted_reason is None:
                uncosted_reason = "no_fifo_layer"
        if cost_used is not None:
            total_theoretical += cost_used
            split[etype]["theoretical_cost"] += cost_used
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
            "theoretical_cost": _num(cost_used),
            "recipe_cost": _num(theoretical),
            "fifo_cost": _num(fifo_cost),
            "missing_ingredients": missing,
            "no_purchase_yet": no_purchase,
            "actual_cogs": _num(actual_cogs_map.get(name)),
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
            "actual_cogs": _num(total_actual_cogs),
            "food_cost_pct": _num(food_cost_pct),
            "status": food_cost_status(food_cost_pct),
            "gross_margin": _num(total_revenue - total_theoretical),
            "split": {
                "dish": {
                    "revenue": _num(split["dish"]["revenue"]),
                    "theoretical_cost": _num(split["dish"]["theoretical_cost"]),
                    "food_cost_pct": _split_pct(split["dish"]["revenue"], split["dish"]["theoretical_cost"]),
                    "gross_margin": _num(split["dish"]["revenue"] - split["dish"]["theoretical_cost"]),
                    "margin_pct": _num(((split["dish"]["revenue"] - split["dish"]["theoretical_cost"]) / split["dish"]["revenue"] * Decimal("100")).quantize(Decimal("0.1"))) if split["dish"]["revenue"] > 0 else None,
                    "items": split["dish"]["items"],
                },
                "resale": {
                    "revenue": _num(split["resale"]["revenue"]),
                    "theoretical_cost": _num(split["resale"]["theoretical_cost"]),
                    "food_cost_pct": _split_pct(split["resale"]["revenue"], split["resale"]["theoretical_cost"]),
                    "gross_margin": _num(split["resale"]["revenue"] - split["resale"]["theoretical_cost"]),
                    "margin_pct": _num(((split["resale"]["revenue"] - split["resale"]["theoretical_cost"]) / split["resale"]["revenue"] * Decimal("100")).quantize(Decimal("0.1"))) if split["resale"]["revenue"] > 0 else None,
                    "items": split["resale"]["items"],
                },
            },
            "breakdown": breakdown,
            "uncosted_items": uncosted_items,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE — owner 2026-09-29 (Osmo: "Delete btn ကော ထည့်ပေးပါ မှားသွားလို့",
#  round 2: "Ingredients က နေ ပြန်ဖျက်မရဘူး ဖြစ်နေတယ် ကြက်ဥကို")
#
#  Rows created by mistake must be removable — but a row that already carries money
#  history (purchases, FIFO layers, SALES) is never silently destroyed: the API answers
#  409 with a machine-readable `reason` (+ counts) and a suggested next step, and the
#  Staff App turns that into a plain-language prompt ("move it to Products", "delete the
#  purchase history too", ...).  Sold products stay protected.
# ─────────────────────────────────────────────────────────────────────────────

_HISTORY_TABLES = ("stock_in", "stock_layers", "daily_stock_check", "food_cart", "stock_hold")


def _ref_count(table: str, branch: int, name: str) -> int:
    """Rows in ``table`` for this branch/item_name; 0 when the table does not exist."""
    try:
        row = _q1("SELECT COUNT(*) AS c FROM " + table + " WHERE branch_id=%s AND item_name=%s",
                  (branch, name))
        return int((row or {}).get("c") or 0)
    except Exception:
        return 0


@router.delete("/item")
async def delete_menu_item(item_name: str = Query(None), id: int = Query(None), purge: int = Query(0),
                           request: Request = None,
                           user: dict = Depends(require_admin_user)):
    """ADMIN-only: delete a sellable menu item (an ``inventory`` row) created by mistake.

    * sold items (any ``stock_out`` / sales row) → 409 ``reason=has_sales`` (never deleted:
      past sales reports would change).
    * stock history (stock-in / layers / cart / hold / consumption) → 409
      ``reason=has_history``; re-send with ``purge=1`` to remove it together with the item.
    """
    _ensure_tables()
    branch = _branch(request)
    name = normalize_item_name(item_name) if item_name else None
    if id is not None:
        row = _q1("SELECT id, item_name FROM inventory WHERE id=%s AND branch_id=%s", (id, branch))
    elif name:
        row = _q1("SELECT id, item_name FROM inventory WHERE branch_id=%s AND item_name=%s", (branch, name))
    else:
        raise HTTPException(status_code=422, detail={"success": False, "error": "id or item_name is required", "code": 422})
    if not row:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Menu item not found", "code": 404})

    nm = row["item_name"]
    sales = _ref_count("stock_out", branch, nm)
    blockers = {}
    for t in _HISTORY_TABLES:
        n = _ref_count(t, branch, nm)
        if n:
            blockers[t] = n
    try:
        cons = _q1("SELECT COUNT(*) AS c FROM ingredient_consumption WHERE branch_id=%s AND dish_item=%s", (branch, nm))
        if cons and int(cons.get("c") or 0):
            blockers["ingredient_consumption"] = int(cons["c"] or 0)
    except Exception:
        pass

    if sales:
        raise HTTPException(status_code=409, detail={
            "success": False, "code": 409, "reason": "has_sales", "sales_rows": sales,
            "blockers": blockers, "suggest": "none",
            "error": "'%s' က ရောင်းရပြီးသား %d ခု ရှိတယ် — ဖျက်လို့ မရဘူး (အတိတ် ရောင်းမှတ်တမ်း/report ပျက်သွားမယ်)။" % (nm, sales),
        })
    if blockers and not int(purge or 0):
        raise HTTPException(status_code=409, detail={
            "success": False, "code": 409, "reason": "has_history", "blockers": blockers,
            "suggest": "purge_history",
            "error": "'%s' မှာ ဝယ်/stock မှတ်တမ်း (%s) ရှိတယ် — အဲ့ဒါတွေပါ ဖျက်မှ ရမယ်။" % (nm, ", ".join("%s:%d" % kv for kv in sorted(blockers.items()))),
        })

    purged = {}
    if int(purge or 0):
        for t in _HISTORY_TABLES:
            try:
                n = _del("DELETE FROM " + t + " WHERE branch_id=%s AND item_name=%s", (branch, nm))
                if n:
                    purged[t] = int(n)
            except Exception:
                pass
        try:
            n = _del("DELETE FROM ingredient_consumption WHERE branch_id=%s AND dish_item=%s", (branch, nm))
            if n:
                purged["ingredient_consumption"] = int(n)
        except Exception:
            pass

    recipes_removed = 0
    try:
        recipes_removed = _del("DELETE FROM menu_recipes WHERE branch_id=%s AND menu_item=%s", (branch, nm))
    except Exception:
        recipes_removed = 0
    try:
        _del("DELETE FROM ingredient_variants WHERE branch_id=%s AND (canonical_name=%s OR variant_name=%s)", (branch, nm, nm))
    except Exception:
        pass
    deleted = _del("DELETE FROM inventory WHERE id=%s AND branch_id=%s", (row["id"], branch))
    logger.info("menu item DELETED id=%s name=%r purged=%s recipes=%s by=%s",
                row["id"], nm, purged, recipes_removed, (user or {}).get("username"))
    return {"success": True, "data": {"deleted": int(deleted), "item_name": nm,
                                      "recipe_rows_removed": int(recipes_removed), "purged": purged}}


@router.delete("/ingredients")
async def delete_ingredient(name: str = Query(None), id: int = Query(None), force: int = Query(0),
                            purge: int = Query(0), request: Request = None,
                            user: dict = Depends(require_admin_user)):
    """ADMIN-only: delete an ingredient master row created by mistake.

    Refusals are structured so the app can offer the right next step:

    * ``reason=sellable_item`` — the same name is a sellable menu row.  Deleting the
      *ingredient mapping* is still possible once the mapping is not needed, but the app
      offers the safe alternative first: move it to 🛒 Products (``POST /ingredients/kind``).
      We refuse outright when it has sales, because the menu item owns that history.
    * ``reason=has_sales`` — already sold ⇒ never deleted.
    * ``reason=used_in_recipes`` — ``force=1`` also removes those recipe lines.
    * ``reason=has_purchases`` — ``purge=1`` also removes the stock-in / FIFO-layer rows.
    """
    _ensure_tables()
    branch = _branch(request)
    nm = normalize_item_name(name) if name else None
    if id is not None:
        row = _q1("SELECT id, name FROM ingredients WHERE id=%s AND branch_id=%s", (id, branch))
    elif nm:
        row = _q1("SELECT id, name FROM ingredients WHERE branch_id=%s AND name=%s", (branch, nm))
    else:
        raise HTTPException(status_code=422, detail={"success": False, "error": "id or name is required", "code": 422})
    if not row:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Ingredient not found", "code": 404})
    inm = row["name"]

    inv = _q1("SELECT id, item_name FROM inventory WHERE branch_id=%s AND item_name=%s", (branch, inm))
    sales = _ref_count("stock_out", branch, inm)
    stock_in_rows = _ref_count("stock_in", branch, inm)
    layer_rows = _ref_count("stock_layers", branch, inm)
    used = []
    try:
        rows = _q("SELECT DISTINCT menu_item FROM menu_recipes WHERE branch_id=%s AND ingredient=%s", (branch, inm)) or []
        used = [r.get("menu_item") for r in rows if r.get("menu_item")]
    except Exception:
        used = []

    # A name that is ALSO a sellable menu row: deleting the *ingredient mapping* is fine when
    # nothing has been sold yet (the item simply lives on as a 🛒 product, history untouched),
    # but a product that has already been SOLD is never deleted — offer "move to Products".
    if inv and sales:
        raise HTTPException(status_code=409, detail={
            "success": False, "code": 409, "reason": "sellable_item", "is_sellable": True,
            "sales_rows": sales, "purchase_rows": stock_in_rows, "layer_rows": layer_rows,
            "suggest": "move_to_product",
            "error": ("'%s' က ရောင်းရပြီးသား %d ခု ရှိတဲ့ ပစ္စည်း ဖြစ်နေတယ် (ဝယ်မှတ်တမ်း %d ခု) — ဖျက်လို့ မရဘူး "
                      "(အတိတ် ရောင်းမှတ်တမ်း/report ပျက်မယ်)။ 🛒 Products ထဲ ရွှေ့လို့ရတယ် — မှတ်တမ်းတွေ အကုန် ကျန်မယ်။")
                     % (inm, sales, stock_in_rows),
        })

    if sales:
        raise HTTPException(status_code=409, detail={
            "success": False, "code": 409, "reason": "has_sales", "sales_rows": sales, "suggest": "none",
            "error": "'%s' က ရောင်းရပြီးသား %d ခု ရှိတယ် — ဖျက်လို့ မရဘူး (report ပျက်မယ်)။" % (inm, sales),
        })
    if used and not int(force or 0):
        raise HTTPException(status_code=409, detail={
            "success": False, "code": 409, "reason": "used_in_recipes", "used_in": used, "suggest": "force_recipes",
            "error": "'%s' ကို recipe %d ခုမှာ သုံးထားတယ်: %s — အဲ့ဒီ recipe လိုင်းတွေပါ ဖျက်မှ ရမယ်။" % (inm, len(used), ", ".join(used)),
        })
    if (stock_in_rows or layer_rows) and not int(purge or 0):
        raise HTTPException(status_code=409, detail={
            "success": False, "code": 409, "reason": "has_purchases", "purchase_rows": stock_in_rows,
            "layer_rows": layer_rows, "suggest": "purge_purchases",
            "error": "'%s' မှာ ဝယ်မှတ်တမ်း %d ခု (FIFO layer %d) ရှိတယ် — အဲ့ဒါတွေပါ ဖျက်မှ ရမယ်။" % (inm, stock_in_rows, layer_rows),
        })

    purged = {}
    if int(purge or 0):
        for t in ("stock_layers", "stock_in"):
            try:
                n = _del("DELETE FROM " + t + " WHERE branch_id=%s AND item_name=%s", (branch, inm))
                if n:
                    purged[t] = int(n)
            except Exception:
                pass
        try:
            n = _del("DELETE FROM ingredient_consumption WHERE branch_id=%s AND ingredient=%s", (branch, inm))
            if n:
                purged["ingredient_consumption"] = int(n)
        except Exception:
            pass

    recipes_removed = 0
    if used and int(force or 0):
        recipes_removed = _del("DELETE FROM menu_recipes WHERE branch_id=%s AND ingredient=%s", (branch, inm))
    try:
        _del("DELETE FROM ingredient_variants WHERE branch_id=%s AND (canonical_name=%s OR variant_name=%s)", (branch, inm, inm))
    except Exception:
        pass
    deleted = _del("DELETE FROM ingredients WHERE id=%s AND branch_id=%s", (row["id"], branch))
    logger.info("ingredient DELETED id=%s name=%r recipes=%s purged=%s by=%s",
                row["id"], inm, recipes_removed, purged, (user or {}).get("username"))
    still_listed = bool(inv and not sales) or bool(stock_in_rows or layer_rows) and not int(purge or 0)
    return {"success": True, "data": {"deleted": int(deleted), "name": inm,
                                      "recipe_rows_removed": int(recipes_removed), "purged": purged,
                                      "moved_to_product": bool(inv), "still_listed": bool(still_listed)}}
