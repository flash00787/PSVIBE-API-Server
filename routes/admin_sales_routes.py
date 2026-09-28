"""PS VIBE API — Admin, Sales & OPEX Routes."""
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from fastapi.responses import JSONResponse
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import _mysql_get_setting, ok, error_response, _parse_pm_and_inject, insert_cash_movement_if_absent, resolve_sale_game
from deps import verify_api_key
from auth import get_current_user, get_staff_or_dashboard_user
from models import GenericResponse
# Single money path (Staff App Wave 2): promo/coupon preview + deposit redeem commit.
from sale_money import (
    compute_totals as _compute_totals,
    commit_deposit_redemption as _commit_deposit,
    deposit_already_in_payment_method as _dep_in_pm,
    get_verified_deposit as _get_verified_deposit,
)
import staff_audit
import logging, json as _json, os
from datetime import datetime, timezone, timedelta
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Admin'])

# ═══════════════════════════════════════
@router.get("/fetch_staff", response_model=GenericResponse, tags=["Staff"], summary="Fetch staff list [MySQL]")
async def api_fetch_staff(auth=Depends(verify_api_key)):
    """Fetch staff list from MySQL staff_records."""
    try:
        rows = _mysql_query("SELECT staff_name FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        return ok([r["staff_name"] for r in rows])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fetch_staff_names", response_model=GenericResponse, tags=["Staff"], summary="Fetch staff names [MySQL]")
async def api_fetch_staff_names(auth=Depends(verify_api_key)):
    """Fetch active staff names from MySQL."""
    try:
        rows = _mysql_query("SELECT staff_name FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        return ok([r["staff_name"] for r in rows])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_food_prices / fetch_food_costs
# ==============================================



@router.post("/inventory/stock-out", response_model=GenericResponse, tags=["Inventory"], summary="Record stock-out (sale) and update inventory [MySQL]")
async def api_inventory_stock_out(req: dict, auth=Depends(verify_api_key)):
    """Record a stock-out from a food sale and decrement inventory quantity."""
    try:
        item_name = req.get("item_name", "")
        qty = int(req.get("qty", 0))
        unit_price = float(req.get("unit_price", 0))
        subtotal = float(req.get("subtotal", 0))
        sale_date = req.get("date", "")
        voucher_no = req.get("voucher_no", "")

        if not item_name or qty <= 0:
            return error_response(message="item_name and qty > 0 required")

        # Insert into stock_out table
        _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (item_name, qty, unit_price, subtotal, sale_date, f"Voucher: {voucher_no}")
        )

        # Decrement inventory.quantity (legacy summary column — NOTE: FIFO stock_in-stock_out is authoritative; use reconcile_inventory_quantities() to sync)
        _mysql_exec(
            "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
            (qty, item_name)
        )

        return ok({"success": True, "item_name": item_name, "qty_deducted": qty})
    except Exception as e:
        return error_response(message=str(e))



# ═══════════════════════════════════════
#  reconcile_inventory_quantities — sync inventory.quantity from FIFO
# ═══════════════════════════════════════
def reconcile_inventory_quantities():
    """Sync inventory.quantity to match authoritative FIFO calculation.
    
    inventory.quantity is a legacy summary column that can drift from stock_in/stock_out.
    This function recalculates it from: SUM(stock_in.quantity) - SUM(stock_out.quantity).
    Call this periodically or after batch stock operations.
    
    Returns dict with {item_name: old_qty, new_qty, drift} for changed items.
    """
    conn = _mc.connect(**_MYSQL_CFG)
    try:
        cur = conn.cursor(dictionary=True)
        # Get all inventory items with their current quantities
        cur.execute("SELECT item_name, quantity FROM inventory")
        items = cur.fetchall()
        changes = []
        for item in items:
            item_name = item["item_name"]
            old_qty = int(item["quantity"] or 0)
            # FIFO calculation: stock_in - stock_out (same as inventory_fifo.py get_fifo_stock)
            cur.execute(
                "SELECT COALESCE((SELECT SUM(quantity) FROM stock_in WHERE item_name=%s), 0) "
                "- COALESCE((SELECT SUM(quantity) FROM stock_out WHERE item_name=%s), 0) AS fifo_qty",
                (item_name, item_name)
            )
            row = cur.fetchone()
            fifo_qty = int(row["fifo_qty"] or 0)
            if fifo_qty != old_qty:
                cur.execute(
                    "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE item_name = %s",
                    (fifo_qty, item_name)
                )
                changes.append({"item_name": item_name, "old_quantity": old_qty, "new_quantity": fifo_qty, "drift": fifo_qty - old_qty})
        conn.commit()
        return changes
    finally:
        conn.close()


