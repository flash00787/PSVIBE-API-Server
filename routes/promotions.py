"""PS VIBE API — Promotion Routes."""
from fastapi import APIRouter, Depends
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from deps import verify_api_key
from helpers import ok, error_response
from models import GenericResponse
import logging

# Single source of truth for money maths — shared with POST /api/sales/record.
from sale_money import compute_totals as _compute_totals

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Promotions"])


def _fmt_time(v):
    """Format a TIME value (pymysql timedelta/datetime.time) to 'HH:MM:SS'."""
    if v is None:
        return None
    if hasattr(v, "total_seconds"):  # timedelta
        total = int(v.total_seconds()) % 86400
        return "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)
    s = str(v)
    return s if s else None


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
            "       start_date, end_date, status, notes, "
            "       applies_auto, weekday_mask, time_window_start, time_window_end, "
            "       tier_charge_mins, tier_cycle_mins "
            "FROM promotions "
            "WHERE status='active' "
            "  AND (start_date IS NULL OR start_date <= CURDATE()) "
            "  AND (end_date IS NULL OR end_date >= CURDATE()) "
            "  AND (applies_auto = 0 OR applies_auto IS NULL) "  # auto-bound promos are NEVER staff-picked
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
                # ── Duration 2+1 (Weekday Happy Hour) fields ──
                "applies_auto": int(r.get("applies_auto") or 0),
                "weekday_mask": int(r.get("weekday_mask") or 0),
                "time_window_start": _fmt_time(r.get("time_window_start")),
                "time_window_end": _fmt_time(r.get("time_window_end")),
                "tier_charge_mins": int(r.get("tier_charge_mins") or 0),
                "tier_cycle_mins": int(r.get("tier_cycle_mins") or 0),
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

    Delegates to :func:`sale_money.compute_totals`, the SAME function
    ``POST /api/sales/record`` uses to commit, so the previewed discount / net is
    exactly what gets charged (single source of truth). Pure function: reads
    promotions + coupon rows, performs NO writes. Mirrors the sale-bot promo engine
    (bot/handlers/discount.py).

    Body (all optional except an amount):
      game_amt, food_total, gross_total, is_member, wallet_mins,
      effective_cost_mins, base_rate, multiplier, promo_id, coupon_code,
      manual_discount, food_items:[{name,qty,subtotal}], foc_item, coupon_minutes
    """
    try:
        if not isinstance(req, dict):
            return error_response(message="Invalid request body")
        # 🎁 Auto-evaluate the duration_2plus1 promo for inline display (read-only).
        # The server resolves the session start and reports the server-computed
        # billable/free minutes; the client never computes the tier itself.
        _req = dict(req)
        try:
            from sale_money import load_active_duration_promo as _load_d2p, compute_duration_2plus1 as _compute_d2p
            _pro = _load_d2p()
            if _pro:
                _sess = None
                _sid = _req.get("session_id")
                _bid = _req.get("booking_id")
                _cid = _req.get("console_id")
                if _sid not in (None, "", "0", 0):
                    try:
                        _sess = _mysql_query_one(
                            "SELECT actual_start, actual_end, actual_duration_mins, status, TIMESTAMPDIFF(MINUTE, actual_start, CASE WHEN status='active' THEN NOW() + INTERVAL 390 MINUTE ELSE actual_end END) AS played_mins FROM console_sessions WHERE id=%s", (int(_sid),))
                    except (TypeError, ValueError):
                        _sess = None
                if not _sess and _bid not in (None, "", 0, "0"):
                    _sess = _mysql_query_one(
                        "SELECT actual_start, actual_end, actual_duration_mins, status, TIMESTAMPDIFF(MINUTE, actual_start, CASE WHEN status='active' THEN NOW() + INTERVAL 390 MINUTE ELSE actual_end END) AS played_mins FROM console_sessions WHERE booking_id=%s ORDER BY id DESC LIMIT 1", (_bid,))
                if not _sess and _cid:
                    _sess = _mysql_query_one(
                        "SELECT actual_start, actual_end, actual_duration_mins, status, TIMESTAMPDIFF(MINUTE, actual_start, CASE WHEN status='active' THEN NOW() + INTERVAL 390 MINUTE ELSE actual_end END) AS played_mins FROM console_sessions WHERE console_id=%s AND status='active' ORDER BY id DESC LIMIT 1", (_cid,))
                if _sess and _sess.get("actual_start"):
                    # ⚠️ 2026-10-15 (Osmo/Kai): the 2+1 tier MUST use the ACTUAL played
                    # minutes (client "mins" — the same basis as the billed game amount).
                    # console_sessions.actual_duration_mins holds the ALLOCATED/planned
                    # duration, so a 75-min session was priced as 180 min
                    # (billable 120 → 24,000 Ks instead of 15,000). actual_duration_mins
                    # is now only a last-resort fallback when no minutes are supplied.
                    _T = int(_sess.get("played_mins") or 0)   # actual play (end-start)
                    if _T <= 0:
                        _T = int(float(_req.get("mins") or 0) or 0)
                    if _T <= 0:
                        _T = int(_sess.get("actual_duration_mins") or 0)
                    _d2p = _compute_d2p(_sess.get("actual_start"), _T, _pro)
                    if _d2p:
                        # 🪙 2026-10-16 (Osmo): the PREVIEW must price a 2+1 exactly like
                        # the COMMIT (admin_sales_routes checkout hook) — i.e. WITH the
                        # member wallet. Without this the Sale screen showed the FULL
                        # billable amount (120m → 20,000 Ks) instead of the wallet
                        # shortfall (3m → 500 Ks); the payment box pre-filled that
                        # inflated figure and staff recorded it
                        # (V-WEB-20261016121214-478: net 500 but paid 20,000).
                        try:
                            from sale_money import price_duration_2plus1 as _price_d2p
                            _mid = str(_req.get("member_id") or "").strip()
                            _bal = None
                            if _mid and _mid not in ("", "0", "0 (Guest)", "-", "Guest"):
                                _w = _mysql_query_one(
                                    "SELECT balance_mins FROM member_wallets WHERE member_id=%s", (_mid,))
                                if _w is not None:
                                    _bal = int(_w.get("balance_mins") or 0)
                            _br = float(_req.get("base_rate") or 0) or 10000.0
                            _mx = float(_req.get("multiplier") or 1.0) or 1.0
                            if _bal is not None:
                                _pr = _price_d2p(_d2p["billable_mins"], _br, _mx, _bal)
                                _d2p = dict(_d2p)
                                _d2p["cash_mins"] = _pr["cash_mins"]
                                _d2p["wallet_mins"] = _pr["wallet_mins"]
                                if _pr["wallet_mins"] > 0 and _pr["cash_mins"] <= 0:
                                    # wallet covers the whole billable tier → no cash for game
                                    _req["is_member"] = True
                                    _req["wallet_mins"] = _pr["wallet_mins"]
                                    _req["effective_cost_mins"] = _pr["wallet_mins"]
                                elif _pr["cash_mins"] > 0:
                                    # partial coverage → wallet + cash shortfall
                                    _req["is_member"] = False
                                    _req["wallet_mins"] = 0
                            _req["base_rate"] = _br
                            _req["multiplier"] = _mx
                        except Exception as _we:
                            logger.warning("wallet-aware 2+1 preview failed: %s", _we)
                        _req["duration_2plus1"] = _d2p
                        _req["promo_id"] = str(_d2p["promo_id"])
        except Exception as _e:
            logger.warning("duration_2plus1 calc eval failed: %s", _e)
        return ok(_compute_totals(_req))
    except Exception as e:
        logger.error("POST /promotions/calc error: %s", e)
        return error_response(message="Could not calculate discount. Please check the cart and retry.")


def _api_promotions_calc_legacy(req: dict):
    """Kept as documentation of the pre-Wave-2 inline maths (no longer called)."""
    try:
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


# ── Staff App: promotion CRUD (2026-10-31) ─────────────────────────────
@router.post("/promotions", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_create(payload: dict, auth=Depends(verify_api_key)):
    """Create a promotion (staff app admin screen)."""
    try:
        name = str(payload.get("name") or payload.get("title") or "").strip()
        if not name:
            return error_response("name is required")
        promo_type = str(payload.get("promo_type") or payload.get("type") or "discount")
        discount_type = str(payload.get("discount_type") or "percent")
        discount_value = float(payload.get("discount_value") or payload.get("discount_percent") or 0)
        start_date = payload.get("start_date") or None
        end_date = payload.get("end_date") or None
        notes = str(payload.get("notes") or "")
        status = str(payload.get("status") or "active")
        _mysql_exec(
            "INSERT INTO promotions (name, promo_name, promo_type, discount_type, discount_value, "
            "                      start_date, end_date, status, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (name, name, promo_type, discount_type, discount_value, start_date, end_date, status, notes),
        )
        row = _mysql_query_one("SELECT * FROM promotions ORDER BY id DESC LIMIT 1")
        return ok({"promotion": row}, message="created")
    except Exception as e:
        logger.exception("promotion create failed")
        return error_response(str(e))


@router.patch("/promotions/{promo_id}", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_update(promo_id: int, payload: dict, auth=Depends(verify_api_key)):
    """Update a promotion."""
    try:
        existing = _mysql_query_one("SELECT * FROM promotions WHERE id=%s", (int(promo_id),))
        if not existing:
            return error_response("promotion not found")
        fields, values = [], []
        for key in ("name", "promo_type", "discount_type", "discount_value",
                    "start_date", "end_date", "status", "notes"):
            if key in payload:
                fields.append(key + "=%s")
                values.append(payload[key])
        if not fields:
            return error_response("nothing to update")
        values.append(int(promo_id))
        _mysql_exec("UPDATE promotions SET " + ", ".join(fields) + " WHERE id=%s", tuple(values))
        row = _mysql_query_one("SELECT * FROM promotions WHERE id=%s", (int(promo_id),))
        return ok({"promotion": row}, message="updated")
    except Exception as e:
        logger.exception("promotion update failed")
        return error_response(str(e))


@router.delete("/promotions/{promo_id}", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_delete(promo_id: int, auth=Depends(verify_api_key)):
    """Archive a promotion (soft delete: status=archived)."""
    try:
        existing = _mysql_query_one("SELECT id FROM promotions WHERE id=%s", (int(promo_id),))
        if not existing:
            return error_response("promotion not found")
        _mysql_exec("UPDATE promotions SET status='archived' WHERE id=%s", (int(promo_id),))
        return ok({"id": int(promo_id)}, message="archived")
    except Exception as e:
        logger.exception("promotion delete failed")
        return error_response(str(e))

