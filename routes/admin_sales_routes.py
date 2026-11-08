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
    price_duration_2plus1 as _price_d2p,
)
import staff_audit
import logging, json as _json, os, re
from datetime import datetime, timezone, timedelta
import dish_consumption as _dish_consume
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Admin'])


def _pm_paid_total(pm) -> float:
    """Sum the amounts inside a payment-method string ("Cash:10000|KPay:5000")."""
    if not pm:
        return 0.0
    import re
    total = 0.0
    for part in re.split(r"[|/]", str(pm)):
        part = part.strip()
        if ":" in part:
            _, _, val = part.partition(":")
            try:
                total += float(val.strip() or 0)
            except ValueError:
                pass
    return total


def _record_sale_due(voucher_no, sale_ref, console_id, staff, net_total, paid, reason):
    """2026-10-30 (Osmo option 2): keep the shortfall as a Payment Due receivable."""
    try:
        due = round(float(net_total) - float(paid), 2)
        if due <= 0.5:
            return
        _mysql_exec(
            "CREATE TABLE IF NOT EXISTS sale_dues ("
            " id BIGINT AUTO_INCREMENT PRIMARY KEY,"
            " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " voucher_no VARCHAR(64) DEFAULT '', client_ref VARCHAR(80) DEFAULT '',"
            " console_id VARCHAR(32) DEFAULT '', staff_name VARCHAR(64) DEFAULT '',"
            " net_total DECIMAL(14,2) DEFAULT 0, paid_total DECIMAL(14,2) DEFAULT 0,"
            " due_amount DECIMAL(14,2) DEFAULT 0, reason VARCHAR(255) DEFAULT '',"
            " status VARCHAR(16) NOT NULL DEFAULT 'open',"
            " KEY idx_status (status), KEY idx_created (created_at)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        _mysql_exec(
            "INSERT INTO sale_dues (voucher_no, client_ref, console_id, staff_name,"
            " net_total, paid_total, due_amount, reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (str(voucher_no or "")[:64], str(sale_ref or "")[:80], str(console_id or "")[:32],
             str(staff or "")[:64], float(net_total or 0), float(paid or 0), due,
             str(reason or "unspecified")[:255]),
        )
        logger.warning("sale due recorded: %s short %.0f Ks (paid %.0f / net %.0f)",
                       voucher_no, due, paid, float(net_total or 0))
    except Exception as e:
        logger.warning("sale due record failed (%s): %s", voucher_no, e)


