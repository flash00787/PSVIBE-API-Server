
# ═══════════════════════════════════════
#  SHEETS — inventory
# ═══════════════════════════════════════
@app.get("/api/sheets/inventory", tags=["Sheets"])
async def api_sheets_inventory(auth=Depends(verify_api_key)):
    """Return inventory data from Inventory sheet."""
    try:
        ws = get_worksheet("Inventory")
        rows = ws.get_all_values()
        if len(rows) < 2:
            return ok({"items": [], "categories": {}, "total_cost": 0})
        items = []
        total_cost = 0
        categories = {}
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            name = row[0].strip() if len(row) > 0 else ""
            cat = row[1].strip() if len(row) > 1 else "Uncategorized"
            qty = int_safe(row[2]) if len(row) > 2 else 0
            cost = int_safe(row[3]) if len(row) > 3 else 0
            price = int_safe(row[4]) if len(row) > 4 else 0
            item_cost = cost * qty
            total_cost += item_cost
            if cat not in categories:
                categories[cat] = 0
            categories[cat] += item_cost
            items.append({
                "name": name, "category": cat, "qty": qty,
                "cost": cost, "price": price, "total": item_cost,
            })
        return ok({"items": items, "categories": categories, "total_cost": total_cost})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  SHEETS — consoles
