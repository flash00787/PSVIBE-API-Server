"""PS VIBE API — Recipe / Food-Cost pure cost engine (unit-testable, NO database).

This module holds ONLY pure functions so it can be unit-tested without touching
MySQL. All database access lives in ``routes/menu_routes.py`` which calls into
these helpers.

Cost method (owner-approved, see memory entry 6aba74ba "Food-cost method proposal"):
  * Ingredient unit cost = moving WEIGHTED AVERAGE from stock_in
        wavg = SUM(total_cost) / SUM(quantity)          (per PURCHASE unit)
    then divided by ``base_per_purchase`` to give the cost per BASE unit (g / ml).
    This is stable against latest-price volatility, unlike "last purchase price".
  * portion_cost = SUM over recipe rows of
        qty_per_portion * cost_per_base_unit * (1 + waste_pct/100)
  * food_cost_pct = portion_cost / selling_price * 100
  * margin         = selling_price - portion_cost
  * margin_pct     = margin / selling_price * 100
  * status: ok (<=35%), warn (>35%..40%), high (>40%)  — Myanmar F&B target 30-35%.

Everything uses ``decimal.Decimal`` (with ROUND_HALF_UP) so results are exact and
deterministic for tests. Money is quantized to 2 dp; unit cost per base unit to
6 dp (per-gram/ml values can be well below one kyat); percentages to 1 dp.

Rounding policy (documented choice):
  * weighted_average_cost  -> 4 dp  (cost per purchase unit)
  * cost_per_base_unit     -> 6 dp  (cost per gram/ml)
  * portion_cost_from      -> 2 dp  (kyat, per plate)
  * food_cost_metrics      -> pct 1 dp, margin 2 dp, margin_pct 1 dp
"""
from decimal import Decimal, ROUND_HALF_UP

# Quantization constants
_Q4 = Decimal("0.0001")   # per-purchase-unit cost
_Q6 = Decimal("0.000001")  # per-base-unit cost (g / ml)
_MONEY = Decimal("0.01")  # kyat
_PCT = Decimal("0.1")     # percentages

# ── Base-unit conversion table (purchase unit -> base units per 1 purchase unit) ──
# Base unit defaults to grams (solids) / millilitres (liquids).  When a purchase
# unit has no conversion row, ``base_per_purchase`` defaults to 1 (cost per
# purchased unit).  Key = normalized (lowercased, stripped) purchase-unit name.
BASE_UNIT_CONVERSIONS = {
    "g": Decimal("1"), "gram": Decimal("1"), "grams": Decimal("1"),
    "kg": Decimal("1000"), "kilogram": Decimal("1000"), "kilograms": Decimal("1000"),
    "ပိဿာ": Decimal("1632"), "viss": Decimal("1632"), "peittha": Decimal("1632"),
    "ကျပ်သား": Decimal("10"), "kyattha": Decimal("10"), "kyat tha": Decimal("10"),
    "ml": Decimal("1"), "millilitre": Decimal("1"), "milliliter": Decimal("1"),
    "l": Decimal("1000"), "litre": Decimal("1000"), "liter": Decimal("1000"),
    "pcs": Decimal("1"), "pc": Decimal("1"), "piece": Decimal("1"), "pieces": Decimal("1"),
    "unit": Decimal("1"), "each": Decimal("1"), "": Decimal("1"),
}