def _normalize_pm_to_net(pm, net_total):
    """Owner Osmo 2026-10-14 (option B): when the SERVER redeems a booking deposit the
    client's payment split still holds the PRE-deposit bill, so the recorded payment
    could exceed the net. Rescale the non-deposit parts so their sum == net_total.
    Deposit '(dep)' parts are preserved verbatim and appended last. Never raises."""
    try:
        net = float(net_total or 0)
        if net <= 0 or not pm:
            return pm
        import re as _re
        toks = [t.strip() for t in _re.split(r"[|/]", str(pm)) if t.strip()]
        keep, deps, bare = [], [], 0
        for t in toks:
            if "(dep)" in t.lower():
                deps.append(t)
                continue
            if ":" in t:
                m, _, v = t.partition(":")
                try:
                    amt = float(v.strip() or "0")
                except ValueError:
                    amt = 0.0
                keep.append([m.strip(), amt])
            else:
                keep.append([t, None])  # bare method (no explicit amount)
                bare += 1
        if bare:
            if len(keep) == 1 and bare == 1:
                keep = [[keep[0][0], net]]   # single bare method = the whole net
            else:
                return pm                     # ambiguous → leave the pm untouched
        _s = sum(a for _m, a in keep)
        if _s > 0 and abs(_s - net) > 0.5:
            _f = net / _s
            scaled = [[m, round(a * _f)] for m, a in keep]
            _diff = int(round(net - sum(a for _m, a in scaled)))
            if scaled:
                scaled[0][1] = int(scaled[0][1] + _diff)
            keep = scaled
        out = [f"{m}:{int(round(a))}" for m, a in keep]
        out += deps
        return "|".join(out)
    except Exception:
        return pm

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
        sid = _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (item_name, qty, unit_price, subtotal, sale_date, f"Voucher: {voucher_no}")
        )
        # Dish → consume recipe ingredients FIFO (idempotent, best-effort)
        _dish_consume.consume_after_stock_out(item_name, qty, sid, voucher_no=voucher_no or None)

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
        _MMT = _dt.timezone(_dt.timedelta(hours=6, minutes=30))
        _mmt_today = (_dt.datetime.now(_MMT)).strftime("%Y-%m-%d")
        _mmt_today_start = _mmt_today + " 00:00:00"
        # 🐛 2026-10-25 Osmo: "Deposit/refund က UTC နဲ့ ဖြစ်နေတယ". MySQL runs in UTC, so
        # cash_movements.created_at is UTC while everything else (sales_daily,
        # console_sessions) is MMT. Build the UTC bounds that correspond to the
        # MMT day so the Today filter and the displayed time are both correct.
        _utc_today_start = (_dt.datetime.strptime(_mmt_today + " 00:00:00", "%Y-%m-%d %H:%M:%S")
                            - _dt.timedelta(minutes=390)).strftime("%Y-%m-%d %H:%M:%S")
        _utc_tomorrow_start = (_dt.datetime.strptime(_mmt_today + " 00:00:00", "%Y-%m-%d %H:%M:%S")
                               + _dt.timedelta(days=1, minutes=-390)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _mysql_query(
            "SELECT sd.id, sd.voucher_no, sd.sale_date, DATE_FORMAT(COALESCE(sd.created_at, sd.sale_date), '%%H:%%i') AS sale_time, "
            "sd.console_id, sd.member_id, "
            "COALESCE(NULLIF(w.member_name, ''), sd.customer_name) AS customer_name, "
            "sd.gross, sd.amount, sd.discount, sd.net, sd.staff_name, sd.payment_method, sd.notes, sd.billable_mins, sd.free_mins, sd.is_topup "
            "FROM sales_daily sd "
            "LEFT JOIN member_wallets w ON w.member_id = sd.member_id AND sd.member_id NOT IN ('', '0', '0 (Guest)', '-', 'Guest') "
            "WHERE sd.sale_date >= %s ORDER BY sd.sale_date DESC, sd.id DESC LIMIT 200", (_mmt_today_start,))
        total_row = _mysql_query_one(
            "SELECT COALESCE(SUM(CASE WHEN is_topup=0 THEN net ELSE 0 END), 0) AS total, "
            "COALESCE(SUM(CASE WHEN is_topup=1 THEN net ELSE 0 END), 0) AS topup_total "
            "FROM sales_daily WHERE sale_date >= %s", (_mmt_today_start,))
        total_net = float(total_row["total"]) if total_row else 0.0
        topup_net = float(total_row["topup_total"]) if total_row else 0.0
        # 💳 Wallet-covered member plays (owner Osmo 2026-10-14): a card session
        # collects no cash for the game, so sales_daily.net holds only food/extras
        # and the wallet value never reached "Today's Sales". Count the value of the
        # wallet minutes consumed — valued at the member's OWN purchase rate
        # (amount paid ÷ minutes bought in topup_log), NOT the cash base rate.
        # The deducted minutes are in notes ("Wallet: -180 mins (bal 600 -> 420)").
        import re as _re
        wallet_net = 0.0
        try:
            _wallet_rows = _mysql_query(
                "SELECT member_id, billable_mins, notes FROM sales_daily "
                "WHERE sale_date >= %s AND is_topup=0 AND notes LIKE '%%Wallet: -%%'",
                (_mmt_today_start,))
            _rate_map = {}
            for _r in _mysql_query("SELECT member_id, SUM(amount) AS amt, SUM(mins_added) AS mins FROM topup_log WHERE mins_added > 0 GROUP BY member_id"):
                _m = float(_r.get("mins") or 0)
                if _m > 0:
                    _rate_map[str(_r.get("member_id"))] = float(_r.get("amt") or 0) / _m
            for _r in _wallet_rows:
                _note = str(_r.get("notes") or "")
                _mm = _re.search(r"Wallet:\s*-(\d+)\s*mins", _note)
                _mins = float(_mm.group(1)) if _mm else float(_r.get("billable_mins") or 0)
                _rate = _rate_map.get(str(_r.get("member_id"))) or 0.0
                wallet_net += _mins * _rate
            wallet_net = round(wallet_net)
        except Exception as _we:
            logger.warning("wallet_net calc failed: %s", _we)
            wallet_net = 0.0
        # 💰 Deposit / refund / forfeit legs (owner Osmo 2026-10-24: "Deposit/refund
        # တွေက trx ပျောက်နေတယ် အဲ့တာတွေပါ today sale ထဲ ပြပေးပါ"). These live in
        # cash_movements (category 'deposit' | 'deposit_refund' | 'deposit_forfeit'),
        # NOT in sales_daily — so the Staff App Today list never showed them.
        # They are appended as read-only rows (tx_kind set) sorted by time.
        deposit_net = 0.0
        refund_net = 0.0
        try:
            _moves = _mysql_query(
                "SELECT id, movement_type, account, amount, note, staff_name, created_at, category "
                "FROM cash_movements WHERE created_at >= %s AND created_at < %s "
                "AND category IN ('deposit','deposit_refund') "
                # Owner Osmo 2026-10-24 22:21: forfeit + rebook rows are noise here
                # ("Forfeit နဲ့ rebook တော့ ဖြုတ်ပေးပါ") — a forfeited booking that is
                # later reactivated writes BOTH a forfeit and a 'Refund forfeit …
                # (reactivated)' row, so the pair only confuses the Today list.
                "AND note NOT LIKE '%%forfeit%%' AND note NOT LIKE '%%reactivat%%' "
                "AND note NOT LIKE '%%rebook%%' "
                "ORDER BY created_at DESC, id DESC LIMIT 200",
                (_utc_today_start, _utc_tomorrow_start))
            for _mv in _moves:
                _cat = str(_mv.get("category") or "")
                _amt = float(_mv.get("amount") or 0)
                _kind = {"deposit": "deposit", "deposit_refund": "refund",
                         "deposit_forfeit": "forfeit"}.get(_cat, "deposit")
                # cash_movements stores ejects as negative already; keep the sign.
                _net = abs(_amt) if _kind == "deposit" else -abs(_amt)
                if _kind == "deposit":
                    deposit_net += abs(_amt)
                else:
                    refund_net += abs(_amt)
                _ct_raw = _mv.get("created_at")
                # created_at is UTC → shift to MMT for display
                _ct = (_ct_raw + _dt.timedelta(minutes=390)) if hasattr(_ct_raw, "strftime") else _ct_raw
                rows.append({
                    "id": "cm-%s" % _mv.get("id"),
                    "voucher_no": (str(_mv.get("note") or "").split(" via ")[0].strip() or ("Deposit" if _kind == "deposit" else _kind.title())),
                    "sale_date": _ct.strftime("%Y-%m-%d") if hasattr(_ct, "strftime") else _mmt_today,
                    "sale_time": _ct.strftime("%H:%M") if hasattr(_ct, "strftime") else "",
                    "console_id": "", "member_id": "-", "customer_name": "",
                    "gross": _net, "amount": _net, "discount": 0, "net": _net,
                    "staff_name": _mv.get("staff_name") or "",
                    "payment_method": _mv.get("account") or "",
                    "notes": _mv.get("note") or "", "billable_mins": 0, "free_mins": 0,
                    "tx_kind": _kind,
                })
        except Exception as _de:
            logger.warning("deposit/refund rows failed: %s", _de)
        return ok({"records": rows, "total_net": total_net, "topup_net": topup_net,
                   "wallet_net": wallet_net, "deposit_net": deposit_net, "refund_net": refund_net,
                   "total_collected": total_net + topup_net + wallet_net, "count": len(rows), "date": _mmt_today})
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
            "SELECT id, name, promo_name, discount_type, discount_value, start_date, end_date, status FROM promotions WHERE status='active'")
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


# ── 💰 Tender-vs-payable guard helpers (2026-10-16, Osmo) ──────────────────────
# Voucher V-WEB-20261016121214-478 showed a cash due of 20,000 Ks (the full
# billable time, wallet NOT netted) instead of the real 500 Ks shortfall; the
# staff entered what the screen showed and the sale was stored net 500 / paid
# 20,000.  Any tender above the payable is now blocked server-side.
_INTEGRITY_SPOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                                "sale_integrity_alerts.jsonl")


def _pm_tender_total(payment_method):
    """Cash actually tendered, summed from a ``payment_method`` string.

    Handles ``Cash:10000|MMQR - CB:10000`` and ``Kpay:0/Cash:0/AYA:0`` and skips
    deposit legs (any part containing ``(`` — e.g. ``MMQR - CB:7000 (dep)``,
    which is already netted out of the payable by the deposit path).
    Returns ``None`` when the string carries no amounts at all (top-ups,
    new-member rows, plain ``MMQR - CB``).
    """
    if not payment_method:
        return None
    total = 0.0
    found = False
    for chunk in re.split(r"[|/]", str(payment_method)):
        part = chunk.strip()
        if not part:
            continue
        # strip '(dep)' / '(DepositRedeemed)' markers instead of skipping the part,
        # otherwise deposit-settled sales under-count in the integrity check.
        part = re.sub(r"\([^)]*\)", "", part).strip()
        if not part:
            continue
        m = re.search(r":\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*$", part)
        if not m:
            continue
        try:
            total += float(m.group(1).replace(",", ""))
            found = True
        except ValueError:
            continue
    return total if found else None