# ═══════════════════════════════════════
@app.get("/api/sheets/consoles", tags=["Sheets"])
async def api_sheets_consoles(auth=Depends(verify_api_key)):
    """Return console list with live status (same data as fetch_console_status, dict format)."""
    try:
        today = today_str()
        setting_sh = get_worksheet(SHEET_SETTING)
        names = setting_sh.col_values(8)[1:]
        types = setting_sh.col_values(9)[1:]
        mults = setting_sh.col_values(10)[1:]

        consoles = {}
        for i, name in enumerate(names):
            if not name.strip():
                continue
            try:
                mult = float_safe(mults[i]) if i < len(mults) else 1.0
                mult = mult if mult > 0 else 1.0
            except Exception:
                mult = 1.0
            ctype = (types[i] if i < len(types) else "").strip()
            consoles[name.strip()] = {
                "id": name.strip(), "type": ctype, "mult": mult,
                "status": "Free", "member": None, "start": None,
                "staff": None, "booking_id": None,
            }

        try:
            bk_rows = get_booking_rows()
            for row in bk_rows[1:]:
                if len(row) < 7:
                    continue
                bk_date = row[1].strip()
                bk_cid = row[2].strip()
                bk_status = row[6].strip()
                if bk_date == today and bk_status in ("Active", "Scheduled", "Pending"):
                    if bk_cid in consoles:
                        consoles[bk_cid]["status"] = bk_status
                        consoles[bk_cid]["member"] = row[3].strip() or "Guest"
                        consoles[bk_cid]["start"] = row[4].strip()
                        consoles[bk_cid]["staff"] = row[7].strip() if len(row) > 7 else ""
                        consoles[bk_cid]["booking_id"] = row[0].strip()
        except Exception as e:
            logger.warning("Console booking overlay error: %s", e)

        free = sum(1 for c in consoles.values() if c["status"] == "Free")
        busy = len(consoles) - free
        return ok({
            "consoles": consoles,
            "total": len(consoles),
            "free": free,
            "busy": busy,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  SHEETS — stock-today
# ═══════════════════════════════════════
@app.get("/api/sheets/stock-today", tags=["Sheets"])
async def api_sheets_stock_today(auth=Depends(verify_api_key)):
    """Return today's stock movement summary from Stock_In / Stock_Out."""
    try:
        today = today_str()
        result = {"date": today, "stock_in": [], "stock_out": [], "in_total": 0, "out_total": 0}

        # Stock In
        try:
            si = get_worksheet(SHEET_STOCK_IN)
            si_rows = si.get_all_values()
            for row in si_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if row[0] else ""
                if d != today:
                    continue
                item = row[1].strip() if len(row) > 1 else ""
                qty = int_safe(row[2]) if len(row) > 2 else 0
                cost = int_safe(row[3]) if len(row) > 3 else 0
                result["stock_in"].append({"item": item, "qty": qty, "cost": cost})
                result["in_total"] += cost * qty
        except Exception as e:
            logger.warning("Stock_In read error: %s", e)

        # Stock Out
        try:
            so = get_worksheet(SHEET_STOCK_OUT)
            so_rows = so.get_all_values()
            for row in so_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if row[0] else ""
                if d != today:
                    continue
                item = row[1].strip() if len(row) > 1 else ""
                qty = int_safe(row[2]) if len(row) > 2 else 0
                cost = int_safe(row[3]) if len(row) > 3 else 0
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
    """Return aggregated daily report data (sales summary, console usage, top members)."""
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

        # Sales_Daily
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            members_set = set()
            for row in sd_raw[1:]:
                if len(row) < 6:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if d != today:
                    continue
                amt = int_safe(row[4]) if len(row) > 4 else 0
                payment = row[5].strip().title() if len(row) > 5 else "Unknown"
                member = row[3].strip() if len(row) > 3 else ""
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

        # Console usage from bookings
        try:
            bk_rows = get_booking_rows()
            for row in bk_rows[1:]:
                if len(row) < 7:
                    continue
                d = row[1].strip() if len(row) > 1 else ""
                if d != today:
                    continue
                cid = row[2].strip() if len(row) > 2 else ""
                if cid not in result["console_usage"]:
                    result["console_usage"][cid] = 0
                result["console_usage"][cid] += 1
        except Exception as e:
            logger.warning("report-data console error: %s", e)

        # Top-ups
        try:
            from sheets_client import get_topup_log_rows
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 5:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                if d != today:
                    continue
                result["top_ups"]["count"] += 1
                result["top_ups"]["amount"] += int_safe(row[3]) if len(row) > 3 else 0
                result["top_ups"]["mins"] += int_safe(row[4]) if len(row) > 4 else 0
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
    """Return staff salary/stats breakdown."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        staff_names = ws.col_values(19)[1:]
        salaries = ws.col_values(20)[1:]

        result = {}
        for i, name in enumerate(staff_names):
            name = name.strip()
            if not name:
                continue
            sal = int_safe(salaries[i]) if i < len(salaries) else 0
            result[name] = {
                "base_salary": sal,
                "deductions": 0,
                "advances": 0,
                "net_pay": sal,
            }

        # Overlay attendance deductions (current month)
        try:
            mmt = now_mmt()
            month_str = f"{mmt.month}/{mmt.year}"
            att_ws = get_worksheet(SHEET_ATTENDANCE_LOG)
            att_rows = att_ws.get_all_values()
            for row in att_rows[1:]:
                if len(row) < 5:
                    continue
                if row[0].strip() != month_str:
                    continue
                staff = row[1].strip()
                if staff in result:
                    late = int_safe(row[3]) if len(row) > 3 else 0
                    deduct = int_safe(row[4]) if len(row) > 4 and row[4].strip() else 500
                    result[staff]["deductions"] = late * deduct
                    result[staff]["net_pay"] = result[staff]["base_salary"] - result[staff]["deductions"]
        except Exception as e:
            logger.warning("staff-breakdown attendance error: %s", e)

        # Overlay salary advances
        try:
            mmt = now_mmt()
            month_str = f"{mmt.year}-{mmt.month:02d}"
            adv_ws = get_worksheet(SHEET_SALARY_ADVANCE)
            adv_rows = adv_ws.get_all_values()
            for row in adv_rows[1:]:
                if len(row) < 4:
                    continue
                date_val = row[0].strip()
                staff = row[1].strip()
                if not staff or not date_val:
                    continue
                if month_str.replace("-", "/") not in date_val and month_str not in date_val:
                    continue
                if staff in result:
                    amt = int_safe(row[2]) if row[2].strip() else 0
                    result[staff]["advances"] += amt
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

        # Revenue from Sales_Daily
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if month_slash not in d:
                    continue
                amt = int_safe(row[4]) if len(row) > 4 else 0
                result["revenue"]["console_rental"] += amt
        except Exception as e:
            logger.warning("pnl sales error: %s", e)

        # TopUp revenue
        try:
            from sheets_client import get_topup_log_rows
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                if month_slash not in d:
                    continue
                amt = int_safe(row[3]) if len(row) > 3 else 0
                result["revenue"]["topup_sales"] += amt
        except Exception as e:
            logger.warning("pnl topup error: %s", e)

        # Expenses from salaries
        try:
            ws = get_worksheet(SHEET_SETTING)
            salaries = ws.col_values(20)[1:]
            for s in salaries:
                result["expenses"]["salaries"] += int_safe(s)
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
    """Return liability summary (card wallet totals, advances, outstanding)."""
    try:
        result = {
            "wallet_liability_mins": 0,
            "wallet_liability_ks": 0,
            "salary_advances": 0,
            "outstanding_payables": 0,
            "total_liability": 0,
        }

        # Wallet liability
        for row in get_member_rows()[1:]:
            if len(row) > 7:
                mins = int_safe(row[7]) if row[7].strip() else 0
                result["wallet_liability_mins"] += mins

        # Base rate for conversion
        try:
            ws = get_worksheet(SHEET_SETTING)
            base_rate = int_safe(ws.cell(2, 2).value)
            result["wallet_liability_ks"] = int(result["wallet_liability_mins"] * base_rate / 60) if base_rate > 0 else 0
        except Exception:
            pass

        # Salary advances (current month)
        try:
            mmt = now_mmt()
            month_slash = f"{mmt.month}/{mmt.year}"
            adv_ws = get_worksheet(SHEET_SALARY_ADVANCE)
            adv_rows = adv_ws.get_all_values()
            for row in adv_rows[1:]:
                if len(row) < 3:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                if d and month_slash not in d:
                    continue
                amt = int_safe(row[2]) if row[2].strip() else 0
                result["salary_advances"] += amt
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
    """Return payment method config and totals for today."""
    try:
        today = today_str()
        methods = {}
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 6:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if d != today:
                    continue
                payment = row[5].strip().title() if len(row) > 5 else "Unknown"
                amt = int_safe(row[4]) if len(row) > 4 else 0
                if payment not in methods:
                    methods[payment] = {"count": 0, "amount": 0}
                methods[payment]["count"] += 1
                methods[payment]["amount"] += amt
        except Exception as e:
            logger.warning("payment-methods error: %s", e)

        return ok({
            "date": today,
            "methods": methods,
            "available": ["Cash", "KPay", "WavePay", "CB Pay", "AYA Pay"],
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  SHEETS — promotions
# ═══════════════════════════════════════
@app.get("/api/sheets/promotions", tags=["Sheets"])
async def api_sheets_promotions(auth=Depends(verify_api_key)):
    """Return active promotions bundled with today's usage count."""
    try:
        today = today_str()
        promos = []
        # Read promotion definitions from Setting!B22:B25 area
        try:
            ws = get_worksheet(SHEET_SETTING)
            for r in range(22, 30):
                name = ws.cell(r, 2).value
                price = ws.cell(r, 3).value
                mins = ws.cell(r, 4).value
                if name and str(name).strip():
                    promos.append({
                        "name": str(name).strip(),
                        "price": int_safe(price),
                        "mins": int_safe(mins),
                        "active": True,
                        "used_today": 0,
                    })
        except Exception as e:
            logger.warning("promotions read error: %s", e)

        # Count usage today
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if d != today:
                    continue
                notes = row[9].strip().lower() if len(row) > 9 else ""
                for p in promos:
                    if p["name"].lower() in notes:
                        p["used_today"] += 1
        except Exception:
            pass

        return ok(promos)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sheets/promotions/all", tags=["Sheets"])
async def api_sheets_promotions_all(auth=Depends(verify_api_key)):
    """Return all promotions (active + historical)."""
    return await api_sheets_promotions(auth)


@app.get("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log(auth=Depends(verify_api_key)):
    """Return promotion usage log."""
    try:
        log = []
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 10:
                    continue
                notes = row[9].strip().lower() if len(row) > 9 else ""
                voucher = row[1].strip() if len(row) > 1 else ""
                d = row[2].strip() if len(row) > 2 else ""
                member = row[3].strip() if len(row) > 3 else ""
                amt = int_safe(row[4]) if len(row) > 4 else 0
                if notes:
                    log.append({
                        "date": d, "voucher": voucher, "member": member,
                        "amount": amt, "promotion": notes,
                    })
        except Exception as e:
            logger.warning("promotions-log error: %s", e)

        return ok({"log": log, "total": len(log)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log_post(req: dict, auth=Depends(verify_api_key)):
    """Log a promotion usage event."""
    try:
        logger.info("PROMO-LOG: member=%s promotion=%s amount=%s", 
                     req.get("member", ""), req.get("promotion", ""), req.get("amount", 0))
        return ok({"logged": True})
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
            from sheets_client import get_sales_daily_rows, get_topup_log_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                dt = _parse_mm_dd_yyyy(d)
                if dt is None:
                    continue
                dt_mmt = dt.replace(tzinfo=MMT_TZ)
                if dt_mmt < week_start:
                    continue
                day_key = dt.strftime("%Y-%m-%d")
                amt = int_safe(row[4]) if len(row) > 4 else 0
                if day_key not in result["daily_revenue"]:
                    result["daily_revenue"][day_key] = 0
                result["daily_revenue"][day_key] += amt
                result["total_revenue"] += amt
                result["total_vouchers"] += 1
        except Exception as e:
            logger.warning("weekly-report sales error: %s", e)

        try:
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                dt = _parse_mm_dd_yyyy(d)
                if dt is None:
                    continue
                dt_mmt = dt.replace(tzinfo=MMT_TZ)
                if dt_mmt < week_start:
                    continue
                amt = int_safe(row[3]) if len(row) > 3 else 0
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
async def api_list_bookings(
    status: str = Query(None),
    memberId: str = Query(None),
    date: str = Query(None),
    auth=Depends(verify_api_key),
):
    """List all bookings with optional filters (status, memberId, date)."""
    try:
        rows = get_booking_rows()
        results = []
        for row in rows[1:]:
            if not row or len(row) < 2:
                continue
            bk_status = row[6].strip() if len(row) > 6 else ""
            bk_member = row[3].strip() if len(row) > 3 else ""
            bk_date = row[1].strip() if len(row) > 1 else ""

            # Parse notes JSON for customer bot fields
            notes_str = row[8].strip() if len(row) > 8 else ""
            tg_id = row[9].strip() if len(row) > 9 else ""
            customer_name = ""
            game_name = ""
            duration_mins = 0
            try:
                if notes_str:
                    import json as _json
                    notes_data = _json.loads(notes_str)
                    customer_name = notes_data.get("customerName", "")
                    game_name = notes_data.get("gameName", "")
                    duration_mins = notes_data.get("durationMins", 0)
                    if not tg_id:
                        tg_id = str(notes_data.get("telegramChatId", ""))
            except Exception:
                pass

            # Apply filters
            if status and bk_status.lower() != status.lower():
                continue
            if memberId and bk_member.lower() != memberId.lower():
                # Also check notes for customer bot bookings
                if not notes_str or memberId.lower() not in notes_str.lower():
                    continue
            if date and bk_date != date:
                continue

            results.append({
                "id": row[0].strip() if len(row) > 0 else "",
                "booking_id": row[0].strip() if len(row) > 0 else "",
                "date": bk_date,
                "console_id": row[2].strip() if len(row) > 2 else "",
                "consoleType": row[2].strip() if len(row) > 2 else "",
                "member_id": bk_member,
                "memberId": bk_member,
                "start": row[4].strip() if len(row) > 4 else "",
                "timeSlot": row[4].strip() if len(row) > 4 else "",
                "end": row[5].strip() if len(row) > 5 else "",
                "endTime": row[5].strip() if len(row) > 5 else "",
                "status": bk_status,
                "staff": row[7].strip() if len(row) > 7 else "",
                "notes": notes_str,
                "customerName": customer_name,
                "gameName": game_name,
                "durationMins": duration_mins,
                "telegramChatId": tg_id,
            })
        return ok({"bookings": results})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  BOOKINGS — broadcast-targets
# ═══════════════════════════════════════
@app.get("/api/bookings/broadcast-targets", tags=["Bookings"])
async def api_bookings_broadcast_targets(auth=Depends(verify_api_key)):
    """Return list of unique telegramChatIds from recent bookings."""
    try:
        rows = get_booking_rows()
        targets = set()
        for row in rows[1:]:
            if len(row) > 9:
                tg = row[9].strip() if row[9] else ""
                if tg and tg.isdigit():
                    targets.add(tg)
            # Also check notes JSON
            if len(row) > 8:
                notes_str = row[8].strip() if row[8] else ""
                try:
                    if notes_str:
                        import json as _json
                        nd = _json.loads(notes_str)
                        tg2 = str(nd.get("telegramChatId", ""))
                        if tg2 and tg2.isdigit():
                            targets.add(tg2)
                except Exception:
                    pass
        return ok(list(targets))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  WAITLIST — CRUD
# ═══════════════════════════════════════

# In-memory waitlist store (survives between API calls during server lifetime)
_waitlist_store: list = []

@app.get("/api/waitlist", tags=["Waitlist"])
async def api_waitlist_list(status: str = Query(None), auth=Depends(verify_api_key)):
    """List waitlist entries, optionally filtered by status."""
    global _waitlist_store
    try:
        if status:
            return ok([w for w in _waitlist_store if w.get("status") == status])
        return ok(_waitlist_store)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/waitlist/{entry_id}", tags=["Waitlist"])
async def api_waitlist_get(entry_id: str, auth=Depends(verify_api_key)):
    """Get a single waitlist entry."""
    global _waitlist_store
    try:
        entry_id_int = int_safe(entry_id)
        for w in _waitlist_store:
            if w.get("id") == entry_id_int:
                return ok(w)
        raise HTTPException(status_code=404, detail=f"Waitlist entry {entry_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/waitlist/notify", tags=["Waitlist"])
async def api_waitlist_notify(req: dict, auth=Depends(verify_api_key)):
    """Notify next person on waitlist. Returns the notified entry or empty."""
    global _waitlist_store
    try:
        console_id = req.get("console_id", "")
        waiting = [w for w in _waitlist_store if w.get("status") == "waiting"]
        if console_id:
            waiting = [w for w in waiting if w.get("console_id") == console_id]
        if waiting:
            entry = waiting[0]
            entry["status"] = "notified"
            return ok(entry)
        return ok(None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/waitlist", tags=["Waitlist"])
async def api_waitlist_add(req: dict, auth=Depends(verify_api_key)):
    """Add a new entry to the waitlist."""
    global _waitlist_store
    try:
        new_id = max((w.get("id", 0) for w in _waitlist_store), default=0) + 1
        entry = {
            "id": new_id,
            "member_id": req.get("member_id", ""),
            "member_name": req.get("member_name", ""),
            "console_id": req.get("console_id", ""),
            "console_type": req.get("console_type", ""),
            "added_at": now_mmt().isoformat(),
            "status": "waiting",
            "notes": req.get("notes", ""),
        }
        _waitlist_store.append(entry)
        return ok(entry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/waitlist/{entry_id}", tags=["Waitlist"])
async def api_waitlist_remove(entry_id: str, auth=Depends(verify_api_key)):
    """Remove a waitlist entry."""
    global _waitlist_store
    try:
        entry_id_int = int_safe(entry_id)
        before = len(_waitlist_store)
        _waitlist_store = [w for w in _waitlist_store if w.get("id") != entry_id_int]
        if len(_waitlist_store) == before:
            raise HTTPException(status_code=404, detail=f"Waitlist entry {entry_id} not found")
        return ok({"removed": entry_id_int})
    except HTTPException:
        raise
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
        from sheets_client import get_sales_daily_rows, get_topup_log_rows
        sd_raw = get_sales_daily_rows()
        for row in sd_raw[1:]:
            if len(row) < 5:
                continue
            d = row[2].strip() if len(row) > 2 else ""
            if month_slash not in d:
                continue
            amt = int_safe(row[4]) if len(row) > 4 else 0
            result["revenue"]["console"] += amt
            result["total_revenue"] += amt
    except Exception as e:
        logger.warning("finance/pnl sales error: %s", e)

    try:
        tu_rows = get_topup_log_rows()
        for row in tu_rows[1:]:
            if len(row) < 4:
                continue
            d = row[0].strip() if len(row) > 0 else ""
            if month_slash not in d:
                continue
            amt = int_safe(row[3]) if len(row) > 3 else 0
            result["revenue"]["topup"] += amt
            result["total_revenue"] += amt
    except Exception as e:
        logger.warning("finance/pnl topup error: %s", e)

    # Expenses
    try:
        ws = get_worksheet(SHEET_SETTING)
        salaries = ws.col_values(20)[1:]
        for s in salaries:
            result["expenses"]["salaries"] += int_safe(s)
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

    # Calculate wallet liability
    for row in get_member_rows()[1:]:
        if len(row) > 7:
            mins = int_safe(row[7]) if row[7].strip() else 0
            result["liabilities"]["wallet_liability"] += mins

    try:
        ws = get_worksheet(SHEET_SETTING)
        base_rate = int_safe(ws.cell(2, 2).value)
        if base_rate > 0:
            result["liabilities"]["wallet_liability"] = int(result["liabilities"]["wallet_liability"] * base_rate / 60)
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

