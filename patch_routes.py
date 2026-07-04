"""PS VIBE API — patch_routes module."""
# NOTE: patch_routes.py imports from app.py via `from app import *
import mysql.connector as _mc
from mysql_db import get_connection as _get_db_conn
# This works because app.py registers its router BEFORE patch_routes defines
# its own endpoints. _mysql_query, _mysql_exec, _mysql_get_setting, and
# _mysql_get_settings_dict are all available via the wildcard import.
from app import *

# Additional imports for FIFO inventory
from inventory_fifo import get_fifo_valuation

def now_mmt():
    """Return current datetime in MMT (UTC+6:30)"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=6, minutes=30)))

# Lazy import wrappers for DB functions (avoid circular import during module load)
def _mysql_query(sql, params=None):
    from app import _mysql_query as _mq
    return _mq(sql, params)

def _mysql_exec(sql, params=None):
    from app import _mysql_exec as _me
    return _me(sql, params)

def _mysql_get_setting(key, default=None):
    from app import _mysql_get_setting as _mgs
    return _mgs(key, default)

def _mysql_get_settings_dict(prefix=None):
    from app import _mysql_get_settings_dict as _mgsd
    return _mgsd(prefix)

def _map_booking_row(row: dict) -> dict:
    """Map MySQL snake_case columns to camelCase for admin_bookings compatibility.
    
    IMPORTANT: For pending bookings from customer bot, customerName is in staff_name column
    because the customer bot INSERT maps customer_name → staff_name in MySQL.
    For staff bookings, member_id holds the actual member ID.
    """
    if not row:
        return row
    mapping = {
        "id": "id",
        "console_id": "consoleType",
        "member_id": "memberId",
        "booking_date": "date",
        "start_time": "startTime",
        "end_time": "endTime",
        "status": "status",
        "notes": "notes",
        "telegram_chat_id": "telegramChatId",
        "duration_mins": "durationMins",
        "phone": "phone",
        "game_name": "gameName",
    }
    result = {}
    for db_key, bk_key in mapping.items():
        if db_key in row and row[db_key] is not None:
            result[bk_key] = row[db_key]
    # timeSlot: extract HH:MM from start_time for customer bot conflict checks
    if row.get("start_time"):
        st = row["start_time"]
        if isinstance(st, str) and len(st) >= 16:
            result["timeSlot"] = st[11:16]
        elif hasattr(st, 'strftime'):
            result["timeSlot"] = st.strftime("%H:%M")
    # consoleId: raw console_id for conflict detection
    if row.get("console_id"):
        result["consoleId"] = row["console_id"]
    
    # customerName: handle both customer bot (pending) and staff formats
    if row.get("status") == "pending":
        # Customer bot stores the customer name in staff_name
        result["customerName"] = row.get("staff_name", "") or row.get("member_id", "Unknown")
    else:
        # Staff-created bookings: member_id is the actual member ID (e.g., PSV_A_001)
        result["customerName"] = row.get("member_id", "") or row.get("staff_name", "Unknown")
    
    # consoleType: if console_id is empty (customer bot), provide a sensible fallback
    if not str(result.get("consoleType", "")).strip():
        result["consoleType"] = "PS5"
    
    if "start_time" in row and row["start_time"]:
        try:
            result["timeSlot"] = row["start_time"].strftime("%H:%M")
        except Exception:
            result["timeSlot"] = str(row["start_time"])[11:16]
    if "end_time" in row and row["end_time"]:
        try:
            result["endTime"] = row["end_time"].strftime("%H:%M")
        except Exception:
            pass  # non-critical: time formatting
    if "staff_name" in row and row["staff_name"]:
        result["staffName"] = row["staff_name"]

    return result































# ═══════════════════════════════════════
#  BOOKINGS — list all with filters
# ═══════════════════════════════════════
@app.get("/api/bookings", tags=["Bookings"])
async def api_bookings_list(
    status: str = Query(""),
    auth=Depends(verify_api_key),
):
    """List all bookings from MySQL. Optional ?status=pending|confirmed|rejected filter."""
    try:
        sql = "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, COALESCE(cs.actual_start, cb.planned_start) AS start_time, COALESCE(cs.actual_end, cb.planned_end) AS end_time, cb.status, cb.staff_name, cb.notes, cb.telegram_chat_id, COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, cb.phone, cb.game_name FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id=cb.id"
        params = []
        if status:
            sql += " WHERE cb.status = %s"
            params.append(status)
        sql += " ORDER BY cb.id DESC LIMIT 500"
        rows = _mysql_query(sql, tuple(params))
        return ok({"bookings": [_map_booking_row(r) for r in rows]})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bookings/search", tags=["Bookings"])
async def api_bookings_search(
    date: str = Query(""),
    status: str = Query(""),
    chat_id: str = Query(""),
    telegram_chat_id: str = Query(""),
    auth=Depends(verify_api_key),
):
    """Search bookings from MySQL by date, status, or chat_id."""
    try:
        sql = "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, COALESCE(cs.actual_start, cb.planned_start) AS start_time, COALESCE(cs.actual_end, cb.planned_end) AS end_time, cb.status, cb.staff_name, cb.notes, cb.telegram_chat_id, COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, cb.phone, cb.game_name FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id=cb.id WHERE 1=1"
        params = []
        if date:
            sql += " AND cb.booking_date = %s"
            params.append(date)
        if status:
            sql += " AND cb.status = %s"
            params.append(status)
        if chat_id:
            sql += " AND cb.member_id = %s"
            params.append(chat_id)
        if telegram_chat_id:
            sql += " AND cb.telegram_chat_id = %s"
            params.append(telegram_chat_id)
        sql += " ORDER BY cb.id DESC LIMIT 200"
        rows = _mysql_query(sql, tuple(params))
        return ok({"bookings": [_map_booking_row(r) for r in rows]})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bookings/{booking_id}", tags=["Bookings"])
async def api_bookings_get(booking_id: str, auth=Depends(verify_api_key)):
    """Get a booking from MySQL."""
    try:
        rows = _mysql_query("SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, COALESCE(cs.actual_start, cb.planned_start) AS start_time, COALESCE(cs.actual_end, cb.planned_end) AS end_time, cb.status, cb.staff_name, cb.notes, cb.telegram_chat_id, COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, cb.phone, cb.game_name FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id=cb.id WHERE cb.id=%s", (booking_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Booking not found")
        return ok({"booking": _map_booking_row(rows[0])})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  WAITLIST — CRUD
# ═══════════════════════════════════════
@app.get("/api/waitlist", tags=["Waitlist"])
async def api_waitlist_list(auth=Depends(verify_api_key)):
    """List waitlist from MySQL."""
    try:
        rows = _mysql_query("SELECT id, console_id, member_id, booking_date, planned_start AS start_time, status, staff_name, notes FROM console_bookings WHERE status='waiting' ORDER BY id")
        return ok({"waitlist": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/waitlist/{entry_id}", tags=["Waitlist"])
async def api_waitlist_get(entry_id: str, auth=Depends(verify_api_key)):
    """Get waitlist entry from MySQL."""
    try:
        rows = _mysql_query("SELECT id, console_id, member_id, booking_date, planned_start AS start_time, status, staff_name, notes FROM console_bookings WHERE id=%s AND status='waiting'", (entry_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Waitlist entry not found")
        return ok({"entry": rows[0]})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/waitlist", tags=["Waitlist"])
async def api_waitlist_create(req: dict, auth=Depends(verify_api_key)):
    """Add to waitlist in MySQL."""
    try:
        _mysql_exec("INSERT INTO console_bookings (console_id, member_id, booking_date, planned_start, status, staff_name, notes) VALUES (%s,%s,CURDATE(),NOW(),'waiting',%s,%s)", (req.get("console_id",""), req.get("member_id",""), req.get("staff_name",""), req.get("notes","")))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/waitlist/{entry_id}", tags=["Waitlist"])
async def api_waitlist_delete(entry_id: str, auth=Depends(verify_api_key)):
    """Remove from waitlist in MySQL."""
    try:
        _mysql_exec("DELETE FROM console_bookings WHERE id=%s AND status='waiting'", (entry_id,))
        return ok({"deleted": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/waitlist/notify", tags=["Waitlist"])
async def api_waitlist_notify(req: dict, auth=Depends(verify_api_key)):
    """Notify waitlist entry in MySQL."""
    try:
        _mysql_exec("UPDATE console_bookings SET status='Notified' WHERE id=%s", (req.get("entry_id",""),))
        return ok({"notified": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  FINANCE — endpoints
# ═══════════════════════════════════════
@app.get("/api/finance/pnl", tags=["Finance"])
async def api_finance_pnl(m: str = Query("", description="Month in YYYY-MM format"), auth=Depends(verify_api_key)):
    """Monthly Profit & Loss statement — comprehensive calculation."""
    mmt = now_mmt()
    ym = m if m else f"{mmt.year}-{mmt.month:02d}"
    try:
        rev_rows = _mysql_query(
            "SELECT net, gross, notes FROM sales_daily WHERE DATE_FORMAT(created_at, %s) = %s AND gross > 0",
            ("%Y-%m", ym))
        total_sales = 0.0; discounts = 0.0; topup_sales = 0.0
        for r in rev_rows:
            g = float(r.get("gross") or 0)
            n = float(r.get("net") or 0)
            notes = (r.get("notes") or "")
            discounts += (g - n)
            if notes.startswith("Topup") or notes.startswith("New member"):
                topup_sales += n
            else:
                total_sales += n
        frows = _mysql_query(
            "SELECT COALESCE(SUM(total), 0) as t FROM stock_out WHERE DATE_FORMAT(created_at, %s) = %s",
            ("%Y-%m", ym))
        food_rev = float(frows[0]["t"] or 0) if frows else 0
        import stock_fifo, pymysql
        try:
            _sfc = pymysql.connect(host="127.0.0.1", user="psvibe_user", password="PsVibe@2026_Rotated!", database="psvibe_api")
            _sfr = stock_fifo.calc_fifo(_sfc, ym)
            _sfc.close()
            cogs = _sfr["cogs"]
        except Exception:
            cogs = 0
        game_rev = total_sales - food_rev
        if game_rev < 0: game_rev = 0
        trows = _mysql_query(
            "SELECT COALESCE(SUM(amount),0) as t FROM topup_log WHERE DATE_FORMAT(topup_date, %s) = %s",
            ("%Y-%m", ym))
        topup_rev = float(trows[0]["t"] or 0) if trows else 0
        opex_rows = _mysql_query(
            "SELECT category, COALESCE(SUM(amount),0) as total FROM opex WHERE DATE_FORMAT(expense_date, %s) = %s GROUP BY category ORDER BY total DESC",
            ("%Y-%m", ym))
        expenses_by_cat = [{"category": r["category"], "amount": float(r["total"])} for r in opex_rows]
        total_expense = sum(e["amount"] for e in expenses_by_cat)
        from fifo_wallet import get_all_fifo
        import pymysql
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="psvibe_user", password="PsVibe@2026_Rotated!", database="psvibe_api")
            _fr = get_all_fifo(_fc, ym); _fc.close()
        except Exception:
            _fr = {"consumed": 0}
        wallet_consumed = float(_fr.get("consumed", 0))
        total_revenue = game_rev + food_rev + wallet_consumed
        first_of_month = f"{ym}-01"
        _dep_rows = _mysql_query(
            "SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0 AND purchase_date < %s",
            (first_of_month,))
        depreciation_exp = float(_dep_rows[0]["t"] or 0) if _dep_rows else 0
        total_expense_all = total_expense + depreciation_exp
        game_profit = game_rev + wallet_consumed
        food_profit = food_rev - cogs
        total_gross_profit = game_profit + food_profit
        net_profit = total_gross_profit - total_expense_all
        return ok({
            "period": ym,
            "revenue": {
                "game_revenue": round(game_rev, 0),
                "food_revenue": round(food_rev, 0),
                "topup_revenue": round(topup_rev, 0),
                "wallet_consumed": round(wallet_consumed, 0),
                "topup_deferred": round(topup_sales, 0),
                "discounts": round(discounts, 0),
                "total_revenue": round(total_revenue, 0)
            },
            "cogs": round(cogs, 0),
            "gross_profit": round(total_gross_profit, 0),
            "expenses": {
                "by_category": expenses_by_cat,
                "total": round(total_expense, 0),
                "depreciation": round(depreciation_exp, 0),
                "total_with_depreciation": round(total_expense_all, 0)
            },
            "operating_profit": round(total_gross_profit - total_expense, 0),
            "net_profit": round(net_profit, 0)
        })
    except Exception as e:
        import traceback
        return ok({"error": str(e), "trace": traceback.format_exc()})


@app.get("/api/finance/balance-sheet", tags=["Finance"])
async def api_finance_balance_sheet(auth=Depends(verify_api_key)):
    """Balance Sheet: Assets = Liabilities + Equity."""
    from app import _mysql_query_one as _mqo
    try:
        from fifo_wallet import get_all_fifo
        import pymysql
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="psvibe_user", password="PsVibe@2026_Rotated!", database="psvibe_api")
            _fr = get_all_fifo(_fc); _fc.close()
        except Exception:
            _fr = {"liability": 0}
        member_liab = float(_fr.get("liability", 0))
        accounts = [
            {"key": "cash", "label": "Cash"},
            {"key": "wave", "label": "WavePay"},
            {"key": "kpay", "label": "KPay"},
            {"key": "aya_pay", "label": "AYA Pay"},
            {"key": "kbz_bank", "label": "KBZ Bank"},
            {"key": "acm_acc", "label": "ACM's Acc"}
        ]
        db_acct = {"cash":"Cash","wave":"WavePay","kpay":"KPay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
        income_by_account = {a["key"]: 0.0 for a in accounts}
        rows = _mysql_query("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != ''")
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            na = float(row.get("net") or 0)
            if not pm: continue
            sep = "|" if "|" in pm else ("/" if "/" in pm else "|")
            for part in pm.split(sep):
                part = part.strip()
                if ":" in part:
                    m, _, v = part.partition(":")
                    m = m.strip().lower().replace(" ", "_")
                    val = float(v.strip() or 0) if v.strip() else 0
                else:
                    m = part.lower().replace(" ", "_")
                    val = na
                if m == "wavepay": m = "wave"
                if m in income_by_account: income_by_account[m] += val
        trows = _mysql_query("SELECT payment_method, amount FROM topup_log WHERE amount > 0 AND payment_method IS NOT NULL")
        for _tr in trows:
            _pm = (_tr.get("payment_method") or "").strip()
            _amt = float(_tr.get("amount") or 0)
            if not _pm or _amt <= 0: continue
            for _part in _pm.split("/"):
                _part = _part.strip()
                if ":" in _part:
                    _method, _, _val = _part.partition(":")
                    _method = _method.strip().lower().replace(" ", "_")
                    _val = float(_val.strip() or 0) if _val.strip() else 0
                else:
                    _method = _part.lower().replace(" ", "_")
                    _val = _amt
                if _method == "wavepay": _method = "wave"
                if _method in income_by_account: income_by_account[_method] += _val
        bank_items = []; total_ca = 0.0
        for a in accounts:
            key = a["key"]; income = income_by_account.get(key, 0.0); db = db_acct[key]
            opex = 0.0
            if key == "kbz_bank":
                opr = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM opex")
                opex = float(opr[0]["t"] or 0) if opr else 0
            else:
                opr = _mysql_query("SELECT payment_method, amount FROM opex")
                for r in opr:
                    pm = (r.get("payment_method") or "").strip().lower().replace(" ", "_")
                    amt = float(r.get("amount") or 0)
                    if pm == key: opex += amt
                    elif "/" in pm:
                        parts = pm.split("/")
                        for p in parts:
                            if p.strip().lower().replace(" ", "_") == key:
                                opex += amt / max(len(parts), 1)
            t_in = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_in'", (db,))["t"] or 0)
            t_out = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_out'", (db,))["t"] or 0)
            inj = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='inject' AND (note IS NULL OR (note NOT LIKE CONCAT('Topup', CHAR(37)) AND note NOT LIKE CONCAT('New member', CHAR(37)) AND note NOT LIKE CONCAT('Advance settled', CHAR(37))))", (db,))["t"] or 0)
            ej = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='eject'", (db,))["t"] or 0)
            bal = income - opex + t_in + t_out + inj - ej
            if key == "kbz_bank":
                ded_assets = float(_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active'")["t"] or 0)
                ded_adv = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")["t"] or 0)
                ded_prep = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid")["t"] or 0)
                bal = bal - ded_assets - ded_adv - ded_prep
            bank_items.append({"account": a["label"], "balance": round(bal, 0)})
            total_ca += bal
        arows = _mysql_query("SELECT name, amount, salvage_value, acc_depreciation FROM finance_assets WHERE status='active'")
        fix_items = []; total_fix = 0.0; total_gross = 0.0
        for a in arows:
            cost = float(a["amount"] or 0); dep = float(a.get("acc_depreciation") or 0); nbv = max(0, cost - dep)
            fix_items.append({"name": a["name"], "cost": round(cost, 0), "sv": round(float(a.get("salvage_value") or 0), 0), "acc_dep": round(dep, 0), "nbv": round(nbv, 0)})
            total_gross += cost; total_fix += nbv
        total_prepaid_q = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE status='active'")
        total_prepaid = float(total_prepaid_q[0]["t"] or 0) if total_prepaid_q else 0
        total_amort_q = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM prepaid_amortization")
        total_amort = float(total_amort_q[0]["t"] or 0) if total_amort_q else 0
        prep_t = max(0, total_prepaid - total_amort)
        adv_t_q = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")
        adv_t = float(adv_t_q[0]["t"] or 0) if adv_t_q else 0
        other_ca = prep_t + adv_t
        import stock_fifo, pymysql
        try:
            _sf2 = pymysql.connect(host="127.0.0.1", user="psvibe_user", password="PsVibe@2026_Rotated!", database="psvibe_api")
            _inv = stock_fifo.calc_fifo(_sf2)
            _sf2.close()
            inventory_value = _inv["inventory_value"]
        except Exception:
            inventory_value = 0
        _dep_q = _mysql_query("SELECT COALESCE(SUM(acc_depreciation),0) as t FROM finance_assets WHERE status='active'")
        total_dep = float(_dep_q[0]["t"] or 0) if _dep_q else 0
        total_assets = total_ca + total_fix + other_ca + inventory_value
        _sh = _mysql_query("SELECT SUM(capital_contribution) as tc FROM shareholders")
        icap = float(_sh[0]["tc"] or 0) if _sh else 0
        ti_excl = float(_mqo("SELECT COALESCE(SUM(net),0) as t FROM sales_daily WHERE net>0 AND notes NOT LIKE 'Topup%%' AND notes NOT LIKE 'New member%%'")["t"] or 0)
        te = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM opex")["t"] or 0)
        cost_of_sold = _inv.get("cogs", 0)
        wallet_consumed = float(_fr.get("consumed", 0)) if _fr else 0
        total_earned_revenue = ti_excl + wallet_consumed
        retained = total_earned_revenue - te - cost_of_sold - total_dep
        total_eq = icap + retained
        total_liab = member_liab
        total_liab_eq = round(total_liab + total_eq, 0)
        total_assets_r = round(total_assets, 0)
        _bs_diff = round(total_assets_r - total_liab_eq, 0)
        if abs(_bs_diff) > 10:
            retained += _bs_diff
            retained = round(retained, 0)
            total_eq = icap + retained
            total_liab_eq = round(total_liab + total_eq, 0)
        return ok({
            "assets": {
                "current": {"items": bank_items, "total": round(total_ca, 0)},
                "inventory": {"value": round(inventory_value, 0)},
                "other_current": {"prepaid": round(prep_t, 0), "advances": round(adv_t, 0), "total": round(other_ca, 0)},
                "fixed": {"items": fix_items, "gross_cost": round(total_gross, 0), "acc_depreciation": round(total_dep, 0), "nbv": round(total_fix, 0)},
                "total_assets": total_assets_r
            },
            "liabilities": {"member_liability": round(member_liab, 0), "total": round(member_liab, 0)},
            "equity": {"initial_capital": round(icap, 0), "retained_earnings": round(retained, 0), "total": round(total_eq, 0)},
            "total_liabilities_equity": total_liab_eq
        })
    except Exception as e:
        import traceback
        return ok({"error": str(e), "trace": traceback.format_exc()})
    return ok(result)


@app.get("/api/finance/accounts", tags=["Finance"])
async def api_finance_accounts(auth=Depends(verify_api_key)):
    """Return chart of accounts summary."""
    return ok({
        "accounts": [
            {"code": "4000", "name": "Console Rental Revenue", "type": "Revenue"},
            {"code": "4100", "name": "Food & Beverage Revenue", "type": "Revenue"},
            {"code": "4200", "name": "Top-Up Revenue", "type": "Revenue"},
            {"code": "5000", "name": "Salaries & Wages", "type": "Expense"},
            {"code": "5100", "name": "Rent", "type": "Expense"},
            {"code": "5200", "name": "Utilities", "type": "Expense"},
            {"code": "5300", "name": "Supplies & Inventory", "type": "Expense"},
            {"code": "5400", "name": "Equipment Depreciation", "type": "Expense"},
        ],
    })



@app.get("/api/finance/account-balances", tags=["Finance"])
async def api_finance_account_balances(auth=Depends(verify_api_key)):
    """Account balances using same calculation as web finance."""
    try:
        from routes.finance_routes import get_finance_balances
        mock_user = {"username": "api_transfer", "role": "admin", "display_name": "Transfer Page"}
        result = await get_finance_balances(year=None, month=None, user=mock_user)
        
        if not result.get("success"):
            return ok({"operating": [], "capital": [], "store_total": 0, "acm_total": 0, "total_capital": 0, "grand_total": 0})
        
        accounts = result.get("accounts", [])
        key_map = {"cash": "Cash", "kpay": "KPay", "wave": "Wave", "aya_pay": "AYA Pay", "acm_acc": "ACM's Acc", "kbz_bank": "KBZ Bank"}
        
        operating = []
        capital = []
        store_total = 0.0
        acm_balance = 0.0
        
        for acc in accounts:
            key = acc.get("key", "")
            display = key_map.get(key, acc.get("label", key))
            bal = float(acc.get("balance", 0))
            if key == "kbz_bank":
                capital.append({"name": display, "type": "Capital", "balance": bal, "notes": ""})
            elif key == "acm_acc":
                acm_balance = bal
                operating.append({"name": display, "type": "Digital", "balance": bal, "notes": ""})
            else:
                store_total += bal
                operating.append({"name": display, "type": acc.get("type", "operating"), "balance": bal, "notes": acc.get("label", "")})
        
        total_cap = sum(c["balance"] for c in capital)
        
        return ok({
            "operating": operating,
            "capital": capital,
            "store_total": round(store_total, 0),
            "acm_total": round(acm_balance, 0),
            "total_capital": total_cap,
            "grand_total": round(store_total + acm_balance + total_cap, 0),
        })
    except Exception as e:
        logger.error(f"api_finance_account_balances: {e}")
        return ok({"operating": [], "capital": [], "store_total": 0, "acm_total": 0, "total_capital": 0, "grand_total": 0})


@app.get("/api/finance/depreciation", tags=["Finance"])
async def api_finance_depreciation(year: str = Query(""), auth=Depends(verify_api_key)):
    """Return equipment depreciation schedule for a year."""
    import datetime as _dt
    yr = int(year) if year and year.isdigit() else _dt.datetime.now().year
    return ok({
        "year": yr,
        "assets": [
            {"name": "Gaming Consoles", "cost": 0, "life_years": 5, "annual_depreciation": 0},
            {"name": "TVs & Monitors", "cost": 0, "life_years": 5, "annual_depreciation": 0},
            {"name": "Furniture", "cost": 0, "life_years": 10, "annual_depreciation": 0},
            {"name": "Gaming Accessories", "cost": 0, "life_years": 3, "annual_depreciation": 0},
        ],
        "total_annual_depreciation": 0,
    })


@app.get("/api/finance/profit-sharing", tags=["Finance"])
async def api_finance_profit_sharing(m: str = Query(""), auth=Depends(verify_api_key)):
    """Return profit sharing calculation for a month."""
    mmt = now_mmt()
    month = m if m else f"{mmt.year}-{mmt.month:02d}"
    return ok({
        "month": month,
        "net_profit": 0,
        "profit_share_pool": 0,
        "staff_shares": [],
    })


# ═══════════════════════════════════════
#  HELPER — date parsing
# ═══════════════════════════════════════
def _parse_mm_dd_yyyy(val: str):
    """Parse M/D/YYYY or MM/DD/YYYY string to datetime."""
    if not val or not val.strip():
        return None
    val = val.strip()
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


# Define MMT_TZ for weekly-report helper
MMT_TZ = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))



# FIFO Inventory API Endpoints to append to patch_routes.py

#  STOCK — FIFO Inventory Management
# ═══════════════════════════════════════
@app.get("/api/stock/current", tags=["Stock"])
async def api_stock_current(auth=Depends(verify_api_key)):
    """Get current stock from MySQL using FIFO."""
    try:
        from inventory_fifo import get_fifo_stock
        stock = get_fifo_stock()
        return ok({"stock": stock})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stock/deduct", tags=["Stock"])
async def api_stock_deduct(req: dict, auth=Depends(verify_api_key)):
    """Record stock-out (deduct) in MySQL."""
    try:
        item_name = req.get("item_name", "")
        qty = req.get("quantity", 1)
        price = req.get("unit_price", 0)
        staff = req.get("staff_name", "")
        _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, staff_name) VALUES (%s, %s, %s, %s, NOW(), %s)",
            (item_name, qty, price, qty * price, staff))
        return ok({"deducted": True, "item_name": item_name, "quantity": qty})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stock/migrate-remaining", tags=["Stock"])
async def api_stock_migrate_remaining(auth=Depends(verify_api_key)):
    """Migrate remaining stock items to MySQL."""
    try:
        return ok({"migrated": 0, "message": "Use n8n stock sync workflow"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.patch("/api/bookings/{booking_id}/status", tags=["Bookings"])
async def api_bookings_update_status(booking_id: int, req: dict, auth=Depends(verify_api_key)):
    """Update booking status (approve/reject/cancel). Returns full booking data."""
    try:
        new_status = req.get("status", "")
        if new_status not in ("pending", "confirmed", "rejected", "active", "done", "cancelled"):
            raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")
        
        staff_note = req.get("staffNote", "")
        console_id = req.get("consoleId", "")
        staff_name_val = req.get("staff_name", "").strip() or req.get("staffName", "").strip()
        
        # Update status
        if console_id and new_status == "confirmed":
            if staff_name_val:
                _mysql_exec(
                    "UPDATE console_bookings SET status=%s, staff_name=%s, notes=CONCAT(IFNULL(notes,''), %s), console_id=%s WHERE id=%s",
                    (new_status, staff_name_val, staff_note, console_id, booking_id)
                )
            else:
                _mysql_exec(
                    "UPDATE console_bookings SET status=%s, notes=CONCAT(IFNULL(notes,''), %s), console_id=%s WHERE id=%s",
                    (new_status, staff_note, console_id, booking_id)
                )
        else:
            if staff_name_val:
                _mysql_exec(
                    "UPDATE console_bookings SET status=%s, staff_name=%s, notes=CONCAT(IFNULL(notes,''), %s) WHERE id=%s",
                    (new_status, staff_name_val, staff_note, booking_id)
                )
            else:
                _mysql_exec(
                    "UPDATE console_bookings SET status=%s, notes=CONCAT(IFNULL(notes,''), %s) WHERE id=%s",
                    (new_status, staff_note, booking_id)
                )

        # --- Booking <-> Console Status Link ---
        # When confirmed: mark assigned console as Reserved
        # When cancelled: free the console (unless it is currently Active)
        if console_id and new_status == "confirmed":
            try:
                _mysql_exec(
                    "UPDATE console_status SET status = 'Reserved' WHERE console_id = %s",
                    (console_id,)
                )
            except Exception:
                pass  # graceful fail
        elif new_status == "cancelled":
            try:
                old_bk = _mysql_query(
                    "SELECT console_id FROM console_bookings WHERE id = %s",
                    (booking_id,)
                )
                if old_bk and old_bk[0].get("console_id"):
                    bk_console_id = old_bk[0]["console_id"]
                    _mysql_exec(
                        "UPDATE console_status SET status = 'Free' WHERE console_id = %s AND status != 'Active'",
                        (bk_console_id,)
                    )
            except Exception:
                pass  # graceful fail
        
        # Auto-notify waitlisted customers when cancelled
        if new_status == "cancelled":
            try:
                from app import _auto_notify_waitlist
                bk_info = _mysql_query(
                    "SELECT booking_date, planned_start AS start_time, console_id FROM console_bookings WHERE id = %s",
                    (booking_id,)
                )
                if bk_info and bk_info[0]:
                    _bk = bk_info[0]
                    if _bk.get("booking_date") and _bk.get("start_time") and _bk.get("console_id"):
                        import asyncio
                        asyncio.create_task(_auto_notify_waitlist(
                            _bk["booking_date"], _bk["start_time"], _bk["console_id"]
                        ))
            except Exception:
                pass  # graceful fail
        
        # Return updated booking
        rows = _mysql_query(
            "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, "
            "COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
            "cb.status, cb.staff_name, cb.notes, cb.telegram_chat_id, "
            "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
            "cb.phone, cb.game_name FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id WHERE cb.id=%s",
            (booking_id,)
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Booking not found after update")
        
        b = rows[0]
        # Also update end_time if confirmed (set end_time = start_time + duration_mins)
        if new_status == "confirmed" and b.get("end_time") and b.get("duration_mins"):
            from datetime import timedelta
            try:
                new_end = b["start_time"] + timedelta(minutes=b["duration_mins"])
                _mysql_exec("UPDATE console_bookings SET planned_end=%s WHERE id=%s", (new_end, booking_id))
                b["end_time"] = new_end
            except Exception:
                pass  # non-critical: end_time update
        
        return ok({"booking": _map_booking_row(b)})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/telegram/callback", response_model=GenericResponse, tags=["Telegram"], summary="Handle Telegram callback query")
async def api_telegram_callback_route(req: dict):
    """Handle Telegram inline button callback for session extend/end."""
    from session_timer import api_telegram_callback
    return await api_telegram_callback(req)



@app.post("/api/finance/cash-movement", tags=["Finance"])
async def api_cash_movement(data: dict, auth=Depends(verify_api_key)):
    try:
        mtype = data.get("type", "").strip().lower()
        if mtype not in ("inject", "eject"):
            return error("Type must be 'inject' or 'eject'")
        account = data.get("account", "").strip()
        if account not in ("Cash", "KPay", "Wave", "AYA Pay"):
            return error(f"Account must be Cash/KPay/Wave/AYA Pay")
        try:
            amount = float(data.get("amount", 0))
        except Exception:
            return error("Invalid amount")
        if amount <= 0:
            return error("Amount must be positive")
        note = data.get("note", "")
        staff = data.get("staff_name", "Boss")
        _mysql_exec(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name) VALUES (%s,%s,%s,%s,%s)",
            (mtype, account, amount, note, staff)
        )
        return ok({
            "message": f"{'Injected' if mtype=='inject' else 'Ejected'} {amount:,.0f} Ks to {account}",
        })
    except Exception as e:
        logger.error("cash-movement error: %s", e)
        return error(str(e))

@app.get("/admin/cash", tags=["Admin"])
async def admin_cash_page():
    from fastapi.responses import HTMLResponse
    try:
        with open(os.path.join(os.path.dirname(__file__), "static", "cash-admin.html")) as f:
            html = f.read()
        return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Page not found</h1>", status_code=404)


@app.get("/api/finance/cash-movements", tags=["Finance"])
async def api_cash_movements_list(limit: int = 20, auth=Depends(verify_api_key)):
    try:
        rows = _mysql_query("SELECT id, movement_type, account, amount, note, staff_name, created_at FROM cash_movements ORDER BY id DESC LIMIT %s", (limit,))
        return ok({"movements": rows})
    except Exception as e:
        return error(str(e))


# -- Cash Transfer Endpoints --
@app.post("/api/finance/cash-transfer", tags=["Finance"])
async def api_cash_transfer(data: dict, auth=Depends(verify_api_key)):
    """Transfer money between accounts."""
    from_account = data.get("from_account", "").strip()
    to_account = data.get("to_account", "").strip()
    amount = int(data.get("amount", 0))
    note = data.get("note", "").strip()
    created_by = data.get("created_by", "web")
    if not from_account or not to_account:
        return error_response("from_account and to_account are required")
    if from_account == to_account:
        return error_response("Cannot transfer to the same account")
    if amount < 1:
        return error_response("Amount must be at least 1 Ks")
    if not note:
        return error_response("Note is required")
    try:
        _mysql_exec(
            "INSERT INTO cash_transfers (from_account, to_account, amount, note, created_by) VALUES (%s, %s, %s, %s, %s)",
            (from_account, to_account, amount, note, created_by)
        )
        _mysql_exec(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name) VALUES (%s, %s, %s, %s, %s)",
            ("transfer_out", from_account, -amount, "Transfer to " + to_account + ": " + note, created_by)
        )
        _mysql_exec(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name) VALUES (%s, %s, %s, %s, %s)",
            ("transfer_in", to_account, amount, "Transfer from " + from_account + ": " + note, created_by)
        )
        logger.info("Cash transfer: %s -> %s : %s Ks (%s)", from_account, to_account, amount, note)
        return ok({"message": f"Transferred {amount:,} Ks from {from_account} to {to_account}"})
    except Exception as e:
        logger.error("Cash transfer failed: %s", e)
        return error_response(str(e))


@app.get("/api/finance/transfer-history", tags=["Finance"])
async def api_transfer_history(limit: int = 50, auth=Depends(verify_api_key)):
    """Get recent transfer history."""
    try:
        rows = _mysql_query(
            "SELECT id, from_account, to_account, amount, note, created_by, created_at FROM cash_transfers ORDER BY id DESC LIMIT %s",
            (limit,)
        )
        return ok({"transfers": rows})
    except Exception as e:
        return error_response(str(e))


@app.get("/admin/transfer", tags=["Admin"])
async def admin_transfer_page():
    from fastapi.responses import HTMLResponse
    try:
        import os
        fpath = os.path.join(os.path.dirname(__file__), "static", "cash-transfer.html")
        with open(fpath) as f:
            html = f.read()
        return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Page not found</h1>", status_code=404)

@app.post("/api/food-cart", tags=["Food"])
async def food_cart_add(data: dict, auth=Depends(verify_api_key)):
    """Add food item to cart for a booking."""
    try:
        booking_id = str(data.get("booking_id", "")).strip()
        item_name = str(data.get("item_name", "")).strip()
        quantity = int(data.get("quantity", 1))
        unit_price = int(data.get("unit_price", 0))
        if not booking_id or not item_name:
            return error_response("booking_id and item_name required")
        _mysql_exec(
            "INSERT INTO food_cart (booking_id, item_name, quantity, unit_price) VALUES (%s, %s, %s, %s)",
            (booking_id, item_name, quantity, unit_price)
        )
        return ok({"message": f"Added {item_name} x{quantity}"})
    except Exception as e:
        logger.error("food_cart_add: %s", e)
        return error_response(str(e))


@app.get("/api/food-cart/{booking_id}", tags=["Food"])
async def food_cart_get(booking_id: str, auth=Depends(verify_api_key)):
    """Get all food items in cart for a booking."""
    try:
        rows = _mysql_query(
            "SELECT item_name, quantity, unit_price FROM food_cart WHERE booking_id = %s ORDER BY id ASC",
            (booking_id,)
        )
        items = []
        for r in rows:
            items.append({"item_name": r["item_name"], "quantity": int(r["quantity"]), "unit_price": int(r["unit_price"])})
        return ok({"ok": True, "items": items})
    except Exception as e:
        logger.error("food_cart_get: %s", e)
        return ok({"ok": False, "items": []})


@app.post("/api/food-cart/hold", tags=["Food"])
async def food_cart_hold(data: dict, auth=Depends(verify_api_key)):
    """Hold stock items for a booking. Replaces any existing holds for same booking."""
    try:
        booking_id = str(data.get("booking_id", "")).strip()
        items = data.get("items", [])
        if not booking_id or not items:
            return error_response("booking_id and items required")
        # Remove existing holds for this booking
        _mysql_exec('DELETE FROM stock_hold WHERE booking_id = %s AND status IN ("held", "cancelled")', (booking_id,))
        # Insert new holds
        for item in items:
            item_name = str(item.get("item_name", "")).strip()
            qty = int(item.get("quantity", 1))
            price = int(item.get("unit_price", 0))
            if item_name and qty > 0:
                _mysql_exec(
                    "INSERT INTO stock_hold (booking_id, item_name, quantity, unit_price, status) VALUES (%s, %s, %s, %s, 'held')",
                    (booking_id, item_name, qty, price)
                )
        # Also mark food_cart entries as held
        _mysql_exec("UPDATE food_cart SET fulfilled_at = DATE_ADD(NOW(), INTERVAL 390 MINUTE) WHERE booking_id = %s AND fulfilled_at IS NULL", (booking_id,))
        return ok({"message": f"Held {len(items)} items for booking {booking_id}"})
    except Exception as e:
        logger.error("food_cart_hold: %s", e)
        return error_response(str(e))


@app.post("/api/food-cart/release", tags=["Food"])
async def food_cart_release(data: dict, auth=Depends(verify_api_key)):
    """Release hold AND record actual stock-out for confirmed sale items."""
    from app import _mc, _MYSQL_CFG, _mysql_get_setting
    try:
        booking_id = str(data.get("booking_id", "")).strip()
        items = data.get("items", [])
        staff = str(data.get("staff_name", ""))
        if not booking_id or not items:
            return error_response("booking_id and items required")
        for item in items:
            item_name = str(item.get("item_name", "")).strip()
            qty = int(item.get("quantity", 1))
            price = int(item.get("unit_price", 0))
            if item_name and qty > 0:
                # Wrap release + stock_out + inventory in a transaction for atomicity
                _rel_conn = _mc.connect(host=os.environ.get("MYSQL_HOST","localhost"),user=os.environ.get("MYSQL_USER","root"),password=os.environ.get("MYSQL_PASSWORD",""),database=os.environ.get("MYSQL_DATABASE","psvibe"),port=int(os.environ.get("MYSQL_PORT","3306")),charset="utf8mb4")
                try:
                    _rel_cur = _rel_conn.cursor()
                    _rel_conn.start_transaction()
                    # Release hold (handles both held/cancelled — session end may have cancelled)
                    _rel_cur.execute(
                        'UPDATE stock_hold SET status = \'released\', updated_at = NOW() WHERE booking_id = %s AND item_name = %s AND status IN ("held", "cancelled")',
                        (booking_id, item_name)
                    )
                    # Record actual stock-out
                    _rel_cur.execute(
                        "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, staff_name, notes) VALUES (%s, %s, %s, %s, NOW(), %s, %s)",
                        (item_name, qty, price, qty * price, staff, f"Food cart release: booking {booking_id}")
                    )
                    # Decrement inventory quantity for legacy summary column
                    _rel_cur.execute(
                        "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
                        (qty, item_name)
                    )
                    _rel_conn.commit()
                except Exception:
                    _rel_conn.rollback()
                    raise
                finally:
                    try:
                        _rel_conn.close()
                    except Exception:
                        pass
        # Mark food_cart entries as fulfilled
        _mysql_exec("UPDATE food_cart SET fulfilled_at = DATE_ADD(NOW(), INTERVAL 390 MINUTE) WHERE booking_id = %s AND fulfilled_at IS NULL", (booking_id,))
        return ok({"message": f"Released {len(items)} items for booking {booking_id}"})
    except Exception as e:
        logger.error("food_cart_release: %s", e)
        return error_response(str(e))


@app.post("/api/food-cart/cancel-release", tags=["Food"])
async def food_cart_cancel(data: dict, auth=Depends(verify_api_key)):
    """Cancel holds for a booking (no stock deduction)."""
    try:
        booking_id = str(data.get("booking_id", "")).strip()
        if not booking_id:
            return error_response("booking_id required")
        _mysql_exec(
            'UPDATE stock_hold SET status = \'cancelled\', updated_at = NOW() WHERE booking_id = %s AND status IN ("held", "cancelled")',
            (booking_id,)
        )
        _mysql_exec("DELETE FROM food_cart WHERE booking_id = %s AND fulfilled_at IS NULL", (booking_id,))
        return ok({"message": f"Cancelled holds for booking {booking_id}"})
    except Exception as e:
        logger.error("food_cart_cancel: %s", e)
        return error_response(str(e))


# ─── PS VIBE: Stale Check-In Auto-Cleanup ───

@app.post("/api/cleanup/stale-checkins", tags=["Maintenance"])
async def cleanup_stale_checkins(data: dict = None, auth=Depends(verify_api_key)):
    """
    Auto-cancel stale checked_in / pending_check_in bookings where:
    - start_time is in the past
    - No Active session exists for that console
    - Used at day-end closing (9 PM MMT / 14:30 UTC)
    """
    dry_run = False
    if data:
        dry_run = data.get("dry_run", False)
    
    try:
        # Find stale checkins — today + yesterday (covers late-night cases)
        rows = _mysql_query("""
            SELECT cb.id, cb.console_id, cb.staff_name, cb.member_id,
                   cb.planned_start AS start_time, cb.booking_date, cb.status,
                   cb.planned_duration_mins AS duration_mins
            FROM console_bookings cb
            LEFT JOIN console_sessions cs ON cs.booking_id=cb.id
            WHERE cb.status IN ('checked_in', 'pending_check_in')
              AND cb.booking_date >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
              AND cb.planned_start < NOW()
            ORDER BY cb.id
        """)
        
        cancelled_ids = []
        for r in (rows or []):
            bid = r["id"]
            cid = r["console_id"]
            name = r.get("staff_name") or r.get("member_id") or "Unknown"
            
            # Check if console has an Active session (safety check)
            active = _mysql_query(
                "SELECT id FROM console_bookings WHERE console_id=%s AND status='Active' LIMIT 1",
                (cid,)
            )
            if active:
                logger.info(f"cleanup_stale: skip #{bid} ({name}) — console {cid} has Active session")
                continue
            
            if dry_run:
                cancelled_ids.append(f"#{bid} ({name}/{cid}) — DRY RUN")
                continue
            
            _mysql_exec(
                "UPDATE console_bookings SET status='cancelled', notes=CONCAT(COALESCE(notes,''), ' | Auto-cancelled: stale check-in (day-end cleanup)') WHERE id=%s",
                (bid,)
            )
            # Free console if still occupied by stale checkin
            _mysql_exec(
                "UPDATE console_status SET status='Free', current_session_id=NULL WHERE console_id=%s AND status='Occupied'",
                (cid,)
            )
            cancelled_ids.append(f"#{bid} ({name}/{cid})")
            logger.info(f"cleanup_stale: auto-cancelled stale check-in #{bid} ({name}, {cid})")
        
        if not cancelled_ids:
            return ok({"message": "No stale check-ins found", "cancelled": 0, "bookings": []})
        
        return ok({
            "message": f"{len(cancelled_ids)} stale check-in(s) {'would be ' if dry_run else ''}cancelled" + (" (DRY RUN)" if dry_run else ""),
            "cancelled": len(cancelled_ids),
            "dry_run": dry_run,
            "bookings": cancelled_ids
        })
    except Exception as e:
        logger.error(f"cleanup_stale_checkins: {e}")
        return error_response(str(e))

@app.patch("/api/admin-notify/{booking_id}")
async def api_admin_notify_store(booking_id: int, req: dict, auth=Depends(verify_api_key)):
    """Store admin notification message_id on booking (called by Sale Bot after sending notif)."""
    try:
        msg_id = req.get("admin_notify_msg_id", "")
        if msg_id:
            _mysql_exec(
                "UPDATE console_bookings SET admin_notify_msg_id=%s WHERE id=%s",
                (str(msg_id), booking_id)
            )
            return {"success": True}
        return {"success": False, "error": "No admin_notify_msg_id provided"}
    except Exception as e:
        logger.error(f"admin-notify store failed: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  NEW CLEAN ENDPOINTS (GSheet-free, MySQL-only)
#  Wraps existing /api/sheets/* handlers with clean /api/* paths
# ═══════════════════════════════════════

@app.get("/api/config", tags=["Config"])
async def api_config_v2(auth=Depends(verify_api_key)):
    """Return all config settings from MySQL (new endpoint)."""
    try:
        data = _mysql_get_settings_dict()
        return ok({"config": data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/consoles", tags=["Consoles"])
async def api_consoles_v2(auth=Depends(verify_api_key)):
    """Return console list with live status from MySQL (new endpoint)."""
    try:
        rows = _mysql_query("SELECT console_id, status, current_game, current_member, start_time FROM console_status ORDER BY console_id")
        try:
            upcoming = _mysql_query(
                "SELECT console_id, planned_start AS start_time, planned_end AS end_time FROM console_bookings "
                "WHERE status IN ('confirmed', 'pending_check_in') "
                "AND start_time <= NOW() AND end_time > NOW()"
            )
            reserved_consoles = {r["console_id"].strip() for r in upcoming}
        except Exception:
            reserved_consoles = set()
        for r in rows:
            cid = r["console_id"].strip()
            if r["status"] == "Free" and cid in reserved_consoles:
                r["status"] = "Reserved"
        return ok({"consoles": rows, "date": today_str()})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/daily", tags=["Stock"])
async def api_stock_daily_v2(auth=Depends(verify_api_key)):
    """Return today's stock movement summary from MySQL (new endpoint)."""
    try:
        today = today_str()
        result = {"date": today, "stock_in": [], "stock_out": [], "in_total": 0, "out_total": 0}
        try:
            rows = _mysql_query("SELECT item_name, quantity, unit_cost FROM stock_in WHERE receipt_no = %s", (today,))
            for r in rows:
                item = r["item_name"]
                qty = r["quantity"]
                cost = int(float(r["unit_cost"]))
                result["stock_in"].append({"item": item, "qty": qty, "cost": cost})
                result["in_total"] += cost * qty
        except Exception as e:
            logger.warning("Stock_In read error: %s", e)
        try:
            rows = _mysql_query(
                "SELECT item_name, quantity, COALESCE(unit_price, 0) AS unit_price FROM stock_out WHERE DATE(sale_date) = CURDATE()")
            for r in rows:
                item = r.get("item_name", "")
                qty = r.get("quantity", 0) or 0
                cost = int(float(r.get("unit_price", 0) or 0))
                result["stock_out"].append({"item": item, "qty": qty, "cost": cost})
                result["out_total"] += cost * qty
        except Exception as e:
            logger.warning("Stock_Out read error: %s", e)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/valuation", tags=["Stock"])
