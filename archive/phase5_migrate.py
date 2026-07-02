#!/usr/bin/env python3
"""Phase 5 Migration Script - Migrate all remaining gspread-only endpoints to MySQL.

Reads app.py, applies targeted MySQL-first modifications, writes the result.
Backup is created automatically.
"""
import re
import sys
from datetime import datetime

APP_PATH = "/root/psvibe_api_server/app.py"

def read_app():
    with open(APP_PATH, 'r') as f:
        return f.read()

def write_app(content):
    backup = f"{APP_PATH}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    with open(backup, 'w') as f:
        with open(APP_PATH, 'r') as src:
            f.write(src.read())
    print(f"Backup saved to {backup}")
    with open(APP_PATH, 'w') as f:
        f.write(content)
    print(f"Wrote {len(content)} bytes to {APP_PATH}")

# ============================================================
# ADD MYSQL WRAPPER FUNCTIONS
# ============================================================

WRAPPERS_TO_ADD = r'''
# ═══════════════════════════════════════
#  PHASE 5 — NEW MYSQL WRAPPERS
# ═══════════════════════════════════════

def _fetch_games_on_console_from_mysql(console_id: str):
    """MySQL: Get active games for a console."""
    try:
        if _use_mysql():
            rows = mysql_query(
                "SELECT DISTINCT game_title FROM console_games WHERE console_id = %s AND status = 'active'",
                (console_id,))
            return [r['game_title'] for r in (rows or [])]
    except Exception as e:
        logger.warning(f"MySQL fetch_games_on_console failed: {e}")
    return None

def _fetch_consoles_with_game_from_mysql(game_title: str):
    """MySQL: Get consoles that have a specific game."""
    try:
        if _use_mysql():
            rows = mysql_query(
                "SELECT DISTINCT console_id, console_name FROM console_games WHERE game_title LIKE %s AND status = 'active'",
                (f"%{game_title}%",))
            if rows:
                return list(dict.fromkeys([r['console_id'] for r in rows]))
    except Exception as e:
        logger.warning(f"MySQL fetch_consoles_with_game failed: {e}")
    return None

def _fetch_rank_table_display_from_mysql():
    """MySQL: Get rank bonus table from settings, format as display string."""
    import json
    try:
        if _use_mysql():
            val = _get_setting("bonus_table")
            if val:
                data = json.loads(val)
                lines = [
                    f"{'Amount (Ks)':<14} {'Warrior':>9} {'Master':>9} {'Immortal':>10}",
                    "-" * 48,
                ]
                for row in data:
                    if len(row) >= 4:
                        amt = int_safe(row[0])
                        if amt == 0:
                            continue
                        lines.append(f"{amt:>14,}  {int_safe(row[1]):>8,}  {int_safe(row[2]):>8,}  {int_safe(row[3]):>9,}")
                if len(lines) > 2:
                    return "\n".join(lines)
    except Exception as e:
        logger.warning(f"MySQL fetch_rank_table_display failed: {e}")
    return None

def _fetch_base_salaries_from_mysql():
    """MySQL: Get base salaries from settings (JSON key)."""
    import json
    try:
        if _use_mysql():
            val = _get_setting("base_salaries")
            if val:
                return json.loads(val)
    except Exception as e:
        logger.warning(f"MySQL fetch_base_salaries failed: {e}")
    return None

def _fetch_attendance_from_mysql(month_str: str):
    """MySQL: Get attendance records for a month. Maps MySQL schema to legacy API format."""
    try:
        if _use_mysql():
            # Convert "YYYY-MM" to date range "%Y-%m"
            rows = mysql_query(
                "SELECT staff_name, DATE_FORMAT(date, '%Y-%m') as ym, "
                "COUNT(*) as total_days, "
                "SUM(CASE WHEN status = 'late' THEN 1 ELSE 0 END) as late_count, "
                "SUM(CASE WHEN status = 'leave' THEN 1 ELSE 0 END) as leave_days "
                "FROM attendance_log WHERE DATE_FORMAT(date, '%Y-%m') = %s "
                "GROUP BY staff_name, ym",
                (month_str,))
            if rows:
                result = {}
                for row in rows:
                    staff = row['staff_name'].strip()
                    if not staff:
                        continue
                    result[staff] = {
                        "leave_days": int(row.get('leave_days', 0)),
                        "late_count": int(row.get('late_count', 0)),
                        "deduct_per_late": 500,
                    }
                return result
    except Exception as e:
        logger.warning(f"MySQL fetch_attendance failed: {e}")
    return None

def _fetch_salary_advances_from_mysql(month_str: str):
    """MySQL: Get salary advances for a month."""
    try:
        if _use_mysql():
            rows = mysql_query(
                "SELECT staff_name, amount, advance_date, repayment_status "
                "FROM salary_advance WHERE DATE_FORMAT(advance_date, '%Y-%m') = %s",
                (month_str,))
            if rows:
                result = {}
                for row in rows:
                    staff = row['staff_name'].strip()
                    if not staff:
                        continue
                    amt = int(float(row.get('amount', 0)))
                    if staff not in result:
                        result[staff] = {"total": 0, "cash": 0, "kpay": 0}
                    result[staff]["total"] += amt
                    result[staff]["cash"] += amt  # default to cash
                return result
    except Exception as e:
        logger.warning(f"MySQL fetch_salary_advances failed: {e}")
    return None

def _fetch_promotions_cached_from_mysql():
    """MySQL: Get active promotions."""
    try:
        if _use_mysql():
            today_mmt = now_mmt().strftime("%Y-%m-%d")
            rows = mysql_query(
                "SELECT id, promo_name, discount_type, discount_value, start_date, end_date, status, notes "
                "FROM promotions WHERE status = 'active' AND (end_date IS NULL OR end_date >= %s)",
                (today_mmt,))
            if rows:
                promos = []
                for row in rows:
                    promos.append({
                        "title": row.get('promo_name', 'Promotion'),
                        "description": row.get('notes', ''),
                        "type": row.get('discount_type', 'general'),
                        "discount_percent": str(row.get('discount_value', '')),
                        "item_name": "",
                        "bundle_items": "",
                        "cashback_percent": "",
                        "conditions": "",
                        "valid_until": str(row.get('end_date', '')),
                        "emoji": "🎁",
                    })
                return promos
    except Exception as e:
        logger.warning(f"MySQL fetch_promotions_cached failed: {e}")
    return None

def _fetch_next_member_id_from_mysql():
    """MySQL: Generate next member ID."""
    try:
        if _use_mysql():
            row = mysql_query_one(
                "SELECT CONCAT('PSV_A_', LPAD(COALESCE(MAX(CAST(SUBSTRING(member_id, 7) AS UNSIGNED)), 0) + 1, 3, '0')) as next_id "
                "FROM members WHERE member_id LIKE 'PSV_A_%'")
            if row and row.get('next_id'):
                return row['next_id']
    except Exception as e:
        logger.warning(f"MySQL next_member_id failed: {e}")
    return None

def _fetch_next_member_row_no_from_mysql():
    """MySQL: Get next row number."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT COUNT(*) + 1 as next_row FROM members")
            if row:
                return int(row['next_row'])
    except Exception as e:
        logger.warning(f"MySQL next_member_row_no failed: {e}")
    return None

def _fetch_referral_code_from_mysql(member_id: str):
    """MySQL: Get referral code for a member."""
    try:
        if _use_mysql():
            row = mysql_query_one(
                "SELECT referral_code FROM member_wallets WHERE member_id = %s",
                (member_id,))
            if row:
                return row.get('referral_code') or None
    except Exception as e:
        logger.warning(f"MySQL fetch_referral_code failed: {e}")
    return None

def _fetch_analytics_member_activity_from_mysql():
    """MySQL: Member activity stats."""
    try:
        if _use_mysql():
            total = mysql_query_one("SELECT COUNT(*) as c FROM member_wallets")
            total_members = total['c'] if total else 0

            tiers = mysql_query("SELECT tier, COUNT(*) as c FROM member_wallets GROUP BY tier")
            tier_dist = []
            for t in (tiers or []):
                tier_dist.append({
                    "tier": t.get('tier', 'Warrior'),
                    "count": t['c'],
                    "pct": round(t['c'] / total_members * 100, 1) if total_members > 0 else 0
                })

            wallet = mysql_query_one("SELECT SUM(balance_mins) as s FROM member_wallets")
            total_wallet = int(wallet['s'] or 0) if wallet else 0

            spend = mysql_query_one("SELECT SUM(total_spend) as s FROM member_wallets")
            total_spend = int(float(spend['s'] or 0)) if spend else 0

            today_mmt = now_mmt().strftime("%Y-%m-%d")
            active_today = mysql_query_one(
                "SELECT COUNT(DISTINCT member_id) as c FROM console_booking "
                "WHERE booking_date = %s AND status = 'Active'",
                (today_mmt,))
            active = active_today['c'] if active_today else 0

            recent = mysql_query_one(
                "SELECT COUNT(DISTINCT member_id) as c FROM topup_log "
                "WHERE topup_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)")
            recent_active = recent['c'] if recent else 0

            return {
                "total_members": total_members,
                "active_today": active,
                "active_last_7d": recent_active,
                "total_wallet_mins": total_wallet,
                "total_spend_ks": total_spend,
                "avg_spend_per_member": round(total_spend / total_members, 2) if total_members > 0 else 0,
                "tier_distribution": tier_dist,
            }
    except Exception as e:
        logger.warning(f"MySQL member_activity failed: {e}")
    return None

def _fetch_analytics_console_usage_from_mysql(days: int = 30):
    """MySQL: Console usage analytics."""
    try:
        if _use_mysql():
            total_bookings = mysql_query_one(
                "SELECT COUNT(*) as c FROM console_booking WHERE booking_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)",
                (days,))
            active_now = mysql_query_one(
                "SELECT COUNT(*) as c FROM console_booking WHERE status = 'Active'")

            consoles_data = mysql_query(
                "SELECT cs.console_id, COALESCE(cb.total_bk, 0) as total_bookings, "
                "COALESCE(cb.active_bk, 0) as active_bookings, "
                "COALESCE(cb.done_bk, 0) as completed_bookings, "
                "COALESCE(cb.cancelled_bk, 0) as cancelled_bookings "
                "FROM console_status cs "
                "LEFT JOIN ("
                "  SELECT console_id, COUNT(*) as total_bk, "
                "  SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END) as active_bk, "
                "  SUM(CASE WHEN status='Done' THEN 1 ELSE 0 END) as done_bk, "
                "  SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END) as cancelled_bk "
                "  FROM console_booking WHERE booking_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
                "  GROUP BY console_id"
                ") cb ON cs.console_id = cb.console_id",
                (days,))

            consoles = []
            for c in (consoles_data or []):
                consoles.append({
                    "console_id": c['console_id'],
                    "type": "",
                    "total_bookings": int(c.get('total_bookings', 0)),
                    "active_bookings": int(c.get('active_bookings', 0)),
                    "completed_bookings": int(c.get('completed_bookings', 0)),
                    "cancelled_bookings": int(c.get('cancelled_bookings', 0)),
                    "total_hours": 0,
                    "unique_members": 0,
                    "daily_series": [],
                })

            # Also add consoles from bookings that may not be in console_status
            extra_consoles = mysql_query(
                "SELECT DISTINCT console_id FROM console_booking "
                "WHERE booking_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
                "AND console_id NOT IN (SELECT console_id FROM console_status)",
                (days,))
            existing_ids = {c['console_id'] for c in consoles}
            for ec in (extra_consoles or []):
                cid = ec['console_id']
                if cid and cid not in existing_ids:
                    consoles.append({
                        "console_id": cid, "type": "",
                        "total_bookings": 0, "active_bookings": 0,
                        "completed_bookings": 0, "cancelled_bookings": 0,
                        "total_hours": 0, "unique_members": 0, "daily_series": [],
                    })

            total_con = len(consoles) or 1
            total_bk = int(total_bookings['c'] or 0) if total_bookings else 0
            util = round(total_bk / (total_con * days), 2)

            return {
                "period_days": days,
                "total_consoles": total_con,
                "total_bookings": total_bk,
                "active_now": int(active_now['c'] or 0) if active_now else 0,
                "avg_bookings_per_console_day": util,
                "daily_series": [],
                "consoles": consoles,
            }
    except Exception as e:
        logger.warning(f"MySQL console_usage failed: {e}")
    return None

def _fetch_analytics_dashboard_from_mysql():
    """MySQL: Full dashboard summary."""
    try:
        if _use_mysql():
            today_mmt = now_mmt().strftime("%Y-%m-%d")
            sales = mysql_query_one(
                "SELECT COALESCE(SUM(amount), 0) as total_sales, COUNT(*) as voucher_count "
                "FROM sales_daily WHERE sale_date = %s", (today_mmt,))
            # Try alternate column name
            if not sales or not sales.get('voucher_count'):
                sales = mysql_query_one(
                    "SELECT COALESCE(SUM(amount), 0) as total_sales, COUNT(*) as voucher_count "
                    "FROM sales_daily WHERE date = %s", (today_mmt,))

            members = mysql_query_one("SELECT COUNT(*) as c FROM member_wallets")
            active = mysql_query_one(
                "SELECT COUNT(DISTINCT member_id) as c FROM console_booking "
                "WHERE booking_date = %s AND status = 'Active'", (today_mmt,))
            consoles_total = mysql_query_one("SELECT COUNT(*) as c FROM console_status")
            consoles_active = mysql_query_one(
                "SELECT COUNT(*) as c FROM console_booking WHERE status = 'Active'")

            week_topups = mysql_query_one(
                "SELECT COALESCE(SUM(amount), 0) as s, COUNT(*) as c FROM topup_log "
                "WHERE topup_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)")

            base_rate_val = _fetch_base_rate_from_mysql()

            total_sales = int(sales['total_sales'] or 0) if sales else 0
            voucher_count = int(sales['voucher_count'] or 0) if sales else 0
            avg_ticket = round(total_sales / voucher_count, 2) if voucher_count > 0 else 0

            return {
                "generated_at": now_mmt().isoformat(),
                "summary": {
                    "today_sales_ks": total_sales,
                    "today_vouchers": voucher_count,
                    "today_avg_ticket_ks": avg_ticket,
                    "active_members_today": int(active['c'] or 0) if active else 0,
                    "total_members": int(members['c'] or 0) if members else 0,
                    "active_consoles": int(consoles_active['c'] or 0) if consoles_active else 0,
                    "total_consoles": int(consoles_total['c'] or 0) if consoles_total else 0,
                    "week_topup_ks": int(week_topups['s'] or 0) if week_topups else 0,
                    "week_topup_count": int(week_topups['c'] or 0) if week_topups else 0,
                    "base_rate_ks_hr": base_rate_val,
                },
            }
    except Exception as e:
        logger.warning(f"MySQL dashboard summary failed: {e}")
    return None
'''

