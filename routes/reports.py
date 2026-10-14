"""PS VIBE API — Report Routes."""
import logging
import re
import calendar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from auth import get_current_user, get_staff_or_dashboard_user
from mysql_db import query as _mysql_query, query_one as _mysql_query_one

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Reports"])


# ═══════════════════════════════════════
#  DASHBOARD STATS
# ═══════════════════════════════════════

@router.get("/dashboard/stats")
async def get_dashboard_stats(user: dict = Depends(get_staff_or_dashboard_user)):
    """Get today's summary statistics for the dashboard"""
    try:
        from datetime import timedelta as _td
        today = datetime.now(timezone(_td(hours=6, minutes=30))).strftime("%Y-%m-%d")  # MMT date — sale_date stored in MMT

        # BOOKING vs SESSION SPLIT (owner order 2026-09-20):
        #   today_bookings = RESERVATIONS-ONLY created today that never became a played session.
        #   today_sessions  = sessions run today (all console_sessions rows for today's bookings).
        #   A booking that turned into a live session is counted only as a session.
        today_bookings = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_bookings cb "
            "WHERE DATE(cb.booking_date) = %s "
            "  AND cb.status NOT IN ('done','Active') "
            "  AND NOT EXISTS (SELECT 1 FROM console_sessions s WHERE s.booking_id = cb.id)", (today,)
        )
        today_bookings = today_bookings["cnt"] if today_bookings else 0

        # Session split by ORIGIN (accurate) — 2026-09-20 `source` column:
        #   walkin_sessions  = started at the counter, no booking/reservation
        #   booking_sessions = came through the bot/app reservation flow
        _ts_row = _mysql_query_one(
            "SELECT COUNT(*) as cnt, "
            "  SUM(CASE WHEN cs.source = 'walkin' THEN 1 ELSE 0 END) as walkin_cnt, "
            "  SUM(CASE WHEN cs.source = 'booking' THEN 1 ELSE 0 END) as booking_cnt "
            "FROM console_sessions cs "
            "JOIN console_bookings cb ON cb.id = cs.booking_id "
            "WHERE DATE(cb.booking_date) = %s", (today,)
        )
        today_sessions = _ts_row["cnt"] if _ts_row else 0
        today_walkin_sessions = int(_ts_row["walkin_cnt"] or 0) if _ts_row else 0
        today_booking_sessions = int(_ts_row["booking_cnt"] or 0) if _ts_row else 0

        today_converted = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_bookings cb "
            "WHERE DATE(cb.booking_date) = %s "
            "  AND cb.status IN ('done','Active') "
            "  AND EXISTS (SELECT 1 FROM console_sessions s WHERE s.booking_id = cb.id)", (today,)
        )
        today_converted = today_converted["cnt"] if today_converted else 0

        active_players = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_status WHERE status = 'Active'"
        )
        active_players = active_players["cnt"] if active_players else 0

        # Game amount ONLY — exclude member topup / new-member mirror rows (they belong to topup_income)
        today_revenue = _mysql_query_one(
            "SELECT COALESCE(SUM(net), 0) as total FROM sales_daily WHERE sale_date >= %s "
            "AND is_topup = 0", (today + " 00:00:00",)
        )
        today_revenue = float(today_revenue["total"]) if today_revenue else 0.0
        # Topup income (separate line; source of truth = topup_log)
        today_topup = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total FROM topup_log WHERE topup_date >= %s", (today + " 00:00:00",)
        )
        today_topup = float(today_topup["total"]) if today_topup else 0.0

        total_members = _mysql_query_one("SELECT COUNT(*) as cnt FROM member_wallets WHERE member_id IS NOT NULL AND member_id != ''")
        total_members = total_members["cnt"] if total_members else 0

        # ── Staff App Wave 2: richer today summary (additive — old keys unchanged) ──
        sales_by_staff = []
        food_sales = 0.0
        sales_count = 0
        try:
            sales_by_staff = list(_mysql_query(
                "SELECT COALESCE(NULLIF(staff_name, ''), '-') AS staff_name, COUNT(*) AS count, "
                "COALESCE(SUM(net), 0) AS net, COALESCE(SUM(gross - amount), 0) AS food "
                "FROM sales_daily WHERE sale_date >= %s "
                "GROUP BY COALESCE(NULLIF(staff_name, ''), '-') ORDER BY net DESC",
                (today + " 00:00:00",)))
        except Exception as _e:
            logger.warning(f"stats sales_by_staff failed: {_e}")
            sales_by_staff = []
        try:
            _fs = _mysql_query_one(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(gross - amount), 0) AS food "
                "FROM sales_daily WHERE sale_date >= %s", (today + " 00:00:00",))
            food_sales = float((_fs or {}).get("food") or 0)
            sales_count = int((_fs or {}).get("cnt") or 0)
        except Exception as _e:
            logger.warning(f"stats food failed: {_e}")
        low_stock = []
        try:
            low_stock = list(_mysql_query(
                "SELECT item_name, quantity, COALESCE(reorder_level, 0) AS reorder_level "
                "FROM inventory WHERE COALESCE(reorder_level, 0) > 0 AND quantity <= reorder_level "
                "ORDER BY quantity ASC LIMIT 50"))
        except Exception as _e:
            logger.warning(f"stats low_stock failed: {_e}")
            low_stock = []

        return {
            "success": True,
            "data": {
                "today_bookings": today_bookings,
                "today_sessions": today_sessions,
                "today_converted_bookings": today_converted,
                # accurate origin split (source-based; supersedes member-id heuristic)
                "walkin_sessions": today_walkin_sessions,
                "booking_sessions": today_booking_sessions,
                "active_players": active_players,
                "today_revenue": today_revenue,  # game amount only (topup excluded)
                "today_topup_income": today_topup,
                "today_collected": today_revenue + today_topup,
                "total_members": total_members,
                "sales_count": sales_count,
                "sales_by_staff": sales_by_staff,
                "food_sales": food_sales,
                "low_stock": low_stock,
                "low_stock_count": len(low_stock),
            }
        }
    except Exception as e:
        logger.error(f"Dashboard stats error: {e}")
        return {"success": True, "data": {
            "today_bookings": 0, "active_players": 0,
            "today_sessions": 0, "today_converted_bookings": 0,
            "walkin_sessions": 0, "booking_sessions": 0,
            "today_revenue": 0, "today_topup_income": 0, "today_collected": 0, "total_members": 0,
            "sales_count": 0, "sales_by_staff": [], "food_sales": 0,
            "low_stock": [], "low_stock_count": 0,
        }}