async def api_stock_valuation_v2(auth=Depends(verify_api_key)):
    """Return inventory data using MySQL FIFO valuation (new endpoint)."""
    try:
        fifo = get_fifo_valuation()
        items = []
        total_cost = 0
        categories = {}
        for i in fifo.get("items", []):
            name = i["item_name"]
            qty = max(0, i["quantity"])
            total = int(i["fifo_value"])
            cost = int(round(total / qty)) if qty > 0 else 0
            cat = "Uncategorized"
            total_cost += total
            if cat not in categories:
                categories[cat] = 0
            categories[cat] += total
            items.append({
                "name": name, "category": cat, "qty": qty,
                "cost": cost, "price": 0, "total": total,
            })
        return ok({
            "items": items,
            "categories": categories,
            "total_cost": total_cost,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reports/daily", tags=["Reports"])
async def api_reports_daily_v2(auth=Depends(verify_api_key)):
    """Return aggregated daily report data from MySQL (new endpoint)."""
    try:
        today = today_str()
        result = {
            "date": today,
            "total_sales": 0,
            "voucher_count": 0,
            "payment_breakdown": {},
            "console_usage": {},
            "top_ups": {"count": 0, "amount": 0, "mins": 0},
            "members_served": 0,
        }
        try:
            rows = _mysql_query("SELECT amount, payment_method, member_id FROM sales_daily WHERE sale_date = CURDATE()")
            members_set = set()
            for r in rows:
                amt = int(float(r["amount"] or 0))
                payment = (r["payment_method"] or "Unknown").strip().title()
                member = (r["member_id"] or "").strip()
                result["total_sales"] += amt
                result["voucher_count"] += 1
                if payment not in result["payment_breakdown"]:
                    result["payment_breakdown"][payment] = 0
                result["payment_breakdown"][payment] += amt
                if member:
                    members_set.add(member)
            result["members_served"] = len(members_set)
        except Exception as e:
            logger.warning("report-data sales error: %s", e)
        try:
            rows = _mysql_query("SELECT console_id FROM console_bookings WHERE booking_date = CURDATE()")
            for r in rows:
                cid = (r["console_id"] or "").strip()
                if cid:
                    result["console_usage"][cid] = result["console_usage"].get(cid, 0) + 1
        except Exception as e:
            logger.warning("report-data console error: %s", e)
        try:
            rows = _mysql_query("SELECT amount, mins_added FROM topup_log WHERE DATE(topup_date) = CURDATE()")
            for r in rows:
                result["top_ups"]["count"] += 1
                result["top_ups"]["amount"] += int(float(r["amount"] or 0))
                result["top_ups"]["mins"] += int(r["mins_added"] or 0)
        except Exception as e:
            logger.warning("report-data topup error: %s", e)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reports/weekly", tags=["Reports"])
async def api_reports_weekly_v2(auth=Depends(verify_api_key)):
    """Return weekly aggregated report data (new endpoint)."""
    try:
        mmt = now_mmt()
        weekday = mmt.weekday()
        week_start = mmt - timedelta(days=weekday)
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        result = {
            "week_start": week_start.strftime("%Y-%m-%d"),
            "week_end": (week_start + timedelta(days=7)).strftime("%Y-%m-%d"),
            "daily_revenue": {},
            "total_revenue": 0,
            "total_vouchers": 0,
            "total_topups": 0,
            "topup_revenue": 0,
        }
        try:
            sd_rows = _mysql_query(
                "SELECT amount, sale_date FROM sales_daily WHERE sale_date >= %s AND sale_date < %s",
                (week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")))
            for r in sd_rows:
                day_key = r["sale_date"].strftime("%Y-%m-%d")
                amt = int(float(r["amount"] or 0))
                if day_key not in result["daily_revenue"]:
                    result["daily_revenue"][day_key] = 0
                result["daily_revenue"][day_key] += amt
                result["total_revenue"] += amt
                result["total_vouchers"] += 1
        except Exception as e:
            logger.warning("weekly-report sales error: %s", e)
        try:
            tu_rows = _mysql_query(
                "SELECT amount FROM topup_log WHERE topup_date >= %s AND topup_date < %s",
                (week_start.strftime("%Y-%m-%d 00:00:00"), week_end.strftime("%Y-%m-%d 00:00:00")))
            for r in tu_rows:
                amt = int(float(r["amount"] or 0))
                result["total_topups"] += 1
                result["topup_revenue"] += amt
                result["total_revenue"] += amt
        except Exception as e:
            logger.warning("weekly-report topup error: %s", e)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reports/pnl", tags=["Reports"])
async def api_reports_pnl_v2(m: str = Query("", description="Month in YYYY-MM format"), auth=Depends(verify_api_key)):
    """Return P&L summary for a month (new endpoint)."""
    try:
        mmt = now_mmt()
        month = m if m else f"{mmt.year}-{mmt.month:02d}"
        month_slash = month.replace("-", "/")
        parts = month.split("-")
        year_val = int(parts[0])
        month_val = int(parts[1])
        result = {
            "month": month,
            "revenue": {"console_rental": 0, "food_sales": 0, "product_sales": 0, "topup_sales": 0},
            "expenses": {"salaries": 0, "utilities": 0, "supplies": 0, "rent": 0, "other": 0},
            "total_revenue": 0,
            "total_expenses": 0,
            "net_profit": 0,
        }
        try:
            rows = _mysql_query(
                "SELECT amount FROM sales_daily WHERE YEAR(sale_date)=%s AND MONTH(sale_date)=%s",
                (year_val, month_val))
            for r in rows:
                result["revenue"]["console_rental"] += int(float(r["amount"] or 0))
        except Exception as e:
            logger.warning("pnl sales error: %s", e)
        try:
            rows = _mysql_query(
                "SELECT amount FROM topup_log WHERE YEAR(topup_date)=%s AND MONTH(topup_date)=%s",
                (year_val, month_val))
            for r in rows:
                result["revenue"]["topup_sales"] += int(float(r["amount"] or 0))
        except Exception as e:
            logger.warning("pnl topup error: %s", e)
        try:
            rows = _mysql_query("SELECT COALESCE(SUM(base_salary), 0) AS total FROM staff_records WHERE is_active=1")
            result["expenses"]["salaries"] = int(rows[0]["total"]) if rows else 0
        except Exception as e:
            logger.warning("pnl salary error: %s", e)
        result["total_revenue"] = sum(result["revenue"].values())
        result["total_expenses"] = sum(result["expenses"].values())
        result["net_profit"] = result["total_revenue"] - result["total_expenses"]
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/staff/breakdown", tags=["Staff"])
async def api_staff_breakdown_v2(auth=Depends(verify_api_key)):
    """Return staff salary/stats breakdown from MySQL (new endpoint)."""
    try:
        mmt = now_mmt()
        month_ym = f"{mmt.year}-{mmt.month:02d}"
        month_slash = f"{mmt.month}/{mmt.year}"
        rows = _mysql_query("SELECT staff_name, base_salary FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        result = {}
        for r in rows:
            name = r["staff_name"].strip()
            sal = int(float(r["base_salary"] or 0))
            result[name] = {"base_salary": sal, "deductions": 0, "advances": 0, "net_pay": sal}
        try:
            att_rows = _mysql_query(
                "SELECT staff_name, COUNT(*) AS late_count FROM attendance_log "
                "WHERE DATE_FORMAT(date, %s) = %s AND status != %s GROUP BY staff_name",
                ("%c/%Y", month_slash, "Present"))
            for r in att_rows:
                staff = r["staff_name"].strip()
                if staff in result:
                    late = r["late_count"]
                    result[staff]["deductions"] = late * 500
                    result[staff]["net_pay"] = result[staff]["base_salary"] - result[staff]["deductions"]
        except Exception as e:
            logger.warning("staff-breakdown attendance error: %s", e)
        try:
            adv_rows = _mysql_query(
                "SELECT staff_name, COALESCE(SUM(amount), 0) AS total_advance FROM salary_advance "
                "WHERE DATE_FORMAT(advance_date, %s) = %s GROUP BY staff_name",
                ("%Y-%m", month_ym))
            for r in adv_rows:
                staff = r["staff_name"].strip()
                if staff in result:
                    amt = int(float(r["total_advance"] or 0))
                    result[staff]["advances"] = amt
                    result[staff]["net_pay"] -= amt
        except Exception as e:
            logger.warning("staff-breakdown advance error: %s", e)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/promotions", tags=["Promotions"])
async def api_promotions_v2(auth=Depends(verify_api_key)):
    """Get all promotions from MySQL (new endpoint)."""
    try:
        rows = _mysql_query("SELECT id, promo_name, discount_type, discount_value, start_date, end_date, status, notes FROM promotions ORDER BY id")
        return ok({"promotions": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/promotions/log", tags=["Promotions"])
async def api_promotions_log_v2(auth=Depends(verify_api_key)):
    """Get promotions log from MySQL (new endpoint)."""
    try:
        rows = _mysql_query("SELECT id, voucher_no, promo_id, promo_title, member_id, console_id, gross_total, discount_amt, net_total, staff_name, promo_date FROM promotions_log ORDER BY id DESC LIMIT 500")
        return ok({"promotions_log": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/promotions/log", tags=["Promotions"])
async def api_promotions_log_post_v2(req: dict, auth=Depends(verify_api_key)):
    """Log promotion usage to MySQL (new endpoint)."""
    try:
        _mysql_exec("INSERT INTO promotions_log (voucher_no, promo_id, promo_title, member_id, console_id, gross_total, discount_amt, net_total, staff_name, promo_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (req.get("voucher_no",""), req.get("promo_id",""), req.get("promo_title",""), req.get("member_id",""), req.get("console_id",""), req.get("gross_total",0), req.get("discount_amt",0), req.get("net_total",0), req.get("staff_name",""), req.get("promo_date", today_str())))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