def apply_changes():
    print("Reading app.py...")
    content = read_app()
    original = content

    # ============================================================
    # 1. Add wrapper functions after the existing one at "#  PHASE 5 — NEW MYSQL WRAPPERS"
    #    Insert before "# ═══════════════  MYSQL STATUS"
    # ============================================================
    marker = "# ═══════════════════════════════════════\n#  MYSQL STATUS\n# ═══════════════════════════════════════"
    if marker in content:
        content = content.replace(marker, WRAPPERS_TO_ADD + "\n" + marker)
        print("✓ Added Phase 5 MySQL wrapper functions")
    else:
        print("⚠ MySQL STATUS marker not found, looking for alternative...")
        # Try alternative marker
        alt_marker = "@app.get(\"/api/mysql/status\""
        if alt_marker in content:
            # Insert before this
            content = content.replace(alt_marker, "\n" + WRAPPERS_TO_ADD + "\n" + alt_marker)
            print("✓ Added Phase 5 MySQL wrapper functions (alt marker)")

    # ============================================================
    # 2. MODIFY: api_get_games_on_console
    # ============================================================
    old = '''@app.get("/api/get_games_on_console/{console_id}", tags=["Games"])
async def api_get_games_on_console(console_id: str, auth=Depends(verify_api_key)):
    """Return list of game titles installed on a specific console."""
    try:
        rows = get_console_game_rows()
        games = []
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip().upper() == console_id.strip().upper() and row[1].strip():
                games.append(row[1].strip())
        return ok(games)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/get_games_on_console/{console_id}", tags=["Games"])
async def api_get_games_on_console(console_id: str, auth=Depends(verify_api_key)):
    """Return list of game titles installed on a specific console — MySQL path with gspread fallback."""
    data = _fetch_games_on_console_from_mysql(console_id)
    if data is not None:
        return ok(data)
    try:
        rows = get_console_game_rows()
        games = []
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip().upper() == console_id.strip().upper() and row[1].strip():
                games.append(row[1].strip())
        return ok(games)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated get_games_on_console")
    else:
        print("⚠ get_games_on_console pattern not found - already migrated?")

    # ============================================================
    # 3. MODIFY: api_get_consoles_with_game
    # ============================================================
    old = '''@app.get("/api/get_consoles_with_game", tags=["Games"])
