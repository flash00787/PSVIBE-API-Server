"""PS VIBE API — Dish → ingredient stock consumption (FIFO ledger).

Owner request (Osmo, 2026-09-28):
    "ဟင်းပွဲ ရောင်းရင် ingredient stock ကိုပါ လျော့ပေးပါ · Ingredient cost ကို FIFO အတိုင်းယူပါ"
= when a made dish (item_type='dish') is sold, actually deduct its recipe's
  ingredients from stock FIFO, and record the FIFO cost as consumption / COGS.

This module is additive.  It introduces:

  * ``stock_layers``          — one row per stock_in purchase, tracking the
                                remaining (unconsumed) quantity of that purchase.
  * ``ingredient_consumption``— one row per (dish, sale-ref, ingredient), the FIFO
                                cost + per-layer breakdown recorded on each dish sale.

The pure FIFO math lives in ``routes.menu_cost`` (``ledger_layers``,
``fifo_consume_split``, ``blended_unit_cost``) so it is unit-testable without a DB.
Everything here is DB plumbing + the consumption side-effects.

Idempotency: a sale is identified by ``ref`` (the stock_out row id — one stock_out
row == one sale line).  ``ingredient_consumption`` has UNIQUE(dish_item, ref,
ingredient) and every path pre-checks that key, so the same sale can never deduct
the same ingredient twice.
"""
import json as _json
import logging
from decimal import Decimal

from mysql_db import query as _q, query_one as _q1, execute as _ex, get_connection as _get_conn
from routes.menu_cost import (
    fifo_consume_split,
    blended_unit_cost,
    _d,
)

logger = logging.getLogger(__name__)

_READY = False


