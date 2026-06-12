from app import *


# Lazy imports to avoid circular dependency with app.py
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

# Additional imports for FIFO inventory
from inventory_fifo import get_fifo_valuation

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
        except:
            result["timeSlot"] = str(row["start_time"])[11:16]
    if "end_time" in row and row["end_time"]:
        try:
            result["endTime"] = row["end_time"].strftime("%H:%M")
        except:
            pass
    if "staff_name" in row and row["staff_name"]:
        result["staffName"] = row["staff_name"]

    return result


SHEET_STOCK_IN = "Stock_In"
SHEET_STOCK_OUT = "Stock_Out"

# ═══════════════════════════════════════
#  SHEETS — inventory
# ═══════════════════════════════════════
@app.get("/api/sheets/inventory", tags=["Sheets"])
async def api_sheets_inventory(auth=Depends(verify_api_key)):
    """Return inventory data using MySQL FIFO valuation (real-time from stock_in/stock_out)."""
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


# ═══════════════════════════════════════
#  SHEETS — consoles
# ═══════════════════════════════════════
@app.get("/api/sheets/consoles", tags=["Sheets"])
async def api_sheets_consoles(auth=Depends(verify_api_key)):
    """Return console list with live status from MySQL. Marks Reserved if confirmed booking within 2 hours."""
    try:
        rows = _mysql_query("SELECT console_id, status, current_game, current_member, start_time FROM console_status ORDER BY console_id")
        
        # Check for confirmed bookings whose time slot includes NOW
        try:
            upcoming = _mysql_query(
                "SELECT console_id, start_time, end_time FROM console_booking "
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


# ═══════════════════════════════════════
#  SHEETS — stock-today
# ═══════════════════════════════════════
@app.get("/api/sheets/stock-today", tags=["Sheets"])
async def api_sheets_stock_today(auth=Depends(verify_api_key)):
    """Return today's stock movement summary from MySQL stock_in / stock_out."""
    try:
        today = today_str()
        result = {"date": today, "stock_in": [], "stock_out": [], "in_total": 0, "out_total": 0}

        # Stock In (MySQL)
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

        # Stock Out (MySQL)
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


# ═══════════════════════════════════════
#  SHEETS — report-data
# ═══════════════════════════════════════
@app.get("/api/sheets/report-data", tags=["Sheets"])
async def api_sheets_report_data(auth=Depends(verify_api_key)):
    """Return aggregated daily report data from MySQL."""
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

        # Sales_Daily (MySQL)
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

        # Console usage from bookings (MySQL)
        try:
            rows = _mysql_query("SELECT console_id FROM console_booking WHERE booking_date = CURDATE()")
            for r in rows:
                cid = (r["console_id"] or "").strip()
                if cid:
                    result["console_usage"][cid] = result["console_usage"].get(cid, 0) + 1
        except Exception as e:
            logger.warning("report-data console error: %s", e)

        # Top-ups (MySQL)
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


# ═══════════════════════════════════════
#  SHEETS — staff-breakdown
# ═══════════════════════════════════════
@app.get("/api/sheets/staff-breakdown", tags=["Sheets"])
async def api_sheets_staff_breakdown(auth=Depends(verify_api_key)):
    """Return staff salary/stats breakdown from MySQL."""
    try:
        mmt = now_mmt()
        month_ym = f"{mmt.year}-{mmt.month:02d}"
        month_slash = f"{mmt.month}/{mmt.year}"

        # Base salaries from staff_records
        rows = _mysql_query("SELECT staff_name, base_salary FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        result = {}
        for r in rows:
            name = r["staff_name"].strip()
            sal = int(float(r["base_salary"] or 0))
            result[name] = {"base_salary": sal, "deductions": 0, "advances": 0, "net_pay": sal}

        # Attendance deductions (current month, non-present days)
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

        # Salary advances (current month)
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


# ═══════════════════════════════════════
#  SHEETS — pnl
# ═══════════════════════════════════════
@app.get("/api/sheets/pnl", tags=["Sheets"])
async def api_sheets_pnl(m: str = Query("", description="Month in YYYY-MM format"), auth=Depends(verify_api_key)):
    """Return P&L summary for a month."""
    try:
        mmt = now_mmt()
        month = m if m else f"{mmt.year}-{mmt.month:02d}"
        month_slash = month.replace("-", "/")

        result = {
            "month": month,
            "revenue": {"console_rental": 0, "food_sales": 0, "product_sales": 0, "topup_sales": 0},
            "expenses": {"salaries": 0, "utilities": 0, "supplies": 0, "rent": 0, "other": 0},
            "total_revenue": 0,
            "total_expenses": 0,
            "net_profit": 0,
        }

        # Revenue from Sales_Daily (MySQL)
        try:
            rows = _mysql_query(
                "SELECT amount FROM sales_daily WHERE YEAR(sale_date)=%s AND MONTH(sale_date)=%s",
                (mmt.year, mmt.month))
            for r in rows:
                result["revenue"]["console_rental"] += int(float(r["amount"] or 0))
        except Exception as e:
            logger.warning("pnl sales error: %s", e)

        # TopUp revenue (MySQL)
        try:
            rows = _mysql_query(
                "SELECT amount FROM topup_log WHERE YEAR(topup_date)=%s AND MONTH(topup_date)=%s",
                (mmt.year, mmt.month))
            for r in rows:
                result["revenue"]["topup_sales"] += int(float(r["amount"] or 0))
        except Exception as e:
            logger.warning("pnl topup error: %s", e)

        # Expenses from salaries (MySQL)
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


# ═══════════════════════════════════════
#  SHEETS — liability
# ═══════════════════════════════════════
@app.get("/api/sheets/liability", tags=["Sheets"])
async def api_sheets_liability(auth=Depends(verify_api_key)):
    """Return liability summary from MySQL."""
    try:
        result = {
            "wallet_liability_mins": 0,
            "wallet_liability_ks": 0,
            "salary_advances": 0,
            "outstanding_payables": 0,
            "total_liability": 0,
        }

        # Wallet liability (MySQL)
        rows = _mysql_query("SELECT COALESCE(SUM(balance_mins), 0) AS total_mins FROM member_wallets")
        result["wallet_liability_mins"] = int(rows[0]["total_mins"]) if rows else 0

        # Base rate from settings for KS conversion
        try:
            base_rate = int(float(_mysql_get_setting("base_rate", 0)))
            if base_rate > 0:
                result["wallet_liability_ks"] = int(result["wallet_liability_mins"] * base_rate / 60)
        except Exception:
            pass

        # Salary advances (current month, MySQL)
        try:
            mmt = now_mmt()
            month_slash = f"{mmt.month}/{mmt.year}"
            rows = _mysql_query(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM salary_advance "
                "WHERE DATE_FORMAT(advance_date, %s) = %s",
                ("%c/%Y", month_slash))
            result["salary_advances"] = int(float(rows[0]["total"] or 0)) if rows else 0
        except Exception as e:
            logger.warning("liability advance error: %s", e)

        result["total_liability"] = result["wallet_liability_ks"] + result["salary_advances"]
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  SHEETS — payment-methods
# ═══════════════════════════════════════
@app.get("/api/sheets/payment-methods", tags=["Sheets"])
async def api_sheets_payment_methods(auth=Depends(verify_api_key)):
    """Get payment methods from MySQL settings."""
    try:
        val = _mysql_get_setting("payment_methods", [])
        return ok({"payment_methods": val if isinstance(val, list) else []})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  SHEETS — promotions
# ═══════════════════════════════════════
@app.get("/api/sheets/promotions", tags=["Sheets"])
async def api_sheets_promotions(auth=Depends(verify_api_key)):
    """Get promotions from MySQL."""
    try:
        rows = _mysql_query("SELECT id, promo_name, discount_type, discount_value, start_date, end_date, status FROM promotions ORDER BY id")
        return ok({"promotions": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sheets/promotions/all", tags=["Sheets"])
async def api_sheets_promotions_all(auth=Depends(verify_api_key)):
    """Get all promotions from MySQL."""
    try:
        rows = _mysql_query("SELECT id, promo_name, discount_type, discount_value, start_date, end_date, status, notes FROM promotions ORDER BY id")
        return ok({"promotions": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log(auth=Depends(verify_api_key)):
    """Get promotions log from MySQL."""
    try:
        rows = _mysql_query("SELECT id, voucher_no, promo_id, promo_title, member_id, console_id, gross_total, discount_amt, net_total, staff_name, promo_date FROM promotions_log ORDER BY id DESC LIMIT 500")
        return ok({"promotions_log": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log_post(req: dict, auth=Depends(verify_api_key)):
    """Log promotion usage to MySQL."""
    try:
        _mysql_exec("INSERT INTO promotions_log (voucher_no, promo_id, promo_title, member_id, console_id, gross_total, discount_amt, net_total, staff_name, promo_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (req.get("voucher_no",""), req.get("promo_id",""), req.get("promo_title",""), req.get("member_id",""), req.get("console_id",""), req.get("gross_total",0), req.get("discount_amt",0), req.get("net_total",0), req.get("staff_name",""), req.get("promo_date", today_str())))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  SHEETS — weekly-report
# ═══════════════════════════════════════
@app.get("/api/sheets/weekly-report", tags=["Sheets"])
async def api_sheets_weekly_report(auth=Depends(verify_api_key)):
    """Return weekly aggregated report data."""
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
        sql = "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking"
        params = []
        if status:
            sql += " WHERE status = %s"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT 500"
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
        sql = "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking WHERE 1=1"
        params = []
        if date:
            sql += " AND booking_date = %s"
            params.append(date)
        if status:
            sql += " AND status = %s"
            params.append(status)
        if chat_id:
            sql += " AND member_id = %s"
            params.append(chat_id)
        if telegram_chat_id:
            sql += " AND telegram_chat_id = %s"
            params.append(telegram_chat_id)
        sql += " ORDER BY id DESC LIMIT 200"
        rows = _mysql_query(sql, tuple(params))
        return ok({"bookings": [_map_booking_row(r) for r in rows]})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bookings/{booking_id}", tags=["Bookings"])
async def api_bookings_get(booking_id: str, auth=Depends(verify_api_key)):
    """Get a booking from MySQL."""
    try:
        rows = _mysql_query("SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking WHERE id=%s", (booking_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Booking not found")
        return ok({"booking": _map_booking_row(rows[0])})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  BOOKINGS — broadcast-targets
# ═══════════════════════════════════════
@app.get("/api/bookings/broadcast-targets", tags=["Bookings"])
async def api_bookings_broadcast_targets(auth=Depends(verify_api_key)):
    """Get broadcast targets from MySQL."""
    try:
        rows = _mysql_query("SELECT DISTINCT member_id FROM console_booking WHERE status='Active'")
        targets = [r["member_id"] for r in rows]
        return ok({"targets": targets, "count": len(targets)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  WAITLIST — CRUD
# ═══════════════════════════════════════
@app.get("/api/waitlist", tags=["Waitlist"])
async def api_waitlist_list(auth=Depends(verify_api_key)):
    """List waitlist from MySQL."""
    try:
        rows = _mysql_query("SELECT id, console_id, member_id, booking_date, start_time, status, staff_name, notes FROM console_booking WHERE status='Waiting' ORDER BY id")
        return ok({"waitlist": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/waitlist/{entry_id}", tags=["Waitlist"])
async def api_waitlist_get(entry_id: str, auth=Depends(verify_api_key)):
    """Get waitlist entry from MySQL."""
    try:
        rows = _mysql_query("SELECT id, console_id, member_id, booking_date, start_time, status, staff_name, notes FROM console_booking WHERE id=%s AND status='Waiting'", (entry_id,))
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
        _mysql_exec("INSERT INTO console_booking (console_id, member_id, booking_date, start_time, status, staff_name, notes) VALUES (%s,%s,CURDATE(),NOW(),'Waiting',%s,%s)", (req.get("console_id",""), req.get("member_id",""), req.get("staff_name",""), req.get("notes","")))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/waitlist/{entry_id}", tags=["Waitlist"])
async def api_waitlist_delete(entry_id: str, auth=Depends(verify_api_key)):
    """Remove from waitlist in MySQL."""
    try:
        _mysql_exec("DELETE FROM console_booking WHERE id=%s AND status='Waiting'", (entry_id,))
        return ok({"deleted": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/waitlist/notify", tags=["Waitlist"])
async def api_waitlist_notify(req: dict, auth=Depends(verify_api_key)):
    """Notify waitlist entry in MySQL."""
    try:
        _mysql_exec("UPDATE console_booking SET status='Notified' WHERE id=%s", (req.get("entry_id",""),))
        return ok({"notified": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  FINANCE — endpoints
# ═══════════════════════════════════════
@app.get("/api/finance/pnl", tags=["Finance"])
async def api_finance_pnl(m: str = Query("", description="Month in YYYY-MM format"), auth=Depends(verify_api_key)):
    """Return P&L data for a month."""
    mmt = now_mmt()
    month = m if m else f"{mmt.year}-{mmt.month:02d}"
    month_slash = month.replace("-", "/")
    
    result = {
        "month": month,
        "revenue": {"console": 0, "food": 0, "products": 0, "topup": 0, "other": 0},
        "cogs": 0,
        "gross_profit": 0,
        "expenses": {"salaries": 0, "rent": 0, "utilities": 0, "supplies": 0, "other": 0},
        "total_revenue": 0,
        "total_expenses": 0,
        "net_profit": 0,
    }

    try:
        rows = _mysql_query(
            "SELECT amount FROM sales_daily WHERE YEAR(sale_date)=%s AND MONTH(sale_date)=%s",
            (mmt.year, mmt.month))
        for r in rows:
            amt = int(float(r["amount"] or 0))
            result["revenue"]["console"] += amt
            result["total_revenue"] += amt
    except Exception as e:
        logger.warning("finance/pnl sales error: %s", e)

    try:
        rows = _mysql_query(
            "SELECT amount FROM topup_log WHERE YEAR(topup_date)=%s AND MONTH(topup_date)=%s",
            (mmt.year, mmt.month))
        for r in rows:
            amt = int(float(r["amount"] or 0))
            result["revenue"]["topup"] += amt
            result["total_revenue"] += amt
    except Exception as e:
        logger.warning("finance/pnl topup error: %s", e)

    # Expenses (MySQL)
    try:
        rows = _mysql_query("SELECT COALESCE(SUM(base_salary), 0) AS total FROM staff_records WHERE is_active=1")
        result["expenses"]["salaries"] = int(float(rows[0]["total"] or 0)) if rows else 0
    except Exception:
        pass

    result["total_expenses"] = sum(result["expenses"].values())
    result["gross_profit"] = result["total_revenue"] - result["cogs"]
    result["net_profit"] = result["total_revenue"] - result["total_expenses"]

    return ok(result)


@app.get("/api/finance/balance-sheet", tags=["Finance"])
async def api_finance_balance_sheet(auth=Depends(verify_api_key)):
    """Return balance sheet summary."""
    result = {
        "assets": {"cash": 0, "inventory_value": 0, "equipment_value": 0, "total": 0},
        "liabilities": {"wallet_liability": 0, "advances": 0, "payables": 0, "total": 0},
        "equity": {"retained_earnings": 0, "total": 0},
    }

    # Wallet liability (MySQL)
    rows = _mysql_query("SELECT COALESCE(SUM(balance_mins), 0) AS total FROM member_wallets")
    total_mins = int(rows[0]["total"]) if rows else 0
    try:
        base_rate = int(float(_mysql_get_setting("base_rate", 0)))
        if base_rate > 0:
            result["liabilities"]["wallet_liability"] = int(total_mins * base_rate / 60)
        else:
            result["liabilities"]["wallet_liability"] = 0
    except Exception:
        result["liabilities"]["wallet_liability"] = 0

    result["liabilities"]["total"] = result["liabilities"]["wallet_liability"]
    result["assets"]["total"] = result["liabilities"]["total"] + result["equity"]["total"]
    
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
    """Return real-time account balances calculated from transactions."""
    try:
        # 1. Get initial balances from accounts table
        rows = _mysql_query("""
            SELECT a.account_name, a.account_type, a.balance, a.notes
            FROM accounts a
            INNER JOIN (
                SELECT account_name, MAX(id) AS max_id
                FROM accounts
                GROUP BY account_name
            ) latest ON a.id = latest.max_id
            ORDER BY FIELD(a.account_type, 'Cash', 'Digital', 'Bank', 'Capital'), a.account_name
        """)
        
        # Map account names to payment method keywords for aggregation
        account_map = {
            "Cash": "cash",
            "KPay": "kpay",
            "Wave": "wave",
            "AYA Pay": "aya",
            "KBZ Bank": None,  # Capital - skip in real-time calc
        }
        
        # Initialize balances with existing account values as base
        operating = []
        capital = []
        total_op = 0.0
        total_cap = 0.0
        base_by_acct = {}
        
        for r in rows:
            bal = float(r["balance"] or 0)
            acct = {
                "name": r["account_name"],
                "type": r["account_type"],
                "balance": bal,
                "notes": r.get("notes", "") or "",
            }
            if r["account_type"] == "Capital":
                capital.append(acct)
                total_cap += bal
            else:
                base_by_acct[r["account_name"]] = bal
        
        # 2. Calculate incoming money from sales_daily
        sale_rows = _mysql_query("""
            SELECT payment_method, notes
            FROM sales_daily
            WHERE sale_date >= '2026-01-01'
        """)
        
        # Parse payment_method field (format: "KPay:amount|Cash:amount") and aggregate
        income_by_acct = {"cash": 0.0, "kpay": 0.0, "wave": 0.0, "aya": 0.0, "acm": 0.0, "kbz": 0.0}
        
        for r in sale_rows:
            _note = (r.get("notes") or "").strip()
            if _note.startswith("Topup") or _note.startswith("New member"):
                continue
            pm = r["payment_method"] or ""

            if "|" in pm:
                # Parse each segment
                segments = pm.split("|")
                for seg in segments:
                    seg = seg.strip()
                    if ":" in seg:
                        method, amt_str = seg.split(":", 1)
                        try:
                            amt = float(amt_str)
                        except:
                            amt = 0
                        method_lower = method.lower().strip()
                        if method_lower == "cash":
                            income_by_acct["cash"] += amt
                        elif method_lower == "kpay":
                            income_by_acct["kpay"] += amt
                        elif method_lower in ("wave", "wavepay"):
                            income_by_acct["wave"] += amt
                        elif method_lower in ("aya", "aya pay"):
                            income_by_acct["aya"] += amt
                        elif "transfer" in method_lower or "bank" in method_lower:
                            income_by_acct["kpay"] += amt
                        elif method_lower == "wave":
                            income_by_acct["wave"] += amt
                        elif method_lower == "aya" or method_lower == "aya pay":
                            income_by_acct["aya"] += amt
            elif ":" in pm:
                # Single method format
                method, amt_str = pm.split(":", 1)
                try:
                    amt = float(amt_str)
                except:
                    amt = total  # fallback: use total
                method_lower = method.lower().strip()
                if method_lower == "cash":
                    income_by_acct["cash"] += amt
                elif method_lower == "kpay":
                    income_by_acct["kpay"] += amt
                elif method_lower in ("wave", "wavepay"):
                    income_by_acct["wave"] += amt
                elif method_lower in ("aya", "aya pay"):
                    income_by_acct["aya"] += amt
        
        # 3. Include cash_movements (inject adds, eject subtracts)
        cm_rows = _mysql_query("SELECT movement_type, account, SUM(amount) as total FROM cash_movements GROUP BY movement_type, account")
        # Fetch inject entries that overlap with topup (notes start with Topup or New member)
        _bad_inject = _mysql_query("SELECT account, SUM(amount) as total FROM cash_movements WHERE movement_type = 'inject' AND (note IS NOT NULL AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37)))) GROUP BY account")
        _bad_inject_map = {}
        for _r in _bad_inject:
            _bad_inject_map[_r['account'].strip().lower()] = float(_r['total'] or 0)
        for r in cm_rows:
            acct_key = r["account"].strip().lower()
            if acct_key == "kpay": acct_key = "kpay"
            elif acct_key == "cash": acct_key = "cash"
            elif acct_key in ("wave", "wavepay"): acct_key = "wave"
            elif acct_key in ("aya", "aya pay"): acct_key = "aya"
            elif acct_key in ("acm", "acm\'s acc"): acct_key = "acm"
            elif acct_key in ("kbz", "kbz bank"): acct_key = "kbz"
            else: continue
            amt = float(r["total"] or 0)
            if r["movement_type"] in ("inject", "transfer_in"):
                amt_adj = amt
                if r["movement_type"] == "inject" and acct_key in _bad_inject_map:
                    amt_adj = amt - _bad_inject_map[acct_key]
                income_by_acct[acct_key] += amt_adj
            elif r["movement_type"] == "eject":
                income_by_acct[acct_key] -= amt
            elif r["movement_type"] == "transfer_out":
                income_by_acct[acct_key] += amt  # negative amount = subtract
        
        # 4. Also add topup_log income
        topup_rows = _mysql_query("""
            SELECT payment_method, amount
            FROM topup_log
            WHERE topup_date >= '2026-01-01'
        """)
        
        for r in topup_rows:
            pm = r["payment_method"] or ""

            if "/" in pm:
                segments = pm.split("/")
                for seg in segments:
                    seg = seg.strip()
                    if ":" in seg:
                        method, amt_str = seg.split(":", 1)
                        try:
                            amt = float(amt_str)
                        except:
                            amt = 0
                        method_lower = method.lower().strip()
                        if method_lower == "cash":
                            income_by_acct["cash"] += amt
                        elif method_lower == "kpay":
                            income_by_acct["kpay"] += amt
                        elif method_lower in ("wave", "wavepay"):
                            income_by_acct["wave"] += amt
                        elif method_lower in ("aya", "aya pay"):
                            income_by_acct["aya"] += amt
                        elif "transfer" in method_lower or "bank" in method_lower:
                            income_by_acct["kpay"] += amt
                        elif method_lower == "wave":
                            income_by_acct["wave"] += amt
                        elif method_lower == "aya" or method_lower == "aya pay":
                            income_by_acct["aya"] += amt
        
        # 4. Subtract OPEX expenses by payment method
        opex_rows = _mysql_query("SELECT payment_method, SUM(amount) as total FROM opex GROUP BY payment_method")
        for r in opex_rows:
            pm = (r["payment_method"] or "").lower().strip()
            amt = float(r["total"] or 0)
            if pm == "cash":
                income_by_acct["cash"] -= amt
            elif pm == "kpay":
                income_by_acct["kpay"] -= amt
            elif pm in ("wave", "wavepay"):
                income_by_acct["wave"] -= amt
            elif pm in ("aya", "aya pay"):
                income_by_acct["aya"] -= amt
            elif pm in ("kbz", "kbz bank"):
                income_by_acct["kbz"] = income_by_acct.get("kbz", 0) - amt

        # 5. Build final operating list with real-time balances
        operating_names = ["Cash", "KPay", "Wave", "AYA Pay", "ACM\'s Acc"]
        for name in operating_names:
            base = base_by_acct.get(name, 0)
            keyword = name.lower()
            # ACM's Acc is stored as "acm" in income_by_acct
            if keyword in ("acm's acc",):
                keyword = "acm"
            income = income_by_acct.get(keyword, 0)
            # Real-time balance = base (from accounts table) + income from transactions
            final_bal = base + income
            icon_notes = {"Cash": "ေငြသား", "KPay": "KPay", "Wave": "Wave", "AYA Pay": "AYA Pay"}
            operating.append({
                "name": name,
                "type": "Cash" if name == "Cash" else "Digital",
                "balance": final_bal,
                "notes": icon_notes.get(name, ""),
            })
            total_op += final_bal
        
        # Capital accounts: KBZ Bank real balance from transactions
        kbz_base = 0.0
        for c in capital:
            if c["name"] == "KBZ Bank":
                kbz_base = c["balance"]
                break
        
        # income_by_acct["kbz"] includes opex + cash movements
        kbz_balance = kbz_base + income_by_acct.get("kbz", 0)
        
        # Deduct capital expenditures from KBZ Bank
        a_rows = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM finance_assets WHERE status='active'")
        av_rows = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances")
        pr_rows = _mysql_query("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid")
        d_rows = _mysql_query("SELECT COALESCE(SUM(disposal_amount),0) as t FROM finance_assets WHERE status='disposed' AND disposal_amount>0")
        asset_ded = float(a_rows[0]["t"] or 0) + float(av_rows[0]["t"] or 0) + float(pr_rows[0]["t"] or 0)
        disposal_add = float(d_rows[0]["t"] or 0)
        kbz_balance = kbz_balance - asset_ded + disposal_add
        
        for c in capital:
            if c["name"] == "KBZ Bank":
                c["balance"] = round(kbz_balance, 0)
                break
        total_cap = sum(c["balance"] for c in capital)
        
        # Separate ACM's Acc from store total
        acm_balance = 0.0
        store_balance = 0.0
        for o in operating:
            if o["name"] == "ACM's Acc":
                acm_balance = o["balance"]
            else:
                store_balance += o["balance"]
        
        # Include ACM in grand total but show separate store_total
        return ok({
            "operating": operating,
            "capital": capital,
            "store_total": round(store_balance, 0),
            "acm_total": round(acm_balance, 0),
            "total_capital": total_cap,
            "grand_total": round(store_balance + acm_balance + total_cap, 0),
        })
    except Exception as e:
        logger.error(f"api_finance_account_balances: {e}")
        return ok({"operating": [], "capital": [], "total_operating": 0, "total_capital": 0, "grand_total": 0})






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
#  SHEETS — config
@app.get("/api/sheets/config", tags=["Sheets"])
async def api_sheets_config(auth=Depends(verify_api_key)):
    """Return all config settings from MySQL."""
    try:
        data = _mysql_get_settings_dict()
        return ok({"config": data})
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
                    "UPDATE console_booking SET status=%s, staff_name=%s, notes=CONCAT(IFNULL(notes,''), %s), console_id=%s WHERE id=%s",
                    (new_status, staff_name_val, staff_note, console_id, booking_id)
                )
            else:
                _mysql_exec(
                    "UPDATE console_booking SET status=%s, notes=CONCAT(IFNULL(notes,''), %s), console_id=%s WHERE id=%s",
                    (new_status, staff_note, console_id, booking_id)
                )
        else:
            if staff_name_val:
                _mysql_exec(
                    "UPDATE console_booking SET status=%s, staff_name=%s, notes=CONCAT(IFNULL(notes,''), %s) WHERE id=%s",
                    (new_status, staff_name_val, staff_note, booking_id)
                )
            else:
                _mysql_exec(
                    "UPDATE console_booking SET status=%s, notes=CONCAT(IFNULL(notes,''), %s) WHERE id=%s",
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
                    "SELECT console_id FROM console_booking WHERE id = %s",
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
        
        # Return updated booking
        rows = _mysql_query(
            "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking WHERE id=%s",
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
                _mysql_exec("UPDATE console_booking SET end_time=%s WHERE id=%s", (new_end, booking_id))
                b["end_time"] = new_end
            except:
                pass
        
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
        except:
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