async def api_get_consoles_with_game(game_title: str = Query(...), auth=Depends(verify_api_key)):
    """Return list of console IDs that have a specific game installed."""
    try:
        rows = get_console_game_rows()
        gl = game_title.strip().lower()
        consoles = []
        for row in rows[1:]:
            if len(row) >= 2 and row[1].strip().lower() == gl and row[0].strip():
                consoles.append(row[0].strip())
        return ok(list(dict.fromkeys(consoles)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/get_consoles_with_game", tags=["Games"])
async def api_get_consoles_with_game(game_title: str = Query(...), auth=Depends(verify_api_key)):
    """Return list of console IDs that have a specific game installed — MySQL path with gspread fallback."""
    data = _fetch_consoles_with_game_from_mysql(game_title)
    if data is not None:
        return ok(data)
    try:
        rows = get_console_game_rows()
        gl = game_title.strip().lower()
        consoles = []
        for row in rows[1:]:
            if len(row) >= 2 and row[1].strip().lower() == gl and row[0].strip():
                consoles.append(row[0].strip())
        return ok(list(dict.fromkeys(consoles)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated get_consoles_with_game")
    else:
        print("⚠ get_consoles_with_game pattern not found")

    # ============================================================
    # 4. MODIFY: api_add_console_game
    # ============================================================
    old = '''@app.post("/api/add_console_game", tags=["Games"])
async def api_add_console_game(req: dict, auth=Depends(verify_api_key)):
    """Add a game installation record to Console_Games."""
    try:
        console_id = req.get("console_id", "")
        game_title = req.get("game_title", "")
        install_type = req.get("install_type", "")
        notes = req.get("notes", "")

        sh = get_worksheet(SHEET_CONSOLE_GAMES)
        date = now_mmt().strftime("%-m/%-d/%Y")
        sh.append_row([console_id, game_title, install_type, date, notes],
                      value_input_option="USER_ENTERED")
        invalidate_cache("console_games")
        return ok({"console_id": console_id, "game_title": game_title})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.post("/api/add_console_game", tags=["Games"])
async def api_add_console_game(req: dict, auth=Depends(verify_api_key)):
    """Add a game installation record to Console_Games — MySQL path with gspread fallback."""
    try:
        console_id = req.get("console_id", "")
        game_title = req.get("game_title", "")
        install_type = req.get("install_type", "")
        notes = req.get("notes", "")

        # MySQL path
        if _use_mysql():
            mysql_execute(
                "INSERT INTO console_games (console_id, console_name, game_title, status, notes) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (console_id, console_id, game_title, notes))
            invalidate_cache("console_games")
            return ok({"console_id": console_id, "game_title": game_title})

        # gspread fallback
        sh = get_worksheet(SHEET_CONSOLE_GAMES)
        date = now_mmt().strftime("%-m/%-d/%Y")
        sh.append_row([console_id, game_title, install_type, date, notes],
                      value_input_option="USER_ENTERED")
        invalidate_cache("console_games")
        return ok({"console_id": console_id, "game_title": game_title})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated add_console_game")
    else:
        print("⚠ add_console_game pattern not found")

    # ============================================================
    # 5. MODIFY: api_remove_console_game
    # ============================================================
    old = '''@app.delete("/api/remove_console_game", tags=["Games"])
async def api_remove_console_game(req: dict, auth=Depends(verify_api_key)):
    """Remove a game installation record from Console_Games."""
    try:
        console_id = req.get("console_id", "")
        game_title = req.get("game_title", "")

        sh = get_worksheet(SHEET_CONSOLE_GAMES)
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if (len(row) >= 2
                    and row[0].strip().upper() == console_id.strip().upper()
                    and row[1].strip().lower() == game_title.strip().lower()):
                sh.delete_rows(i)
                invalidate_cache("console_games")
                return ok({"console_id": console_id, "game_title": game_title})
        raise HTTPException(status_code=404, detail=f"Game {game_title} not found on console {console_id}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.delete("/api/remove_console_game", tags=["Games"])
async def api_remove_console_game(req: dict, auth=Depends(verify_api_key)):
    """Remove a game installation record from Console_Games — MySQL path with gspread fallback."""
    try:
        console_id = req.get("console_id", "")
        game_title = req.get("game_title", "")

        # MySQL path
        if _use_mysql():
            result = mysql_execute(
                "UPDATE console_games SET status = 'inactive' WHERE console_id = %s AND game_title = %s AND status = 'active'",
                (console_id, game_title))
            if result > 0:
                invalidate_cache("console_games")
                return ok({"console_id": console_id, "game_title": game_title})
            raise HTTPException(status_code=404, detail=f"Game {game_title} not found on console {console_id}")

        # gspread fallback
        sh = get_worksheet(SHEET_CONSOLE_GAMES)
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if (len(row) >= 2
                    and row[0].strip().upper() == console_id.strip().upper()
                    and row[1].strip().lower() == game_title.strip().lower()):
                sh.delete_rows(i)
                invalidate_cache("console_games")
                return ok({"console_id": console_id, "game_title": game_title})
        raise HTTPException(status_code=404, detail=f"Game {game_title} not found on console {console_id}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated remove_console_game")
    else:
        print("⚠ remove_console_game pattern not found")

    # ============================================================
    # 6. MODIFY: api_fetch_rank_table_display
    # ============================================================
    old = '''@app.get("/api/fetch_rank_table_display", tags=["Settings"])
async def api_fetch_rank_table_display(auth=Depends(verify_api_key)):
    """Fetch Setting!O1:R5 and return formatted string table for display."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        rows = ws.get("O1:R5")
        if not rows:
            return ok("_(data not available)_")
        lines = [
            f"{'Amount (Ks)':<14} {'Warrior':>9} {'Master':>9} {'Immortal':>10}",
            "-" * 48,
        ]
        for row in rows[1:]:
            if len(row) < 4:
                continue
            amt = int_safe(row[0])
            if amt == 0:
                continue
            lines.append(f"{amt:>14,}  {int_safe(row[1]):>8,}  {int_safe(row[2]):>8,}  {int_safe(row[3]):>9,}")
        return ok("\\n".join(lines))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/fetch_rank_table_display", tags=["Settings"])
async def api_fetch_rank_table_display(auth=Depends(verify_api_key)):
    """Fetch rank bonus table for display — MySQL path with gspread fallback."""
    data = _fetch_rank_table_display_from_mysql()
    if data is not None:
        return ok(data)
    try:
        ws = get_worksheet(SHEET_SETTING)
        rows = ws.get("O1:R5")
        if not rows:
            return ok("_(data not available)_")
        lines = [
            f"{'Amount (Ks)':<14} {'Warrior':>9} {'Master':>9} {'Immortal':>10}",
            "-" * 48,
        ]
        for row in rows[1:]:
            if len(row) < 4:
                continue
            amt = int_safe(row[0])
            if amt == 0:
                continue
            lines.append(f"{amt:>14,}  {int_safe(row[1]):>8,}  {int_safe(row[2]):>8,}  {int_safe(row[3]):>9,}")
        return ok("\\n".join(lines))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated fetch_rank_table_display")
    else:
        print("⚠ fetch_rank_table_display pattern not found - maybe already done?")

    # ============================================================
    # 7. MODIFY: api_fetch_base_salaries
    # ============================================================
    old = '''@app.get("/api/fetch_base_salaries", tags=["Staff"])
async def api_fetch_base_salaries(auth=Depends(verify_api_key)):
    """Fetch staff base salaries from Setting!S:T columns."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        staff = ws.col_values(19)[1:]
        salaries = ws.col_values(20)[1:]
        result = {}
        for i, name in enumerate(staff):
            name = name.strip()
            if not name:
                continue
            sal_str = salaries[i].strip() if i < len(salaries) else "0"
            result[name] = int_safe(sal_str)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/fetch_base_salaries", tags=["Staff"])
async def api_fetch_base_salaries(auth=Depends(verify_api_key)):
    """Fetch staff base salaries — MySQL path with gspread fallback."""
    data = _fetch_base_salaries_from_mysql()
    if data is not None:
        return ok(data)
    try:
        ws = get_worksheet(SHEET_SETTING)
        staff = ws.col_values(19)[1:]
        salaries = ws.col_values(20)[1:]
        result = {}
        for i, name in enumerate(staff):
            name = name.strip()
            if not name:
                continue
            sal_str = salaries[i].strip() if i < len(salaries) else "0"
            result[name] = int_safe(sal_str)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated fetch_base_salaries")
    else:
        print("⚠ fetch_base_salaries pattern not found")

    # ============================================================
    # 8. MODIFY: api_fetch_attendance
    # ============================================================
    old = '''@app.get("/api/fetch_attendance/{month_str}", tags=["Attendance"])
async def api_fetch_attendance(month_str: str, auth=Depends(verify_api_key)):
    """Fetch attendance records for a month from Attendance_Log."""
    try:
        ws = get_worksheet(SHEET_ATTENDANCE_LOG)
        rows = ws.get_all_values()
        result = {}
        for row in rows[1:]:
            if len(row) < 4:
                continue
            if row[0].strip() != month_str:
                continue
            staff = row[1].strip()
            if not staff:
                continue
            result[staff] = {
                "leave_days": int_safe(row[2]) if len(row) > 2 else 0,
                "late_count": int_safe(row[3]) if len(row) > 3 else 0,
                "deduct_per_late": int_safe(row[4]) if len(row) > 4 and row[4].strip() else 500,
            }
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/fetch_attendance/{month_str}", tags=["Attendance"])
async def api_fetch_attendance(month_str: str, auth=Depends(verify_api_key)):
    """Fetch attendance records for a month — MySQL path with gspread fallback."""
    data = _fetch_attendance_from_mysql(month_str)
    if data is not None:
        return ok(data)
    try:
        ws = get_worksheet(SHEET_ATTENDANCE_LOG)
        rows = ws.get_all_values()
        result = {}
        for row in rows[1:]:
            if len(row) < 4:
                continue
            if row[0].strip() != month_str:
                continue
            staff = row[1].strip()
            if not staff:
                continue
            result[staff] = {
                "leave_days": int_safe(row[2]) if len(row) > 2 else 0,
                "late_count": int_safe(row[3]) if len(row) > 3 else 0,
                "deduct_per_late": int_safe(row[4]) if len(row) > 4 and row[4].strip() else 500,
            }
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated fetch_attendance")
    else:
        print("⚠ fetch_attendance pattern not found")

    # ============================================================
    # 9. MODIFY: api_fetch_salary_advances
    # ============================================================
    old = '''@app.get("/api/fetch_salary_advances/{month_str}", tags=["Attendance"])
async def api_fetch_salary_advances(month_str: str, auth=Depends(verify_api_key)):
    """Return {staff: {total, cash, kpay}} for the given month (YYYY-MM)."""
    try:
        ws = get_worksheet(SHEET_SALARY_ADVANCE)
        rows = ws.get_all_values()
        result = {}
        for row in rows[1:]:
            if len(row) < 5:
                continue
            date_val = row[0].strip()
            staff = row[1].strip()
            if not staff or not date_val:
                continue
            if month_str.replace("-", "/") not in date_val and month_str not in date_val:
                continue
            amt = int_safe(row[2]) if row[2].strip() else 0
            pay_method = row[3].strip().upper() if len(row) > 3 else "CASH"
            if staff not in result:
                result[staff] = {"total": 0, "cash": 0, "kpay": 0}
            result[staff]["total"] += amt
            if "KPAY" in pay_method:
                result[staff]["kpay"] += amt
            else:
                result[staff]["cash"] += amt
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/fetch_salary_advances/{month_str}", tags=["Attendance"])
async def api_fetch_salary_advances(month_str: str, auth=Depends(verify_api_key)):
    """Return {staff: {total, cash, kpay}} for the given month (YYYY-MM) — MySQL path with gspread fallback."""
    data = _fetch_salary_advances_from_mysql(month_str)
    if data is not None:
        return ok(data)
    try:
        ws = get_worksheet(SHEET_SALARY_ADVANCE)
        rows = ws.get_all_values()
        result = {}
        for row in rows[1:]:
            if len(row) < 5:
                continue
            date_val = row[0].strip()
            staff = row[1].strip()
            if not staff or not date_val:
                continue
            if month_str.replace("-", "/") not in date_val and month_str not in date_val:
                continue
            amt = int_safe(row[2]) if row[2].strip() else 0
            pay_method = row[3].strip().upper() if len(row) > 3 else "CASH"
            if staff not in result:
                result[staff] = {"total": 0, "cash": 0, "kpay": 0}
            result[staff]["total"] += amt
            if "KPAY" in pay_method:
                result[staff]["kpay"] += amt
            else:
                result[staff]["cash"] += amt
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated fetch_salary_advances")
    else:
        print("⚠ fetch_salary_advances pattern not found")

    # ============================================================
    # 10. MODIFY: api_fetch_promotions_cached
    # ============================================================
    old = '''@app.get("/api/fetch_promotions_cached", tags=["Promotions"])
async def api_fetch_promotions_cached(auth=Depends(verify_api_key)):
    """Fetch active promotions from Promotions sheet (cached 60s)."""
    try:'''

    new = '''@app.get("/api/fetch_promotions_cached", tags=["Promotions"])
async def api_fetch_promotions_cached(auth=Depends(verify_api_key)):
    """Fetch active promotions — MySQL path with gspread fallback."""
    data = _fetch_promotions_cached_from_mysql()
    if data is not None:
        return ok(data)
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated fetch_promotions_cached")
    else:
        print("⚠ fetch_promotions_cached pattern not found")

    # ============================================================
    # 11. MODIFY: api_save_attendance
    # ============================================================
    old = '''@app.post("/api/save_attendance", tags=["Attendance"])
async def api_save_attendance(req: dict, auth=Depends(verify_api_key)):
    """Save/update attendance record for a staff in Attendance_Log."""
    try:
        month_str = req.get("month_str", "")
        staff = req.get("staff", "")
        leave_days = req.get("leave_days", 0)
        late_count = req.get("late_count", 0)
        deduct_per_late = req.get("deduct_per_late", 500)

        sh = get_worksheet(SHEET_ATTENDANCE_LOG)
        rows = sh.get_all_values()
        found = False
        for i, row in enumerate(rows[1:], start=2):
            if row[0].strip() == month_str and row[1].strip() == staff:
                sh.update(f"A{i}:E{i}", [[month_str, staff, leave_days, late_count, deduct_per_late]])
                found = True
                break
        if not found:
            sh.append_row([month_str, staff, leave_days, late_count, deduct_per_late])
        return ok({"staff": staff, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.post("/api/save_attendance", tags=["Attendance"])
async def api_save_attendance(req: dict, auth=Depends(verify_api_key)):
    """Save/update attendance record for a staff — MySQL path with gspread fallback."""
    try:
        month_str = req.get("month_str", "")
        staff = req.get("staff", "")
        leave_days = req.get("leave_days", 0)
        late_count = req.get("late_count", 0)
        deduct_per_late = req.get("deduct_per_late", 500)

        # MySQL path: Log attendance entries individually
        if _use_mysql() and leave_days > 0:
            mysql_execute(
                "INSERT INTO attendance_log (staff_name, date, status, notes) VALUES (%s, CURDATE(), 'leave', %s)",
                (staff, f"{leave_days} leave days"))
        if _use_mysql() and late_count > 0:
            mysql_execute(
                "INSERT INTO attendance_log (staff_name, date, status, notes) VALUES (%s, CURDATE(), 'late', %s)",
                (staff, f"{late_count} late marks, deduct={deduct_per_late}"))

        # gspread fallback
        sh = get_worksheet(SHEET_ATTENDANCE_LOG)
        rows = sh.get_all_values()
        found = False
        for i, row in enumerate(rows[1:], start=2):
            if row[0].strip() == month_str and row[1].strip() == staff:
                sh.update(f"A{i}:E{i}", [[month_str, staff, leave_days, late_count, deduct_per_late]])
                found = True
                break
        if not found:
            sh.append_row([month_str, staff, leave_days, late_count, deduct_per_late])
        return ok({"staff": staff, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated save_attendance")
    else:
        print("⚠ save_attendance pattern not found")

    # ============================================================
    # 12. MODIFY: api_bookings (customer bot)
    # ============================================================
    old = '''@app.post("/api/bookings", tags=["Bookings"])
async def api_bookings(req: dict, auth=Depends(verify_api_key)):
    """Create a booking from customer bot payload."""
    try:
        import json as _json
        sh = get_worksheet(SHEET_CONSOLE_BOOKING)
        now = now_mmt()
        date_formatted = now.strftime("%-m/%-d/%Y")
        time_s = now.strftime("%H:%M")
        seq = now.strftime("%H%M")
        console_type = req.get("consoleType", "")
        console_id = console_type  # Store as-is

        notes_data = {
            "customerName": req.get("customerName", ""),
            "phone": req.get("phone", ""),
            "timeSlot": req.get("timeSlot", ""),
            "consoleType": req.get("consoleType", ""),
            "durationMins": req.get("durationMins", 0),
            "gameName": req.get("gameName", ""),
            "telegramChatId": req.get("telegramChatId", ""),
            "username": req.get("username", ""),
        }
        notes_json = _json.dumps(notes_data)
        bk_id = f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ', '').replace('-', '')}-{seq}"
        sh.append_row([bk_id, date_formatted, console_id, "", time_s, "", "Pending", "", notes_json],
                      value_input_option="USER_ENTERED")
        invalidate_cache("bookings")
        return ok({"id": bk_id, "status": "Pending"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.post("/api/bookings", tags=["Bookings"])
async def api_bookings(req: dict, auth=Depends(verify_api_key)):
    """Create a booking from customer bot payload — MySQL path with gspread fallback."""
    try:
        import json as _json
        now = now_mmt()
        date_formatted = now.strftime("%-m/%-d/%Y")
        time_s = now.strftime("%H:%M")
        seq = now.strftime("%H%M")
        console_type = req.get("consoleType", "")
        console_id = console_type  # Store as-is

        notes_data = {
            "customerName": req.get("customerName", ""),
            "phone": req.get("phone", ""),
            "timeSlot": req.get("timeSlot", ""),
            "consoleType": req.get("consoleType", ""),
            "durationMins": req.get("durationMins", 0),
            "gameName": req.get("gameName", ""),
            "telegramChatId": req.get("telegramChatId", ""),
            "username": req.get("username", ""),
        }
        notes_json = _json.dumps(notes_data)
        bk_id = f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ', '').replace('-', '')}-{seq}"

        # MySQL path
        if _use_mysql():
            mysql_execute(
                "INSERT INTO console_booking (console_id, member_id, booking_date, start_time, status, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (console_id, "", now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S"), "Pending", notes_json))
            invalidate_cache("bookings")
            return ok({"id": bk_id, "status": "Pending"})

        # gspread fallback
        sh = get_worksheet(SHEET_CONSOLE_BOOKING)
        sh.append_row([bk_id, date_formatted, console_id, "", time_s, "", "Pending", "", notes_json],
                      value_input_option="USER_ENTERED")
        invalidate_cache("bookings")
        return ok({"id": bk_id, "status": "Pending"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated bookings (customer bot)")
    else:
        print("⚠ bookings pattern not found")

    # ============================================================
    # 13. MODIFY: api_next_member_id
    # ============================================================
    old = '''@app.get("/api/next_member_id", tags=["Members"])
async def api_next_member_id(auth=Depends(verify_api_key)):
    """Auto-increment member ID: PSV_A_003 -> PSV_A_004."""
    try:'''

    new = '''@app.get("/api/next_member_id", tags=["Members"])
async def api_next_member_id(auth=Depends(verify_api_key)):
    """Auto-increment member ID: PSV_A_003 -> PSV_A_004 — MySQL path with gspread fallback."""
    data = _fetch_next_member_id_from_mysql()
    if data is not None:
        return ok(data)
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated next_member_id")
    else:
        print("⚠ next_member_id pattern not found")

    # ============================================================
    # 14. MODIFY: api_next_member_row_no
    # ============================================================
    old = '''@app.get("/api/next_member_row_no", tags=["Members"])
async def api_next_member_row_no(auth=Depends(verify_api_key)):
    """Return next sequential row number for Card_Wallet Column A."""
    try:'''

    new = '''@app.get("/api/next_member_row_no", tags=["Members"])
async def api_next_member_row_no(auth=Depends(verify_api_key)):
    """Return next sequential row number — MySQL path with gspread fallback."""
    data = _fetch_next_member_row_no_from_mysql()
    if data is not None:
        return ok(data)
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated next_member_row_no")
    else:
        print("⚠ next_member_row_no pattern not found")

    # ============================================================
    # 15. MODIFY: api_fetch_referral_code
    # ============================================================
    old = '''@app.get("/api/fetch_referral_code/{member_id}", tags=["Members"])
async def api_fetch_referral_code(member_id: str, auth=Depends(verify_api_key)):
    """Fetch referral code for a member from Card_Wallet."""
    try:'''

    new = '''@app.get("/api/fetch_referral_code/{member_id}", tags=["Members"])
async def api_fetch_referral_code(member_id: str, auth=Depends(verify_api_key)):
    """Fetch referral code for a member — MySQL path with gspread fallback."""
    data = _fetch_referral_code_from_mysql(member_id)
    if data is not None:
        return ok(data)
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated fetch_referral_code")
    else:
        print("⚠ fetch_referral_code pattern not found")

    # ============================================================
    # 16. MODIFY: api_save_referral_code
    # ============================================================
    old = '''@app.post("/api/save_referral_code", tags=["Members"])
async def api_save_referral_code(req: dict, auth=Depends(verify_api_key)):
    """Save referral code for a member in Card_Wallet."""
    try:'''

    new = '''@app.post("/api/save_referral_code", tags=["Members"])
async def api_save_referral_code(req: dict, auth=Depends(verify_api_key)):
    """Save referral code for a member — MySQL path with gspread fallback."""
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Updated save_referral_code docstring")
    else:
        print("⚠ save_referral_code pattern not found")

    # Now update the body of save_referral_code to add MySQL
    old = '''        member_id = req.get("member_id", "")
        code = req.get("code", "")
        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                ws.update_cell(i, 14, code)
                invalidate_cache("members")
                return ok({"member_id": member_id, "code": code})
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")'''

    new_save_ref = '''        member_id = req.get("member_id", "")
        code = req.get("code", "")

        # MySQL path
        if _use_mysql():
            result = mysql_execute(
                "UPDATE member_wallets SET referral_code = %s WHERE member_id = %s",
                (code, member_id))
            if result > 0:
                return ok({"member_id": member_id, "code": code})
            # Try members table if wallet not found
            result = mysql_execute(
                "INSERT INTO member_wallets (member_id, referral_code, balance_mins, tier) VALUES (%s, %s, 0, 'Warrior') "
                "ON DUPLICATE KEY UPDATE referral_code = %s",
                (member_id, code, code))
            if result > 0:
                return ok({"member_id": member_id, "code": code})

        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                ws.update_cell(i, 14, code)
                invalidate_cache("members")
                return ok({"member_id": member_id, "code": code})
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")'''

    if old in content:
        content = content.replace(old, new_save_ref)
        print("✓ Migrated save_referral_code body")
    else:
        print("⚠ save_referral_code body pattern not found")

    # ============================================================
    # 17. MODIFY: api_update_member_effective_rate
    # ============================================================
    old = '''        member_id = req.get("member_id", "")
        rate = req.get("rate", 0.0)
        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                ws.update_cell(i, 12, round(float(rate), 4))
                invalidate_cache("members")
                return ok({"member_id": member_id, "rate": rate})
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new_eff_rate = '''        member_id = req.get("member_id", "")
        rate = req.get("rate", 0.0)

        # MySQL path
        if _use_mysql():
            result = mysql_execute(
                "UPDATE member_wallets SET effective_rate = %s WHERE member_id = %s",
                (round(float(rate), 4), member_id))
            if result > 0:
                return ok({"member_id": member_id, "rate": rate})
            raise HTTPException(status_code=404, detail=f"Member {member_id} not found")

        # gspread fallback
        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                ws.update_cell(i, 12, round(float(rate), 4))
                invalidate_cache("members")
                return ok({"member_id": member_id, "rate": rate})
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new_eff_rate)
        print("✓ Migrated update_member_effective_rate")
    else:
        print("⚠ update_member_effective_rate pattern not found")

    # ============================================================
    # 18. MODIFY: api_track_referral
    # ============================================================
    old_track_ref = '''        now = now_mmt()
        date_str = now.strftime("%-m/%-d/%Y")
        time_str = now.strftime("%H:%M:%S")

        ws = get_worksheet(SHEET_REFERRAL_LOG)

        # Ensure headers exist
        all_rows = ws.get_all_values()
        if not all_rows:
            ws.append_row(["Date", "Time", "Referrer ID", "Referred User ID",
                           "Referred Username", "Status"])
            logger.info("Created Referral_Log headers")

        ws.append_row([date_str, time_str, referrer_id, referred_user_id,
                       referred_username or "", "clicked"],
                      value_input_option="USER_ENTERED")

        logger.info("Referral tracked: referrer=%s referred=%s", referrer_id, referred_user_id)
        return ok({"status": "ok"})'''

    new_track_ref = '''        now = now_mmt()

        # MySQL path
        if _use_mysql():
            mysql_execute(
                "INSERT INTO referral_log (member_id, referrer_id, referral_code, source, notes) "
                "VALUES (%s, %s, %s, %s, %s)",
                (referred_user_id, referrer_id, referred_username, "customer_bot",
                 f"Referred user: {referred_username}"))
            logger.info("Referral tracked (MySQL): referrer=%s referred=%s", referrer_id, referred_user_id)
            return ok({"status": "ok"})

        # gspread fallback
        date_str = now.strftime("%-m/%-d/%Y")
        time_str = now.strftime("%H:%M:%S")
        ws = get_worksheet(SHEET_REFERRAL_LOG)

        # Ensure headers exist
        all_rows = ws.get_all_values()
        if not all_rows:
            ws.append_row(["Date", "Time", "Referrer ID", "Referred User ID",
                           "Referred Username", "Status"])
            logger.info("Created Referral_Log headers")

        ws.append_row([date_str, time_str, referrer_id, referred_user_id,
                       referred_username or "", "clicked"],
                      value_input_option="USER_ENTERED")

        logger.info("Referral tracked: referrer=%s referred=%s", referrer_id, referred_user_id)
        return ok({"status": "ok"})'''

    if old_track_ref in content:
        content = content.replace(old_track_ref, new_track_ref)
        print("✓ Migrated track_referral")
    else:
        print("⚠ track_referral pattern not found")

    # ============================================================
    # 19. MODIFY: api_set_game_disc_count
    # ============================================================
    old = '''@app.put("/api/set_game_disc_count", tags=["Games"])
async def api_set_game_disc_count(req: dict, auth=Depends(verify_api_key)):
    """Update column D (Available Discs) for a game row in Game_Library."""
    try:
        row_num = req.get("row_num", 0)
        count = req.get("count", 0)
        sh = get_worksheet(SHEET_GAME_LIBRARY)
        sh.update_cell(row_num, 4, count)
        invalidate_cache("games")
        return ok({"row_num": row_num, "count": count})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.put("/api/set_game_disc_count", tags=["Games"])
async def api_set_game_disc_count(req: dict, auth=Depends(verify_api_key)):
    """Update disc count for a game in Game_Library — MySQL path with gspread fallback."""
    try:
        game_title = req.get("game_title", "")
        count = req.get("count", 0)
        row_num = req.get("row_num", 0)

        # MySQL path
        if _use_mysql() and game_title:
            mysql_execute(
                "UPDATE games_library SET disc_count = %s WHERE game_title = %s",
                (count, game_title))
            invalidate_cache("games")
            return ok({"game_title": game_title, "count": count})

        # gspread fallback
        sh = get_worksheet(SHEET_GAME_LIBRARY)
        sh.update_cell(row_num, 4, count)
        invalidate_cache("games")
        return ok({"row_num": row_num, "count": count})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated set_game_disc_count")
    else:
        print("⚠ set_game_disc_count pattern not found")

    # ============================================================
    # 20. MODIFY: api_update_game_library_install
    # ============================================================
    old = '''@app.put("/api/update_game_library_install", tags=["Games"])
async def api_update_game_library_install(req: dict, auth=Depends(verify_api_key)):
    """Set TRUE/FALSE checkbox in Game_Library for (game_title, console_id)."""
    try:'''

    new = '''@app.put("/api/update_game_library_install", tags=["Games"])
async def api_update_game_library_install(req: dict, auth=Depends(verify_api_key)):
    """Set install status for a game — MySQL path with gspread fallback."""
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Updated update_game_library_install docstring")
    else:
        print("⚠ update_game_library_install pattern not found")

    # Add MySQL path at start of try block for update_game_library_install
    old_init = '''        game_title = req.get("game_title", "")
        console_id = req.get("console_id", "")
        installed = req.get("installed", False)

        sh = get_worksheet(SHEET_GAME_LIBRARY)'''

    new_init = '''        game_title = req.get("game_title", "")
        console_id = req.get("console_id", "")
        installed = req.get("installed", False)

        # MySQL path: upsert into console_games
        if _use_mysql():
            if installed:
                mysql_execute(
                    "INSERT INTO console_games (console_id, console_name, game_title, status) "
                    "VALUES (%s, %s, %s, 'active') "
                    "ON DUPLICATE KEY UPDATE status = 'active'",
                    (console_id, console_id, game_title))
            else:
                mysql_execute(
                    "UPDATE console_games SET status = 'inactive' WHERE console_id = %s AND game_title = %s",
                    (console_id, game_title))
            invalidate_cache("games")
            return ok({"game": game_title, "console": console_id, "installed": installed})

        # gspread fallback
        sh = get_worksheet(SHEET_GAME_LIBRARY)'''

    if old_init in content:
        content = content.replace(old_init, new_init)
        print("✓ Migrated update_game_library_install body")
    else:
        print("⚠ update_game_library_install init pattern not found")

    # ============================================================
    # 21. MODIFY: api_add_console_to_setting
    # ============================================================
    old = '''@app.post("/api/add_console_to_setting", tags=["Console"])
async def api_add_console_to_setting(req: dict, auth=Depends(verify_api_key)):
    """Append a new console to Setting!H:J."""
    try:
        console_id = req.get("console_id", "")
        ctype = req.get("ctype", "")
        multiplier = req.get("multiplier", 1.0)
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)
        next_row = len(names) + 1
        ws.update(f"H{next_row}:J{next_row}", [[console_id, ctype, str(multiplier)]],
                  value_input_option="USER_ENTERED")
        return ok({"console_id": console_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.post("/api/add_console_to_setting", tags=["Console"])
async def api_add_console_to_setting(req: dict, auth=Depends(verify_api_key)):
    """Add a new console — MySQL path with gspread fallback."""
    try:
        console_id = req.get("console_id", "")
        ctype = req.get("ctype", "")
        multiplier = req.get("multiplier", 1.0)

        # MySQL path
        if _use_mysql():
            mysql_execute(
                "INSERT INTO console_status (console_id, status) VALUES (%s, 'available') "
                "ON DUPLICATE KEY UPDATE status = 'available'",
                (console_id,))
            return ok({"console_id": console_id})

        # gspread fallback
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)
        next_row = len(names) + 1
        ws.update(f"H{next_row}:J{next_row}", [[console_id, ctype, str(multiplier)]],
                  value_input_option="USER_ENTERED")
        return ok({"console_id": console_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated add_console_to_setting")
    else:
        print("⚠ add_console_to_setting pattern not found")

    # ============================================================
    # 22. MODIFY: api_remove_console_from_setting
    # ============================================================
    old = '''@app.delete("/api/remove_console_from_setting/{console_id}", tags=["Console"])
async def api_remove_console_from_setting(console_id: str, auth=Depends(verify_api_key)):
    """Clear a console row from Setting!H:J."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)
        for i, name in enumerate(names):
            if name.strip() == console_id.strip():
                row = i + 1
                ws.update(f"H{row}:J{row}", [["", "", ""]])
                return ok({"console_id": console_id})
        raise HTTPException(status_code=404, detail=f"Console {console_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.delete("/api/remove_console_from_setting/{console_id}", tags=["Console"])
async def api_remove_console_from_setting(console_id: str, auth=Depends(verify_api_key)):
    """Remove a console — MySQL path with gspread fallback."""
    try:
        # MySQL path
        if _use_mysql():
            result = mysql_execute("DELETE FROM console_status WHERE console_id = %s", (console_id,))
            if result > 0:
                return ok({"console_id": console_id})
            raise HTTPException(status_code=404, detail=f"Console {console_id} not found")

        # gspread fallback
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)
        for i, name in enumerate(names):
            if name.strip() == console_id.strip():
                row = i + 1
                ws.update(f"H{row}:J{row}", [["", "", ""]])
                return ok({"console_id": console_id})
        raise HTTPException(status_code=404, detail=f"Console {console_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated remove_console_from_setting")
    else:
        print("⚠ remove_console_from_setting pattern not found")

    # ============================================================
    # 23. MODIFY: Analytics endpoints - add MySQL-first wrappers
    # ============================================================
    # member_activity
    old = '''@app.get("/api/analytics/member_activity", tags=["Analytics"])
async def api_analytics_member_activity(auth=Depends(verify_api_key)):
    """Return member activity stats: total members, tier distribution, active today, wallet totals."""
    try:
        from analytics import get_member_activity
        return ok(get_member_activity())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/analytics/member_activity", tags=["Analytics"])
async def api_analytics_member_activity(auth=Depends(verify_api_key)):
    """Return member activity stats — MySQL path with gspread fallback."""
    data = _fetch_analytics_member_activity_from_mysql()
    if data is not None:
        return ok(data)
    try:
        from analytics import get_member_activity
        return ok(get_member_activity())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated analytics/member_activity")
    else:
        print("⚠ analytics/member_activity pattern not found")

    # console_usage
    old = '''@app.get("/api/analytics/console_usage", tags=["Analytics"])
async def api_analytics_console_usage(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    auth=Depends(verify_api_key),
):
    """Return console usage stats: bookings per console, utilization rate, daily series."""
    try:
        from analytics import get_console_usage
        return ok(get_console_usage(days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/analytics/console_usage", tags=["Analytics"])
async def api_analytics_console_usage(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    auth=Depends(verify_api_key),
):
    """Return console usage stats — MySQL path with gspread fallback."""
    data = _fetch_analytics_console_usage_from_mysql(days)
    if data is not None:
        return ok(data)
    try:
        from analytics import get_console_usage
        return ok(get_console_usage(days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated analytics/console_usage")
    else:
        print("⚠ analytics/console_usage pattern not found")

    # dashboard
    old = '''@app.get("/api/analytics/dashboard", tags=["Analytics"])
async def api_analytics_dashboard(auth=Depends(verify_api_key)):
    """Return full BI dashboard summary with all KPIs."""
    try:
        from analytics import get_dashboard_summary
        return ok(get_dashboard_summary())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    new = '''@app.get("/api/analytics/dashboard", tags=["Analytics"])
async def api_analytics_dashboard(auth=Depends(verify_api_key)):
    """Return full BI dashboard summary — MySQL path with gspread fallback."""
    data = _fetch_analytics_dashboard_from_mysql()
    if data is not None:
        # Also include member_activity data
        member_data = _fetch_analytics_member_activity_from_mysql()
        if member_data:
            data["member_activity"] = member_data
        return ok(data)
    try:
        from analytics import get_dashboard_summary
        return ok(get_dashboard_summary())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated analytics/dashboard")
    else:
        print("⚠ analytics/dashboard pattern not found")

    # weekly_trends - use MySQL daily_sales + topup_log for this
    old = '''@app.get("/api/analytics/weekly_trends", tags=["Analytics"])
async def api_analytics_weekly_trends(
    weeks: int = Query(4, ge=1, le=52, description="Number of weeks to analyze"),
    auth=Depends(verify_api_key),
):
    """Return weekly trends: sales, top-ups, and console usage aggregated by week."""
    try:'''

    new = '''@app.get("/api/analytics/weekly_trends", tags=["Analytics"])
async def api_analytics_weekly_trends(
    weeks: int = Query(4, ge=1, le=52, description="Number of weeks to analyze"),
    auth=Depends(verify_api_key),
):
    """Return weekly trends — MySQL path with gspread fallback."""
    # MySQL path
    if _use_mysql():
        days = weeks * 7
        topups = mysql_query(
            "SELECT DATE_FORMAT(topup_date, '%Y-%u') as week, COUNT(*) as cnt, "
            "COALESCE(SUM(amount), 0) as total, COALESCE(SUM(mins_added), 0) as mins "
            "FROM topup_log WHERE topup_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
            "GROUP BY week ORDER BY week", (days,))
        bookings = mysql_query(
            "SELECT DATE_FORMAT(booking_date, '%Y-%u') as week, COUNT(*) as cnt "
            "FROM console_booking WHERE booking_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
            "GROUP BY week ORDER BY week", (days,))
        return ok({
            "period_weeks": weeks,
            "topup_weekly": [{"week": r['week'], "count": r['cnt'], "amount": int(r['total'] or 0), "mins": int(r['mins'] or 0)} for r in (topups or [])],
            "console_weekly": [{"week": r['week'], "count": r['cnt']} for r in (bookings or [])],
        })
    try:'''

    if old in content:
        content = content.replace(old, new)
        print("✓ Migrated analytics/weekly_trends")
    else:
        print("⚠ analytics/weekly_trends pattern not found")

    # ============================================================
    # Verify and write
    # ============================================================
    if content == original:
        print("\n⚠ NO CHANGES WERE MADE! The patterns may have already been applied.")
    else:
        write_app(content)
        print(f"\n✓ Modified {APP_PATH} — {len(content)} bytes")

if __name__ == "__main__":
    apply_changes()
