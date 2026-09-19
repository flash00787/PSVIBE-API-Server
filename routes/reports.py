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

        today_bookings = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_bookings WHERE DATE(booking_date) = %s", (today,)
        )
        today_bookings = today_bookings["cnt"] if today_bookings else 0

        active_players = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_status WHERE status = 'Active'"
        )
        active_players = active_players["cnt"] if active_players else 0

        today_revenue = _mysql_query_one(
            "SELECT COALESCE(SUM(net), 0) as total FROM sales_daily WHERE sale_date >= %s", (today + " 00:00:00",)
        )
        today_revenue = float(today_revenue["total"]) if today_revenue else 0.0

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
                "active_players": active_players,
                "today_revenue": today_revenue,
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
            "today_revenue": 0, "total_members": 0,
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
                   payment_method, notes, created_at
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
            f"SELECT COALESCE(SUM(amount), 0) as total_amount, COALESCE(SUM(gross), 0) as total_gross, COALESCE(SUM(discount), 0) as total_discount, COALESCE(SUM(net), 0) as total_net, COALESCE(SUM(gross - amount), 0) as total_food, COALESCE(SUM(CAST(REGEXP_REPLACE(notes, '.*Mins?:? *([0-9]+).*', '$1') AS UNSIGNED)), 0) as total_mins FROM sales_daily WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )

        entries = []
        for r in rows:
            notes = r.get("notes") or ""
            # Parse play minutes from notes: "Mins: XX" or "Mins:XX"
            play_mins = 0
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
            }
        }
    except Exception as e:
        logger.error(f"GET /sales-daily error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  NOTE: Report stubs removed — real implementations
#  are now in dashboard_routes (stock/finance/staff route files)
# ═══════════════════════════════════════