def _spool_integrity_alert(**payload):
    """Append one JSON line to the integrity spool (read by the daily re-check)."""
    try:
        payload.setdefault("at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        os.makedirs(os.path.dirname(os.path.abspath(_INTEGRITY_SPOOL)), exist_ok=True)
        with open(_INTEGRITY_SPOOL, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as _e:  # never break a sale on logging
        logger.warning("integrity spool write failed: %s", _e)


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
        # ⏱️ 2026-11-05 (Osmo): the staff app sends the ±10 "Adjust" from the
        # session-end voucher. Keep the explicit delta so the wallet clamp and the
        # 2+1 re-pricing honour the staff correction instead of silently reverting
        # to the raw session minutes (voucher used to print the ORIGINAL mins).
        try:
            mins_adjust = int(float(req.get("mins_adjust") or 0))
        except (TypeError, ValueError):
            mins_adjust = 0
        mins_adjust = max(-10, min(10, mins_adjust))
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

        # ── Idempotency guard (voucher_no) — added 2026-10-09 ─────────────
        # The staff webapp generates ONE voucher per sale (V-WEB-…) and reuses it
        # on every retry, while client_ref is regenerated per submit attempt; so a
        # retried submit inserted duplicate rows (2026-10-09: 11 rows for
        # V-WEB-20261009185359-355). Dedupe on the stable voucher_no.
        if voucher_no:
            _dup_v = _mysql_query_one(
                "SELECT voucher_no FROM sales_daily WHERE voucher_no=%s LIMIT 1",
                (voucher_no,)
            )
            if _dup_v:
                return ok({"success": True, "duplicate": True, "existing": True,
                           "voucher_no": _dup_v.get("voucher_no", "")})

        # ── Audit identity: explicit staff field wins, else JWT identity ─────────
        if not staff:
            staff = staff_audit.identity_from_request(request, req)
        # Stable per-sale ref for idempotent money side-effects (coupon/deposit).
        sale_ref = client_ref or (voucher_no or None)

        # ── Duration 2+1 (Weekday Happy Hour) — auto-bind at checkout ────────
        # Server-side re-pricing. Applies only when an ACTIVE duration_2plus1 promo
        # is in its date window AND the session STARTED Mon–Fri 09:00 ≤ t < 15:00
        # (MMT). Extension/top-up minutes count (start time is the criterion).
        # Any error here logs and falls through to the normal (unpromoted) path —
        # checkout must never break.
        _d2p_applied = None
        try:
            from sale_money import (
                load_active_duration_promo as _load_d2p,
                compute_duration_2plus1 as _compute_d2p,
            )
            _promo_row = _load_d2p()
            if _promo_row:
                _sess_row = None
                _sid_in = req.get("session_id")
                if _sid_in not in (None, "", "0", 0):
                    try:
                        _sess_row = _mysql_query_one(
                            "SELECT id, actual_start, actual_end, actual_duration_mins, status, TIMESTAMPDIFF(MINUTE, actual_start, CASE WHEN status='active' THEN NOW() + INTERVAL 390 MINUTE ELSE actual_end END) AS played_mins FROM console_sessions WHERE id=%s",
                            (int(_sid_in),))
                    except (TypeError, ValueError):
                        _sess_row = None
                if not _sess_row and booking_id not in (None, "", 0, "0"):
                    _sess_row = _mysql_query_one(
                        "SELECT id, actual_start, actual_end, actual_duration_mins, status, TIMESTAMPDIFF(MINUTE, actual_start, CASE WHEN status='active' THEN NOW() + INTERVAL 390 MINUTE ELSE actual_end END) AS played_mins FROM console_sessions "
                        "WHERE booking_id=%s ORDER BY id DESC LIMIT 1", (booking_id,))
                if not _sess_row and console_id:
                    _sess_row = _mysql_query_one(
                        "SELECT id, actual_start, actual_end, actual_duration_mins, status, TIMESTAMPDIFF(MINUTE, actual_start, CASE WHEN status='active' THEN NOW() + INTERVAL 390 MINUTE ELSE actual_end END) AS played_mins FROM console_sessions "
                        "WHERE console_id=%s AND status='active' ORDER BY id DESC LIMIT 1", (console_id,))
                if _sess_row and _sess_row.get("actual_start"):
                    # ⚠️ 2026-10-15 (Osmo/Kai): the 2+1 tier MUST use the ACTUAL played
                    # minutes from the request (same basis as the billed game amount).
                    # console_sessions.actual_duration_mins holds the ALLOCATED/planned
                    # duration — using it priced a 75-min session as 180 min
                    # (billable 120 = 24,000 Ks instead of 15,000). Fallback only.
                    _T = int(_sess_row.get("played_mins") or 0)   # actual play (end-start)
                    if _T <= 0:
                        _T = int(float(mins or 0) or 0)
                    if _T <= 0:
                        _T = int(_sess_row.get("actual_duration_mins") or 0)
                    # Explicit staff adjust (±10) wins over the session-derived value.
                    if mins_adjust and _T > 0:
                        _T = max(0, _T + mins_adjust)
                    _d2p = _compute_d2p(_sess_row.get("actual_start"), _T, _promo_row)
                    if _d2p:
                        _B = _d2p["billable_mins"]
                        _F = _d2p["free_mins"]
                        # Console multiplier + base rate: a discounted session prices
                        # EXACTLY like a normal session of billable_mins. base_rate
                        # comes from the request, falling back to the base_rate SETTING
                        # (10,000) — never a hardcoded 500.0.
                        _mult = float(req.get("multiplier") or 1.0) or 1.0
                        _base_rate = float(req.get("base_rate") or 0) or float(
                            _mysql_get_setting("base_rate", 10000) or 0) or 10000.0
                        # Member wallet balance in SCALED minutes (None = walk-in/guest).
                        _mid = str(member_id or "").strip()
                        _bal = None
                        if _mid and _mid not in ("", "0", "0 (Guest)", "-", "Guest"):
                            _w = _mysql_query_one(
                                "SELECT balance_mins FROM member_wallets WHERE member_id=%s", (_mid,))
                            _bal = int(_w.get("balance_mins") or 0) if _w else None
                        _price = _price_d2p(_B, _base_rate, _mult, _bal)
                        _wallet_mins = _price["wallet_mins"]        # SCALED minutes deducted
                        _cash_mins = _price["cash_mins"]            # UNSCALED billable minutes in cash
                        _cash_ks = _price["cash_ks"]
                        # compute_totals prices cash as `cash_mins × base_rate × multiplier / 60`
                        # (UNSCALED cash minutes, multiplier applied there) and the wallet as
                        # `wallet_mins × base_rate / 60` (SCALED minutes). The promo only changes
                        # the MINUTES, never the rate.
                        req["duration_2plus1"] = {
                            "promo_id": _d2p["promo_id"],
                            "title": _d2p["title"],
                            "total_mins": _T,
                            "billable_mins": _B,
                            "free_mins": _F,
                            "cash_mins": _cash_mins,
                            "wallet_mins": _wallet_mins,
                        }
                        req["promo_id"] = str(_d2p["promo_id"])
                        req["base_rate"] = _base_rate
                        req["multiplier"] = _mult
                        req["mins"] = _T
                        mins = _T
                        base_rate = _base_rate
                        multiplier = _mult
                        game_amt = float(_cash_ks)
                        if _wallet_mins > 0:
                            req["is_member"] = True
                            req["wallet_mins"] = _wallet_mins
                            req["effective_cost_mins"] = _wallet_mins
                            req["wallet_deduct"] = _wallet_mins
                            if _cash_mins > 0:
                                # Wallet insufficient for the full billable tier → the
                                # uncovered billable minutes fall back to cash game_amt.
                                req["is_member"] = False
                                req["wallet_mins"] = 0
                                req["game_amt"] = _cash_ks
                                req["gross_total"] = _cash_ks + float(food_total)
                        else:
                            req["wallet_deduct"] = 0
                            req["game_amt"] = _cash_ks
                            req["gross_total"] = _cash_ks + float(food_total)
                        _d2p_applied = {
                            "promo_id": _d2p["promo_id"],
                            "session_id": _sess_row.get("id"),
                            "total_mins": _T,
                            "billable_mins": _B,
                            "free_mins": _F,
                        }
                        logger.info(
                            "2+1 promo applies voucher=%s session=%s T=%sm billable=%sm free=%sm",
                            voucher_no, _sess_row.get("id"), _T, _B, _F)
        except Exception as _d2p_e:
            logger.warning("2+1 promo checkout hook error (falling through): %s", _d2p_e)
            _d2p_applied = None

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

        # ── 🎮 Extra controller rental (owner Osmo 2026-10-25) ─────────────────
        # A console includes N controllers (setting included_controllers, default 2).
        # Every controller BEYOND that is chargeable at extra_controller_rate
        # (default 3,000 Ks/hour), prorated PER MINUTE, CASH ONLY (never wallet).
        # Accepts extra_ctrls (TOTAL controllers incl. the free ones) or
        # extra_ctrl_qty (already-net extra count).
        extra_ctrl_qty = 0
        extra_ctrl_mins = 0
        extra_ctrl_amount = 0.0
        _ctrl_rate = 3000.0
        try:
            _incl = int(float(_mysql_get_setting("included_controllers", 2) or 2))
            _ctrl_rate = float(_mysql_get_setting("extra_controller_rate", 3000) or 3000)
            _tot_in = req.get("extra_ctrls")
            _qty_in = req.get("extra_ctrl_qty")
            if _tot_in not in (None, "", 0, "0", "0.0"):
                extra_ctrl_qty = max(0, int(float(_tot_in)) - _incl)
            elif _qty_in not in (None, "", 0, "0", "0.0"):
                extra_ctrl_qty = max(0, int(float(_qty_in)))
            _mins_in = req.get("extra_ctrl_mins")
            if _mins_in not in (None, "", 0, "0", "0.0"):
                extra_ctrl_mins = max(0, int(float(_mins_in)))
            elif mins:
                extra_ctrl_mins = max(0, int(float(mins)))
            if extra_ctrl_qty > 0 and extra_ctrl_mins > 0:
                extra_ctrl_amount = round(extra_ctrl_qty * (extra_ctrl_mins / 60.0) * _ctrl_rate)
                net_total += extra_ctrl_amount
                logger.info("extra controllers: qty=%s mins=%s rate=%s -> %s Ks (voucher %s)",
                            extra_ctrl_qty, extra_ctrl_mins, _ctrl_rate, extra_ctrl_amount, voucher_no)
        except Exception as _ec:
            logger.warning("extra controller calc failed (voucher %s): %s", voucher_no, _ec)

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
                            # 🚨 Owner Osmo 2026-10-14 (option B): the client's split still
                            # holds the PRE-deposit bill → rescale its non-deposit parts
                            # to the new net so the recorded payment never exceeds it.
                            payment_method = _normalize_pm_to_net(payment_method, net_total)
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
        if _d2p_applied:
            extra_notes.append(
                f"2+1 Promo: billable {_d2p_applied['billable_mins']}m / free {_d2p_applied['free_mins']}m")
        if coupon_code:
            extra_notes.append(f"Coupon: {coupon_code}")
        if deposit_deducted:
            extra_notes.append(f"DepositRedeemed: {deposit_deducted}")
        # Owner 2026-11-08: a settled sale must release the food holds for that
        # booking, otherwise held rows pile up forever (2 were already stale).
        try:
            if booking_id:
                _mysql_exec(
                    "UPDATE stock_hold SET status='released', updated_at=NOW() "
                    "WHERE booking_id=%s AND status='held'", (booking_id,))
        except Exception as _she:
            logger.warning("stock hold release on sale failed (bk %s): %s", booking_id, _she)
        if sale_ref:
            # note the human voucher number; client_ref keeps its "sale:" prefix for
            # idempotency but the printed/sales note must not read "sale:V-WEB-..."
            extra_notes.append(f"SaleRef: {voucher_no or str(sale_ref).replace('sale:', '')}")
        if mins:
            extra_notes.append(f"Mins: {mins}")
        if base_rate:
            extra_notes.append(f"BaseRate: {base_rate}")
        if extra_ctrl_qty > 0:
            extra_notes.append(
                f"ExtraCtrl: {extra_ctrl_qty} x {extra_ctrl_mins}min @{int(_ctrl_rate)} = {int(extra_ctrl_amount)}")
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
        # ⏱️ Clamp to the session's ACTUAL minutes (owner 2026-10-25). The app used to
        # send the PLANNED block (60) for a 26-minute session, so the member was
        # charged 60 mins. Never let the wallet be debited more than was played.
        if _wallet_deduct > 0 and _is_member_sale:
            try:
                _w_sid = req.get("session_id")
                _w_row = None
                if _w_sid not in (None, "", "0", 0):
                    _w_row = _mysql_query_one(
                        "SELECT TIMESTAMPDIFF(MINUTE, actual_start, COALESCE(actual_end, (NOW() + INTERVAL 390 MINUTE))) AS m "
                        "FROM console_sessions WHERE id=%s", (int(_w_sid),))
                elif booking_id not in (None, "", 0, "0"):
                    _w_row = _mysql_query_one(
                        "SELECT TIMESTAMPDIFF(MINUTE, actual_start, COALESCE(actual_end, (NOW() + INTERVAL 390 MINUTE))) AS m "
                        "FROM console_sessions WHERE booking_id=%s ORDER BY id DESC LIMIT 1", (booking_id,))
                if _w_row and _w_row.get("m") is not None:
                    _w_actual = max(0, int(_w_row["m"]))
                    # Allow the staff's explicit positive adjust (≤ +10) on top of the
                    # actual play time; anything larger is still a mis-send and clamped.
                    _w_cap = int(round((_w_actual + max(0, mins_adjust)) * (float(req.get("multiplier") or 1) or 1)))
                    if _w_actual > 0 and _wallet_deduct > _w_cap + 2:
                        logger.warning("wallet deduct clamped to ACTUAL minutes: %s -> %s (session %s, voucher %s)",
                                       _wallet_deduct, _w_cap, _w_sid, voucher_no)
                        _wallet_deduct = _w_cap
            except Exception as _wce:
                logger.warning("wallet actual-min clamp skipped: %s", _wce)
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
        gross = game_amt + food_total + extra_ctrl_amount

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

        # 2026-10-13 (forfeit->rebook link hardening, Osmo): a booking-bound sale MUST
        # carry the booking's session id so reconciliation can resolve its voucher
        # (Tier1: sales_daily.session_id -> console_sessions.booking_id). resolve_sale_game
        # only resolves the session when no game_name hint is supplied; enforce it here.
        if not _session_id and booking_id not in (None, "", 0, "0"):
            try:
                _bs = _mysql_query_one(
                    "SELECT id FROM console_sessions WHERE booking_id=%s ORDER BY id DESC LIMIT 1",
                    (booking_id,))
                if _bs:
                    _session_id = _bs.get("id")
                    logger.info("sale session link backfilled: booking=%s session=%s voucher=%s",
                                booking_id, _session_id, voucher_no)
            except Exception as _bse:
                logger.warning("session link backfill failed booking=%s: %s", booking_id, _bse)

        # ── 🎮 Session fallback (2026-10-25, owner: "Save sale လုပ်လိုက်တာ မပေါ်လာဘူး") ──
        # A sale saved from the app sometimes arrives WITHOUT session_id (console-picked
        # voucher / older bundle). The controller-rental ledger is then never settled, so
        # net_total stays 0 and the tender guard blocks a legitimate controller payment:
        #   "OVER-PAYMENT BLOCKED ... tendered=1750.0 net=0.0"
        # Resolve the console's most recent session (still active, or ended within the
        # last 2h in MMT — NOW()+390min because session timestamps are stored in MMT)
        # that has no sales_daily row yet, and use it for the ledger + voucher link.
        if not _session_id and console_id:
            try:
                _cand = _mysql_query_one(
                    "SELECT cs.id FROM console_sessions cs "
                    "WHERE cs.console_id=%s "
                    "  AND (cs.status='active' OR cs.actual_end >= (NOW() + INTERVAL 390 MINUTE) - INTERVAL 120 MINUTE) "
                    "  AND NOT EXISTS (SELECT 1 FROM sales_daily sd WHERE sd.session_id=cs.id) "
                    "ORDER BY cs.id DESC LIMIT 1", (console_id,))
                if _cand:
                    _session_id = _cand.get("id")
                    logger.info("sale session fallback: console=%s session=%s voucher=%s",
                                console_id, _session_id, voucher_no)
            except Exception as _cfe:
                logger.warning("sale session fallback failed console=%s: %s", console_id, _cfe)

        # ── 🎮 Controller-rental ledger (owner Osmo 2026-10-25) ────────────────
        # When the session has rental EVENTS (rented mid-session, maybe returned
        # early), the ledger is authoritative — each event carries its own
        # start/return time and is prorated per minute. Settle whatever is still
        # open at checkout and use the ledger totals instead of the manual stepper.
        if _session_id:
            try:
                # 🐞 2026-10-25: the ledger import used the TOP-LEVEL name and blew up
                # ("No module named 'console_routes'") on every sale → rentals never
                # settled → net_total stayed 0 → OVER-PAYMENT BLOCKED. Routers live in
                # the `routes` package, so import that first with a flat fallback.
                try:
                    from routes.console_routes import _ctrl_settle_open as _settle_ctrl
                except ImportError:
                    from console_routes import _ctrl_settle_open as _settle_ctrl
                # ⏱️ Settle at the session's END time when the session is already done —
                # settling at "now" kept the rental timer running while staff filled the
                # voucher, inflating the controller charge past what the screen showed
                # (C-01/4866: 21:38:36 end gave exactly 1,750 Ks; settling 10 min late
                # gave 2,450 Ks -> the tender 1,750 would be rejected as under-payment).
                _when = None
                try:
                    try:
                        from routes.console_routes import now_mmt as _now_mmt, _ctrl_aware as _aware
                    except ImportError:
                        from console_routes import now_mmt as _now_mmt, _ctrl_aware as _aware
                    _srow = _mysql_query_one(
                        "SELECT status, actual_end FROM console_sessions WHERE id=%s", (_session_id,))
                    if _srow and str(_srow.get("status") or "") != "active" and _srow.get("actual_end"):
                        _when = _aware(_srow["actual_end"], _now_mmt())
                except Exception:
                    _when = None
                _settle_ctrl(_session_id, _when)
                _lr = _mysql_query_one(
                    "SELECT COALESCE(SUM(qty),0) AS q, COALESCE(SUM(minutes),0) AS m, "
                    "COALESCE(SUM(amount),0) AS a FROM session_controller_rentals "
                    "WHERE session_id=%s", (_session_id,)) or {}
                _lq = int(_lr.get("q") or 0)
                _la = float(_lr.get("a") or 0)
                if _lq > 0 or _la > 0:
                    net_total += (_la - float(extra_ctrl_amount or 0))
                    extra_ctrl_qty = _lq
                    extra_ctrl_mins = int(_lr.get("m") or 0)
                    extra_ctrl_amount = _la
                    gross = game_amt + food_total + extra_ctrl_amount
                    logger.info("controller ledger: session=%s qty=%s mins=%s amount=%s (voucher %s)",
                                _session_id, _lq, extra_ctrl_mins, _la, voucher_no)
            except Exception as _lge:
                logger.warning("controller ledger settle failed (session %s): %s", _session_id, _lge)

        # ── 💰 GUARD: tender must never exceed the payable (2026-10-16, Osmo) ──────
        # The display bug above let staff hand in 20,000 Ks against a 500 Ks bill.
        # Reject the write so a wrong screen amount can never reach the books; the
        # cashier reloads the sale (the preview is wallet-aware since the same fix).
        # Tolerance 1 Ks covers rounding only.
        try:
            _tendered = _pm_tender_total(payment_method)
            # Deposit sales carry a "(dep)" marker and are reconciled by the deposit
            # path (their cash leg already includes the deposit) → never guard them.
            _has_dep = "(" in str(payment_method or "")
            if _tendered is not None and not _has_dep and _tendered > float(net_total) + 1:
                _msg = ("Tender %s Ks > cash due %s Ks — over-payment blocked. "
                        "Refresh the sale and re-enter the payment amount."
                        % (f"{_tendered:,.0f}", f"{float(net_total):,.0f}"))
                logger.warning(
                    "OVER-PAYMENT BLOCKED voucher=%s member=%s console=%s tendered=%s net=%s pm=%s staff=%s",
                    voucher_no, member_id, console_id, _tendered, net_total, payment_method, staff)
                _spool_integrity_alert(kind="over_payment_blocked", voucher_no=voucher_no,
                                       member_id=member_id, console_id=console_id,
                                       tendered=_tendered, payable=float(net_total),
                                       payment_method=payment_method, staff=staff)
                return error_response(message=_msg, code=409,
                                      detail={"tendered": _tendered, "payable": float(net_total)})
            if _tendered is not None and not _has_dep and _tendered < float(net_total) - 1:
                logger.warning(
                    "UNDER-PAYMENT voucher=%s member=%s console=%s tendered=%s net=%s pm=%s staff=%s",
                    voucher_no, member_id, console_id, _tendered, net_total, payment_method, staff)
                _spool_integrity_alert(kind="under_payment", voucher_no=voucher_no,
                                       member_id=member_id, console_id=console_id,
                                       tendered=_tendered, payable=float(net_total),
                                       payment_method=payment_method, staff=staff)
        except Exception as _ge:
            logger.warning("tender guard skipped: %s", _ge)

        # ── 🐛 Duplicate-sale guard (owner Osmo 2026-10-25: "ဒီ voucher နှစ်ဆက
        # တူနေသလိုပဲ့") ─────────────────────────────────────────────────────
        # A session that is closed by the session-end auto-sale can ALSO be saved
        # by the Staff App from an older draft → two sales_daily rows for one
        # physical session (2026-10-25 C-03: V-WEB-…140450-696 session 4837 at
        # 14:04:59 + V-WEB-…125854-798 session NULL at 14:05:15, both 10,000 Ks).
        # voucher_no/client_ref idempotency cannot catch it because the drafts
        # carry DIFFERENT voucher numbers. Guard on the SESSION instead.
        try:
            _dup_sale = None
            if _session_id:
                _dup_sale = _mysql_query_one(
                    "SELECT voucher_no, net FROM sales_daily WHERE session_id=%s LIMIT 1",
                    (_session_id,))
            if not _dup_sale and console_id:
                # No session on the request → use the console's most recent CLOSED
                # session (a new/active session must never block a fresh sale).
                _last_sess = _mysql_query_one(
                    "SELECT id, status FROM console_sessions WHERE console_id=%s "
                    "ORDER BY id DESC LIMIT 1", (console_id,))
                if _last_sess and str(_last_sess.get("status") or "").lower() in ("done", "ended", "completed"):
                    _dup_sale = _mysql_query_one(
                        "SELECT sd.voucher_no, sd.net FROM sales_daily sd "
                        "WHERE sd.session_id=%s "
                        "AND ABS(TIMESTAMPDIFF(SECOND, sd.created_at, NOW() + INTERVAL 390 MINUTE)) <= 900 "
                        "LIMIT 1", (_last_sess["id"],))
            if _dup_sale:
                _dup_net = float(_dup_sale.get("net") or 0)
                if abs(_dup_net - float(net_total)) <= 1:
                    logger.warning(
                        "DUPLICATE SALE BLOCKED voucher=%s console=%s session=%s net=%s staff=%s existing=%s",
                        voucher_no, console_id, _session_id, net_total, staff,
                        _dup_sale.get("voucher_no"))
                    return ok({"success": True, "duplicate": True, "existing": True,
                               "voucher_no": _dup_sale.get("voucher_no", ""),
                               "message": "This session is already billed — duplicate sale not saved."})
        except Exception as _dg:
            logger.warning("duplicate-sale guard skipped: %s", _dg)

        # Never persist a walk-in/unknown name as a member id (report pollution).
        _stored_member_id = member_id
        if not _is_member_sale and not _is_guest_label:
            _stored_member_id = ""
        _mysql_exec(
            "INSERT INTO sales_daily (voucher_no, sale_date, console_id, member_id, amount, gross, discount, net, staff_name, customer_name, payment_method, notes, client_ref, session_id, game_name, game_id, billable_mins, free_mins, extra_ctrl_qty, extra_ctrl_mins, extra_ctrl_amount) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (voucher_no, sale_date, console_id, _stored_member_id, game_amt, gross, discount, net_total, staff, customer_name, payment_method, combined_notes, client_ref, _session_id, _game_name, _game_id,
             (_d2p_applied or {}).get("billable_mins"), (_d2p_applied or {}).get("free_mins"),
             extra_ctrl_qty, extra_ctrl_mins, extra_ctrl_amount)
        )

        # ── Payment Due (owner Osmo 2026-10-30, option 2) ─────────────────────
        # A sale may settle for less than the net total; keep the shortfall as a
        # receivable instead of silently losing it.
        try:
            _paid_now = _pm_paid_total(payment_method)
            _record_sale_due(voucher_no, client_ref, console_id, staff,
                             net_total, _paid_now,
                             req.get("due_reason") or req.get("notes") or "unspecified")
        except Exception as _de:
            logger.warning("payment-due block skipped: %s", _de)

        # ── 🎮 Controller rental snapshot on the session (owner Osmo 2026-10-25) ──
        # Keeps console_sessions as the single source of truth for "how many extra
        # controllers were rented" so the console card / reports can show it.
        if _session_id and extra_ctrl_qty > 0:
            try:
                _mysql_exec(
                    "UPDATE console_sessions SET extra_ctrls=%s, extra_ctrl_mins=%s, "
                    "extra_ctrl_amount=%s WHERE id=%s",
                    (extra_ctrl_qty, extra_ctrl_mins, extra_ctrl_amount, _session_id))
                logger.info("session %s: extra controllers %s x %smin = %s Ks",
                            _session_id, extra_ctrl_qty, extra_ctrl_mins, extra_ctrl_amount)
            except Exception as _sce:
                logger.warning("session controller snapshot failed (session %s): %s", _session_id, _sce)

        # ── 2+1 promo snapshot on the session (owner Osmo 2026-10-08) ──────────
        if _d2p_applied:
            try:
                _snap_sid = _d2p_applied.get("session_id") or _session_id
                if _snap_sid:
                    _mysql_exec(
                        "UPDATE console_sessions SET promo_id=%s, promo_applied=1, billable_mins=%s, free_mins=%s "
                        "WHERE id=%s",
                        (_d2p_applied["promo_id"], _d2p_applied["billable_mins"],
                         _d2p_applied["free_mins"], _snap_sid))
            except Exception as _d2ps_e:
                logger.warning("2+1 console_sessions snapshot failed: %s", _d2ps_e)

        # ── Session duration safety net (owner 2026-09-29 Osmo) ───────────────────
        # Sessions closed by a sale occasionally ended up with actual_duration_mins = 0
        # (timer/automation miss) while money WAS collected — that skews the duration /
        # hourly reports. Whenever a sale is bound to a session, backfill the duration
        # from the sale's mins (fallback: wall-clock between actual_start/actual_end).
        if _session_id:
            try:
                _mins_fix = int(float(mins or 0))
                if _mins_fix <= 0:
                    _row = _mysql_query_one(
                        "SELECT TIMESTAMPDIFF(MINUTE, actual_start, actual_end) AS m "
                        "FROM console_sessions WHERE id=%s", (_session_id,))
                    _mins_fix = int((_row or {}).get("m") or 0)
                if _mins_fix > 0:
                    _n = _mysql_exec(
                        "UPDATE console_sessions SET actual_duration_mins=%s "
                        "WHERE id=%s AND (actual_duration_mins IS NULL OR actual_duration_mins=0)",
                        (_mins_fix, _session_id))
                    if _n:
                        logger.info("session duration backfilled: session=%s -> %s min (voucher=%s)",
                                    _session_id, _mins_fix, voucher_no)
            except Exception as _se:
                logger.warning("session duration backfill failed session=%s: %s", _session_id, _se)

        # Wallet balance was already deducted BEFORE the insert (wallet detail block above) so
        # the sale note could carry the before -> after balances. Never deduct twice here.
        if _wallet_deduct > 0 and _is_member_sale:
            logger.debug("wallet already deducted for voucher=%s member=%s", voucher_no, member_id)

        # 💰 Owner 2026-11-08: a recorded sale must move the drawer too. The payment
        # string is "Cash:5000|MMQR:3000" (or "Cash:2500"), so split it and inject each
        # part into its own account. Idempotent: the note carries the voucher number
        # and an identical inject is never duplicated.
        try:
            _sale_note = "Sale " + str(voucher_no or sale_ref)
            for _part in [x.strip() for x in re.split(r"[|/]", str(payment_method or "")) if x.strip()]:
                if ":" not in _part:
                    continue
                _acct, _amt = _part.rsplit(":", 1)
                _acct = _acct.strip()
                try:
                    _amt_f = float(_amt)
                except (TypeError, ValueError):
                    continue
                if not _acct or _amt_f <= 0:
                    continue
                _dup = _mysql_query_one(
                    "SELECT id FROM cash_movements WHERE movement_type='inject' "
                    "AND account=%s AND amount=%s AND note=%s LIMIT 1",
                    (_acct, _amt_f, _sale_note))
                if _dup:
                    logger.info("sale cash inject skipped (exists): %s %s", _acct, _amt_f)
                    continue
                _mysql_exec(
                    "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at) "
                    "VALUES ('inject', %s, %s, %s, %s, NOW())",
                    (_acct, _amt_f, _sale_note, staff))
                logger.info("sale cash inject: voucher=%s account=%s amount=%s", voucher_no, _acct, _amt_f)
        except Exception as _cme:
            logger.warning("sale cash inject failed (voucher %s): %s", voucher_no, _cme)

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
            "duration_2plus1": _d2p_applied,
            "billable_mins": (_d2p_applied or {}).get("billable_mins"),
            "free_mins": (_d2p_applied or {}).get("free_mins"),
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

        sid = _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, staff_name, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (item_name, qty, unit_cost, total_cost, sale_date, staff, notes)
        )
        _dish_consume.consume_after_stock_out(item_name, qty, sid)

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
        sid = _mysql_exec(
            "INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (batch_id, item_name, qty, unit_cost, supplier or "Manual", notes or "", payment_method, paid_by, staff)
        )
        # Keep the FIFO ledger current (idempotent layer per purchase)
        _dish_consume.ensure_layer_for_stock_in(sid)

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
        quantity = float(req.get("quantity", 0) or 0)  # decimal-safe (owner Osmo 2026-09-29)
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
        sid = _mysql_exec(
            """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (batch_id, item_name, quantity, unit_cost, source or "", receipt_no or "", payment_method, paid_by, staff_name)
        )
        _dish_consume.ensure_layer_for_stock_in(sid)

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
                qty = round(float(it.get("quantity", 0) or 0), 2)  # 0.9 L etc. (owner Osmo 2026-09-29)
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
        def _find_inv(name):
            """Exact match first, then TRIM-insensitive (owner Osmo 2026-11-05:
            'Oishi Prawn' / 'SunKist' were saved with a trailing space, so the
            trimmed picker value never matched them and the batch was rejected)."""
            row = _mysql_query_one("SELECT id, item_name, quantity FROM inventory WHERE item_name=%s", (name,))
            if row:
                return row
            return _mysql_query_one(
                "SELECT id, item_name, quantity FROM inventory WHERE TRIM(item_name)=%s ORDER BY id LIMIT 1",
                (name.strip(),))

        def _exists_ing(name):
            if _mysql_query_one("SELECT id FROM ingredients WHERE name=%s", (name,)):
                return True
            return bool(_mysql_query_one(
                "SELECT id FROM ingredients WHERE TRIM(name)=%s LIMIT 1", (name.strip(),)))

        def _exists_hist(name):
            if _mysql_query_one("SELECT id FROM stock_in WHERE item_name=%s LIMIT 1", (name,)):
                return True
            return bool(_mysql_query_one(
                "SELECT id FROM stock_in WHERE TRIM(item_name)=%s LIMIT 1", (name.strip(),)))

        unknown = []
        targets = []
        for it in cleaned:
            canon = it["item_name"].strip()
            inv = _find_inv(it["item_name"])
            if inv:
                # Always store the canonical (trimmed) name on the new lot row.
                targets.append({"item": it, "inv_id": inv["id"],
                                "name": (inv.get("item_name") or canon).strip() or canon})
                continue
            if _exists_ing(it["item_name"]) or _exists_hist(it["item_name"]):
                targets.append({"item": it, "inv_id": None, "name": canon})
            else:
                unknown.append(it["item_name"])
        if unknown:
            return error_response(message="Not registered — add these in Recipe & Cost first: " + ", ".join(unknown))

        # ── Insert stock_in rows (one per item, same batch base_id + seq suffix ──
        # stock_in.batch_id is UNIQUE → per-item suffix keeps grouping without collision
        added_rows = []
        for idx, t in enumerate(targets, start=1):
            it = t["item"]
            _nm = t.get("name") or (it.get("item_name") or "").strip()
            batch_id = f"{base_id}-{idx:02d}"
            if t["inv_id"]:
                _mysql_exec(
                    "UPDATE inventory SET quantity = quantity + %s, last_updated = NOW() WHERE id = %s",
                    (it["quantity"], t["inv_id"])
                )
            sid = _mysql_exec(
                """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (batch_id, _nm, it["quantity"], it["unit_cost"], source or "", receipt_no or "", payment_method, paid_by, staff_name)
            )
            _dish_consume.ensure_layer_for_stock_in(sid)
            added_rows.append({
                "item_name": _nm,
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
    """Stock-In picker: ingredients/products only (dishes excluded), deduped.

    2026-10-29 (Osmo): "ဟင်းပွဲတွေကို stock in item မှာ ဖြုတ်ပေးပါ" — a dish is
    produced, never purchased, so it must not appear here. Each row also carries
    the counted qty + its buy unit so the picker can show "12 pcs".
    """
    try:
        inv = {}
        for r in _mysql_query("SELECT item_name, quantity, item_type FROM inventory"):
            nm = (r["item_name"] or "").strip()
            if nm:
                inv[nm] = {"qty": float(r.get("quantity") or 0),
                           "type": (r.get("item_type") or "").strip().lower()}

        dishes = set()
        try:
            for r in _mysql_query("SELECT DISTINCT menu_item FROM menu_recipes"):
                dishes.add((r["menu_item"] or "").strip())
        except Exception:
            dishes = set()

        kind_map, conv = {}, {}
        try:
            for r in _mysql_query("SELECT name, kind, purchase_unit, base_unit FROM ingredients"):
                nm = (r["name"] or "").strip()
                kind_map[nm] = (r.get("kind") or "").strip().lower()
                conv[nm] = {"purchase_unit": (r.get("purchase_unit") or "").strip(),
                            "base_unit": (r.get("base_unit") or "").strip()}
        except Exception:
            pass

        var_map = {}
        try:
            for r in _mysql_query("SELECT canonical_name, variant_name FROM ingredient_variants"):
                var_map[(r["variant_name"] or "").strip()] = (r["canonical_name"] or "").strip()
        except Exception:
            pass

        names = set(inv.keys()) | set(kind_map.keys())
        try:
            for r in _mysql_query("SELECT DISTINCT item_name FROM stock_in"):
                names.add((r["item_name"] or "").strip())
        except Exception:
            pass

        qty_by_canon = {}
        for nm, v in inv.items():
            c = var_map.get(nm) or nm
            qty_by_canon[c] = qty_by_canon.get(c, 0.0) + v["qty"]

        items, details = [], []
        for n in sorted(x for x in names if x):
            canon = var_map.get(n) or n
            if n != canon:
                continue  # variants collapse into the canonical row
            t = (inv.get(n) or {}).get("type") or ""
            if t == "dish" or n in dishes:
                continue  # dishes are produced, never stock-in
            if n in inv:
                k = kind_map.get(n) or "product"
            else:
                k = kind_map.get(n) or "ingredient"
            c = conv.get(n) or {}
            unit = c.get("purchase_unit") or c.get("base_unit") or ""
            items.append(n)
            details.append({
                "name": n,
                "kind": k,
                "canonical": canon,
                "variant_of": None,
                "qty": round(qty_by_canon.get(n, 0.0), 3),
                "unit": unit,
            })
        return ok({"items": items, "details": details})
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


@router.post("/dashboard/cash-movement", tags=["Finance"], summary="Insert a cash movement (VIBE Group settle/refund)")
async def api_cash_movement_group(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    mt = (body.get("movement_type") or "inject").strip()
    acct = (body.get("account") or "").strip()
    try:
        amt = int(float(body.get("amount", 0) or 0))
    except Exception:
        amt = 0
    note = (body.get("note") or "").strip()
    ref = (body.get("reference") or "").strip()
    if mt not in ("inject", "eject", "transfer_out", "transfer_in", "dividend"):
        return {"success": False, "error": "invalid movement_type"}
    if not acct or amt <= 0:
        return {"success": False, "error": "account and amount required"}
    full_note = (note + (f" [{ref}]" if ref else "")).strip()
    _mysql_exec(
        "INSERT INTO cash_movements (movement_type,account,amount,note,staff_name,created_at) VALUES (%s,%s,%s,%s,%s,NOW())",
        (mt, acct, amt, full_note, "VIBE Group"))
    row = _mysql_query_one("SELECT id FROM cash_movements ORDER BY id DESC LIMIT 1")
    return {"success": True, "data": {"id": row["id"] if row else None}}


@router.delete("/dashboard/cash-movement/{movement_id}", tags=["Finance"], summary="Delete a cash movement (VIBE Group settle undo)")
async def api_cash_movement_delete_group(movement_id: int, user: dict = Depends(get_staff_or_dashboard_user)):
    _mysql_exec("DELETE FROM cash_movements WHERE id = %s", (movement_id,))
    return {"success": True, "data": {"id": movement_id}}


@router.post("/dashboard/opex/record", tags=["OPEX"], summary="Record OPEX from VIBE Group (opex + cash movement)")
async def api_opex_record_group(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Called by VIBE Group when OPEX is entered for PS VIBE.
    Writes BOTH the opex ledger row and a cash_movements row so the expense shows
    up in the PS VIBE app and affects the computed account balance."""
    from datetime import datetime as _dt, timedelta as _td
    _mmt_now = _dt.utcnow() + _td(hours=6, minutes=30)
    cat = (body.get("category") or "").strip()
    desc = (body.get("description") or "").strip()
    try:
        amt = int(float(body.get("amount", 0) or 0))
    except Exception:
        amt = 0
    pmt = (body.get("payment_method") or "Cash").strip()
    ref = (body.get("ref") or "").strip()
    dt = (body.get("expense_date") or _mmt_now.strftime("%Y-%m-%d")).strip()
    if not cat or amt <= 0:
        return {"success": False, "error": "Category and amount required"}
    desc_full = desc + (f" [{ref}]" if ref else "")
    note = f"OPEX: {cat}" + (f" - {desc}" if desc else "") + (f" [{ref}]" if ref else "")
    _mysql_exec(
        "INSERT INTO opex (category,description,amount,payment_method,recorded_by,expense_date) VALUES (%s,%s,%s,%s,%s,%s)",
        (cat, desc_full, amt, pmt, "VIBE Group", dt))
    # Balance is computed from the opex table itself (get_finance_balances subtracts it),
    # so we must NOT also write a cash_movements eject (that would double-count).
    try:
        acc = _mysql_query_one("SELECT id, balance FROM accounts WHERE account_name = %s", (pmt,))
        if acc:
            _mysql_exec("UPDATE accounts SET balance = %s WHERE id = %s",
                        (float(acc["balance"]) - amt, acc["id"]))
    except Exception:
        pass
    return {"success": True, "data": {"ref": ref, "account": pmt, "amount": amt, "category": cat}}


@router.delete("/dashboard/opex/record", tags=["OPEX"], summary="Delete a VIBE Group OPEX in PS VIBE")
async def api_opex_delete_group(ref: str = Query(...), user: dict = Depends(get_staff_or_dashboard_user)):
    if not ref:
        return {"success": False, "error": "ref required"}
    rows = _mysql_query("SELECT id, amount, payment_method FROM opex WHERE description LIKE %s", (f"%[{ref}]%",))
    for r in rows:
        _mysql_exec("DELETE FROM opex WHERE id = %s", (r["id"],))
        try:
            acc = _mysql_query_one("SELECT id, balance FROM accounts WHERE account_name = %s", (r.get("payment_method") or "",))
            if acc:
                _mysql_exec("UPDATE accounts SET balance = %s WHERE id = %s",
                            (float(acc["balance"]) + float(r.get("amount") or 0), acc["id"]))
        except Exception:
            pass
    _mysql_exec("DELETE FROM cash_movements WHERE note LIKE %s", (f"%[{ref}]%",))
    return {"success": True, "data": {"ref": ref, "removed": len(rows)}}


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