# ═══════════════════════════════════════
#  SALES DAILY
# ═══════════════════════════════════════

@router.get("/dashboard/sales-daily")
async def dashboard_get_sales_daily(
    date: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List sales daily records. Supports single date OR custom period (start_date..end_date).
    Default: current month (if no date/period specified)."""
    try:
        where = ["1=1"]
        params = []
        if start_date and end_date:
            where.append("sale_date >= %s AND sale_date <= %s")
            params.extend([start_date, end_date])
        elif date:
            where.append("sale_date = %s")
            params.append(date)
        else:
            # Default to current month
            _now = datetime.now()
            _month_start = _now.replace(day=1).strftime("%Y-%m-%d")
            # last day of current month
            _last_day = calendar.monthrange(_now.year, _now.month)[1]
            _month_end = _now.replace(day=_last_day).strftime("%Y-%m-%d")
            where.append("sale_date >= %s AND sale_date <= %s")
            params.extend([_month_start, _month_end])
        if search:
            where.append("(voucher_no LIKE %s OR member_id LIKE %s OR staff_name LIKE %s OR notes LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])

        sql = f"""
            SELECT id, voucher_no, sale_date, console_id, member_id,
                   amount, gross, discount, net, staff_name,
                   payment_method, notes, created_at, billable_mins, free_mins
            FROM sales_daily
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM sales_daily WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        summary = _mysql_query_one(
            f"SELECT COALESCE(SUM(amount), 0) as total_amount, COALESCE(SUM(gross), 0) as total_gross, COALESCE(SUM(discount), 0) as total_discount, COALESCE(SUM(net), 0) as total_net, COALESCE(SUM(gross - amount), 0) as total_food, COALESCE(SUM(COALESCE(billable_mins, CAST(REGEXP_REPLACE(notes, '.*Mins?:? *([0-9]+).*', '$1') AS UNSIGNED))), 0) as total_mins, COALESCE(SUM(billable_mins), 0) as total_billable_mins, COALESCE(SUM(free_mins), 0) as total_free_mins FROM sales_daily WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )

        entries = []
        for r in rows:
            notes = r.get("notes") or ""
            # Parse play minutes from notes: "Mins: XX" or "Mins:XX"
            play_mins = 0
            # 2+1 promo (Osmo 2026-10-14): notes "Mins:" is the TOTAL session time
            # incl. the FREE hour — use the BILLABLE minutes when present.
            _bm = r.get("billable_mins")
            if _bm:
                play_mins = int(_bm)
            else:
                m_mins = re.search(r'Mins?:?\s*(\d+)', notes, re.IGNORECASE)
                if m_mins:
                    play_mins = int(m_mins.group(1))
            play_hrs = round(play_mins / 60, 1)
            game_amt = float(r.get("amount") or 0)
            gross = float(r.get("gross") or 0)
            food_amt = max(0, gross - game_amt)
            entries.append({
                "id": r["id"],
                "voucher_no": r.get("voucher_no"),
                "sale_date": str(r["sale_date"]) if r.get("sale_date") else None,
                "console_id": r.get("console_id"),
                "member_id": r.get("member_id"),
                "amount": game_amt,
                "game_amt": game_amt,
                "food_amt": food_amt,
                "play_mins": play_mins,
                "play_hrs": play_hrs,
                "billable_mins": int(r.get("billable_mins") or 0),
                "free_mins": int(r.get("free_mins") or 0),
                "gross": gross,
                "discount": float(r.get("discount") or 0),
                "net": float(r.get("net") or 0),
                "staff_name": r.get("staff_name"),
                "payment_method": r.get("payment_method"),
                "notes": notes,
                "sale_time": r["created_at"].strftime("%H:%M") if r.get("created_at") else None,  # MMT HH:MM (from created_at)
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {
            "success": True,
            "data": entries,
            "total": total,
            "summary": {
                "total_amount": float(summary["total_amount"] or 0) if summary else 0,
                "total_gross": float(summary["total_gross"] or 0) if summary else 0,
                "total_discount": float(summary["total_discount"] or 0) if summary else 0,
                "total_net": float(summary["total_net"] or 0) if summary else 0,
                "total_food": float(summary["total_food"] or 0) if summary else 0,
                "total_mins": int(summary["total_mins"] or 0) if summary else 0,
                "total_hrs": round(int(summary["total_mins"] or 0) / 60, 1) if summary else 0,
                # 🆓 2+1 promo free minutes — shown SEPARATELY (never billed).
                "total_billable_mins": int(summary["total_billable_mins"] or 0) if summary else 0,
                "total_free_mins": int(summary["total_free_mins"] or 0) if summary else 0,
                "total_free_hrs": round(int(summary["total_free_mins"] or 0) / 60, 1) if summary else 0,
            }
        }
    except Exception as e:
        logger.error(f"GET /sales-daily error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  NOTE: Report stubs removed — real implementations
#  are now in dashboard_routes (stock/finance/staff route files)
# ═══════════════════════════════════════