def ensure_tables() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for the ledger + consumption tables."""
    global _READY
    if _READY:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_layers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    branch_id INT NOT NULL DEFAULT 1,
                    item_name VARCHAR(200) NOT NULL,
                    qty_in DECIMAL(14,4) NOT NULL DEFAULT 0,
                    qty_remaining DECIMAL(14,4) NOT NULL DEFAULT 0,
                    unit_cost DECIMAL(12,4) NOT NULL DEFAULT 0,
                    stock_in_id INT NULL,
                    purchased_at TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_stock_layers_stock_in (stock_in_id),
                    KEY idx_layers_item (item_name),
                    KEY idx_layers_branch_item (branch_id, item_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingredient_consumption (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    branch_id INT NOT NULL DEFAULT 1,
                    dish_item VARCHAR(200) NOT NULL,
                    ingredient VARCHAR(200) NOT NULL,
                    qty DECIMAL(14,4) NOT NULL DEFAULT 0,
                    unit_cost DECIMAL(12,4) NOT NULL DEFAULT 0,
                    cost DECIMAL(14,4) NOT NULL DEFAULT 0,
                    layer_breakdown JSON NULL,
                    short_by DECIMAL(14,4) NOT NULL DEFAULT 0,
                    ref VARCHAR(200) NOT NULL,
                    voucher_no VARCHAR(100) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_consumption (dish_item, ref, ingredient),
                    KEY idx_consumption_dish (dish_item),
                    KEY idx_consumption_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()
        _READY = True
    finally:
        conn.close()


# ── Ledger maintenance ────────────────────────────────────────────────────────
def ensure_layer_for_stock_in(stock_in_id) -> bool:
    """Create a stock_layers row for a freshly inserted stock_in row (idempotent).

    Reads the stock_in row and inserts a layer with qty_remaining = quantity.
    Returns True when a layer was created, False when one already existed (or the
    stock_in row is missing / quantity <= 0).  Never raises — a layer failure must
    never block a purchase.
    """
    try:
        ensure_tables()
        row = _q1(
            "SELECT id, branch_id, item_name, quantity, unit_cost, created_at FROM stock_in WHERE id=%s",
            (stock_in_id,),
        )
        if not row or _d(row["quantity"]) <= 0:
            return False
        if _q1("SELECT id FROM stock_layers WHERE stock_in_id=%s", (stock_in_id,)):
            return False
        _ex(
            "INSERT INTO stock_layers (branch_id, item_name, qty_in, qty_remaining, unit_cost, stock_in_id, purchased_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (row["branch_id"], row["item_name"], row["quantity"], row["quantity"],
             row["unit_cost"], stock_in_id, row["created_at"]),
        )
        return True
    except Exception as e:  # noqa: BLE001 — best-effort, never break a purchase
        logger.warning("ensure_layer_for_stock_in(%s) failed: %s", stock_in_id, e)
        return False


def _ingredient_bpp(ingredient, branch_id=1):
    """base_per_purchase for an ingredient (base units per 1 purchase unit), default 1."""
    row = _q1("SELECT base_per_purchase FROM ingredients WHERE name=%s AND branch_id=%s", (ingredient, branch_id))
    if row and row.get("base_per_purchase") is not None:
        bpp = _d(row["base_per_purchase"])
        if bpp > 0:
            return bpp
    return Decimal("1")


def _canonical_map(branch_id=1):
    """variant_name -> canonical_name from ingredient_variants."""
    m = {}
    try:
        for r in _q("SELECT canonical_name, variant_name FROM ingredient_variants WHERE branch_id=%s", (branch_id,)):
            m[r["variant_name"]] = r["canonical_name"]
    except Exception:
        pass
    return m


def _historical_consumption(branch_id=1):
    """Total historical consumption per CANONICAL item_name (PURCHASE units) = direct
    stock_out + recipe usage (dish sales x recipe qty x (1+waste%) / base_per_purchase).
    Variant spellings are merged to the canonical name (same as the costing engine) so
    the opening ledger balances EXACTLY match the pre-existing FIFO estimate (no
    regression).  Read-only."""
    canon = _canonical_map(branch_id)
    consumption = {}
    for r in _q("SELECT item_name, SUM(quantity) AS qty FROM stock_out WHERE branch_id=%s GROUP BY item_name", (branch_id,)):
        cname = canon.get(r["item_name"], r["item_name"])
        consumption[cname] = consumption.get(cname, Decimal("0")) + _d(r["qty"])
    dish_sales = dict(consumption)
    for r in _q("SELECT menu_item, ingredient, qty_per_portion, waste_pct FROM menu_recipes WHERE branch_id=%s", (branch_id,)):
        c_ing = canon.get(r["ingredient"], r["ingredient"])
        c_dish = canon.get(r["menu_item"], r["menu_item"])
        sold = dish_sales.get(c_dish, Decimal("0"))
        if sold <= 0:
            continue
        needed_base = _d(r["qty_per_portion"]) * sold * (Decimal("1") + _d(r["waste_pct"]) / Decimal("100"))
        add = needed_base / _ingredient_bpp(r["ingredient"], branch_id)
        consumption[c_ing] = consumption.get(c_ing, Decimal("0")) + add
    return consumption


def backfill_stock_layers() -> dict:
    """Backfill one stock_layers row per stock_in purchase (oldest -> newest), idempotently.

    ``qty_remaining`` is opened at the correct value: historical consumption (direct
    stock_out + recipe usage, variant-merged) is consumed FIFO so the ledger reflects
    ACTUAL remaining from day one (the display never regresses behind the estimate).
    Skips any stock_in row that already has a layer.  Returns counts.
    """
    ensure_tables()
    canon = _canonical_map()
    rows = _q(
        "SELECT id, branch_id, item_name, quantity, unit_cost, created_at FROM stock_in "
        "WHERE quantity > 0 ORDER BY created_at ASC, id ASC"
    )
    consumption = _historical_consumption()

    # group by CANONICAL name (variant-merged), oldest -> newest, and consume FIFO
    by_item = {}
    for r in rows:
        cname = canon.get(r["item_name"], r["item_name"])
        by_item.setdefault(cname, []).append(r)

    created = skipped = 0
    for cname, item_rows in by_item.items():
        remaining = consumption.get(cname, Decimal("0"))
        for r in item_rows:
            sid = r["id"]
            if _q1("SELECT id FROM stock_layers WHERE stock_in_id=%s", (sid,)):
                skipped += 1
                continue
            qty = _d(r["quantity"])
            if remaining > 0:
                take = qty if remaining >= qty else remaining
                remaining -= take
                qty_remaining = qty - take
            else:
                qty_remaining = qty
            try:
                _ex(
                    "INSERT INTO stock_layers (branch_id, item_name, qty_in, qty_remaining, unit_cost, stock_in_id, purchased_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (r["branch_id"], r["item_name"], qty, qty_remaining,
                     r["unit_cost"], sid, r["created_at"]),
                )
                created += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("backfill layer for stock_in %s failed: %s", sid, e)
                skipped += 1
    return {"created": created, "skipped": skipped}


# ── Dish detection + ingredient name resolution ───────────────────────────────
def is_dish_item(item_name, branch_id=1) -> bool:
    """True when ``item_name`` is a made dish (item_type='dish' OR has a recipe)."""
    try:
        inv = _q1("SELECT item_type FROM inventory WHERE item_name=%s", (item_name,))
        if inv and (str(inv.get("item_type") or "").strip().lower() == "dish"):
            return True
        rec = _q1(
            "SELECT id FROM menu_recipes WHERE menu_item=%s AND branch_id=%s LIMIT 1",
            (item_name, branch_id),
        )
        return bool(rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("is_dish_item(%s) failed: %s", item_name, e)
        return False


def _ingredient_names(ingredient, branch_id):
    """Resolve an ingredient (with variant merging) to the list of stock_layers
    item_names to consume from.  Canonical name + every variant of it."""
    canon = ingredient
    row = _q1(
        "SELECT canonical_name FROM ingredient_variants WHERE variant_name=%s AND branch_id=%s",
        (ingredient, branch_id),
    )
    if row:
        canon = row["canonical_name"]
    names = {canon}
    for r in _q(
        "SELECT variant_name FROM ingredient_variants WHERE canonical_name=%s AND branch_id=%s",
        (canon, branch_id),
    ):
        names.add(r["variant_name"])
    return canon, names


# ── Consumption ───────────────────────────────────────────────────────────────
def consume_dish_recipe(dish_item, qty_sold, ref, voucher_no=None, branch_id=1) -> dict:
    """Deduct a dish's recipe ingredients FIFO and record the FIFO cost.

    For each menu_recipes row of ``dish_item``: needed base units =
    qty_per_portion x qty_sold x (1 + waste%/100), converted to purchase units via
    the ingredient's base_per_purchase, then consumed FIFO (oldest layer first)
    from stock_layers.qty_remaining.  Updates each layer's qty_remaining, decrements
    the ingredient's inventory.quantity (down to 0), and inserts an
    ingredient_consumption row carrying the per-layer FIFO split + total cost.

    Idempotent on (dish_item, ref, ingredient).  Insufficient stock consumes what
    remains and records ``short_by`` (never blocks the sale).
    """
    ensure_tables()
    recipes = _q(
        "SELECT ingredient, qty_per_portion, waste_pct FROM menu_recipes "
        "WHERE menu_item=%s AND branch_id=%s ORDER BY id",
        (dish_item, branch_id),
    )
    if not recipes:
        return {"consumed": False, "reason": "no_recipe", "rows": []}

    result_rows = []
    consumed_any = False
    for rec in recipes:
        ingredient = rec["ingredient"]
        canon, names = _ingredient_names(ingredient, branch_id)

        # Idempotency guard — same sale ref + dish + ingredient already consumed.
        if _q1(
            "SELECT id FROM ingredient_consumption WHERE dish_item=%s AND ref=%s AND ingredient=%s",
            (dish_item, ref, ingredient),
        ):
            continue

        # base_per_purchase (base units per 1 purchase unit) — default 1.
        bpp = Decimal("1")
        ing_row = _q1(
            "SELECT base_per_purchase FROM ingredients WHERE name=%s AND branch_id=%s",
            (ingredient, branch_id),
        )
        if ing_row and ing_row.get("base_per_purchase") is not None:
            bpp = _d(ing_row["base_per_purchase"])
        if bpp <= 0:
            bpp = Decimal("1")

        needed_base = _d(rec["qty_per_portion"]) * _d(qty_sold) * (Decimal("1") + _d(rec["waste_pct"]) / Decimal("100"))
        needed = needed_base / bpp

        # Layers FIFO (oldest -> newest) across the canonical + variant names.
        placeholders = ",".join(["%s"] * len(names))
        layer_rows = _q(
            f"SELECT id, qty_remaining, unit_cost FROM stock_layers "
            f"WHERE item_name IN ({placeholders}) AND branch_id=%s AND qty_remaining > 0 "
            f"ORDER BY purchased_at ASC, id ASC",
            (*names, branch_id),
        )
        layers = [{"id": l["id"], "qty": _d(l["qty_remaining"]), "unit_cost": _d(l["unit_cost"])} for l in layer_rows]

        split = fifo_consume_split(layers, needed)
        breakdown = [
            {"layer_id": u["layer_id"], "unit_cost": float(u["unit_cost"]), "qty_taken": float(u["qty_taken"])}
            for u in split["layers_used"]
        ]
        unit_cost = blended_unit_cost(split)
        cost = split["total_cost"]

        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                # 1) deduct layers FIFO
                for u in split["layers_used"]:
                    cur.execute(
                        "UPDATE stock_layers SET qty_remaining = qty_remaining - %s WHERE id=%s",
                        (u["qty_taken"], u["layer_id"]),
                    )
                # 2) decrement ingredient inventory (legacy summary, down to 0)
                cur.execute(
                    "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
                    (split["consumed"], canon),
                )
                # 3) record consumption (FIFO cost)
                cur.execute(
                    "INSERT INTO ingredient_consumption "
                    "(branch_id, dish_item, ingredient, qty, unit_cost, cost, layer_breakdown, short_by, ref, voucher_no) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (branch_id, dish_item, ingredient, split["consumed"],
                     unit_cost if unit_cost is not None else Decimal("0"),
                     cost, _json.dumps(breakdown, ensure_ascii=False),
                     split["short_by"], ref, voucher_no),
                )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            logger.warning("consume_dish_recipe %s / %s / %s failed: %s", dish_item, ref, ingredient, e)
            continue
        finally:
            conn.close()

        consumed_any = True
        if split["short_by"] > 0:
            logger.warning(
                "FIFO shortfall: dish=%s ingredient=%s needed=%s consumed=%s short_by=%s ref=%s",
                dish_item, ingredient, needed, split["consumed"], split["short_by"], ref,
            )
        result_rows.append({
            "ingredient": ingredient,
            "canonical": canon,
            "consumed": split["consumed"],
            "cost": cost,
            "unit_cost": unit_cost,
            "short_by": split["short_by"],
            "layers_used": breakdown,
        })

    return {"consumed": consumed_any, "rows": result_rows}


def consume_after_stock_out(item_name, qty, stock_out_id, voucher_no=None, branch_id=1):
    """Call after a stock_out row is written.  If the item is a dish, consume its
    recipe ingredients FIFO (idempotent on stock_out:{id}).  Best-effort — a
    failure never blocks the sale."""
    try:
        if not item_name or not stock_out_id:
            return None
        if not is_dish_item(item_name, branch_id):
            return None
        return consume_dish_recipe(
            item_name, qty, f"stock_out:{stock_out_id}",
            voucher_no=voucher_no, branch_id=branch_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("consume_after_stock_out(%s, %s) failed: %s", item_name, qty, e)
        return None