@router.get("/inventory/reconcile", response_model=GenericResponse, tags=["Inventory"], summary="Reconcile inventory.quantity from FIFO tracking")
async def api_reconcile_inventory(auth=Depends(verify_api_key)):
    """Recalculate inventory.quantity from authoritative FIFO stock_in/stock_out.
    
    inventory.quantity is a legacy summary column. The true stock is derived from
    stock_in - stock_out (FIFO). This endpoint syncs the summary column.
    """
    try:
        changes = reconcile_inventory_quantities()
        return ok({
            "message": f"Reconciled {len(changes)} items",
            "changes": changes
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  fetch_games / fetch_game_library
# ═══════════════════════════════════════

# ═══════════════════════════════════════
@router.get("/fetch_alltime_effective_rate", response_model=GenericResponse, tags=["Analytics"], summary="All-time average effective rate [MySQL]")
async def api_fetch_alltime_effective_rate(auth=Depends(verify_api_key)):
    """All-time average effective rate (Ks/min) from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT SUM(total_spend) as total_spend, SUM(balance_mins) as total_mins FROM member_wallets WHERE total_spend > 0")
        r = rows[0] if rows else {"total_spend": 0, "total_mins": 0}
        total_spend = float(r["total_spend"] or 0)
        total_mins = float(r["total_mins"] or 0)
        rate = round(total_spend / total_mins, 2) if total_mins > 0 else 0.0
        return ok({"alltime_effective_rate": rate, "total_spend": total_spend, "total_mins": total_mins})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_effective_rate
# ═══════════════════════════════════════

# ═══════════════════════════════════════
@router.get("/fetch_base_salaries", response_model=GenericResponse, tags=["Staff"], summary="Fetch base salaries [MySQL]")
async def api_fetch_base_salaries(auth=Depends(verify_api_key)):
    """Fetch base salaries from MySQL staff_records."""
    try:
        rows = _mysql_query("SELECT staff_name, base_salary, role FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        return ok({"salaries": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_attendance
# ═══════════════════════════════════════
@router.get("/fetch_attendance/{month_str}", response_model=GenericResponse, tags=["Staff"], summary="Fetch attendance [MySQL]")
async def api_fetch_attendance(month_str: str, auth=Depends(verify_api_key)):
    """Fetch attendance for a month from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT a.staff_id, s.staff_name, a.check_in, a.check_out, a.date, a.hours_worked, a.status, a.hourly_rate, a.daily_pay "
            "FROM attendance_log a LEFT JOIN staff_records s ON s.staff_id = a.staff_id "
            "WHERE DATE_FORMAT(a.date, %s)=%s ORDER BY a.date DESC",
            ("%Y-%m", month_str))
        return ok({"attendance": rows, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save_attendance", response_model=GenericResponse, tags=["Staff"], summary="Save attendance [MySQL]")
async def api_save_attendance(req: dict, auth=Depends(verify_api_key)):
    """Save attendance for a staff member (Staff App + bot).

    * check-in  — ``status`` in Present/Late (default Present) or ``action=check_in``
    * check-out — ``status`` in Check-Out/Checked Out/Done/Out or ``action=check_out``

    Returns the resulting today row so the app can render status without a second call.
    """
    try:
        staff_name = req.get("staff_name") or req.get("staff", "")
        status_val = req.get("status", "Present")
        action = str(req.get("action") or "").strip().lower()
        _status_lc = str(status_val or "").strip().lower()
        _is_checkout = action in ("check_out", "checkout", "out") or _status_lc in (
            "check-out", "checkout", "check_out", "checked out", "checked_out", "done", "out")
        # Resolve staff_id from staff_name (bot/app send the name string).
        staff_id = req.get("staff_id")
        if not staff_id and staff_name:
            rec = _mysql_query_one("SELECT staff_id FROM staff_records WHERE staff_name = %s", (staff_name,))
            staff_id = rec["staff_id"] if rec else None
        if not staff_id:
            raise HTTPException(status_code=400, detail=f"Unknown staff: {staff_name!r}")

        if _is_checkout:
            record = _mysql_query_one(
                "SELECT * FROM attendance_log WHERE staff_id = %s AND date = CURDATE() "
                "AND status IN ('checked_in','Present','Late') ORDER BY id DESC LIMIT 1", (staff_id,))
            if not record:
                return error_response(message="No open check-in found for today")
            hours_worked = 0.0
            daily_pay = 0.0
            try:
                _ci = record.get("check_in")
                if _ci:
                    _nowu = datetime.now(timezone.utc)
                    _ci_dt = _ci if hasattr(_ci, "timestamp") else datetime.fromisoformat(str(_ci).replace("Z", "+00:00"))
                    if _ci_dt.tzinfo is not None:
                        _ci_dt = _ci_dt.astimezone(timezone.utc).replace(tzinfo=None)
                    hours_worked = round((_nowu - _ci_dt).total_seconds() / 3600.0, 2)
                    daily_pay = round(hours_worked * float(record.get("hourly_rate") or 0), 2)
            except Exception as _ce:
                logger.warning("attendance check-out hours calc fallback: %s", _ce)
            _mysql_exec(
                "UPDATE attendance_log SET check_out = NOW(), hours_worked = %s, daily_pay = %s, "
                "status = 'checked_out' WHERE id = %s",
                (hours_worked, daily_pay, record["id"]))
            _row = _mysql_query_one("SELECT * FROM attendance_log WHERE id = %s", (record["id"],))
            return ok({"saved": True, "staff_id": staff_id, "action": "check_out",
                       "today": _safe_attendance_row(_row)})

        # Check-in / status update (upsert today's row).
        existing = _mysql_query_one(
            "SELECT id FROM attendance_log WHERE staff_id = %s AND date = CURDATE()", (staff_id,))
        if existing:
            _mysql_exec(
                "UPDATE attendance_log SET status = %s, check_in = COALESCE(check_in, NOW()) WHERE id = %s",
                (status_val, existing["id"]))
            _rid = existing["id"]
        else:
            _rid = _mysql_exec(
                "INSERT INTO attendance_log (staff_id, check_in, date, status) VALUES (%s, NOW(), CURDATE(), %s)",
                (staff_id, status_val))
        _row = _mysql_query_one("SELECT * FROM attendance_log WHERE id = %s", (_rid,))
        return ok({"saved": True, "staff_id": staff_id, "action": "check_in",
                   "today": _safe_attendance_row(_row)})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _safe_attendance_row(row):
    """JSON-safe projection of an attendance_log row."""
    if not row:
        return None
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None and not isinstance(v, (int, float, str, bool)) else v)
    return out


@router.get("/attendance/today", response_model=GenericResponse, tags=["Staff"], summary="Today's attendance row [MySQL]")
async def api_attendance_today(staff_name: str = "", staff_id: int = 0, auth=Depends(verify_api_key)):
    """Return today's attendance row for a staff member (by name or id), or null."""
    try:
        _sid = int(staff_id) if staff_id else None
        if not _sid and staff_name.strip():
            rec = _mysql_query_one("SELECT staff_id FROM staff_records WHERE staff_name = %s", (staff_name.strip(),))
            _sid = rec["staff_id"] if rec else None
        row = None
        if _sid:
            row = _mysql_query_one(
                "SELECT * FROM attendance_log WHERE staff_id = %s AND date = CURDATE() "
                "ORDER BY id DESC LIMIT 1", (_sid,))
        return ok({"today": _safe_attendance_row(row), "staff_id": _sid, "staff_name": staff_name.strip()})
    except Exception as e:
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  sales list-today (Mini App)
# ═══════════════════════════════════════
@router.get("/sales/list-today", response_model=GenericResponse, tags=["Sales"], summary="Today's sales list [MySQL]")
async def api_sales_list_today(auth=Depends(verify_api_key)):
    """Return today's sales records + total net for the Mini App."""
    try:
        import datetime as _dt
        _mmt_today = (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=6, minutes=30)))).strftime("%Y-%m-%d")
        _mmt_today_start = _mmt_today + " 00:00:00"
        rows = _mysql_query(
            "SELECT sd.id, sd.voucher_no, sd.sale_date, DATE_FORMAT(COALESCE(sd.created_at, sd.sale_date), '%%H:%%i') AS sale_time, "
            "sd.console_id, sd.member_id, "
            "COALESCE(NULLIF(w.member_name, ''), sd.customer_name) AS customer_name, "
            "sd.gross, sd.amount, sd.discount, sd.net, sd.staff_name, sd.payment_method, sd.notes "
            "FROM sales_daily sd "
            "LEFT JOIN member_wallets w ON w.member_id = sd.member_id AND sd.member_id NOT IN ('', '0', '0 (Guest)', '-', 'Guest') "
            "WHERE sd.sale_date >= %s ORDER BY sd.sale_date DESC, sd.id DESC LIMIT 200", (_mmt_today_start,))
        total_row = _mysql_query_one(
            "SELECT COALESCE(SUM(CASE WHEN is_topup=0 THEN net ELSE 0 END), 0) AS total, "
            "COALESCE(SUM(CASE WHEN is_topup=1 THEN net ELSE 0 END), 0) AS topup_total "
            "FROM sales_daily WHERE sale_date >= %s", (_mmt_today_start,))
        total_net = float(total_row["total"]) if total_row else 0.0
        topup_net = float(total_row["topup_total"]) if total_row else 0.0
        return ok({"records": rows, "total_net": total_net, "topup_net": topup_net,
                   "total_collected": total_net + topup_net, "count": len(rows), "date": _mmt_today})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_salary_advances
