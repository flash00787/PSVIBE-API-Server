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
    "လီတာ": Decimal("1000"),  # Burmese litre = 1000 ml
    "ထုပ်": Decimal("1"),  # pack / bundle — no fixed size, cost per pack
    "ဗူး": Decimal("1"),  # bottle / tin — no fixed size, cost per bottle/tin
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


# ── Item type (owner 2026-09-28: split resale vs made dishes) ──────────────
# ``resale`` = packaged / ready-made goods costed straight from the purchase price
# (stock_in weighted average; 1 purchase unit == 1 sold unit, NO base-unit
# conversion).  ``dish`` = a made dish costed from a menu_recipes recipe.
ITEM_TYPES = ("resale", "dish")


def normalize_item_type(value):
    """Return the canonical item_type ("resale"|"dish") or None for invalid input.

    Accepts friendly aliases so the UI / API can send human labels:
      dish  <- dish | made | made-dish | made_dish
      resale <- resale | ready | ready-made | ready_made | packaged
    Unknown / empty / None -> None (caller falls back to auto-classification).
    """
    t = str(value or "").strip().lower()
    if t in ("dish", "made", "made-dish", "made dish", "made_dish", "madedish"):
        return "dish"
    if t in ("resale", "ready", "ready-made", "ready_made", "readymade", "packaged"):
        return "resale"
    return None


def classify_item_type(explicit_type, has_recipe):
    """Auto-classify an item's effective type (owner 2026-09-28).

    dish   if explicitly set to dish, OR it has >=1 menu_recipes row
    resale otherwise.

    This is the rule that keeps existing rows behaving sensibly: a packaged item
    (no recipe) stays resale and is costed from its purchase price; an item the
    owner built a recipe for is automatically a made dish.
    """
    t = normalize_item_type(explicit_type)
    if t == "dish":
        return "dish"
    if has_recipe:
        return "dish"
    return "resale"


def resale_unit_cost(purchases):
    """Purchase unit cost for a RESALE item = weighted average of stock_in rows.

    Same weighted-average math as the ingredient engine, but with NO base-unit
    conversion (1 purchase unit == 1 sold unit).  Returns None when there is no
    purchase history — the caller must flag the row as "no purchase yet" and must
    NEVER report it as 0-cost.
    """
    return weighted_average_cost(purchases)


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


def filter_items_by_type(items, item_type):
    """Pure type filter for /api/menu/costs (resale | dish | all / None).

    ``items`` is an iterable of dicts carrying an ``item_type`` key.  Returns a
    list of the matching items; unknown values behave as "all" (no filtering).
    """
    t = normalize_item_type(item_type)
    if t is None:
        return list(items)
    return [i for i in items if (i or {}).get("item_type") == t]


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


# ── Ingredient vs product (owner 2026-09-28 Osmo: "ခုက ရောနေသလို ဖြစ်နေတယ်") ──
# A stock item is EITHER a recipe ingredient (raw: rice, oil, tea-leaf, eggs,
# meat…) OR a sellable product (packaged: cakes, chips, soft drinks…).  The
# recipe picker must offer ingredients only.  Kind is persisted on the
# ``ingredients`` table (column ``kind``) and defaults by heuristic: an item that
# also exists as a sellable inventory row -> product, otherwise ingredient.
INGREDIENT_KINDS = ("ingredient", "product")


def normalize_kind(value):
    """Return the canonical kind ("ingredient"|"product") or None for invalid input.

    Accepts friendly aliases so the UI / API can send human labels:
      ingredient <- ingredient | ingredients | raw | ဟင်းခံ | ဟင်းခံပစ္စည်း
      product    <- product | products | resale | sellable | ready | packaged
    Unknown / empty / None -> None (caller falls back to the heuristic).
    """
    t = str(value or "").strip().lower()
    if t in ("ingredient", "ingredients", "raw", "ဟင်းခံ", "ဟင်းခံပစ္စည်း", "ဟင်းခံပစ္စည်းများ"):
        return "ingredient"
    if t in ("product", "products", "resale", "sellable", "ready", "ready-made", "ready_made", "readymade", "packaged"):
        return "product"
    return None


def classify_item_kind(explicit_kind, is_sellable):
    """Effective kind for a stock item (owner 2026-09-28).

    Explicit kind (persisted on ``ingredients.kind``) always wins.  Otherwise the
    default heuristic: an item that is also a sellable inventory row -> product;
    anything else -> ingredient (raw).  This keeps existing data behaving
    sensibly WITHOUT a backfill — packaged goods already sit in ``inventory`` and
    therefore become products, while raw stock like eggs / rice / oil stays
    ingredient.
    """
    k = normalize_kind(explicit_kind)
    if k:
        return k
    return "product" if is_sellable else "ingredient"


def filter_items_by_kind(items, kind):
    """Pure kind filter for /api/menu/ingredients (ingredient | product | all / None).

    ``items`` is an iterable of dicts carrying a ``kind`` key.  Unknown values
    behave as "all" (no filtering)."""
    k = normalize_kind(kind)
    if k is None:
        return list(items)
    return [i for i in items if (i or {}).get("kind") == k]


def ingredient_display(weighted_avg, purchase_unit, base_unit, base_per_purchase):
    """How to show an ingredient's unit cost (owner 2026-09-28: stop "/g" for all).

    Returns a dict:
      * has_conversion — True when a real base-unit conversion is configured
                         (base_per_purchase > 1), e.g. ပိဿာ = 1632 g.
      * unit_label     — the unit the cost is expressed in:
                           conversion configured -> base unit (g / ml)
                           no conversion         -> the REAL purchase unit
                                                     (ပိဿာ / ကျပ်သား / kg / ထုပ် / ဗူး / …)
      * cost_basis     — 'base' | 'purchase' (which number to display)
      * display_cost   — the cost figure matching unit_label (None = unknown)

    No conversion -> cost is shown per PURCHASE unit (never a fake per-gram figure).
    Pure + unit-testable.
    """
    bpp = _d(base_per_purchase) if base_per_purchase is not None else None
    if bpp is not None and bpp > 1:
        unit = str(base_unit or "g").strip() or "g"
        cpb = cost_per_base_unit(weighted_avg, bpp) if weighted_avg is not None else None
        return {
            "has_conversion": True,
            "unit_label": unit,
            "cost_basis": "base",
            "display_cost": cpb,
        }
    unit = str(purchase_unit or "pcs").strip() or "pcs"
    return {
        "has_conversion": False,
        "unit_label": unit,
        "cost_basis": "purchase",
        "display_cost": weighted_avg,
    }
