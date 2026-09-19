"""PS VIBE API — Promotion Routes."""
from fastapi import APIRouter, Depends
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from deps import verify_api_key
from helpers import ok, error_response
from models import GenericResponse
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Promotions"])


@router.get("/promotions/active-list", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_active_list(auth=Depends(verify_api_key)):
    """Active, in-window promotions for the Staff App sale-screen picker.

    Returns a normalised list the app can drop straight into a dropdown:
    ``[{id,title,type,discount_percent,discount_target,notes,start_date,end_date}]``.
    Read-only; safe for staff.
    """
    try:
        rows = _mysql_query(
            "SELECT id, name, promo_name, promo_type, discount_type, discount_value, "
            "       start_date, end_date, status, notes FROM promotions "
            "WHERE status='active' "
            "  AND (start_date IS NULL OR start_date <= CURDATE()) "
            "  AND (end_date IS NULL OR end_date >= CURDATE()) "
            "ORDER BY id DESC"
        )
        promos = []
        for r in rows:
            _type = (r.get("discount_type") or r.get("promo_type") or "general")
            promos.append({
                "id": r.get("id"),
                "title": r.get("promo_name") or r.get("name") or f"Promo #{r.get('id')}",
                "type": _type,
                "discount_percent": float(r["discount_value"]) if r.get("discount_value") is not None else 0,
                "discount_target": r.get("discount_target") or "overall",
                "notes": r.get("notes") or "",
                "start_date": str(r.get("start_date")) if r.get("start_date") else None,
                "end_date": str(r.get("end_date")) if r.get("end_date") else None,
            })
        return ok({"promotions": promos, "count": len(promos)})
    except Exception as e:
        logger.error("GET /promotions/active-list error: %s", e)
        return error_response(message="Could not load promotions")


def _to_int(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@router.post("/promotions/calc", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_calc(req: dict, auth=Depends(verify_api_key)):
    """Server-side discount calculator — the source of truth for money maths.

    The Staff App posts the cart and gets back the exact discount / net payable /
    bonus minutes so the client never computes money itself. Pure function: reads
    promotions + coupon rows, performs NO writes. Mirrors the sale-bot promo engine
    (bot/handlers/discount.py).

    Body (all optional except an amount):
      game_amt, food_total, gross_total, is_member, wallet_mins,
      effective_cost_mins, base_rate, multiplier, promo_id, coupon_code,
      manual_discount, food_items:[{name,qty,subtotal}], foc_item
    """
    try:
        if not isinstance(req, dict):
            return error_response(message="Invalid request body")

        game_amt = _to_int(req.get("game_amt"))
        food_total = _to_int(req.get("food_total"))
        gross_total_in = _to_int(req.get("gross_total"))
        is_member = bool(req.get("is_member"))
        wallet_mins = _to_int(req.get("wallet_mins"))
        eff_mins = _to_int(req.get("effective_cost_mins") or req.get("remin_mins"))
        base_rate = _to_float(req.get("base_rate"), 500.0) or 500.0
        multiplier = _to_float(req.get("multiplier"), 1.0) or 1.0
        manual_discount = _to_int(req.get("manual_discount"))
        food_items = req.get("food_items") or []

        # ── wallet vs cash session (mirror discount.py) ──────────────────────
        is_wallet_session = bool(is_member and wallet_mins > 0)
        wallet_game_value = 0
        if is_wallet_session:
            if eff_mins > 0:
                if req.get("effective_cost_mins"):
                    # multiplier already baked into effective_cost_mins when supplied
                    wallet_game_value = _to_int(eff_mins * base_rate / 60)
                else:
                    wallet_game_value = _to_int(eff_mins * base_rate * multiplier / 60)
            gross = wallet_game_value + food_total
            cash_payable = food_total
        else:
            gross = gross_total_in if gross_total_in > 0 else (game_amt + food_total)
            cash_payable = gross

        breakdown = []
        promo_discount = 0
        coupon_discount = 0
        foc_discount = 0
        bonus_mins = 0
        promo_title = ""
        coupon_code = ""

        # ── promotion ────────────────────────────────────────────────────────
        promo_id = str(req.get("promo_id") or "").strip()
        if promo_id:
            row = None
            if promo_id.isdigit():
                row = _mysql_query_one("SELECT * FROM promotions WHERE id=%s", (int(promo_id),))
            if row:
                ptype = row.get("discount_type") or row.get("promo_type") or "general"
                pct = _to_float(row.get("discount_value"))
                target = row.get("discount_target") or "overall"
                promo_title = row.get("promo_name") or row.get("name") or f"Promo #{promo_id}"
                if ptype == "bonus_mins":
                    bonus_mins = _to_int(req.get("bonus_mins") or pct)
                    breakdown.append({"label": f"Promo {promo_title}: +{bonus_mins} bonus mins", "amount": 0})
                elif ptype in ("discount", "cashback") and pct:
                    if is_wallet_session and target in ("game_only", "overall"):
                        bonus_mins = _to_int(eff_mins * pct / 100) or _to_int(60 * pct / 100)
                        breakdown.append({"label": f"Promo {promo_title}: {pct:g}% -> +{bonus_mins} bonus mins", "amount": 0})
                    else:
                        base = game_amt if target == "game_only" else food_total if target == "food_only" else gross
                        promo_discount = _to_int(base * pct / 100)
                        breakdown.append({"label": f"Promo {promo_title}: {pct:g}% off {target}", "amount": -promo_discount})
                else:
                    # bundle / free_item / general — FOC a chosen food line if provided
                    foc_item = str(req.get("foc_item") or "").strip()
                    if foc_item and isinstance(food_items, list):
                        for fi in food_items:
                            if isinstance(fi, dict) and str(fi.get("name") or "").strip() == foc_item:
                                foc_discount = _to_int(fi.get("subtotal"))
                                break
                    if foc_discount > gross:
                        foc_discount = gross
                    if foc_discount:
                        breakdown.append({"label": f"Promo {promo_title}: FOC {foc_item}", "amount": -foc_discount})
                    else:
                        breakdown.append({"label": f"Promo {promo_title} (no discount applied)", "amount": 0})
            else:
                breakdown.append({"label": f"Promo #{promo_id} not found/active", "amount": 0})

        # ── coupon ───────────────────────────────────────────────────────────
        coupon_code = str(req.get("coupon_code") or "").strip().upper()
        coupon_valid = False
        if coupon_code:
            c = _mysql_query_one(
                "SELECT coupon_code, balance_minutes, status, expiry_date FROM member_coupons WHERE coupon_code=%s",
                (coupon_code,))
            if not c:
                breakdown.append({"label": f"Coupon {coupon_code}: not found", "amount": 0})
            elif c.get("status") != "active":
                breakdown.append({"label": f"Coupon {coupon_code}: {c.get('status')}", "amount": 0})
            elif _to_int(c.get("balance_minutes")) <= 0:
                breakdown.append({"label": f"Coupon {coupon_code}: no minutes left", "amount": 0})
            else:
                coupon_valid = True
                coupon_discount = _to_int(_to_int(c.get("balance_minutes")) * base_rate / 60)
                breakdown.append({"label": f"Coupon {coupon_code}: {_to_int(c.get('balance_minutes'))} mins", "amount": -coupon_discount})

        if manual_discount:
            breakdown.append({"label": "Manual discount", "amount": -manual_discount})

        total_discount = promo_discount + coupon_discount + foc_discount + manual_discount
        if total_discount > cash_payable:
            total_discount = cash_payable
        net_total = max(0, cash_payable - total_discount)

        return ok({
            "is_wallet_session": is_wallet_session,
            "game_amt": game_amt,
            "food_total": food_total,
            "wallet_game_value": wallet_game_value,
            "gross_total": gross,
            "cash_payable": cash_payable,
            "promo_id": promo_id or None,
            "promo_title": promo_title or None,
            "coupon_code": coupon_code or None,
            "coupon_valid": coupon_valid,
            "promo_discount": promo_discount,
            "coupon_discount": coupon_discount,
            "foc_discount": foc_discount,
            "manual_discount": manual_discount,
            "total_discount": total_discount,
            "bonus_mins": bonus_mins,
            "net_total": net_total,
            "breakdown": breakdown,
        })
    except Exception as e:
        logger.error("POST /promotions/calc error: %s", e)
        return error_response(message="Could not calculate discount. Please check the cart and retry.")


@router.get("/promotions")
async def get_promotions(active_only: bool = False, auth=Depends(verify_api_key)):
    """List all promotions."""
    rows = _mysql_query("SELECT * FROM promotions ORDER BY id DESC")
    return {"success": True, "promotions": rows}


@router.get("/promotions/log")
async def get_promotions_log(limit: int = 50, auth=Depends(verify_api_key)):
    """Get promotion usage log."""
    rows = _mysql_query(
        "SELECT * FROM promotions_log ORDER BY id DESC LIMIT %s", (limit,)
    )
    return {"success": True, "log": rows}


@router.post("/promotions/log")
async def add_promotion_log(data: dict, auth=Depends(verify_api_key)):
    """Record promotion usage."""
    _mysql_exec(
        "INSERT INTO promotions_log (voucher_no, promo_id, promo_title, member_id, "
        "console_id, gross_total, discount_amt, net_total, staff_name, promo_date) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            data.get("voucher_no", ""), data.get("promo_id", ""),
            data.get("promo_title", ""), data.get("member_id", ""),
            data.get("console_id", ""), data.get("gross_total", 0),
            data.get("discount_amt", 0), data.get("net_total", 0),
            data.get("staff_name", ""), data.get("promo_date", ""),
        ),
    )
    return {"success": True, "message": "Logged"}