def _d(value):
    """Coerce any numeric-ish value to Decimal safely (None -> 0)."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _norm(unit_name):
    return str(unit_name or "").strip().lower()


def lookup_conversion(purchase_unit):
    """Return base units per 1 purchase unit for a known unit name, else None."""
    return BASE_UNIT_CONVERSIONS.get(_norm(purchase_unit))


def normalize_item_name(name):
    """Return the stripped menu-item name, or None when empty / whitespace-only.

    Pure + unit-testable — used by ``POST/PATCH /api/menu/item`` so the create / rename
    validation has no DB dependency.
    """
    n = str(name or "").strip()
    return n or None


def parse_selling_price(value):
    """Return a Decimal selling price when ``value`` is a non-negative number, else None.

    Pure + unit-testable — rejects negative prices and non-numeric strings at the
    validation layer before anything touches the DB.
    """
    if value is None:
        return None
    try:
        p = _d(value)
    except Exception:
        return None
    if p < 0:
        return None
    return p


def weighted_average_cost(purchases):
    """Moving weighted average cost from stock_in purchase rows.

    ``purchases`` is an iterable of ``(quantity, total_cost)`` tuples.
    Rows with quantity <= 0 are ignored (guard against bad data).  Returns a
    Decimal quantized to 4 dp, or None when there are no valid rows.
    """
    total_qty = Decimal("0")
    total_cost = Decimal("0")
    for qty, cost in purchases:
        q = _d(qty)
        c = _d(cost)
        if q <= 0:
            continue
        total_qty += q
        total_cost += c
    if total_qty <= 0:
        return None
    return (total_cost / total_qty).quantize(_Q4, rounding=ROUND_HALF_UP)


def cost_per_base_unit(weighted_avg, base_per_purchase):
    """Divide the per-purchase-unit cost by ``base_per_purchase`` -> cost per base unit.

    Returns Decimal (6 dp) or None when the inputs are missing / non-positive.
    """
    w = _d(weighted_avg) if weighted_avg is not None else None
    bpp = _d(base_per_purchase)
    if w is None or bpp <= 0:
        return None
    return (w / bpp).quantize(_Q6, rounding=ROUND_HALF_UP)


def portion_cost_from(recipe_rows, unit_costs):
    """Sum recipe rows into a per-portion cost.

    ``recipe_rows``  : iterable of ``(qty_per_portion, waste_pct, ingredient)``.
    ``unit_costs``   : dict ingredient -> Decimal cost per base unit (None = unknown).

    Returns ``(portion_cost, missing)`` where ``portion_cost`` is a Decimal (2 dp)
    or None if ANY ingredient has an unknown cost, and ``missing`` is the list of
    ingredient names whose cost could not be resolved.  We NEVER silently return 0
    for an unknown ingredient — the caller must flag the row.
    """
    total = Decimal("0")
    missing = []
    for qty, waste, ingredient in recipe_rows:
        uc = unit_costs.get(ingredient) if isinstance(unit_costs, dict) else None
        if uc is None:
            missing.append(ingredient)
            continue
        q = _d(qty)
        w = _d(waste)
        total += q * uc * (Decimal("1") + w / Decimal("100"))
    if missing:
        return None, missing
    return total.quantize(_MONEY, rounding=ROUND_HALF_UP), []


def food_cost_metrics(portion_cost, selling_price):
    """Compute food_cost_pct / margin / margin_pct from a portion cost + selling price.

    Returns a dict of Decimals, or None when selling price <= 0 or portion_cost is None.
    """
    sp = _d(selling_price)
    if portion_cost is None or sp <= 0:
        return None
    pct = (portion_cost / sp * Decimal("100")).quantize(_PCT, rounding=ROUND_HALF_UP)
    margin = (sp - portion_cost).quantize(_MONEY, rounding=ROUND_HALF_UP)
    margin_pct = (margin / sp * Decimal("100")).quantize(_PCT, rounding=ROUND_HALF_UP)
    return {
        "portion_cost": portion_cost,
        "selling_price": sp.quantize(_MONEY, rounding=ROUND_HALF_UP),
        "food_cost_pct": pct,
        "margin": margin,
        "margin_pct": margin_pct,
    }


def food_cost_status(pct):
    """Return the colour-band status for a food-cost percentage.

    ok   <= 35%
    warn  > 35% .. 40%
    high  > 40%
    """
    if pct is None:
        return "unknown"
    p = _d(pct)
    if p <= Decimal("35"):
        return "ok"
    if p <= Decimal("40"):
        return "warn"
    return "high"