# ═══════════════════════════════════════
@router.get("/fetch_salary_advances/{month_str}", response_model=GenericResponse, tags=["Staff"], summary="Fetch salary advances [MySQL]")
async def api_fetch_salary_advances(month_str: str, auth=Depends(verify_api_key)):
    """Fetch salary advances for a month from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT staff_name, amount, advance_date, repayment_status, notes FROM salary_advance WHERE DATE_FORMAT(advance_date, %s)=%s ORDER BY advance_date DESC",
            ("%Y-%m", month_str))
        return ok({"advances": rows, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_promotions_cached
# ═══════════════════════════════════════
@router.get("/fetch_promotions_cached", response_model=GenericResponse, tags=["Promotions"], summary="Fetch promotions [MySQL]")
async def api_fetch_promotions_cached(auth=Depends(verify_api_key)):
    """Fetch active promotions from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT id, promo_name, discount_type, discount_value, start_date, end_date, status FROM promotions WHERE status='active'")
        return ok({"promotions": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_allowed_staff_ids
# ═══════════════════════════════════════
@router.get("/fetch_allowed_staff_ids", response_model=GenericResponse, tags=["Staff"], summary="Fetch allowed staff IDs [MySQL]")
async def api_fetch_allowed_staff_ids(auth=Depends(verify_api_key)):
    """Fetch allowed staff Telegram user IDs from MySQL settings."""
    try:
        val = _mysql_get_setting("allowed_staff_ids", "")
        if isinstance(val, str) and val.strip():
            ids = [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]
            return ok(ids)
        return ok([])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  next_voucher / next_member_id / next_member_row_no
# ═══════════════════════════════════════
@router.get("/next_member_id", tags=["Utility"], summary="Get next member ID [MySQL]")
async def api_next_member_id(auth=Depends(verify_api_key)):
    """Get next available member ID from MySQL."""
    try:
        rows = _mysql_query("SELECT MAX(CAST(REPLACE(member_id, 'PSV_A_', '') AS UNSIGNED)) as max_id FROM member_wallets")
        last = rows[0]["max_id"] if rows and rows[0]["max_id"] else 0
        return ok(f"PSV_A_{last + 1:03d}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/next_member_row_no", tags=["Utility"], summary="Get next member row [MySQL]")
async def api_next_member_row_no(auth=Depends(verify_api_key)):
    """Get next member row number from MySQL."""
    try:
        rows = _mysql_query("SELECT COUNT(*) as cnt FROM member_wallets")
        count = rows[0]["cnt"] if rows else 0
        return ok(count + 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/promotions/active", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_active(auth=Depends(verify_api_key)):
    try:
        rows = _mysql_query("SELECT * FROM promotions WHERE promo_type='cashback_coupon' AND start_date <= CURDATE() AND end_date >= CURDATE() ORDER BY id DESC LIMIT 1")
        if rows:
            row = rows[0]
            return ok({"promotion": {
                "id": row["id"],
                "name": row["name"],
                "promo_type": row["promo_type"],
                "start_date": str(row["start_date"]),
                "end_date": str(row["end_date"]),
                "coupon_expiry_date": str(row["coupon_expiry_date"])
            }})
        return ok({"promotion": None})
    except Exception as e:
        return error_response(message=str(e))



# ═══════════════════════════════════════


def _reverse_outstanding_forfeits(booking_id):
    """Safety net: reverse any outstanding forfeit injects for a booking before redeem.

    Restore paths already insert reversal ejects when deposit_status=='forfeited' at
    restore time, but if the booking was restored via a manual DB edit (bypassing
    code), the forfeit injects remain in cash_movements with no matching eject.
    This function catches that case at redeem time - idempotent: if already
    reversed, does nothing.

    Returns: (reversed_count, total_reversed_amount)
    """
    try:
        bk_str = str(booking_id)
        forfeit_pattern = f"Forfeit BK#{bk_str}%"

        # Get sum of forfeit injects
        inj_rows = _mysql_query(
            "SELECT COALESCE(SUM(amount),0) AS total FROM cash_movements "
            "WHERE movement_type='inject' AND note LIKE %s",
            (forfeit_pattern,)
        )
        inj_total = int(inj_rows[0]["total"] or 0) if inj_rows else 0
        if inj_total <= 0:
            return (0, 0)

        # Get sum of existing reversal ejects (both patterns)
        refund_pattern = f"Refund forfeit BK#{bk_str}%"
        reverse_pattern = f"%deposit reverse%BK#{bk_str}%"
        ej_rows = _mysql_query(
            "SELECT COALESCE(SUM(amount),0) AS total FROM cash_movements "
            "WHERE movement_type='eject' AND (note LIKE %s OR note LIKE %s)",
            (refund_pattern, reverse_pattern)
        )
        ej_total = int(ej_rows[0]["total"] or 0) if ej_rows else 0

        diff = inj_total - ej_total
        if diff <= 0:
            return (0, 0)

        # Get individual forfeit inject rows (may be multiple per-booking - e.g. partial)
        inj_details = _mysql_query(
            "SELECT id, account, amount FROM cash_movements "
            "WHERE movement_type='inject' AND note LIKE %s ORDER BY id",
            (forfeit_pattern,)
        )

        reversed_count = 0
        reversed_total = 0
        remaining_to_reverse = diff

        for row in inj_details:
            if remaining_to_reverse <= 0:
                break
            amt = int(row["amount"] or 0)
            if amt <= 0:
                continue
            account = row["account"] or "KPay"
            # Reverse at most the remaining diff (proportional handling for multi-inject)
            reverse_amt = min(amt, remaining_to_reverse)

            insert_cash_movement_if_absent(
                "eject",
                account,
                reverse_amt,
                f"Refund forfeit BK#{bk_str}%",
                f"Refund forfeit BK#{bk_str} (deposit reverse - redeemed, session played) via {account}",
                "system",
                "deposit_refund",
            )
            reversed_count += 1
            reversed_total += reverse_amt
            remaining_to_reverse -= reverse_amt

        logger.info(
            "_reverse_outstanding_forfeits BK#%s: reversed %s injects, total %s Ks (diff was %s Ks)",
            booking_id, reversed_count, reversed_total, diff
        )
        return (reversed_count, reversed_total)
    except Exception as e:
        logger.warning("_reverse_outstanding_forfeits BK#%s error (non-fatal): %s", booking_id, e)
        return (0, 0)


def _resolve_booking_from_console(console_id):
    """Resolve the single outstanding-deposit booking for a console at session end.

    Called when a sale payload carries no ``booking_id`` but does carry a console.
    Returns ``(booking_id, session_id)`` for the console's booking whose deposit is
    still outstanding (``verified``/``paid``, amount>0, no redemption row) and whose
    session just ended (``actual_end`` within the last ~6h in MMT) or is still
    active. Only ever returns a result for EXACTLY ONE candidate — ambiguity or
    zero candidates returns ``(None, None)`` so we never guess and never touch a
    wrong booking's money.
    """
    if not console_id:
        return (None, None)
    try:
        rows = _mysql_query(
            "SELECT cb.id AS booking_id, cb.deposit_amount, cs.id AS session_id, "
            "       cs.actual_end, cs.status AS session_status "
            "FROM console_bookings cb "
            "JOIN console_sessions cs ON cs.booking_id = cb.id "
            "WHERE cb.console_id = %s "
            "  AND cb.deposit_amount > 0 "
            "  AND cb.deposit_status IN ('verified','paid') "
            "  AND cb.booking_date = DATE(NOW() + INTERVAL 390 MINUTE) "
            "  AND (cs.status = 'active' "
            "       OR (cs.actual_end IS NOT NULL "
            "           AND cs.actual_end >= (NOW() + INTERVAL 390 MINUTE) - INTERVAL 6 HOUR)) "
            "  AND NOT EXISTS (SELECT 1 FROM deposit_redemptions dr WHERE dr.booking_id = cb.id) "
            "ORDER BY (cs.actual_end IS NULL) DESC, cs.actual_end DESC, cs.id DESC",
            (console_id,),
        )
        if not rows:
            return (None, None)
        if len(rows) > 1:
            ids = ", ".join(str(r["booking_id"]) for r in rows)
            logger.warning(
                "deposit auto-resolve AMBIGUOUS for console %s: %s candidates [%s] — no auto-redeem",
                console_id, len(rows), ids,
            )
            return (None, None)
        r = rows[0]
        return (r["booking_id"], r["session_id"])
    except Exception as e:
        logger.warning("deposit auto-resolve lookup failed for console %s: %s", console_id, e)
        return (None, None)


@router.post("/sales/record", response_model=GenericResponse, tags=["Sales"], summary="Save sales record [MySQL]")
async def api_sales_record(req: dict, request: Request, auth=Depends(verify_api_key)):
    """Save a sales record to MySQL sales_daily table (replaces direct GSheets write).

    Money path (Staff App Wave 2):
      * ``promo_id`` / ``coupon_code`` present -> discount + net are recomputed
        server-side via :func:`sale_money.compute_totals` (same function as
        ``/api/promotions/calc``), so the charged net matches the preview.
      * a verified booking deposit is redeemed **exactly once** server-side and
        deducted from the payable net unless the caller already folded it into
        ``payment_method`` (the sale bot's ``(dep)`` marker).
    """
    try:
        voucher_no = req.get("voucher_no", "")
        member_id = req.get("member_id", "")
        console_id = req.get("console_id", "")
        mins = req.get("mins", 0)
        base_rate = float(req.get("base_rate", 0))
        multiplier = float(req.get("multiplier", 1.0))
        game_amt = float(req.get("game_amt", 0))
        food_items = req.get("food_items", "")
        food_total = float(req.get("food_total", 0))
        discount = float(req.get("discount", 0))
        net_total = float(req.get("net_total", 0))
        payment_method = req.get("payment_method", "")
        staff = req.get("staff", "")
        created_at = req.get("created_at", "")
        notes = req.get("notes", "")
        promotion_name = req.get("promotion_name", "")
        coupon_code = req.get("coupon_code", "")
        booking_id = req.get("booking_id", "")
        client_ref = req.get("client_ref") or None

        # ── Idempotency guard (client_ref) ────────────────────────────────
        if client_ref:
            existing = _mysql_query_one(
                "SELECT voucher_no FROM sales_daily WHERE client_ref=%s",
                (client_ref,)
            )
            if existing:
                return ok({"success": True, "duplicate": True, "existing": True,
                          "voucher_no": existing.get("voucher_no", "")})

        if not voucher_no:
            return error_response(message="voucher_no required")

        # ── Audit identity: explicit staff field wins, else JWT identity ─────────
        if not staff:
            staff = staff_audit.identity_from_request(request, req)
        # Stable per-sale ref for idempotent money side-effects (coupon/deposit).
        sale_ref = client_ref or (voucher_no or None)

        # ── Single money path: server recompute when a promo/coupon is attached ──
        promo_id_in = str(req.get("promo_id") or "").strip()
        coupon_in = str(req.get("coupon_code") or "").strip()
        promo_breakdown = None
        coupon_redeem = None
        _calc = None
        if promo_id_in or coupon_in:
            try:
                _calc = _compute_totals(dict(req))
                discount = float(_calc["total_discount"])
                net_total = float(_calc["net_total"])
                if _calc.get("promo_title"):
                    promotion_name = _calc["promo_title"]
                if _calc.get("promo_id"):
                    req["_promo_id"] = _calc["promo_id"]
                promo_breakdown = _calc["breakdown"]
            except Exception as _ce:
                logger.warning("promo recompute failed voucher=%s: %s", voucher_no, _ce)

            # Redeem the coupon in the SAME money path (idempotent on sale_ref) so the
            # coupon balance actually charged matches the discount the client previewed.
            if (coupon_in and _calc and _calc.get("coupon_valid")
                    and _calc.get("coupon_minutes_used", 0) > 0):
                try:
                    from sale_money import redeem_coupon as _redeem_coupon
                    coupon_redeem = _redeem_coupon(
                        coupon_in, _calc["coupon_minutes_used"],
                        sale_ref=sale_ref, staff_name=staff)
                    logger.info("Coupon redeem in sale path: %s -> %s", coupon_in, coupon_redeem)
                except Exception as _cpe:
                    logger.warning("coupon redeem in sale path failed: %s", _cpe)

            # Record promo usage for reporting (best-effort, additive).
            if promo_id_in and _calc and _calc.get("promo_title"):
                try:
                    _mysql_exec(
                        "INSERT INTO promotions_log (voucher_no, promo_id, promo_title, member_id, "
                        "console_id, gross_total, discount_amt, net_total, staff_name, promo_date) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURDATE())",
                        (voucher_no, promo_id_in, _calc["promo_title"], member_id,
                         console_id, float(_calc["gross_total"]), float(_calc["total_discount"]),
                         float(_calc["net_total"]), staff))
                except Exception as _ple:
                    logger.warning("promotions_log insert failed: %s", _ple)

        # ── Deposit redemption (server-side, exactly once) ───────────────────────
        deposit_deducted = 0
        deposit_info = None
        resolved_session_id = None
        booking_auto_resolved = False
        # 2026-09-26 (owner): when the payload has no booking link but a console is
        # present, resolve the console's session-end booking server-side and redeem
        # its outstanding deposit. Exactly-one candidate only; never guess.
        if not booking_id and console_id:
            _rb, _rs = _resolve_booking_from_console(console_id)
            if _rb:
                booking_id = _rb
                resolved_session_id = _rs
                booking_auto_resolved = True
                logger.info(
                    "deposit auto-resolved BK#%s from console %s (voucher %s) session=%s",
                    booking_id, console_id, voucher_no, _rs,
                )
        if booking_id:
            try:
                _client_handled_dep = _dep_in_pm(payment_method)
                _dep = _get_verified_deposit(booking_id)
                # 2026-09-26 (owner): a booking-bound session end must also redeem a
                # SUBMITTED-but-unverified ('paid') deposit.
                if _dep.get("status") in ("verified", "paid") and _dep.get("amount", 0) > 0:
                    # Safety net: reverse any outstanding forfeit injects before redeeming
                    _reverse_outstanding_forfeits(booking_id)
                    _commit = _commit_deposit(
                        booking_id, sale_ref=sale_ref, voucher_no=voucher_no,
                        staff_name=staff, source="sale")
                    if _commit.get("redeemed"):
                        deposit_deducted = int(_commit.get("amount") or 0)
                        if not _client_handled_dep:
                            # Staff App path: server is the source of truth -> deduct from net
                            # (deposit was already recognised as income at verify time).
                            net_total = max(0.0, float(net_total) - deposit_deducted)
                            # 2026-09-28: include every live method (mmqr_cb was missing ->
                            # an MMQR-paid deposit was mislabelled KPay, understating MMQR by 6,000).
                            _dep_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay",
                                          "cash": "Cash", "mmqr_cb": "MMQR - CB"}.get(
                                (_dep.get("method") or "kpay"), None)
                            if _dep_label is None:  # unknown method -> use it verbatim, never KPay
                                _dep_label = str(_dep.get("method") or "KPay")
                            _marker = f"{_dep_label}:{deposit_deducted} (dep)"
                            payment_method = (payment_method + "|" + _marker) if payment_method else _marker
                            logger.info(
                                "Deposit redeemed server-side: BK#%s -%s Ks (sale_ref=%s, staff=%s, method=%s)",
                                booking_id, deposit_deducted, sale_ref, staff, _dep.get("method"))
                        else:
                            logger.info(
                                "Deposit redeem confirmed (client pre-deducted): BK#%s %s Ks",
                                booking_id, deposit_deducted)
                        deposit_info = {"booking_id": booking_id, "amount": deposit_deducted,
                                        "method": _dep.get("method"), "already": False}
                    else:
                        deposit_info = {"booking_id": booking_id,
                                        "amount": int(_commit.get("amount") or 0),
                                        "already": True, "reason": _commit.get("reason")}
                        logger.info("Deposit redeem no-op BK#%s: %s", booking_id, _commit.get("reason"))
            except Exception as dep_e:
                logger.warning("Deposit redemption check failed for BK#%s: %s", booking_id, dep_e)

        # Build enriched notes with extended fields
        extra_notes = []
        if promotion_name:
            extra_notes.append(f"Promotion: {promotion_name}")
        if coupon_code:
            extra_notes.append(f"Coupon: {coupon_code}")
        if deposit_deducted:
            extra_notes.append(f"DepositRedeemed: {deposit_deducted}")
        if sale_ref:
            extra_notes.append(f"SaleRef: {sale_ref}")
        if mins:
            extra_notes.append(f"Mins: {mins}")
        if base_rate:
            extra_notes.append(f"BaseRate: {base_rate}")
        if multiplier and multiplier != 1.0:
            extra_notes.append(f"Multiplier: {multiplier}")
        # ── Session food detail (2026-09-20) ──────────────────────────────────
        # Food ordered DURING a session lives in food_cart (keyed by booking_id) and the
        # app does not resend it with the sale, so the voucher + notes had no food detail.
        # Pull it from food_cart when the client sent none but a food total is present.
        if food_total > 0 and not food_items and booking_id:
            try:
                _fr = _mysql_query(
                    "SELECT item_name, quantity, unit_price FROM food_cart WHERE booking_id=%s ORDER BY id",
                    (booking_id,))
                if _fr:
                    food_items = [
                        {"name": r.get("item_name") or "", "qty": float(r.get("quantity") or 0),
                         "subtotal": float(r.get("quantity") or 0) * float(r.get("unit_price") or 0)}
                        for r in _fr
                    ]
            except Exception as _e:
                logger.warning("food_cart detail lookup failed (booking %s): %s", booking_id, _e)
        # Store the food as a readable "item xQty, item xQty" list (the voucher parses this).
        if isinstance(food_items, (list, tuple)):
            _parts = []
            for _it in food_items:
                if isinstance(_it, dict):
                    _nm = str(_it.get("name") or "").strip()
                    _q = _it.get("qty") or 0
                    if _nm:
                        # Include the unit price so the voucher can show cost per item
                        # (owner 2026-09-20 11:56): "name xQty @unit = subtotal".
                        _sub = float(_it.get("subtotal") or 0)
                        _unit = (_sub / float(_q)) if (float(_q or 0) > 0 and _sub > 0) else float(_it.get("unit_price") or _it.get("price") or 0)
                        if float(_q or 0) > 0 and _unit > 0:
                            _line_tot = _sub if _sub > 0 else _unit * float(_q)
                            _parts.append("%s x%d @%d = %d" % (_nm, int(float(_q)), int(round(_unit)), int(round(_line_tot))))
                        elif float(_q or 0) > 0:
                            _parts.append("%s x%d" % (_nm, int(float(_q))))
                        else:
                            _parts.append(_nm)
                else:
                    _p = str(_it).strip()
                    if _p:
                        _parts.append(_p)
            food_items = ", ".join(_parts)
        if food_items:
            extra_notes.append(f"Food: {food_items}")
        # ── Wallet balance deduction detail (owner ask 2026-09-20) ────────────
        # Member card sales: record how many mins left the wallet + the balance before/after
        # on the sale so the voucher and reports can show it in detail.
        _wallet_deduct = int(req.get("wallet_deduct", 0) or 0)
        _wallet_before = None
        _wallet_after = None
        # ── Attribution hardening (2026-09-26) ─────────────────────────────────
        # A member_id is only a *member* when a real member_wallets row exists for it.
        # Walk-in booker names must never drive wallet deduction, member_name lookups,
        # or member attribution. Resolve once and reuse for wallet + customer_name.
        _member_id_clean = str(member_id or "").strip()
        _is_guest_label = (not _member_id_clean) or _member_id_clean in ("", "0", "0 (Guest)", "-", "Guest")
        _member_wallet_row = None
        if not _is_guest_label:
            _member_wallet_row = _mysql_query_one(
                "SELECT member_id, member_name, COALESCE(balance_mins, 0) AS balance_mins FROM member_wallets WHERE member_id=%s",
                (_member_id_clean,))
        _is_member_sale = bool(_member_wallet_row)
        if _wallet_deduct > 0 and _is_member_sale:
            try:
                _wallet_before = int(float(_member_wallet_row.get("balance_mins") or 0))
                _wallet_after = max(0, _wallet_before - _wallet_deduct)
                _mysql_exec(
                    "UPDATE member_wallets SET balance_mins=%s, total_used_mins = COALESCE(total_used_mins, 0) + %s WHERE member_id=%s",
                    (_wallet_after, _wallet_deduct, _member_id_clean),
                )
                extra_notes.append(f"Wallet: -{_wallet_deduct} mins (bal {_wallet_before} -> {_wallet_after})")
                logger.info("Wallet deducted: %s mins for member %s (%s -> %s)", _wallet_deduct, _member_id_clean, _wallet_before, _wallet_after)
            except Exception as _we:
                logger.warning("wallet deduct detail failed (member %s): %s", _member_id_clean, _we)
        combined_notes = notes
        if extra_notes:
            combined_notes = (notes + " | " if notes else "") + " | ".join(extra_notes)

        sale_date = created_at if created_at else datetime.now(timezone(timedelta(hours=6, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
        gross = game_amt + food_total

        # Resolve customer_name from member_wallets ONLY for a real member (staff name
        # must NOT leak here). Walk-in/unknown member_ids preserve the supplied guest
        # name verbatim and never pull a name out of member_wallets.
        customer_name = req.get("customer_name") or ""
        if _is_member_sale and _member_wallet_row and _member_wallet_row.get("member_name"):
            customer_name = _member_wallet_row["member_name"]

        # ── Game attribution (2026-09-20): populate game_name + game_id at sale time ──
        # Resolved from an explicit hint / the linked session / the console's current
        # game. NULLs when unknown — the insert must never break on a missing game.
        _game_name, _game_id, _session_id = resolve_sale_game(
            console_id, session_id=req.get("session_id") or resolved_session_id,
            booking_id=booking_id, game_name_hint=req.get("game_name"))

        # Never persist a walk-in/unknown name as a member id (report pollution).
        _stored_member_id = member_id
        if not _is_member_sale and not _is_guest_label:
            _stored_member_id = ""
        _mysql_exec(
            "INSERT INTO sales_daily (voucher_no, sale_date, console_id, member_id, amount, gross, discount, net, staff_name, customer_name, payment_method, notes, client_ref, session_id, game_name, game_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (voucher_no, sale_date, console_id, _stored_member_id, game_amt, gross, discount, net_total, staff, customer_name, payment_method, combined_notes, client_ref, _session_id, _game_name, _game_id)
        )

        # Wallet balance was already deducted BEFORE the insert (wallet detail block above) so
        # the sale note could carry the before -> after balances. Never deduct twice here.
        if _wallet_deduct > 0 and _is_member_sale:
            logger.debug("wallet already deducted for voucher=%s member=%s", voucher_no, member_id)

        logger.info("Sales record saved: voucher=%s member=%s console=%s net=%s deposit_deducted=%s staff=%s", voucher_no, member_id, console_id, net_total, deposit_deducted, staff)
        return ok({
            "voucher_no": voucher_no,
            "success": True,
            "net_total": net_total,            # payable after any deposit deduction
            "payable_total": net_total,        # alias the Staff App can charge
            "discount": discount,
            "deposit_deducted": deposit_deducted,
            "deposit": deposit_info,
            "promotion_name": promotion_name or None,
            "coupon_code": coupon_code or None,
            "breakdown": promo_breakdown,
            "coupon_redeem": coupon_redeem,
            "sale_ref": sale_ref,
            "wallet_deduct": _wallet_deduct,
            "wallet_balance_before": _wallet_before,
            "wallet_balance_after": _wallet_after,
            "booking_auto_resolved": booking_auto_resolved,
            "resolved_booking_id": booking_id if booking_auto_resolved else None,
            "resolved_session_id": resolved_session_id,
        })
    except Exception as e:
        logger.error(f"api_sales_record: {e}")
        return error_response(str(e))


@router.post("/stock-out/log", response_model=GenericResponse, tags=["Stock"], summary="Log stock-out record [MySQL]")
async def api_stock_out_log(req: dict, auth=Depends(verify_api_key)):
    """Insert a stock-out record into stock_out table (replaces direct GSheets append)."""
    try:
        item_name = req.get("item_name", "")
        qty = int(req.get("qty", 0))
        unit_cost = float(req.get("unit_cost", 0))
        total_cost = float(req.get("total_cost", 0))
        staff = req.get("staff", "")
        notes = req.get("notes", "")
        created_at = req.get("created_at", "")

        if not item_name or qty <= 0:
            return error_response(message="item_name and qty > 0 required")

        sale_date = created_at if created_at else datetime.now(timezone(timedelta(hours=6, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")

        _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, staff_name, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (item_name, qty, unit_cost, total_cost, sale_date, staff, notes)
        )

        # Decrement inventory quantity
        _mysql_exec(
            "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
            (qty, item_name)
        )

        logger.info("Stock-out logged: item=%s qty=%s staff=%s", item_name, qty, staff)
        return ok({"item_name": item_name, "qty": qty, "success": True})
    except Exception as e:
        logger.error(f"api_stock_out_log: {e}")
        return error_response(str(e))


@router.post("/stock-in/log", response_model=GenericResponse, tags=["Stock"], summary="Log stock-in record [MySQL]")
async def api_stock_in_log(req: dict, auth=Depends(verify_api_key)):
    """Insert a stock-in record into stock_in table (replaces direct GSheets append).

    If payment_method is provided, also creates a corresponding cash_movements eject
    entry to keep the balance sheet accurate.
    """
    try:
        item_name = req.get("item_name", "")
        qty = int(req.get("qty", 0))
        unit_cost = float(req.get("unit_cost", 0))
        staff = req.get("staff", "")
        supplier = req.get("supplier", "")
        notes = req.get("notes", "")
        created_at = req.get("created_at", "")
        payment_method = req.get("payment_method", "")
        paid_by = req.get("paid_by", "")

        if not item_name or qty <= 0:
            return error_response(message="item_name and qty > 0 required")

        batch_id = f"IN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{item_name[:20].replace(' ', '_')}"

        total_amount = qty * unit_cost

        # Note: total_cost is STORED GENERATED column (= quantity * unit_cost), do not insert
        _mysql_exec(
            "INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (batch_id, item_name, qty, unit_cost, supplier or "Manual", notes or "", payment_method, paid_by, staff)
        )

        # Update inventory quantity
        _mysql_exec(
            "INSERT INTO inventory (item_name, category, quantity, unit_price, reorder_level, last_updated) "
            "VALUES (%s, %s, %s, %s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE quantity = quantity + VALUES(quantity), last_updated = NOW()",
            (item_name, "", qty, unit_cost, 0)
        )

        # 🆕 Create cash_movements eject entry if payment_method is provided
        if payment_method and payment_method.strip():
            eject_note = f"Stock In: {item_name} x{qty}"
            _mysql_exec(
                "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("eject", payment_method.strip(), total_amount, eject_note, staff)
            )
            logger.info("Stock-in eject created: account=%s amount=%s note=%s", payment_method, total_amount, eject_note)

        logger.info("Stock-in logged: batch=%s item=%s qty=%s payment=%s", batch_id, item_name, qty, payment_method or "N/A")
        return ok({"batch_id": batch_id, "item_name": item_name, "qty": qty, "success": True})
    except Exception as e:
        logger.error(f"api_stock_in_log: {e}")
        return error_response(str(e))


@router.post("/stock/in", response_model=GenericResponse, tags=["Stock"], summary="Stock-in purchase with payment deduction [MySQL]")
async def api_stock_in(req: dict, auth=Depends(verify_api_key)):
    """Record stock-in purchase AND deduct from payment account balance.

    Creates a stock_in record and corresponding cash_movements eject entry.
    Supports both single-payment (Cash/KPay) and split-payment (Cash/KPay combo).
    """
    try:
        item_name = req.get("item_name", "")
        quantity = int(req.get("quantity", 0))
        unit_cost = float(req.get("unit_cost", 0))
        payment_method = req.get("payment_method", "")  # display string, may be composite
        paid_by = req.get("paid_by", "")
        staff_name = req.get("staff_name", "")
        source = req.get("source", "")
        receipt_no = req.get("receipt_no", "")
        # Split-payment amounts (optional — used when payment_method is composite like "Cash X / KPay Y")
        cash_amount = req.get("cash_amount")
        kpay_amount = req.get("kpay_amount")

        if not item_name or quantity <= 0:
            return error_response(message="item_name and quantity > 0 required")

        import uuid
        batch_id = "SI-" + uuid.uuid4().hex[:12].upper()
        total_amount = quantity * unit_cost

        # ── Resolve target (owner 2026-09-28 Osmo): Stock-In only records
        # purchases of EXISTING items.  It never creates a new inventory row and
        # never touches inventory.unit_price (selling price is set in the Menu
        # Register).  Raw ingredients live as ingredients/stock rows and stay
        # purchasable; a truly unknown name is rejected with a clear error.
        inv = _mysql_query_one("SELECT id, quantity FROM inventory WHERE item_name=%s", (item_name,))
        if inv:
            _mysql_exec(
                "UPDATE inventory SET quantity = quantity + %s, last_updated = NOW() WHERE id = %s",
                (quantity, inv["id"])
            )
        else:
            ing = _mysql_query_one("SELECT id FROM ingredients WHERE name=%s", (item_name,))
            hist = _mysql_query_one("SELECT id FROM stock_in WHERE item_name=%s LIMIT 1", (item_name,))
            if not ing and not hist:
                return error_response(message=f"'{item_name}' is not registered yet — add it in Recipe & Cost first.")

        # Insert into stock_in
        _mysql_exec(
            """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (batch_id, item_name, quantity, unit_cost, source or "", receipt_no or "", payment_method, paid_by, staff_name)
        )

        # Create cash_movements entries to deduct from account balance
        note = f"Stock In: {item_name} x{quantity}"
        if cash_amount is not None and kpay_amount is not None:
            # Split payment: create separate eject entries for Cash and KPay
            if float(cash_amount) > 0:
                _mysql_exec(
                    """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("eject", "Cash", float(cash_amount), note, staff_name)
                )
            if float(kpay_amount) > 0:
                _mysql_exec(
                    """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("eject", "KPay", float(kpay_amount), note, staff_name)
                )
        elif payment_method:
            # Single payment: one eject entry for the payment method
            _mysql_exec(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                   VALUES (%s, %s, %s, %s, %s)""",
                ("eject", payment_method, total_amount, note, staff_name)
            )

        logger.info(
            "Stock-in via /api/stock/in: batch=%s item=%s qty=%s cost=%s payment=%s amount=%s split=Cash%s/KPay%s",
            batch_id, item_name, quantity, unit_cost, payment_method, total_amount,
            cash_amount or 0, kpay_amount or 0
        )
        return ok({
            "batch_id": batch_id,
            "item_name": item_name,
            "qty": quantity,
            "unit_cost": unit_cost,
            "total": total_amount,
            "payment_method": payment_method,
            "success": True
        })
    except Exception as e:
        logger.error(f"api_stock_in: {e}")
        return error_response(str(e))


@router.post("/stock/in/batch", response_model=GenericResponse, tags=["Stock"], summary="Multi-item stock-in (batch) with payment deduction [MySQL]")
async def api_stock_in_batch(req: dict, auth=Depends(verify_api_key)):
    """Multi-item stock-in: one request = one batch_id covering several items.

    Body: {"items": [{"item_name", "quantity", "unit_cost"}, ...],
           "payment_method", "staff_name", "paid_by", "source", "receipt_no",
           "cash_amount"?, "kpay_amount"?}
    """
    try:
        items = req.get("items") or []
        if not isinstance(items, list) or not items:
            return error_response(message="items[] (non-empty) required")

        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("item_name", "")).strip()
            try:
                qty = int(it.get("quantity", 0))
            except (TypeError, ValueError):
                qty = 0
            try:
                cost = float(it.get("unit_cost", 0))
            except (TypeError, ValueError):
                cost = 0
            if not name or qty <= 0:
                continue
            cleaned.append({"item_name": name, "quantity": qty, "unit_cost": cost})

        if not cleaned:
            return error_response(message="At least one valid item (name + qty>0) required")

        payment_method = req.get("payment_method", "")
        paid_by = req.get("paid_by", "")
        staff_name = req.get("staff_name", "")
        source = req.get("source", "")
        receipt_no = req.get("receipt_no", "")
        cash_amount = req.get("cash_amount")
        kpay_amount = req.get("kpay_amount")

        import uuid
        base_id = "SI-" + uuid.uuid4().hex[:12].upper()
        total_amount = sum(it["quantity"] * it["unit_cost"] for it in cleaned)

        # ── Resolve every target BEFORE inserting anything (owner 2026-09-28 Osmo):
        # Stock-In only records purchases of EXISTING items — it never creates a
        # new inventory row and never touches inventory.unit_price (the selling
        # price is set in Recipe & Cost).  Ingredients / stock-only items stay
        # purchasable; unknown names are rejected up front (no partial writes).
        unknown = []
        targets = []
        for it in cleaned:
            inv = _mysql_query_one("SELECT id, quantity FROM inventory WHERE item_name=%s", (it["item_name"],))
            if inv:
                targets.append({"item": it, "inv_id": inv["id"]})
                continue
            ing = _mysql_query_one("SELECT id FROM ingredients WHERE name=%s", (it["item_name"],))
            hist = _mysql_query_one("SELECT id FROM stock_in WHERE item_name=%s LIMIT 1", (it["item_name"],))
            if ing or hist:
                targets.append({"item": it, "inv_id": None})
            else:
                unknown.append(it["item_name"])
        if unknown:
            return error_response(message="Not registered — add these in Recipe & Cost first: " + ", ".join(unknown))

        # ── Insert stock_in rows (one per item, same batch base_id + seq suffix ──
        # stock_in.batch_id is UNIQUE → per-item suffix keeps grouping without collision
        added_rows = []
        for idx, t in enumerate(targets, start=1):
            it = t["item"]
            batch_id = f"{base_id}-{idx:02d}"
            if t["inv_id"]:
                _mysql_exec(
                    "UPDATE inventory SET quantity = quantity + %s, last_updated = NOW() WHERE id = %s",
                    (it["quantity"], t["inv_id"])
                )
            _mysql_exec(
                """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (batch_id, it["item_name"], it["quantity"], it["unit_cost"], source or "", receipt_no or "", payment_method, paid_by, staff_name)
            )
            added_rows.append({
                "item_name": it["item_name"],
                "qty": it["quantity"],
                "unit_cost": it["unit_cost"],
                "subtotal": it["quantity"] * it["unit_cost"],
            })

        # ── Payment deduction (aggregated; split Cash/KPay supported) ──
        note = f"Stock In (batch): {base_id}"
        if cash_amount is not None and kpay_amount is not None:
            if float(cash_amount) > 0:
                _mysql_exec(
                    """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("eject", "Cash", float(cash_amount), note, staff_name)
                )
            if float(kpay_amount) > 0:
                _mysql_exec(
                    """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("eject", "KPay", float(kpay_amount), note, staff_name)
                )
        elif payment_method and total_amount > 0:
            _mysql_exec(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                   VALUES (%s, %s, %s, %s, %s)""",
                ("eject", payment_method, total_amount, note, staff_name)
            )

        logger.info(
            "Stock-in batch via /api/stock/in/batch: batch=%s items=%d total=%s payment=%s split=Cash%s/KPay%s",
            base_id, len(cleaned), total_amount, payment_method, cash_amount or 0, kpay_amount or 0
        )
        return ok({
            "batch_id": base_id,
            "items": added_rows,
            "total": total_amount,
            "payment_method": payment_method,
            "success": True
        })
    except Exception as e:
        logger.error(f"api_stock_in_batch: {e}")
        return error_response(str(e))


@router.get("/stock/pick-items", response_model=GenericResponse, tags=["Stock"], summary="Item names selectable in Stock-In [MySQL]")
async def api_stock_pick_items(auth=Depends(verify_api_key)):
    """Distinct item names the Stock-In picker may select (owner 2026-09-28).

    = inventory (sellable menu items) + ingredients (raw) + prior stock_in names.
    Stock-In no longer creates items, so this is the full set of EXISTING items.
    """
    try:
        names = set()
        for r in _mysql_query("SELECT item_name FROM inventory"):
            names.add((r["item_name"] or "").strip())
        for r in _mysql_query("SELECT name FROM ingredients"):
            names.add((r["name"] or "").strip())
        for r in _mysql_query("SELECT DISTINCT item_name FROM stock_in"):
            names.add((r["item_name"] or "").strip())
        items = sorted(n for n in names if n)
        return ok({"items": items})
    except Exception as e:
        logger.error(f"api_stock_pick_items: {e}")
        return error_response(str(e))





# ── OPEX / Operating Expenses ─────────────────────────────────────────────────
@router.get("/opex/list", tags=["OPEX"], summary="List expenses")
async def api_opex_list(
    date_from: str = "", date_to: str = "",
    category: str = "", limit: int = 100,
    user: dict = Depends(get_staff_or_dashboard_user),
):
    from datetime import datetime as _dt, timedelta as _td
    import calendar
    _mmt_now = _dt.utcnow() + _td(hours=6, minutes=30)
    # Default to current month if no date filter provided
    if not date_from and not date_to:
        date_from = _mmt_now.strftime("%Y-%m-01")
        _last_day = calendar.monthrange(_mmt_now.year, _mmt_now.month)[1]
        date_to = _mmt_now.replace(day=_last_day).strftime("%Y-%m-%d")
    sql = "SELECT * FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    if category:
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY expense_date DESC, created_at DESC LIMIT %s"
    params.append(limit)
    rows = _mysql_query(sql, tuple(params))
    return {"success": True, "data": list(rows)}


@router.post("/opex/add", tags=["OPEX"], summary="Add expense")
async def api_opex_add(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    from datetime import datetime as _dt, timedelta as _td
    _mmt_now = _dt.utcnow() + _td(hours=6, minutes=30)
    cat = body.get("category", "").strip()
    desc = body.get("description", "").strip()
    amt = int(body.get("amount", 0))
    pmt = body.get("payment_method", "Cash").strip()
    by = body.get("recorded_by", "").strip()
    dt = body.get("expense_date", _mmt_now.strftime("%Y-%m-%d")).strip()
    if not cat or amt <= 0:
        return {"success": False, "error": "Category and amount required"}
    _mysql_exec("INSERT INTO opex (category,description,amount,payment_method,recorded_by,expense_date) VALUES (%s,%s,%s,%s,%s,%s)", (cat, desc, amt, pmt, by, dt))
    # Deduct from account balance if payment method matches an account name
    try:
        acc = _mysql_query_one("SELECT id, balance FROM accounts WHERE account_name = %s", (pmt,))
        if acc:
            new_bal = float(acc["balance"]) - amt
            _mysql_exec("UPDATE accounts SET balance = %s WHERE id = %s", (new_bal, acc["id"]))
    except Exception:
        pass
    return {"success": True, "data": {"msg": f"{cat}: {amt:,} Ks recorded"}}


@router.get("/opex/summary", tags=["OPEX"], summary="Expense summary by category + date")
async def api_opex_summary(
    date_from: str = "", date_to: str = "",
    user: dict = Depends(get_staff_or_dashboard_user),
):
    from datetime import datetime as _dt, timedelta as _td
    import calendar
    _mmt_now = _dt.utcnow() + _td(hours=6, minutes=30)
    # Default to current month if no date filter provided
    if not date_from and not date_to:
        date_from = _mmt_now.strftime("%Y-%m-01")
        _last_day = calendar.monthrange(_mmt_now.year, _mmt_now.month)[1]
        date_to = _mmt_now.replace(day=_last_day).strftime("%Y-%m-%d")
    sql = "SELECT category, SUM(amount) as total, COUNT(*) as count FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    sql += " GROUP BY category ORDER BY total DESC"
    rows = _mysql_query(sql, tuple(params)) if params else _mysql_query(sql)
    grand_total = sum(r["total"] for r in rows) if rows else 0
    return {"success": True, "data": {"categories": list(rows), "grand_total": grand_total}}





# ================================================================
#  WALLET — deduct/bonus (Sale Bot calls this when member plays)
# ================================================================


# ================================================================
#  SALE DRAFTS — shared draft vouchers (owner 2026-09-24)
#  Drafts were localStorage-only (per device), so a draft saved on
#  one staff phone was invisible to everyone else. Moved to MySQL
#  so all staff see the same parked vouchers.
# ================================================================
@router.get("/sales/drafts", response_model=GenericResponse, tags=["Sales"], summary="List shared draft vouchers [MySQL]")
async def api_sale_drafts_list(auth=Depends(verify_api_key)):
    try:
        # created_at is stored in UTC (DB session tz = UTC) but staff read Myanmar time,
        # so shift the draft timestamp to MMT (+06:30 = 390 min) before returning it.
        # Bug (Osmo 2026-09-26): Drafts showed "Saved 2026-09-26 08:17" (=UTC) instead
        # of the local 14:47.
        rows = _mysql_query(
            "SELECT id, staff_name, label, total, form_json, food_json, "
            "DATE_ADD(created_at, INTERVAL 390 MINUTE) AS created_at_mmt "
            "FROM sale_drafts ORDER BY id DESC")
        drafts = []
        for r in rows:
            d = {
                "id": r["id"],
                "staff_name": r.get("staff_name") or "",
                "label": r.get("label") or "",
                "total": float(r.get("total") or 0),
                "at": (str(r.get("created_at_mmt") or ""))[:16],
            }
            try:
                d["form"] = _json.loads(r["form_json"]) if r.get("form_json") else {}
            except Exception:
                d["form"] = {}
            try:
                d["food"] = _json.loads(r["food_json"]) if r.get("food_json") else []
            except Exception:
                d["food"] = []
            drafts.append(d)
        return ok({"drafts": drafts})
    except Exception as e:
        logger.error("sales/drafts list failed: %s", e)
        return error_response(message=str(e))


@router.post("/sales/drafts", response_model=GenericResponse, tags=["Sales"], summary="Save a shared draft voucher [MySQL]")
async def api_sale_drafts_save(req: dict, request: Request, auth=Depends(verify_api_key)):
    """Persist a parked sale voucher. Body: {label, total, form, food}.

    If ``draft_id`` is present the existing row is updated, else a new one is created.
    Returns the server draft id so the client can reconcile its local copy.
    """
    try:
        draft_id = req.get("draft_id") or req.get("id")
        label = (req.get("label") or "")[:255]
        try:
            total = float(req.get("total") or 0)
        except Exception:
            total = 0.0
        staff = staff_audit.identity_from_request(request, req)
        form_obj = req.get("form") if isinstance(req.get("form"), dict) else {}
        food_arr = req.get("food") if isinstance(req.get("food"), list) else []
        form_json = _json.dumps(form_obj, ensure_ascii=False)
        food_json = _json.dumps(food_arr, ensure_ascii=False)

        if draft_id:
            _mysql_exec(
                "UPDATE sale_drafts SET label=%s, total=%s, form_json=%s, food_json=%s, staff_name=%s WHERE id=%s",
                (label, total, form_json, food_json, staff, int(draft_id)))
            return ok({"draft_id": int(draft_id)})

        new_id = _mysql_exec(
            "INSERT INTO sale_drafts (label, total, form_json, food_json, staff_name) VALUES (%s,%s,%s,%s,%s)",
            (label, total, form_json, food_json, staff))
        return ok({"draft_id": new_id})
    except Exception as e:
        logger.error("sales/drafts save failed: %s", e)
        return error_response(message=str(e))


@router.delete("/sales/drafts/{draft_id}", response_model=GenericResponse, tags=["Sales"], summary="Delete a shared draft voucher [MySQL]")
async def api_sale_drafts_delete(draft_id: int, auth=Depends(verify_api_key)):
    try:
        _mysql_exec("DELETE FROM sale_drafts WHERE id=%s", (draft_id,))
        return ok({"deleted": draft_id})
    except Exception as e:
        logger.error("sales/drafts delete failed: %s", e)
        return error_response(message=str(e))
