"""PS VIBE Dashboard — Dashboard Data API Endpoints"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


@router.get("/stats")
async def get_dashboard_stats(user: dict = Depends(get_current_user)):
    """Get today's summary statistics for the dashboard"""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        today_bookings = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_booking WHERE DATE(booking_date) = %s", (today,)
        )
        today_bookings = today_bookings["cnt"] if today_bookings else 0

        active_players = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_booking WHERE DATE(booking_date) = %s AND status IN ('Active', 'Confirmed')",
            (today,)
        )
        active_players = active_players["cnt"] if active_players else 0

        today_revenue = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total FROM sales_daily WHERE DATE(sale_date) = %s", (today,)
        )
        today_revenue = float(today_revenue["total"]) if today_revenue else 0.0

        total_members = _mysql_query_one("SELECT COUNT(*) as cnt FROM members")
        total_members = total_members["cnt"] if total_members else 0

        return {
            "success": True,
            "data": {
                "today_bookings": today_bookings,
                "active_players": active_players,
                "today_revenue": today_revenue,
                "total_members": total_members
            }
        }
    except Exception as e:
        logger.error(f"Dashboard stats error: {e}")
        return {"success": True, "data": {
            "today_bookings": 0, "active_players": 0,
            "today_revenue": 0, "total_members": 0
        }}


@router.get("/consoles")
async def get_console_status(user: dict = Depends(get_current_user)):
    """Get all consoles with their current status"""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = _mysql_query("""
            SELECT c.console_id as id, c.console_id as name, c.status,
                   cb.id as booking_id, cb.member_id as customer_name,
                   cb.start_time, cb.end_time, cb.status as booking_status
            FROM console_status c
            LEFT JOIN (
                SELECT cb1.*
                FROM console_booking cb1
                INNER JOIN (
                    SELECT console_id, MAX(id) as max_id
                    FROM console_booking
                    WHERE DATE(booking_date) = %s
                    AND status IN ('Active', 'Confirmed')
                    GROUP BY console_id
                ) cb2 ON cb1.id = cb2.max_id
            ) cb ON c.console_id = cb.console_id
                AND c.status = 'Active'
            ORDER BY c.console_id
        """, (today,))

        consoles = []
        for row in rows:
            consoles.append({
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "current_booking": {
                    "id": row["booking_id"],
                    "customer": row["customer_name"],
                    "start": str(row["start_time"]) if row.get("start_time") else None,
                    "end": str(row["end_time"]) if row.get("end_time") else None,
                    "status": row["booking_status"]
                } if row.get("booking_id") else None
            })

        return {"success": True, "data": consoles}
    except Exception as e:
        logger.error(f"Console status error: {e}")
        return {"success": True, "data": []}


@router.get("/schedule")
async def get_today_schedule(user: dict = Depends(get_current_user)):
    """Get today's booking schedule"""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = _mysql_query("""
            SELECT cb.id, c.console_id as console_name, cb.member_id as customer_name,
                   cb.start_time, cb.end_time, cb.status,
                   cb.duration_mins, cb.phone
            FROM console_booking cb
            JOIN console_status c ON cb.console_id = c.console_id
            WHERE DATE(cb.booking_date) = %s
            ORDER BY cb.start_time ASC
        """, (today,))

        schedule = []
        for row in rows:
            schedule.append({
                "id": row["id"],
                "console": row["console_name"],
                "customer": row["customer_name"],
                "start": str(row["start_time"]) if row.get("start_time") else None,
                "end": str(row["end_time"]) if row.get("end_time") else None,
                "status": row["status"],
                "duration": row.get("duration_mins"),
                "phone": row["phone"]
            })

        return {"success": True, "data": schedule}
    except Exception as e:
        logger.error(f"Schedule error: {e}")
        return {"success": True, "data": []}


@router.get("/revenue-trend")
async def get_revenue_trend(days: int = Query(7, ge=1, le=30), user: dict = Depends(get_current_user)):
    """Get revenue data for charting"""
    try:
        rows = _mysql_query("""
            SELECT DATE(created_at) as dt, COALESCE(SUM(amount), 0) as total
            FROM sales_daily
            WHERE sale_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY dt
            ORDER BY dt ASC
        """, (days,))

        data = [{"date": str(r["dt"]), "revenue": float(r["total"])} for r in rows]
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Revenue trend error: {e}")
        return {"success": True, "data": []}

# ═══════════════════════════════════════
#  BOOKINGS — CRUD
# ═══════════════════════════════════════
@router.get("/bookings")
async def dashboard_get_bookings(
    status: str | None = Query(None),
    date: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List bookings with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if status:
            where.append("cb.status = %s")
            params.append(status)
        if date:
            where.append("DATE(cb.booking_date) = %s")
            params.append(date)
        if search:
            where.append("(cb.member_id LIKE %s OR cb.staff_name LIKE %s OR cb.phone LIKE %s OR cb.notes LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])

        sql = f"""
            SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date,
                   cb.start_time, cb.end_time, cb.status, cb.staff_name,
                   cb.notes, cb.duration_mins, cb.phone, cb.game_name, cb.created_at
            FROM console_booking cb
            WHERE {' AND '.join(where)}
            ORDER BY cb.id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM console_booking cb WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        bookings = []
        for r in rows:
            bookings.append({
                "id": r["id"],
                "console_id": r.get("console_id"),
                "member_id": r.get("member_id"),
                "booking_date": str(r["booking_date"]) if r.get("booking_date") else None,
                "start_time": str(r["start_time"]) if r.get("start_time") else None,
                "end_time": str(r["end_time"]) if r.get("end_time") else None,
                "status": r.get("status"),
                "staff_name": r.get("staff_name"),
                "notes": r.get("notes"),
                "duration_mins": r.get("duration_mins"),
                "phone": r.get("phone"),
                "game_name": r.get("game_name"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": bookings, "total": total}
    except Exception as e:
        logger.error(f"GET /bookings error: {e}")
        return {"success": False, "error": str(e)}



@router.delete("/bookings/cleanup")
async def dashboard_cleanup_bookings(user: dict = Depends(get_current_user)):
    """Clean up cancelled, rejected, and old bookings."""
    try:
        d1 = _mysql_delete(
            "DELETE FROM console_booking WHERE status IN (%s, %s, %s, %s, %s)",
            ("cancelled", "Cancelled", "rejected", "Rejected", "Done")
        )
        d2 = _mysql_delete(
            "DELETE FROM console_booking WHERE booking_date < DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
        )
        total = d1 + d2
        return {
            "success": True,
            "data": {"deleted_count": total, "message": f"Cleaned up {total} bookings"}
        }
    except Exception as e:
        logger.error(f"DELETE /bookings/cleanup error: {e}")
        return {"success": False, "error": str(e)}

@router.get("/bookings/{booking_id}")
async def dashboard_get_booking(booking_id: int, user: dict = Depends(get_current_user)):
    """Get a single booking by ID."""
    try:
        row = _mysql_query_one(
            """SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date,
                      cb.start_time, cb.end_time, cb.status, cb.staff_name,
                      cb.notes, cb.duration_mins, cb.phone, cb.game_name, cb.created_at,
                      cb.telegram_chat_id
               FROM console_booking cb WHERE cb.id = %s""",
            (booking_id,),
        )
        if not row:
            return {"success": False, "error": "Booking not found"}
        return {
            "success": True,
            "data": {
                "id": row["id"],
                "console_id": row.get("console_id"),
                "member_id": row.get("member_id"),
                "booking_date": str(row["booking_date"]) if row.get("booking_date") else None,
                "start_time": str(row["start_time"]) if row.get("start_time") else None,
                "end_time": str(row["end_time"]) if row.get("end_time") else None,
                "status": row.get("status"),
                "staff_name": row.get("staff_name"),
                "notes": row.get("notes"),
                "duration_mins": row.get("duration_mins"),
                "phone": row.get("phone"),
                "game_name": row.get("game_name"),
                "created_at": str(row["created_at"]) if row.get("created_at") else None,
                "telegram_chat_id": row.get("telegram_chat_id"),
            },
        }
    except Exception as e:
        logger.error(f"GET /bookings/{booking_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/bookings/{booking_id}")
async def dashboard_update_booking(booking_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a booking."""
    try:
        existing = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        updates = []
        params = []
        for field in ["console_id", "member_id", "status", "staff_name", "notes",
                       "duration_mins", "phone", "game_name"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])
        if "booking_date" in req and req["booking_date"]:
            updates.append("booking_date = %s")
            params.append(req["booking_date"])
        if "start_time" in req and req["start_time"]:
            updates.append("start_time = %s")
            params.append(req["start_time"])
        if "end_time" in req and req["end_time"]:
            updates.append("end_time = %s")
            params.append(req["end_time"])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        params.append(booking_id)
        _mysql_execute(f"UPDATE console_booking SET {', '.join(updates)} WHERE id = %s", tuple(params))

        updated = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "console_id": updated.get("console_id"),
                "member_id": updated.get("member_id"),
                "booking_date": str(updated["booking_date"]) if updated.get("booking_date") else None,
                "start_time": str(updated["start_time"]) if updated.get("start_time") else None,
                "end_time": str(updated["end_time"]) if updated.get("end_time") else None,
                "status": updated.get("status"),
                "staff_name": updated.get("staff_name"),
                "notes": updated.get("notes"),
                "duration_mins": updated.get("duration_mins"),
                "phone": updated.get("phone"),
                "game_name": updated.get("game_name"),
                "created_at": str(updated["created_at"]) if updated.get("created_at") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /bookings/{booking_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/bookings/{booking_id}")
async def dashboard_delete_booking(booking_id: int, user: dict = Depends(get_current_user)):
    """Delete a booking."""
    try:
        existing = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        _mysql_delete("DELETE FROM console_booking WHERE id = %s", (booking_id,))
        return {"success": True, "data": {"deleted": booking_id}}
    except Exception as e:
        logger.error(f"DELETE /bookings/{booking_id} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  MEMBERS — CRUD
# ═══════════════════════════════════════
@router.get("/members")
async def dashboard_get_members(
    search: str | None = Query(None),
    tier: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List members with optional search and tier filter."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(m.member_id LIKE %s OR m.member_name LIKE %s OR COALESCE(m.phone,'') LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if tier:
            where.append("m.tier = %s")
            params.append(tier)

        sql = f"""
            SELECT m.member_id, m.member_name as name, COALESCE(m.phone,''), m.balance_mins, m.tier,
                   m.total_spend, m.lifetime_spend, m.join_date, m.last_updated
            FROM member_wallets m
            WHERE {' AND '.join(where)}
            ORDER BY m.member_id ASC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM member_wallets m WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        members = []
        for r in rows:
            members.append({
                "member_id": r["member_id"],
                "name": r.get("name"),
                "phone": r.get("phone"),
                "balance_minutes": r.get("balance_mins"),
                "tier": r.get("tier"),
                "total_spend": float(r.get("total_spend") or 0),
                "lifetime_spend": float(r.get("lifetime_spend") or 0),
                "join_date": str(r["join_date"]) if r.get("join_date") else None,
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })
        return {"success": True, "data": members, "total": total}
    except Exception as e:
        logger.error(f"GET /members error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/members/{member_id}")
async def dashboard_get_member(member_id: str, user: dict = Depends(get_current_user)):
    """Get a single member by member_id."""
    try:
        row = _mysql_query_one(
            """SELECT member_id, member_name, phone, balance_mins, tier,
                      total_spend, lifetime_spend, join_date, last_updated
               FROM member_wallets WHERE member_id = %s""",
            (member_id,),
        )
        if not row:
            return {"success": False, "error": "Member not found"}
        return {
            "success": True,
            "data": {
                "member_id": row["member_id"],
                "name": row.get("member_name"),
                "phone": row.get("phone"),
                "balance_minutes": row.get("balance_mins"),
                "tier": row.get("tier"),
                "total_spend": float(row.get("total_spend") or 0),
                "lifetime_spend": float(row.get("lifetime_spend") or 0),
                "join_date": str(row["join_date"]) if row.get("join_date") else None,
                "last_updated": str(row["last_updated"]) if row.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"GET /members/{member_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/members/{member_id}")
async def dashboard_update_member(member_id: str, req: dict, user: dict = Depends(get_current_user)):
    """Update a member."""
    try:
        existing = _mysql_query_one("SELECT * FROM member_wallets WHERE member_id = %s", (member_id,))
        if not existing:
            return {"success": False, "error": "Member not found"}

        updates = []
        params = []
        field_map = {
            "name": "member_name", "phone": "phone",
            "balance_minutes": "balance_mins", "tier": "tier",
        }
        for json_field, db_field in field_map.items():
            if json_field in req:
                updates.append(f"{db_field} = %s")
                params.append(req[json_field])
        if "total_spend" in req:
            updates.append("total_spend = %s")
            params.append(req["total_spend"])
        if "lifetime_spend" in req:
            updates.append("lifetime_spend = %s")
            params.append(req["lifetime_spend"])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(member_id)
        _mysql_execute(f"UPDATE member_wallets SET {', '.join(updates)} WHERE member_id = %s", tuple(params))

        updated = _mysql_query_one(
            "SELECT member_id, member_name, phone, balance_mins, tier, total_spend, lifetime_spend, join_date, last_updated FROM member_wallets WHERE member_id = %s",
            (member_id,),
        )
        return {
            "success": True,
            "data": {
                "member_id": updated["member_id"],
                "name": updated.get("member_name"),
                "phone": updated.get("phone"),
                "balance_minutes": updated.get("balance_mins"),
                "tier": updated.get("tier"),
                "total_spend": float(updated.get("total_spend") or 0),
                "lifetime_spend": float(updated.get("lifetime_spend") or 0),
                "join_date": str(updated["join_date"]) if updated.get("join_date") else None,
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /members/{member_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/members/{member_id}/topups")
async def dashboard_get_member_topups(
    member_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get topup history for a member."""
    try:
        rows = _mysql_query(
            """SELECT id, member_id, amount, mins_added, topup_date,
                      staff_name, payment_method, balance_before, balance_after,
                      balance_mins_before, balance_mins_after
               FROM topup_log WHERE member_id = %s
               ORDER BY topup_date DESC LIMIT %s OFFSET %s""",
            (member_id, limit, offset),
        )
        count_row = _mysql_query_one(
            "SELECT COUNT(*) as total FROM topup_log WHERE member_id = %s", (member_id,)
        )
        total = count_row["total"] if count_row else 0

        topups = []
        for r in rows:
            topups.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "amount": float(r.get("amount") or 0),
                "mins_added": r.get("mins_added"),
                "topup_date": str(r["topup_date"]) if r.get("topup_date") else None,
                "staff_name": r.get("staff_name"),
                "payment_method": r.get("payment_method"),
                "balance_before": float(r.get("balance_before") or 0),
                "balance_after": float(r.get("balance_after") or 0),
                "balance_mins_before": r.get("balance_mins_before"),
                "balance_mins_after": r.get("balance_mins_after"),
            })
        return {"success": True, "data": topups, "total": total}
    except Exception as e:
        logger.error(f"GET /members/{member_id}/topups error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  COUPONS — View All
# ═══════════════════════════════════════
@router.get("/coupons")
async def dashboard_get_coupons(
    search: str | None = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List all member coupons with member details."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(c.coupon_code LIKE %s OR c.member_id LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like])

        sql = f'''
            SELECT c.id, c.coupon_code, c.member_id, c.original_minutes,
                   c.balance_minutes, c.issued_date, c.expiry_date, c.status,
                   c.redeemed_at, m.name, COALESCE(m.phone,'')
            FROM member_coupons c
            LEFT JOIN members m ON c.member_id = m.member_id
            WHERE {' AND '.join(where)}
            ORDER BY c.issued_date DESC
            LIMIT %s OFFSET %s
        '''
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_sql = f"SELECT COUNT(*) as total FROM member_coupons c WHERE {' AND '.join(where)}"
        cnt_params = params[:-2]  # remove limit/offset
        count_row = _mysql_query_one(count_sql, tuple(cnt_params))
        total = count_row["total"] if count_row else 0

        coupons = []
        for r in rows:
            coupons.append({
                "id": r["id"],
                "code": r["coupon_code"],
                "member_id": r["member_id"],
                "member_name": r.get("name") or "-",
                "phone": r.get("phone") or "-",
                "original_minutes": r["original_minutes"],
                "balance_minutes": r["balance_minutes"],
                "issued_date": str(r["issued_date"])[:16] if r.get("issued_date") else "",
                "expiry_date": str(r["expiry_date"])[:10] if r.get("expiry_date") else "",
                "status": r["status"],
                "redeemed_at": str(r["redeemed_at"])[:16] if r.get("redeemed_at") else "",
            })
        return {"success": True, "data": coupons, "total": total}
    except Exception as e:
        logger.error(f"GET /coupons error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  INVENTORY — CRUD
# ═══════════════════════════════════════

@router.get("/topups")
async def dashboard_get_topups(
    search: str = "",
    limit: int = 100,
    user: dict = Depends(get_current_user),
):
    """List all topup logs with search support."""
    try:
        from mysql_db import query as _mq
        sql = """
            SELECT id, member_id, amount, mins_added, topup_date,
                   staff_name, payment_method, balance_before, balance_after,
                   balance_mins_before, balance_mins_after, notes, created_at
            FROM topup_log
        """
        where = []
        params = []
        if search:
            where.append("member_id LIKE %s")
            s = f"%{search}%"
            params.append(s)
            params.append(s)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY topup_date DESC LIMIT %s"
        params.append(int(limit))
        rows = _mq(sql, tuple(params)) if params else _mq(sql)
        return {"success": True, "data": list(rows), "total": len(rows)}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.get("/inventory")
async def dashboard_get_inventory(
    search: str | None = Query(None),
    category: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List inventory items with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("item_name LIKE %s")
            like = f"%{search}%"
            params.append(like)
        if category:
            where.append("category = %s")
            params.append(category)

        sql = f"""
            SELECT id, item_name, category, quantity, unit_price, reorder_level, last_updated
            FROM inventory
            WHERE {' AND '.join(where)}
            ORDER BY quantity DESC, item_name ASC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM inventory WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "item_name": r["item_name"],
                "category": r.get("category"),
                "quantity": r.get("quantity", 0),
                "unit_price": float(r.get("unit_price") or 0),
                "reorder_level": r.get("reorder_level", 0),
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })
        return {"success": True, "data": items, "total": total}
    except Exception as e:
        logger.error(f"GET /inventory error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/inventory")
async def dashboard_create_inventory(req: dict, user: dict = Depends(get_current_user)):
    """Create a new inventory item."""
    try:
        item_name = req.get("item_name", "")
        if not item_name:
            return {"success": False, "error": "item_name is required"}

        new_id = _mysql_execute(
            """INSERT INTO inventory (item_name, category, quantity, unit_price, reorder_level, last_updated)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (
                item_name,
                req.get("category", ""),
                req.get("quantity", 0),
                req.get("unit_price", 0),
                req.get("reorder_level", 0),
            ),
        )
        return {
            "success": True,
            "data": {
                "id": new_id,
                "item_name": item_name,
                "category": req.get("category", ""),
                "quantity": req.get("quantity", 0),
                "unit_price": req.get("unit_price", 0),
                "reorder_level": req.get("reorder_level", 0),
            },
        }
    except Exception as e:
        logger.error(f"POST /inventory error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/inventory/{item_id}")
async def dashboard_update_inventory(item_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update an inventory item."""
    try:
        existing = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not existing:
            return {"success": False, "error": "Inventory item not found"}

        updates = []
        params = []
        for field in ["item_name", "category", "quantity", "unit_price", "reorder_level"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(item_id)
        _mysql_execute(f"UPDATE inventory SET {', '.join(updates)} WHERE id = %s", tuple(params))

        updated = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "item_name": updated["item_name"],
                "category": updated.get("category"),
                "quantity": updated.get("quantity", 0),
                "unit_price": float(updated.get("unit_price") or 0),
                "reorder_level": updated.get("reorder_level", 0),
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /inventory/{item_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/inventory/{item_id}")
async def dashboard_delete_inventory(item_id: int, user: dict = Depends(get_current_user)):
    """Delete an inventory item."""
    try:
        existing = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not existing:
            return {"success": False, "error": "Inventory item not found"}

        _mysql_delete("DELETE FROM inventory WHERE id = %s", (item_id,))
        return {"success": True, "data": {"deleted": item_id}}
    except Exception as e:
        logger.error(f"DELETE /inventory/{item_id} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  PROMOTIONS — CRUD
# ═══════════════════════════════════════
@router.get("/promotions")
async def dashboard_get_promotions(
    status: str | None = Query(None),
    promo_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List promotions with optional filters."""
    try:
        where = ["name IS NOT NULL AND name != ''"]
        where.append("name NOT LIKE 'Test%%'")
        params = []
        if status:
            where.append("status = %s")
            params.append(status)
        if promo_type:
            where.append("promo_type = %s")
            params.append(promo_type)

        sql = f"""
            SELECT p.id, p.name, p.promo_type, p.promo_name, p.discount_type, p.discount_value,
                   p.start_date, p.end_date, p.status, p.notes, p.created_at
            FROM promotions p
            INNER JOIN (
                SELECT name, MAX(id) as max_id
                FROM promotions
                WHERE {' AND '.join(where)}
                GROUP BY name
            ) latest ON p.id = latest.max_id
            ORDER BY p.id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM (SELECT name, MAX(id) FROM promotions WHERE {' AND '.join(where)} AND name NOT LIKE CONCAT('Test', CHAR(37)) GROUP BY name) sub",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        promos = []
        for r in rows:
            promos.append({
                "id": r["id"],
                "name": r.get("name"),
                "promo_type": r.get("promo_type"),
                "promo_name": r.get("promo_name"),
                "discount_type": r.get("discount_type"),
                "discount_value": float(r.get("discount_value") or 0) if r.get("discount_value") else None,
                "start_date": str(r["start_date"]) if r.get("start_date") else None,
                "end_date": str(r["end_date"]) if r.get("end_date") else None,
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": promos, "total": total}
    except Exception as e:
        logger.error(f"GET /promotions error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/promotions")
async def dashboard_create_promotion(req: dict, user: dict = Depends(get_current_user)):
    """Create a new promotion."""
    try:
        name = req.get("name") or req.get("promo_name", "")
        if not name:
            return {"success": False, "error": "name or promo_name is required"}

        new_id = _mysql_execute(
            """INSERT INTO promotions
               (name, promo_type, promo_name, discount_type, discount_value,
                start_date, end_date, status, notes, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (
                name,
                req.get("promo_type", ""),
                req.get("promo_name", name),
                req.get("discount_type", ""),
                req.get("discount_value", 0),
                req.get("start_date", None),
                req.get("end_date", None),
                req.get("status", "active"),
                req.get("notes", ""),
            ),
        )
        return {
            "success": True,
            "data": {
                "id": new_id,
                "name": name,
                "promo_type": req.get("promo_type", ""),
                "promo_name": req.get("promo_name", name),
                "discount_type": req.get("discount_type", ""),
                "discount_value": req.get("discount_value", 0),
                "start_date": str(req.get("start_date", "")),
                "end_date": str(req.get("end_date", "")),
                "status": req.get("status", "active"),
                "notes": req.get("notes", ""),
            },
        }
    except Exception as e:
        logger.error(f"POST /promotions error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/promotions/{promo_id}")
async def dashboard_update_promotion(promo_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a promotion."""
    try:
        existing = _mysql_query_one("SELECT * FROM promotions WHERE id = %s", (promo_id,))
        if not existing:
            return {"success": False, "error": "Promotion not found"}

        updates = []
        params = []
        for field in ["name", "promo_type", "promo_name", "discount_type",
                       "discount_value", "status", "notes"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])
        if "start_date" in req and req["start_date"]:
            updates.append("start_date = %s")
            params.append(req["start_date"])
        if "end_date" in req and req["end_date"]:
            updates.append("end_date = %s")
            params.append(req["end_date"])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        params.append(promo_id)
        _mysql_execute(f"UPDATE promotions SET {', '.join(updates)} WHERE id = %s", tuple(params))

        updated = _mysql_query_one("SELECT * FROM promotions WHERE id = %s", (promo_id,))
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "name": updated.get("name"),
                "promo_type": updated.get("promo_type"),
                "promo_name": updated.get("promo_name"),
                "discount_type": updated.get("discount_type"),
                "discount_value": float(updated.get("discount_value") or 0) if updated.get("discount_value") else None,
                "start_date": str(updated["start_date"]) if updated.get("start_date") else None,
                "end_date": str(updated["end_date"]) if updated.get("end_date") else None,
                "status": updated.get("status"),
                "notes": updated.get("notes"),
                "created_at": str(updated["created_at"]) if updated.get("created_at") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /promotions/{promo_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/promotions/{promo_id}")
async def dashboard_delete_promotion(promo_id: int, user: dict = Depends(get_current_user)):
    """Delete a promotion."""
    try:
        existing = _mysql_query_one("SELECT * FROM promotions WHERE id = %s", (promo_id,))
        if not existing:
            return {"success": False, "error": "Promotion not found"}

        _mysql_delete("DELETE FROM promotions WHERE id = %s", (promo_id,))
        return {"success": True, "data": {"deleted": promo_id}}
    except Exception as e:
        logger.error(f"DELETE /promotions/{promo_id} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  GAMES LIBRARY — CRUD
# ═══════════════════════════════════════
@router.get("/games")
async def dashboard_get_games(
    search: str | None = Query(None),
    genre: str | None = Query(None),
    solo_multi: str | None = Query(None),
    final_status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List games with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("game_title LIKE %s")
            params.append(f"%{search}%")
        if genre:
            where.append("genre = %s")
            params.append(genre)
        if solo_multi:
            where.append("solo_multi = %s")
            params.append(solo_multi)
        if final_status:
            where.append("final_status = %s")
            params.append(final_status)

        sql = f"""
            SELECT game_title, genre, solo_multi, final_status, disc_count, last_updated
            FROM games_library
            WHERE {' AND '.join(where)}
            ORDER BY game_title ASC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM games_library WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        games = []
        for r in rows:
            games.append({
                "game_title": r["game_title"],
                "genre": r.get("genre"),
                "solo_multi": r.get("solo_multi"),
                "final_status": r.get("final_status"),
                "disc_count": r.get("disc_count"),
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })
        return {"success": True, "data": games, "total": total}
    except Exception as e:
        logger.error(f"GET /games error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/games")
async def dashboard_create_game(req: dict, user: dict = Depends(get_current_user)):
    """Create a new game entry."""
    try:
        game_title = req.get("game_title", "")
        if not game_title:
            return {"success": False, "error": "game_title is required"}

        existing = _mysql_query_one("SELECT game_title FROM games_library WHERE game_title = %s", (game_title,))
        if existing:
            return {"success": False, "error": "Game already exists"}

        _mysql_execute(
            """INSERT INTO games_library (game_title, genre, solo_multi, final_status, disc_count, last_updated)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (
                game_title,
                req.get("genre", ""),
                req.get("solo_multi", ""),
                req.get("final_status", ""),
                req.get("disc_count", 0),
            ),
        )
        return {
            "success": True,
            "data": {
                "game_title": game_title,
                "genre": req.get("genre", ""),
                "solo_multi": req.get("solo_multi", ""),
                "final_status": req.get("final_status", ""),
                "disc_count": req.get("disc_count", 0),
            },
        }
    except Exception as e:
        logger.error(f"POST /games error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/games/{game_title:path}")
async def dashboard_update_game(game_title: str, req: dict, user: dict = Depends(get_current_user)):
    """Update a game entry."""
    try:
        existing = _mysql_query_one("SELECT game_title FROM games_library WHERE game_title = %s", (game_title,))
        if not existing:
            return {"success": False, "error": "Game not found"}

        updates = []
        params = []
        for field in ["genre", "solo_multi", "final_status", "disc_count"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(game_title)
        _mysql_execute(f"UPDATE games_library SET {', '.join(updates)} WHERE game_title = %s", tuple(params))

        updated = _mysql_query_one(
            "SELECT game_title, genre, solo_multi, final_status, disc_count, last_updated FROM games_library WHERE game_title = %s",
            (game_title,),
        )
        return {
            "success": True,
            "data": {
                "game_title": updated["game_title"],
                "genre": updated.get("genre"),
                "solo_multi": updated.get("solo_multi"),
                "final_status": updated.get("final_status"),
                "disc_count": updated.get("disc_count"),
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /games/{game_title} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/games/{game_title:path}")
async def dashboard_delete_game(game_title: str, user: dict = Depends(get_current_user)):
    """Delete a game entry."""
    try:
        existing = _mysql_query_one("SELECT game_title FROM games_library WHERE game_title = %s", (game_title,))
        if not existing:
            return {"success": False, "error": "Game not found"}

        _mysql_delete("DELETE FROM games_library WHERE game_title = %s", (game_title,))
        return {"success": True, "data": {"deleted": game_title}}
    except Exception as e:
        logger.error(f"DELETE /games/{game_title} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  STOCK IN / STOCK OUT
# ═══════════════════════════════════════
@router.post("/stock-in")
async def dashboard_stock_in(req: dict, user: dict = Depends(get_current_user)):
    """Record stock-in for an inventory item."""
    try:
        item_id = req.get("item_id")
        quantity = req.get("quantity", 0)
        unit_cost = req.get("unit_cost", 0)
        source = req.get("source", "")
        receipt_no = req.get("receipt_no", "")
        payment_method = req.get("payment_method", "")
        paid_by = req.get("paid_by", "")
        staff_name = req.get("staff_name", "")

        if not item_id or quantity <= 0:
            return {"success": False, "error": "item_id and positive quantity are required"}

        # Fetch item
        item = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not item:
            return {"success": False, "error": "Inventory item not found"}

        import uuid
        batch_id = "SI-" + uuid.uuid4().hex[:12].upper()

        # Insert into stock_in
        _mysql_execute(
            """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (batch_id, item["item_name"], quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
        )

        # Update inventory quantity
        new_qty = int(item["quantity"] or 0) + quantity
        _mysql_execute(
            "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
            (new_qty, item_id)
        )

        # Record cash_movements eject for payment
        total_cost_val = quantity * unit_cost
        if payment_method and total_cost_val > 0:
            note = f"Stock In: {item['item_name']} x{quantity}"
            _mysql_execute(
                "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                ("eject", payment_method, total_cost_val, note, staff_name)
            )

        return {
            "success": True,
            "data": {
                "batch_id": batch_id,
                "item_name": item["item_name"],
                "quantity_added": quantity,
                "new_quantity": new_qty,
                "unit_cost": unit_cost,
                "payment_method": payment_method,
                "paid_by": paid_by,
                "staff_name": staff_name,
            }
        }
    except Exception as e:
        logger.error(f"POST /stock-in error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/stock-in/{entry_id}")
async def dashboard_update_stock_in(entry_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a stock-in record."""
    try:
        existing = _mysql_query_one("SELECT * FROM stock_in WHERE id = %s", (entry_id,))
        if not existing:
            return {"success": False, "error": "Stock-in record not found"}

        item_name = req.get("item_name", existing.get("item_name"))
        quantity = req.get("quantity", existing.get("quantity"))
        unit_cost = req.get("unit_cost", existing.get("unit_cost"))
        source = req.get("source", existing.get("source"))
        receipt_no = req.get("receipt_no", existing.get("receipt_no"))
        payment_method = req.get("payment_method", existing.get("payment_method"))
        paid_by = req.get("paid_by", existing.get("paid_by"))
        staff_name = req.get("staff_name", existing.get("staff_name"))

        old_qty = int(existing.get("quantity") or 0)
        new_qty = int(quantity or 0)

        # Reverse old inventory
        old_item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (existing["item_name"],))
        if old_item:
            old_inv_qty = max(0, int(old_item["quantity"] or 0) - old_qty)
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (old_inv_qty, old_item["id"])
            )

        # Apply new inventory
        new_item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (item_name,))
        if new_item:
            new_inv_qty = int(new_item["quantity"] or 0) + new_qty
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (new_inv_qty, new_item["id"])
            )

        _mysql_execute(
            """UPDATE stock_in SET item_name=%s, quantity=%s, unit_cost=%s, source=%s,
               receipt_no=%s, payment_method=%s, paid_by=%s, staff_name=%s WHERE id=%s""",
            (item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name, entry_id)
        )

        # NOTE: cash_movements NOT updated here to avoid double-counting with consolidated entry.
        # Consolidated entry (KBZ Bank: primary account) covers all old purchases.
        # New individual records create cash_movements in POST /stock-in endpoint.

        return {"success": True, "data": {"id": entry_id, "updated": item_name}}
    except Exception as e:
        logger.error(f"PUT /stock-in/{entry_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/stock-out")
async def dashboard_stock_out(req: dict, user: dict = Depends(get_current_user)):
    """Record stock-out (deduction) for an inventory item."""
    try:
        item_id = req.get("item_id")
        quantity = req.get("quantity", 0)
        unit_price = req.get("unit_price", 0)
        notes = req.get("notes", "")

        if not item_id or quantity <= 0:
            return {"success": False, "error": "item_id and positive quantity are required"}

        # Fetch item
        item = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not item:
            return {"success": False, "error": "Inventory item not found"}

        current_qty = int(item["quantity"] or 0)
        if quantity > current_qty:
            return {"success": False, "error": f"Not enough stock. Available: {current_qty}"}

        # Insert into stock_out
        total_amount = quantity * unit_price
        _mysql_execute(
            """INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, notes)
               VALUES (%s, %s, %s, %s, NOW(), %s)""",
            (item["item_name"], quantity, unit_price, total_amount, notes)
        )

        # Update inventory quantity
        new_qty = current_qty - quantity
        _mysql_execute(
            "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
            (new_qty, item_id)
        )

        return {
            "success": True,
            "data": {
                "item_name": item["item_name"],
                "quantity_deducted": quantity,
                "new_quantity": new_qty,
                "unit_price": unit_price,
                "total": total_amount,
            }
        }
    except Exception as e:
        logger.error(f"POST /stock-out error: {e}")
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════
#  MEMBERS — DELETE
# ═══════════════════════════════════════
@router.delete("/members/{member_id}")
async def dashboard_delete_member(member_id: str, user: dict = Depends(get_current_user)):
    """Delete a member."""
    try:
        existing = _mysql_query_one("SELECT * FROM member_wallets WHERE member_id = %s", (member_id,))
        if not existing:
            return {"success": False, "error": "Member not found"}

        _mysql_delete("DELETE FROM member_wallets WHERE member_id = %s", (member_id,))
        _mysql_delete("DELETE FROM topup_log WHERE member_id = %s", (member_id,))
        _mysql_delete("DELETE FROM members WHERE member_id = %s", (member_id,))
        return {"success": True, "data": {"deleted": member_id}}
    except Exception as e:
        logger.error(f"DELETE /members/{member_id} error: {e}")
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════
#  STOCK IN — List & Delete
# ═══════════════════════════════════════
@router.get("/stock-in")
async def dashboard_get_stock_in(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List stock-in records."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(item_name LIKE %s OR batch_id LIKE %s OR source LIKE %s OR staff_name LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])

        sql = f"""
            SELECT id, batch_id, item_name, quantity, unit_cost, source,
                   receipt_no, payment_method, paid_by, staff_name, created_at
            FROM stock_in
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM stock_in WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "batch_id": r.get("batch_id"),
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit_cost": float(r.get("unit_cost") or 0),
                "source": r.get("source"),
                "receipt_no": r.get("receipt_no"),
                "payment_method": r.get("payment_method"),
                "paid_by": r.get("paid_by"),
                "staff_name": r.get("staff_name"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /stock-in error: {e}")
        return {"success": False, "error": str(e)}

@router.delete("/stock-in/{entry_id}")
async def dashboard_delete_stock_in(entry_id: int, user: dict = Depends(get_current_user)):
    """Delete a stock-in record and reverse inventory quantity."""
    try:
        existing = _mysql_query_one("SELECT * FROM stock_in WHERE id = %s", (entry_id,))
        if not existing:
            return {"success": False, "error": "Stock-in record not found"}

        # Reverse inventory: find item by name and subtract quantity
        item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (existing["item_name"],))
        if item:
            new_qty = max(0, int(item["quantity"] or 0) - int(existing["quantity"] or 0))
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (new_qty, item["id"])
            )

        _mysql_delete("DELETE FROM stock_in WHERE id = %s", (entry_id,))
        return {"success": True, "data": {"deleted": entry_id}}
    except Exception as e:
        logger.error(f"DELETE /stock-in/{entry_id} error: {e}")
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════
#  STOCK OUT — List & Delete
# ═══════════════════════════════════════
@router.get("/stock-out")
async def dashboard_get_stock_out(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List stock-out records."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(item_name LIKE %s OR staff_name LIKE %s OR notes LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like])

        sql = f"""
            SELECT id, item_name, quantity, unit_price, total, sale_date,
                   staff_name, notes, created_at
            FROM stock_out
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM stock_out WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit_price": float(r.get("unit_price") or 0),
                "total": float(r.get("total") or 0),
                "sale_date": str(r["sale_date"]) if r.get("sale_date") else None,
                "staff_name": r.get("staff_name"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /stock-out error: {e}")
        return {"success": False, "error": str(e)}

@router.delete("/stock-out/{entry_id}")
async def dashboard_delete_stock_out(entry_id: int, user: dict = Depends(get_current_user)):
    """Delete a stock-out record and restore inventory quantity."""
    try:
        existing = _mysql_query_one("SELECT * FROM stock_out WHERE id = %s", (entry_id,))
        if not existing:
            return {"success": False, "error": "Stock-out record not found"}

        # Restore inventory: find item by name and add quantity back
        item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (existing["item_name"],))
        if item:
            new_qty = int(item["quantity"] or 0) + int(existing["quantity"] or 0)
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (new_qty, item["id"])
            )

        _mysql_delete("DELETE FROM stock_out WHERE id = %s", (entry_id,))
        return {"success": True, "data": {"deleted": entry_id}}
    except Exception as e:
        logger.error(f"DELETE /stock-out/{entry_id} error: {e}")
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════
#  SALES DAILY — List
# ═══════════════════════════════════════
@router.get("/sales-daily")
async def dashboard_get_sales_daily(
    date: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List sales daily records."""
    try:
        where = ["1=1"]
        params = []
        if date:
            where.append("sale_date = %s")
            params.append(date)
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
            f"SELECT COALESCE(SUM(amount), 0) as total_amount, COALESCE(SUM(gross), 0) as total_gross, COALESCE(SUM(discount), 0) as total_discount, COALESCE(SUM(net), 0) as total_net FROM sales_daily WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "voucher_no": r.get("voucher_no"),
                "sale_date": str(r["sale_date"]) if r.get("sale_date") else None,
                "console_id": r.get("console_id"),
                "member_id": r.get("member_id"),
                "amount": float(r.get("amount") or 0),
                "gross": float(r.get("gross") or 0),
                "discount": float(r.get("discount") or 0),
                "net": float(r.get("net") or 0),
                "staff_name": r.get("staff_name"),
                "payment_method": r.get("payment_method"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {
            "success": True,
            "data": entries,
            "total": total,
            "summary": {
                "total_amount": float(summary["total_amount"] or 0),
                "total_gross": float(summary["total_gross"] or 0),
                "total_discount": float(summary["total_discount"] or 0),
                "total_net": float(summary["total_net"] or 0),
            } if summary else None
        }
    except Exception as e:
        logger.error(f"GET /sales-daily error: {e}")
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════
#  FINANCIAL REPORT
# ═══════════════════════════════════════
@router.get("/financial-report")
async def dashboard_financial_report(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get financial summary with pagination and SQL aggregation."""
    try:
        # Use SQL aggregation for totals
        assets_tot = _mysql_query_one("SELECT COALESCE(SUM(amount), 0) as total, COUNT(*) as cnt FROM finance_assets WHERE status = 'active'")
        assets_total = float(assets_tot["total"] or 0)
        assets_count = int(assets_tot["cnt"] or 0)
        assets = _mysql_query(
            "SELECT id, name, purchase_date, per_price, qty, disposed_qty, amount, payment_method, salvage_value, useful_life, monthly_dep, months_elapsed, acc_depreciation, book_value, notes, status FROM finance_assets ORDER BY purchase_date DESC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        payables_tot = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total, COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END), 0) as pending FROM finance_payables"
        )
        payables_total = float(payables_tot["total"] or 0)
        payables_pending = float(payables_tot["pending"] or 0)
        payables = _mysql_query(
            "SELECT id, payee, amount, due_date, status FROM finance_payables ORDER BY due_date ASC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        receivables_tot = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total, COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END), 0) as pending FROM finance_receivables"
        )
        receivables_total = float(receivables_tot["total"] or 0)
        receivables_pending = float(receivables_tot["pending"] or 0)
        receivables = _mysql_query(
            "SELECT id, payer, amount, due_date, status FROM finance_receivables ORDER BY due_date ASC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        advances_tot = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total, COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END), 0) as pending FROM finance_advances"
        )
        advances_total = float(advances_tot["pending"] or 0)
        advances_pending = float(advances_tot["pending"] or 0)
        advances = _mysql_query(
            "SELECT id, member_id, amount, advance_date, settle_date, status, notes FROM finance_advances ORDER BY advance_date DESC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        prepaid_tot = _mysql_query_one("SELECT COALESCE(SUM(amount), 0) as total FROM finance_prepaid")
        prepaid_total = float(prepaid_tot["total"] or 0)
        prepaid = _mysql_query(
            "SELECT id, description, amount, settle_date, status FROM finance_prepaid ORDER BY settle_date ASC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        opex_rows = _mysql_query("SELECT COALESCE(SUM(amount), 0) as total FROM opex WHERE expense_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)")
        opex_30d = float(opex_rows[0]["total"] or 0) if opex_rows else 0

        # Get disposal records
        disps = _mysql_query("SELECT ad.id, ad.asset_id, fa.name as asset_name, ad.disposed_qty, ad.disposal_amount, ad.disposal_date, ad.book_value_at_disposal, ad.profit_loss FROM asset_disposals ad LEFT JOIN finance_assets fa ON ad.asset_id = fa.id ORDER BY ad.created_at DESC LIMIT 50")
        disposal_records = []
        for d in (disps or []):
            disposal_records.append({
                "id": d["id"],
                "asset_id": d["asset_id"],
                "asset_name": d.get("asset_name") or "Unknown",
                "disposed_qty": int(d.get("disposed_qty") or 1),
                "disposal_amount": float(d.get("disposal_amount") or 0),
                "disposal_date": str(d.get("disposal_date") or ""),
                "book_value_at_disposal": float(d.get("book_value_at_disposal") or 0),
                "profit_loss": float(d.get("profit_loss") or 0)
            })

        # --- Auto Depreciation Calculation ---
        import datetime as _dt_module
        _today_dep = _dt_module.datetime(2026, 6, 1)  # Depreciation base: June 1, 2026
        _assets_result = []
        for _a in assets:
            _ul = int(_a.get("useful_life", 0) or 0)
            _sv = float(_a.get("salvage_value", 0) or 0)
            _amt = float(_a.get("amount", 0) or 0)
            if _ul > 0 and _amt > 0:
                _useful_m = _ul * 12
                _monthly = max(0, (_amt - _sv) / _useful_m) if _useful_m > 0 else 0
                _pd = _a.get("purchase_date")
                if _pd:
                    try:
                        if hasattr(_pd, "strftime"):
                            _me = (_today_dep.year - _pd.year) * 12 + (_today_dep.month - _pd.month)
                        else:
                            _pd2 = _dt_module.datetime.strptime(str(_pd)[:10], "%Y-%m-%d")
                            _me = (_today_dep.year - _pd2.year) * 12 + (_today_dep.month - _pd2.month)
                    except:
                        _me = int(_a.get("months_elapsed", 0) or 0)
                else:
                    _me = int(_a.get("months_elapsed", 0) or 0)
                if _me < 0: _me = 0
                _acc_d = min(_monthly * _me, _amt - _sv)
                _bv = _amt - _acc_d
                _a["monthly_dep"] = round(_monthly, 0)
                _a["months_elapsed"] = _me
                _a["acc_depreciation"] = round(_acc_d, 0)
                _a["book_value"] = round(_bv, 0)
            _assets_result.append({
                "id": _a["id"], "name": _a.get("name"),
                "purchase_date": str(_a["purchase_date"]) if _a.get("purchase_date") else None,
                "per_price": float(_a.get("per_price", 0) or 0),
                "qty": int(_a.get("qty", 1) or 1),
                "disposed_qty": int(_a.get("disposed_qty", 0) or 0),
                "amount": float(_a["amount"] or 0),
                "payment_method": _a.get("payment_method") or "",
                "salvage_value": float(_a.get("salvage_value", 0) or 0),
                "useful_life": int(_a.get("useful_life", 0) or 0),
                "monthly_dep": float(_a.get("monthly_dep", 0) or 0),
                "months_elapsed": int(_a.get("months_elapsed", 0) or 0),
                "acc_depreciation": float(_a.get("acc_depreciation", 0) or 0),
                "book_value": float(_a.get("book_value", 0) or 0) or (float(_a.get("amount", 0) or 0) - float(_a.get("acc_depreciation", 0) or 0)),
                "status": _a.get("status") or "active",
                "disposal_amount": float(_a.get("disposal_amount", 0) or 0) if _a.get("status") == "disposed" else None,
                "disposal_date": str(_a["disposal_date"]) if _a.get("disposal_date") else None,
                "profit_loss": float(_a.get("profit_loss", 0) or 0) if _a.get("status") == "disposed" else None,
                "notes": _a.get("notes"),
            })
        
        return {
            "success": True,
            "data": {
                "assets": _assets_result,
                "assets_total": assets_total, "assets_count": assets_count,
                "advances_total": advances_total, "advances_pending": advances_pending,
                "prepaid_total": prepaid_total,
                "net_position": assets_total - advances_pending - prepaid_total,
                "payables": [{
                    "id": p["id"], "payee": p.get("payee"),
                    "amount": float(p["amount"] or 0),
                    "due_date": str(p["due_date"]) if p.get("due_date") else None,
                    "status": p.get("status"),
                } for p in payables],
                "payables_total": payables_total, "payables_pending": payables_pending,
                "receivables": [{
                    "id": r["id"], "payer": r.get("payer"),
                    "amount": float(r["amount"] or 0),
                    "due_date": str(r["due_date"]) if r.get("due_date") else None,
                    "status": r.get("status"),
                } for r in receivables],
                "receivables_total": receivables_total, "receivables_pending": receivables_pending,
                "advances": [{
                    "id": a["id"], "member_id": a.get("member_id"),
                    "amount": float(a["amount"] or 0),
                    "advance_date": str(a["advance_date"]) if a.get("advance_date") else None,
                    "settle_date": str(a["settle_date"]) if a.get("settle_date") else None,
                    "status": a.get("status"), "notes": a.get("notes"),
                } for a in advances],
                "advances_total": advances_total, "advances_pending": advances_pending,
                "prepaid": [{
                    "id": p["id"], "description": p.get("description"),
                    "amount": float(p["amount"] or 0),
                    "settle_date": str(p["settle_date"]) if p.get("settle_date") else None,
                    "status": p.get("status"),
                } for p in prepaid],
                "prepaid_total": prepaid_total,
                "opex_30d": opex_30d,
                "disposal_records": disposal_records,
            }
        }
    except Exception as e:
        logger.error(f"GET /financial-report error: {e}")
        return {"success": False, "error": str(e)}


# ── Asset CRUD ──
@router.post("/assets/create")
async def dashboard_create_asset(body: dict = {}, user: dict = Depends(get_current_user)):
    """Create a new fixed asset record with depreciation fields."""
    from mysql_db import execute as _e
    try:
        name = (body.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "Name is required"}
        pd_ = body.get("purchase_date") or None
        pp = float(body.get("per_price", 0) or 0)
        qt = int(body.get("qty", 1) or 1)
        amt = pp * qt
        pm = body.get("payment_method", "")
        sv = float(body.get("salvage_value", 0) or 0)
        ul = int(body.get("useful_life", 0) or 0)
        nt = body.get("notes", "")
        import datetime as _d
        if ul > 0 and amt > sv:
            um = ul * 12
            md = max(0, (amt - sv) / um)
            me = 0
            if pd_:
                try: me = max(0, (_d.date.today().year - _d.date.fromisoformat(pd_).year) * 12 + (_d.date.today().month - _d.date.fromisoformat(pd_).month))
                except Exception: me = 0
            ad = min(md * me, amt - sv)
            bv = amt - ad
        else:
            md, me, ad, bv = 0, 0, 0, amt
        aid = _e("INSERT INTO finance_assets (name,purchase_date,per_price,qty,amount,payment_method,salvage_value,useful_life,monthly_dep,months_elapsed,acc_depreciation,book_value,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')",
            (name, pd_, pp, qt, amt, pm, sv, ul, round(md, 0), me, round(ad, 0), round(bv, 0), nt))
        return {"success": True, "message": "Asset created", "data": {"id": aid, "name": name, "amount": round(amt, 0), "book_value": round(bv, 0)}}
    except Exception as e:
        logger.error(f"POST /assets/create error: {e}")
        return {"success": False, "error": str(e)}
@router.put("/assets/{asset_id}/dispose")

async def dashboard_dispose_asset(asset_id: int, req: dict = {}, user: dict = Depends(get_current_user)):
    """Dispose an asset (partial or full) with profit/loss calculation."""
    from mysql_db import execute as _mysql_execute2, query_one as _mysql_query_one2
    try:
        asset = _mysql_query_one2(
            "SELECT id, name, per_price, qty, disposed_qty, amount, acc_depreciation, book_value, salvage_value, status FROM finance_assets WHERE id = %s",
            (asset_id,)
        )
        if not asset:
            return {"success": False, "error": "Asset not found"}
        if asset["status"] == "disposed":
            return {"success": False, "error": "Asset already disposed"}
        current_qty = int(asset["qty"] or 0)
        disposed_already = int(asset["disposed_qty"] or 0)
        remaining_qty = current_qty
        if remaining_qty <= 0:
            return {"success": False, "error": "No quantity left to dispose"}
        dispose_qty = int(req.get("qty", 0) or 0)
        if dispose_qty <= 0 or dispose_qty > remaining_qty:
            dispose_qty = remaining_qty
        sale_amount = float(req.get("sale_amount", 0) or 0)
        total_original_qty = current_qty + disposed_already
        total_amount = float(asset["amount"] or 0)
        acc_dep = float(asset["acc_depreciation"] or 0)
        total_book = total_amount - acc_dep
        if total_original_qty > 0:
            prop_book = total_book * (dispose_qty / total_original_qty)
        else:
            prop_book = 0
        profit_loss = sale_amount - prop_book
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        _mysql_execute2(
            "INSERT INTO asset_disposals (asset_id, disposed_qty, disposal_amount, disposal_date, book_value_at_disposal, profit_loss, note) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (asset_id, dispose_qty, sale_amount, today, prop_book, profit_loss, "Disposed " + str(dispose_qty) + " of " + str(total_original_qty) + " units")
        )
        is_full = (dispose_qty >= remaining_qty)
        if is_full:
            _mysql_execute2("UPDATE finance_assets SET status = 'disposed', disposal_amount = %s, disposal_date = %s, profit_loss = %s, disposed_qty = disposed_qty + %s WHERE id = %s", (sale_amount, today, profit_loss, dispose_qty, asset_id))
        else:
            new_qty = remaining_qty - dispose_qty
            new_disposed_qty = disposed_already + dispose_qty
            _mysql_execute2("UPDATE finance_assets SET qty = %s, disposed_qty = %s WHERE id = %s", (new_qty, new_disposed_qty, asset_id))
        if sale_amount > 0:
            return_acct = req.get("return_account") or "Cash"
            label = ("Asset disposal: " + (asset["name"] or "Unknown") + " (x" + str(dispose_qty) + ")")
            _mysql_execute2("INSERT INTO cash_movements (movement_type, account, amount, note, created_at) VALUES (%s, %s, %s, %s, NOW())", ("inject", return_acct, sale_amount, label))
        return {"success": True, "message": "Asset disposed" if is_full else "Partial disposal recorded", "data": {"sale_amount": sale_amount, "book_value": round(prop_book, 0), "profit_loss": round(profit_loss, 0), "type": "profit" if profit_loss >= 0 else "loss", "disposed_qty": dispose_qty, "remaining_qty": 0 if is_full else new_qty, "is_partial": not is_full}}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.get("/finance/balances")
async def get_finance_balances(user: dict = Depends(get_current_user)):
    """Returns live balance for each payment account."""
    accounts = [{"key": "cash", "label": "Cash", "icon": "💰", "type": "operating"},{"key": "wave", "label": "WavePay", "icon": "📱", "type": "digital"},{"key": "aya_pay", "label": "AYA Pay", "icon": "💳", "type": "bank"},{"key": "kpay", "label": "KPay", "icon": "💳", "type": "digital"},{"key": "kbz_bank", "label": "KBZ Bank", "icon": "🏦", "type": "capital"},{"key": "acm_acc", "label": "ACM's Acc", "icon": "🏦", "type": "transfer"}]
    try:
        from mysql_db import query as _mq, query_one as _mqo, execute as _me
        import re
        import pymysql
        from fifo_wallet import get_all_fifo
        try:
            _fifo_conn = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
            _fifo_result = get_all_fifo(_fifo_conn)
            _fifo_conn.close()
        except Exception:
            _fifo_result = {"liability": 0, "consumed": 0}
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != ''")
        income_by_account = {acc["key"]: 0.0 for acc in accounts}
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            net_amount = float(row.get("net") or 0)
            if not pm: continue
            parts = pm.split("|")
            for part in parts:
                part = part.strip()
                if ":" in part:
                    method, _, val = part.partition(":")
                    method = method.strip().lower().replace(" ", "_")
                    try:
                        val = float(val.strip() or 0) if val.strip() else 0
                    except ValueError:
                        val = net_amount / len(parts) if parts else 0
                else:
                    method = part.lower().replace(" ", "_")
                    val = net_amount / len(parts) if parts else 0
                # Normalize payment methods to account keys
                if method == "wavepay":
                    method = "wave"
                if method in income_by_account:
                    income_by_account[method] += val
        # Topup income
        topup_rows = _mq("SELECT payment_method, amount FROM topup_log WHERE amount > 0 AND payment_method IS NOT NULL")
        for _tr in topup_rows:
            _pm = (_tr.get("payment_method") or "").strip()
            if not _pm: continue
            for _part in _pm.split("/"):
                _part = _part.strip()
                if ":" in _part:
                    _method, _, _val = _part.partition(":")
                    _method = _method.strip().lower().replace(" ", "_")
                    try:
                        _val = float(_val.strip() or 0) if _val.strip() else 0
                    except ValueError:
                        continue
                    if _method == "wavepay": _method = "wave"
                    if _method in income_by_account:
                        income_by_account[_method] += _val

        opex_rows = _mq("SELECT payment_method, COALESCE(SUM(amount), 0) as total FROM opex WHERE description NOT LIKE %s GROUP BY payment_method", ("%Prepaid Rent Amortization%",))
        opex_by_acct = {acc["key"]: 0.0 for acc in accounts}
        for row in opex_rows:
            pm = (row.get("payment_method") or "").strip().lower().replace(" ", "_")
            if pm in opex_by_acct:
                opex_by_acct[pm] += float(row["total"] or 0)
        # Stock-in purchase payments (deducted from accounts)
        # Note: use CONCAT to avoid LIKE '%/%' conflicting with PyMySQL %-formatting
        si_all_rows = _mq("SELECT payment_method, COALESCE(SUM(quantity * unit_cost), 0) as total FROM stock_in WHERE payment_method IS NOT NULL AND payment_method != '' GROUP BY payment_method")
        si_payments = {"Cash": 0.0, "KPay": 0.0, "Wave": 0.0, "AYA Pay": 0.0, "KBZ Bank": 0.0, "ACM's Acc": 0.0}
        for row in si_all_rows:
            pm = (row.get("payment_method") or "").strip()
            amt = float(row["total"] or 0)
            if pm in si_payments:
                si_payments[pm] = si_payments.get(pm, 0) + amt
            elif "/" in pm:
                # Composite payment: "Cash 5000 / KPay 3000" — parse and split
                for part in pm.split("/"):
                    part = part.strip()
                    for method in si_payments:
                        if part.startswith(method):
                            try:
                                amt_part = float(part[len(method):].strip().replace(",", ""))
                                si_payments[method] = si_payments.get(method, 0) + amt_part
                            except (ValueError, IndexError):
                                pass
                            break

        cash_rows = _mq("SELECT movement_type, account, COALESCE(SUM(amount), 0) as total FROM cash_movements GROUP BY movement_type, account")
        cash_map = {}
        for row in cash_rows:
            key = f"{row['movement_type']}|{row['account']}"
            cash_map[key] = cash_map.get(key, 0) + float(row["total"] or 0)
        # Map account keys to DB account names for cash_movements lookup
        # Subtract inject entries that overlap with topup (notes start with Topup or New member)
        _bad_inject_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'inject' AND (note IS NOT NULL AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37)))) GROUP BY account")
        for _r in _bad_inject_rows:
            _k = "inject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        key_to_name = {"cash": "Cash", "wave": "Wave", "kpay": "KPay", "aya_pay": "AYA Pay", "kbz_bank": "KBZ Bank", "acm_acc": "ACM's Acc"}
        # Capital expenditure queries (all from KBZ Bank)
        _asset_purchases = _mqo("SELECT COALESCE(SUM(per_price * qty), 0) as total FROM finance_assets WHERE status = 'active'")
        _advances_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_advances")
        _prepaid_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_prepaid")
        _disposal_proceeds = _mqo("SELECT COALESCE(SUM(disposal_amount), 0) as total FROM finance_assets WHERE status = 'disposed' AND disposal_amount > 0")
        result = []
        for acc in accounts:
            key = acc["key"]
            db_name = key_to_name.get(key, key.capitalize())
            income = income_by_account.get(key, 0)
            opex = opex_by_acct.get(key, 0)
            trans_in = cash_map.get(f"transfer_in|{db_name}", 0)
            trans_out = cash_map.get(f"transfer_out|{db_name}", 0)
            inject = cash_map.get(f"inject|{db_name}", 0)
            eject = cash_map.get(f"eject|{db_name}", 0)
            si_pay = si_payments.get(db_name, 0)
            # Formula: income - opex + transfers - stock_in_payments + inject - eject
            # Note: stock_in purchases also create cash_movements eject entries,
            # so stock_in_payments may partially overlap with eject for non-split entries.
            # stock_in_payments tracked for info only; cash_movements eject entries already account for stock-in deductions
            balance = income - opex + trans_in - abs(trans_out) + inject - eject
            # Capital expenditure adjustments (all from KBZ Bank)
            ded_assets = 0
            ded_advances = 0
            ded_prepaid = 0
            add_proceeds = 0
            if key == "kbz_bank":
                ded_assets = float(_asset_purchases.get("total", 0) or 0)
                ded_advances = float(_advances_total.get("total", 0) or 0)
                ded_prepaid = float(_prepaid_total.get("total", 0) or 0)
                add_proceeds = float(_disposal_proceeds.get("total", 0) or 0)
                balance = balance - ded_assets - ded_advances - ded_prepaid + add_proceeds
            result.append({"key": key, "label": acc["label"], "icon": acc["icon"], "type": acc["type"], "income": round(income, 0), "opex": round(opex, 0), "transfers_in": round(trans_in, 0), "transfers_out": round(abs(trans_out), 0), "injections": round(inject, 0), "ejections": round(eject, 0), "stock_in_payments": round(si_pay, 0), "capital_asset_purchases": round(ded_assets, 0), "capital_advances": round(ded_advances, 0), "capital_prepaid": round(ded_prepaid, 0), "capital_disposal_proceeds": round(add_proceeds, 0), "balance": round(balance, 0)})
        total_balance = sum(a["balance"] for a in result) or 0
        total_income = sum(a["income"] for a in result) or 0
        total_expense = sum(a["opex"] for a in result) or 0
        # Separate ACM store accounts (Cash, KPay, Wave, AYA Pay)
        store_accounts = [a for a in result if a["key"] in ("cash", "kpay", "wave", "aya_pay")]
        acm_accounts = [a for a in result if a["key"] == "acm_acc"]
        capital_accounts = [a for a in result if a["key"] == "kbz_bank"]
        store_total = sum(a["balance"] for a in store_accounts) or 0
        acm_total = sum(a["balance"] for a in acm_accounts) or 0
        capital_total = sum(a["balance"] for a in capital_accounts) or 0
        
        # Revenue breakdown — food has NO discount, ALL discount on game
        # Game revenue = sales_daily net (food with no discount) + wallet consumption (FIFO)
        _game = float(_mqo("SELECT COALESCE(ROUND(SUM(GREATEST(net - (gross - amount), 0))),0) as t FROM sales_daily").get("t",0) or 0) + _fifo_result["consumed"]
        _food = _mqo("SELECT COALESCE(ROUND(SUM(LEAST(gross - amount, net))),0) as t FROM sales_daily")
        _topup = _mqo("SELECT COALESCE(ROUND(SUM(amount)),0) as t FROM topup_log")
        # Member liability = FIFO: oldest topups consumed first
        _liability = _fifo_result["liability"]
        _discount = _mqo("SELECT COALESCE(ROUND(SUM(gross - net)),0) as t FROM sales_daily")
        game_revenue = float(_game or 0)
        food_revenue = float(_food.get("t", 0) or 0)
        topup_revenue = float(_topup.get("t", 0) or 0)
        discount_total = float(_discount.get("t", 0) or 0)
        member_liability = float(_fifo_result["liability"] or 0)
        
        return {"success": True, "accounts": result, "totals": {
            "total_balance": round(total_balance, 0),
            "total_income": round(total_income, 0),
            "total_expense": round(total_expense, 0),
            "store_total": round(store_total, 0),
            "acm_total": round(acm_total, 0),
            "capital_total": round(capital_total, 0),
            "game_revenue": round(game_revenue, 0),
            "food_revenue": round(food_revenue, 0),
            "topup_revenue": round(topup_revenue, 0),
            "discount_total": round(max(0, discount_total), 0),
            "member_liability": round(member_liability, 0),
            "advances_total": round(_advances_total.get("total", 0) or 0, 0),
            "prepaid_total": round(_prepaid_total.get("total", 0) or 0, 0),
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}
@router.get("/opex")
async def dashboard_opex_list(
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    user: dict = Depends(get_current_user),
):
    from mysql_db import query as _mq
    sql = "SELECT * FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    if search:
        sql += " AND (category LIKE %s OR description LIKE %s)"
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    sql += " ORDER BY expense_date DESC, created_at DESC LIMIT 200"
    rows = _mq(sql, tuple(params)) if params else _mq(sql)
    return {"success": True, "data": list(rows)}


@router.post("/opex")
async def dashboard_opex_add(body: dict = {}, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    from datetime import datetime, timedelta
    _mmt = timedelta(hours=6, minutes=30)
    _today = (datetime.utcnow() + _mmt).strftime("%Y-%m-%d")
    cat = body.get("category", "").strip()
    desc = body.get("description", "").strip()
    amt = int(body.get("amount", 0))
    pmt = body.get("payment_method", "Cash").strip()
    dt = body.get("expense_date", _today).strip()
    if not cat or amt <= 0:
        return {"success": False, "error": "Category and amount required"}
    _me(
        "INSERT INTO opex (category,description,amount,payment_method,recorded_by,expense_date) VALUES (%s,%s,%s,%s,%s,%s)",
        (cat, desc, amt, pmt, user.get("username", "admin"), dt),
    )
    return {"success": True, "data": {"msg": f"{cat}: {amt:,} Ks recorded"}}


@router.get("/opex/summary")
async def dashboard_opex_summary(
    date_from: str = "",
    date_to: str = "",
    user: dict = Depends(get_current_user),
):
    from mysql_db import query as _mq
    sql = "SELECT category, SUM(amount) as total, COUNT(*) as count FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    sql += " GROUP BY category ORDER BY total DESC"
    rows = _mq(sql, tuple(params)) if params else _mq(sql)
    grand_total = sum(r["total"] for r in rows) if rows else 0
    return {"success": True, "data": {"categories": list(rows), "grand_total": grand_total}}


@router.delete("/opex/{item_id}")
async def dashboard_opex_delete(item_id: int, user: dict = Depends(get_current_user)):
    from mysql_db import query_one as _mqo
    from mysql_db import execute as _me
    item = _mqo("SELECT id, category, recorded_by FROM opex WHERE id = %s", (item_id,))
    if not item:
        return {"success": False, "error": "Expense not found"}
    _me("DELETE FROM opex WHERE id = %s", (item_id,))
    return {"success": True, "data": {"msg": f"{item['category']}: deleted"}}


# ── Financial Statement: Monthly P&L ──
@router.get("/financial/pnl")
async def get_monthly_pnl(year: int = 2026, month: int = 6, user: dict = Depends(get_current_user)):
    """Monthly Profit & Loss statement."""
    from mysql_db import query as _mq, query_one as _mqo
    ym = f"{year:04d}-{month:02d}"
    try:
        rev_rows = _mq("SELECT net, gross, amount, notes FROM sales_daily WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0", (ym,))
        game_rev = 0.0; food_rev = 0.0; discounts = 0.0; topup_sales = 0.0
        for r in rev_rows:
            g = float(r.get("gross") or 0)
            n = float(r.get("net") or 0)
            a = float(r.get("amount") or 0)
            notes = (r.get("notes") or "")
            discounts += (g - n)
            # Food has NO discount: food_rev = min(gross - amount, net)
            food_amt = max(g - a, 0)
            food_rev += min(food_amt, n)
            # Exclude topup entries from game_rev (topups are deferred revenue)
            # Only wallet_consumed (FIFO) counts as topup-derived revenue
            if notes.startswith("Topup") or notes.startswith("New member"):
                topup_sales += max(n - food_amt, 0)
            else:
                # Game gets the remaining after food: game_rev = max(net - food_amt, 0)
                game_rev += max(n - food_amt, 0)
        trows = _mq("SELECT COALESCE(SUM(amount),0) as t FROM topup_log WHERE DATE_FORMAT(topup_date, '%%Y-%%m') = %s", (ym,))
        topup_rev = float(trows[0]["t"] or 0) if trows else 0
        import stock_fifo, pymysql
        _sfc = pymysql.connect(host='127.0.0.1', user='root', password='PsVibe@MySQL2024!', database='psvibe_api')
        _sfr = stock_fifo.calc_fifo(_sfc)
        _sfc.close()
        cogs = _sfr['cogs']
        opex_rows = _mq("SELECT category, COALESCE(SUM(amount),0) as total FROM opex WHERE DATE_FORMAT(expense_date, '%%Y-%%m') = %s GROUP BY category ORDER BY total DESC", (ym,))
        expenses_by_cat = [{"category": r["category"], "amount": float(r["total"])} for r in opex_rows]
        total_expense = sum(e["amount"] for e in expenses_by_cat)
        from fifo_wallet import get_all_fifo
        import pymysql
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
            _fr = get_all_fifo(_fc); _fc.close()
        except Exception:
            _fr = {"consumed": 0}
        wallet_consumed = float(_fr.get("consumed", 0))
        total_revenue = game_rev + food_rev + wallet_consumed
        gross_profit = total_revenue - cogs
        # Depreciation expense for this month
        _dep_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0")
        depreciation_exp = float(_dep_rows[0]["t"] or 0) if _dep_rows else 0
        total_expense_all = total_expense + depreciation_exp
        net_profit = gross_profit - total_expense_all
        return {"success": True, "data": {
            "period": ym,
            "revenue": {"game_revenue": round(game_rev,0), "food_revenue": round(food_rev,0), "topup_revenue": round(topup_rev,0), "wallet_consumed": round(wallet_consumed,0), "topup_deferred": round(topup_sales,0), "discounts": round(discounts,0), "total_revenue": round(total_revenue,0)},
            "cogs": round(cogs,0), "gross_profit": round(gross_profit,0),
            "expenses": {"by_category": expenses_by_cat, "total": round(total_expense,0), "depreciation": round(depreciation_exp,0), "total_with_depreciation": round(total_expense_all,0)},
            "operating_profit": round(gross_profit - total_expense,0),
            "net_profit": round(net_profit,0)
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Financial Statement: Balance Sheet ──

@router.put("/advance/{advance_id}/settle")
async def dashboard_settle_advance(advance_id: int, body: dict = {}, user: dict = Depends(get_current_user)):
    """Mark advance as settled — optional cash return to account"""
    from mysql_db import execute as _me, query_one as _mqo
    try:
        row = _mqo("SELECT id, member_id, amount, status FROM finance_advances WHERE id = %s", (advance_id,))
        if not row:
            return {"success": False, "error": "Advance not found"}
        if row.get("status") == "settled":
            return {"success": False, "error": "Already settled"}
        settle_date = body.get("settle_date", "2026-06-09")
        notes = body.get("notes", "")
        return_account = body.get("return_account", "")
        return_amount = float(body.get("return_amount", 0) or 0)
        member = row.get("member_id", "Unknown")
        _me("UPDATE finance_advances SET status='settled', settle_date=%s, notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
            (settle_date, " | Settled: " + notes if notes else "", advance_id))
        if return_account and return_amount > 0:
            _me("INSERT INTO cash_movements (movement_type, account, amount, note, staff_name) VALUES (%s,%s,%s,%s,%s)",
                ("inject", return_account, return_amount, f"Advance settled - {member} returned {return_amount:,.0f} Ks", user.get("username", "dashboard")))
        return {"success": True, "data": {"id": advance_id, "settle_date": settle_date, "cash_returned": return_amount if return_account else 0}}
    except Exception as e:
        return {"success": False, "error": str(e)}
@router.get("/financial/balance-sheet")
async def get_balance_sheet(user: dict = Depends(get_current_user)):
    """Balance Sheet: Assets = Liabilities + Equity. Uses same calc as finance/balances."""
    from mysql_db import query as _mq, query_one as _mqo
    import re, pymysql
    try:
        from fifo_wallet import get_all_fifo
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVib...4!", database="psvibe_api")
            _fr = get_all_fifo(_fc); _fc.close()
        except Exception:
            _fr = {"liability": 0}

        accounts = [
            {"key": "cash", "label": "Cash"},
            {"key": "wave", "label": "WavePay"},
            {"key": "kpay", "label": "KPay"},
            {"key": "aya_pay", "label": "AYA Pay"},
            {"key": "kbz_bank", "label": "KBZ Bank"},
            {"key": "acm_acc", "label": "ACM's Acc"}
        ]
        # Map key -> DB account name for cash_movements queries
        db_acct = {"cash":"Cash","wave":"WavePay","kpay":"KPay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
        bank_items = []
        total_ca = 0.0

        # --- Income from sales_daily (exact same logic as finance/balances) ---
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != ''")
        income_by_account = {a["key"]: 0.0 for a in accounts}
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            net_amount = float(row.get("net") or 0)
            if not pm: continue
            sep = "|" if "|" in pm else ("/" if "/" in pm else "|")
            for part in pm.split(sep):
                part = part.strip()
                if ":" in part:
                    method, _, val = part.partition(":")
                    method = method.strip().lower().replace(" ", "_")
                    val = float(val.strip() or 0) if val.strip() else 0
                else:
                    method = part.lower().replace(" ", "_")
                    val = net_amount
                if method == "wavepay": method = "wave"
                if method in income_by_account:
                    income_by_account[method] += val

        # 🆕 ADD topup_log income (member card purchases were MISSING!)
        trows = _mq("SELECT payment_method, amount FROM topup_log WHERE amount > 0 AND payment_method IS NOT NULL")
        for _tr in trows:
            _pm = (_tr.get("payment_method") or "").strip()
            _amt = float(_tr.get("amount") or 0)
            if not _pm or _amt <= 0:
                continue
            # Topup PM format: "KPay:90000/Cash:0" (pipe-delimited with :amount)
            for _part in _pm.split("/"):
                _part = _part.strip()
                if ":" in _part:
                    _method, _, _val = _part.partition(":")
                    _method = _method.strip().lower().replace(" ", "_")
                    _val = float(_val.strip() or 0) if _val.strip() else 0
                else:
                    _method = _part.lower().replace(" ", "_")
                    _val = _amt
                if _method == "wavepay":
                    _method = "wave"
                if _method in income_by_account:
                    income_by_account[_method] += _val

        for a in accounts:
            key = a["key"]
            income = income_by_account.get(key, 0.0)

            # OPEX — KBZ Bank pays all, others get 0
            opex = 0.0
            if key == "kbz_bank":
                opr = _mq("SELECT COALESCE(SUM(amount),0) as t FROM opex")
                opex = float(opr[0]["t"] or 0) if opr else 0
            else:
                opr = _mq("SELECT payment_method, amount FROM opex")
                for r in opr:
                    pm = (r.get("payment_method") or "").strip().lower().replace(" ", "_")
                    amt = float(r.get("amount") or 0)
                    if pm == key:
                        opex += amt
                    elif "/" in pm:
                        parts = pm.split("/")
                        for p in parts:
                            if p.strip().lower().replace(" ", "_") == key:
                                opex += amt / len(parts)

            # Cash movements — use DB account name (label), not key
            db = db_acct[key]
            t_in = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_in'", (db,))["t"] or 0)
            t_out = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_out'", (db,))["t"] or 0)
            inj = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='inject' AND (note IS NULL OR (note NOT LIKE CONCAT('Topup', CHAR(37)) AND note NOT LIKE CONCAT('New member', CHAR(37))))", (db,))["t"] or 0)
            ej = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='eject'", (db,))["t"] or 0)

            # t_out is stored as negative in DB, so + t_out works (= subtract)
            bal = income - opex + t_in + t_out + inj - ej

            if key == "kbz_bank":
                ded_assets = float(_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active'")["t"] or 0)
                ded_adv = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances")["t"] or 0)
                ded_prep = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid")["t"] or 0)
                bal = bal - ded_assets - ded_adv - ded_prep

            bank_items.append({"account": a["label"], "balance": round(bal,0)})
            total_ca += bal

        # Fixed Assets (Net Book Value)
        arows = _mq("SELECT name, amount, salvage_value, acc_depreciation FROM finance_assets WHERE status='active'")
        fix_items = []; total_fix = 0.0; total_gross = 0.0
        for a in arows:
            cost = float(a["amount"] or 0); dep = float(a.get("acc_depreciation") or 0); nbv = max(0, cost - dep)
            fix_items.append({"name":a["name"],"cost":round(cost,0),"sv":round(float(a.get("salvage_value") or 0),0),"acc_dep":round(dep,0),"nbv":round(nbv,0)})
            total_gross += cost
            total_fix += nbv

        total_prepaid = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE status='active'")["t"] or 0)
        total_amort = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM prepaid_amortization")["t"] or 0)
        prep_t = max(0, total_prepaid - total_amort)
        adv_t = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")["t"] or 0)
        other_ca = prep_t + adv_t

        # Depreciation (accumulated)
        _dep = _mq("SELECT COALESCE(SUM(acc_depreciation),0) as t FROM finance_assets WHERE status='active'")
        total_dep = float(_dep[0]["t"] or 0) if _dep else 0
        _md_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0")
        monthly_dep_total = float(_md_rows[0]["t"] or 0) if _md_rows else 0

        # Inventory Value via FIFO
        import stock_fifo, pymysql
        _sf2 = pymysql.connect(host='127.0.0.1', user='root', password='PsVibe@MySQL2024!', database='psvibe_api')
        _inv = stock_fifo.calc_fifo(_sf2)
        _sf2.close()
        inventory_value = _inv['inventory_value']

        total_assets = total_ca + total_fix + other_ca + inventory_value

        # Liabilities — direct FIFO wallet calculation
        _ml2 = _mq("""
            SELECT mw.member_id, mw.balance_mins,
                COALESCE((SELECT tl.amount FROM topup_log tl 
                 WHERE tl.member_id=mw.member_id AND tl.amount>0
                 ORDER BY tl.topup_date ASC LIMIT 1),0) as paid,
                COALESCE((SELECT tl.mins_added FROM topup_log tl 
                 WHERE tl.member_id=mw.member_id AND tl.amount>0
                 ORDER BY tl.topup_date ASC LIMIT 1),0) as mins
            FROM member_wallets mw
            WHERE mw.balance_mins > 0 AND mw.balance_mins IS NOT NULL
        """)
        _member_liab_val = 0.0
        _member_details = []
        for _r2 in _ml2:
            _paid2 = float(_r2.get('paid') or 0)
            _mins2 = float(_r2.get('mins') or 1)
            _bal2 = float(_r2.get('balance_mins') or 0)
            if _mins2 > 0 and _paid2 > 0 and _bal2 > 0:
                _member_liab_val += _paid2 / _mins2 * _bal2
                _member_details.append({
                    "member_id": _r2["member_id"],
                    "balance_mins": _bal2,
                    "paid": _paid2,
                    "mins_bought": _mins2,
                    "rate_per_min": round(_paid2 / _mins2, 0),
                    "liability": round(_paid2 / _mins2 * _bal2, 0)
                })
        member_liab = round(_member_liab_val, 0)

        # Equity: initial capital from KBZ + retained earnings
        _sh = _mq("SELECT SUM(capital_contribution) as tc FROM shareholders")
        icap = float(_sh[0]["tc"] or 0) if _sh else 0
        ti = float(_mqo("SELECT COALESCE(SUM(net),0) as t FROM sales_daily WHERE net>0")["t"] or 0)
        te = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM opex")["t"] or 0)
        cost_of_sold = _inv['cogs']  # FIFO-based COGS
        _excl_inj = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type = 'inject' AND (note IS NOT NULL AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37))))")["t"] or 0)
        retained = ti - te - cost_of_sold - member_liab - total_dep + _excl_inj  # depreciation
        total_eq = icap + retained

        total_liab = member_liab
        # Auto-balancing: small rounding adjustment to zero out data-entry discrepancies
        total_liab_eq = round(total_liab + total_eq, 0)
        total_assets = round(total_assets, 0)
        # Adjust retained earnings by the remaining diff (rounding/data-entry gap)
        _bs_diff = round(total_assets - total_liab_eq, 0)
        if _bs_diff != 0:
            retained += _bs_diff
            _excl_inj_local = locals().get('_excl_inj', 0)
            retained = round(retained, 0)
            total_eq = icap + retained
            total_liab_eq = round(total_liab + total_eq, 0)

        return {"success": True, "data": {
            "assets": {
                "current":{"items":bank_items,"total":round(total_ca,0)},
                "inventory":{"value":round(inventory_value,0)},
                "other_current":{"prepaid":round(prep_t,0),"advances":round(adv_t,0),"total":round(other_ca,0)},
                "fixed":{"items":fix_items,"gross_cost":round(total_gross,0),"acc_depreciation":round(total_dep,0),"monthly_dep_total":round(monthly_dep_total,0),"nbv":round(total_fix,0)},
                "total_assets":total_assets
            },
            "liabilities":{"member_liability":round(member_liab,0),"member_details":_member_details,"total":round(member_liab,0)},
            "equity":{"initial_capital":round(icap,0),"retained_earnings":round(retained,0),"depreciation_reserve":round(total_dep,0),"total":round(total_eq,0)},
            "total_liabilities_equity":total_liab_eq
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Financial Statement: Cash Flow (Redesigned) ──
@router.get("/financial/cashflow")
async def get_cashflow(year: int = 2026, month: int = 6, user: dict = Depends(get_current_user)):
    """Cash Flow Statement — closing balance matches Web Finance Balances."""
    from mysql_db import query as _mq, query_one as _mqo
    ym = f"{year:04d}-{month:02d}"
    try:
        # Opening: all KBZ capital deposits
        ki_all = _mq("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account='KBZ Bank' AND movement_type='transfer_in'", ())
        opener = float(ki_all[0]["t"]) if ki_all else 0

        # ─── Per-account calculation (same as Web Finance) ───
        acct_keys = ["cash","wave","aya_pay","kpay","kbz_bank","acm_acc"]
        key2name = {"cash":"Cash","wave":"Wave","aya_pay":"AYA Pay","kpay":"KPay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}

        # Income: sales_daily by payment method
        inc = {k: 0.0 for k in acct_keys}
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != ''")
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            na = float(row.get("net") or 0)
            if not pm: continue
            parts = pm.split("|")
            for p in parts:
                p = p.strip()
                m = p.lower().replace(" ", "_")
                if ":" in m:
                    m, _, v = m.partition(":")
                    try: val = float(v.strip() or 0)
                    except: val = na / max(len(parts), 1)
                else:
                    val = na / max(len(parts), 1)
                if m == "wavepay": m = "wave"
                if m in inc: inc[m] += val

        # Topup income
        tr = _mq("SELECT payment_method, amount FROM topup_log WHERE amount>0 AND payment_method IS NOT NULL")
        for _r in tr:
            pm = (_r.get("payment_method") or "").strip()
            if not pm: continue
            for p in pm.split("/"):
                p = p.strip()
                if ":" not in p: continue
                m, _, v = p.partition(":")
                m = m.strip().lower().replace(" ", "_")
                try: val = float(v.strip() or 0)
                except: continue
                if m == "wavepay": m = "wave"
                if m in inc: inc[m] += val

        inc_total = sum(inc.values())

        # OPEX by payment method
        opex = {k: 0.0 for k in acct_keys}
        ox = _mq("SELECT payment_method, COALESCE(SUM(amount),0) as t FROM opex WHERE description NOT LIKE %s GROUP BY payment_method", ("%Prepaid Rent Amortization%",))
        for _r in ox:
            m = (_r.get("payment_method") or "").strip().lower().replace(" ", "_")
            if m in opex: opex[m] += float(_r["t"] or 0)
        opex_total = sum(opex.values())

        # Cash movements
        cm = {}
        for _r in _mq("SELECT movement_type, account, COALESCE(SUM(amount),0) as t FROM cash_movements GROUP BY movement_type, account"):
            cm[f"{_r['movement_type']}|{_r['account']}"] = float(_r["t"] or 0)

        # Remove inject entries that duplicate topups
        bi = _mq("SELECT account, COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type='inject' AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37))) GROUP BY account")
        for _r in bi:
            k = "inject|" + _r["account"]
            if k in cm: cm[k] -= float(_r["t"] or 0)

        # Capital expenditure
        _ap = float((_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active'") or {}).get("t",0))
        _av = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances") or {}).get("t",0))
        _pp = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid") or {}).get("t",0))
        _dp = float((_mqo("SELECT COALESCE(SUM(disposal_amount),0) as t FROM finance_assets WHERE status='disposed' AND disposal_amount > 0") or {}).get("t",0))

        total_bal = 0.0
        for k in acct_keys:
            db = key2name[k]
            bal = inc[k] - opex[k] + cm.get(f"transfer_in|{db}",0) - abs(cm.get(f"transfer_out|{db}",0)) + cm.get(f"inject|{db}",0) - cm.get(f"eject|{db}",0)
            if k == "kbz_bank":
                bal = bal - _ap - _av - _pp + _dp
            total_bal += bal

        # Stock = ejections
        total_stock = sum(cm.get(f"eject|{db}",0) for db in key2name.values())

        # Advance recovery (KBZ inject with Advance settled note)
        _adv_rec = _mq("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type='inject' AND account='KBZ Bank' AND note LIKE 'Advance settled%%'", ())
        adv_rec = float(_adv_rec[0]["t"]) if _adv_rec else 0

        net_op = inc_total - opex_total - total_stock
        net_inv = adv_rec + _dp - _ap - _av - _pp

        op_items = [
            {"label": "Game & Top-up Income", "emoji": "🎮", "amount": round(inc_total, 0), "type": "inflow"},
            {"label": "Operating Expenses", "emoji": "📉", "amount": round(opex_total, 0), "type": "outflow"},
            {"label": "Stock Purchases", "emoji": "📦", "amount": round(total_stock, 0), "type": "outflow"},
        ]

        inv_items = []
        if _ap: inv_items.append({"label": "PS5 Consoles & Equipment", "emoji": "🎮", "amount": round(_ap, 0), "type": "outflow"})
        if _av: inv_items.append({"label": "Advances Paid", "emoji": "📋", "amount": round(_av, 0), "type": "outflow"})
        if _pp: inv_items.append({"label": "Prepaid Expenses", "emoji": "📅", "amount": round(_pp, 0), "type": "outflow"})
        if adv_rec: inv_items.append({"label": "Advance Recovered (Furniture)", "emoji": "♻️", "amount": round(adv_rec, 0), "type": "inflow"})
        if _dp: inv_items.append({"label": "Asset Sale Proceeds", "emoji": "♻️", "amount": round(_dp, 0), "type": "inflow"})
        if not inv_items:
            inv_items.append({"label": "No investing activity", "emoji": "➖", "amount": 0, "type": "inflow"})

        total_in = inc_total + adv_rec + _dp
        total_out = opex_total + total_stock + _ap + _av + _pp

        return {"success": True, "data": {
            "period": ym,
            "opening_balance": round(opener, 0),
            "sections": {
                "operating": {"title": "Daily Operations", "items": op_items, "subtotal": round(net_op, 0)},
                "investing": {"title": "Investing Activities", "items": inv_items, "subtotal": round(net_inv, 0)}
            },
            "summary": {
                "opening_balance": round(opener, 0),
                "total_inflows": round(total_in, 0),
                "total_outflows": round(total_out, 0),
                "net_change": round(net_op + net_inv, 0),
                "closing_balance": round(total_bal, 0)
            }
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}
@router.get('/shareholders')
async def get_shareholders(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq('SELECT * FROM shareholders ORDER BY capital_contribution DESC')
    return {'success': True, 'data': rows}

@router.post('/shareholders')
async def add_shareholder(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    name = req.get('name', '').strip()
    if not name: return {'success': False, 'error': 'Name required'}
    _me('INSERT INTO shareholders (name,role,capital_contribution,ownership_pct,notes) VALUES (%s,%s,%s,%s,%s)',
        (name, req.get('role', 'Shareholder'), float(req.get('capital_contribution',0) or 0),
         float(req.get('ownership_pct',0) or 0), req.get('notes', '')))
    return {'success': True, 'message': 'Shareholder ' + name + ' added'}

@router.put('/shareholders/{sid}')
async def update_shareholder(sid: int, req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    sets, vals = [], []
    for k in ('name','role','capital_contribution','ownership_pct','notes'):
        if k in req: sets.append(k + '=%s'); vals.append(req[k])
    if not sets: return {'success': False, 'error': 'No fields'}
    vals.append(sid)
    _me('UPDATE shareholders SET ' + ','.join(sets) + ' WHERE id=%s', vals)
    return {'success': True, 'message': 'Updated'}

@router.delete('/shareholders/{sid}')
async def delete_shareholder(sid: int, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    _me('DELETE FROM shareholders WHERE id=%s', (sid,))
    return {'success': True, 'message': 'Deleted'}



# ── Capital Movements ──
@router.get('/capital/movements')
async def get_capital_movements(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq("SELECT * FROM capital_movements ORDER BY created_at DESC")
    return {"success": True, "data": rows}

@router.post('/capital/inject')
async def capital_inject(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me, query as _mq
    sid = int(req.get("shareholder_id", 0))
    amount = float(req.get("amount", 0))
    pm = (req.get("payment_method", "") or "").strip()
    notes = (req.get("notes", "") or "").strip()
    if not sid or amount <= 0:
        return {"success": False, "error": "Valid shareholder_id and amount required"}
    sh = _mq("SELECT id, name, capital_contribution FROM shareholders WHERE id=%s", (sid,))
    if not sh:
        return {"success": False, "error": "Shareholder not found"}
    sh_name = sh[0]["name"]
    new_cap = float(sh[0]["capital_contribution"] or 0) + amount
    pm_map = {"kpay":"KPay","cash":"Cash","wavepay":"WavePay","wave":"WavePay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
    db_account = pm_map.get(pm.lower().replace(" ","_"), pm) if pm else "KPay"
    _me("INSERT INTO capital_movements (shareholder_id,shareholder_name,type,amount,payment_method,payment_account,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (sid, sh_name, "inject", amount, pm, db_account, notes))
    _me("UPDATE shareholders SET capital_contribution=%s WHERE id=%s", (new_cap, sid))
    full_note = "Capital inject: " + sh_name + (" (" + notes + ")" if notes else "")
    _me("INSERT INTO cash_movements (account,movement_type,amount,note,created_at) VALUES (%s,%s,%s,%s,NOW())",
        (db_account, "inject", amount, full_note))
    return {"success": True, "data": {"shareholder_id": sid,"shareholder_name": sh_name,"amount": amount,"payment_account": db_account,"new_capital_contribution": new_cap}}

@router.post('/capital/eject')
async def capital_eject(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me, query as _mq
    sid = int(req.get("shareholder_id", 0))
    amount = float(req.get("amount", 0))
    pm = (req.get("payment_method", "") or "").strip()
    notes = (req.get("notes", "") or "").strip()
    if not sid or amount <= 0:
        return {"success": False, "error": "Valid shareholder_id and amount required"}
    sh = _mq("SELECT id, name, capital_contribution FROM shareholders WHERE id=%s", (sid,))
    if not sh:
        return {"success": False, "error": "Shareholder not found"}
    sh_name = sh[0]["name"]
    current_cap = float(sh[0]["capital_contribution"] or 0)
    if amount > current_cap:
        return {"success": False, "error": "Eject amount exceeds capital contribution"}
    new_cap = current_cap - amount
    pm_map = {"kpay":"KPay","cash":"Cash","wavepay":"WavePay","wave":"WavePay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
    db_account = pm_map.get(pm.lower().replace(" ","_"), pm) if pm else "Cash"
    _me("INSERT INTO capital_movements (shareholder_id,shareholder_name,type,amount,payment_method,payment_account,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (sid, sh_name, "eject", amount, pm, db_account, notes))
    _me("UPDATE shareholders SET capital_contribution=%s WHERE id=%s", (new_cap, sid))
    full_note = "Capital eject: " + sh_name + (" (" + notes + ")" if notes else "")
    _me("INSERT INTO cash_movements (account,movement_type,amount,note,created_at) VALUES (%s,%s,%s,%s,NOW())",
        (db_account, "eject", amount, full_note))
    return {"success": True, "data": {"shareholder_id": sid,"shareholder_name": sh_name,"amount": amount,"payment_account": db_account,"new_capital_contribution": new_cap}}


# ── Profit Distribution ──
@router.get('/profit-distribution/calculate')
async def calculate_profit_distribution(user: dict = Depends(get_current_user)):
    """Calculate profit distribution: 10% mgmt fee -> proportional split"""
    from mysql_db import query as _mq
    import pymysql
    from datetime import datetime, date
    
    try:
        from fifo_wallet import get_all_fifo
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_PASSWORD", ""), database="psvibe_api")
            _fr = get_all_fifo(_fc); _fc.close()
        except Exception:
            _fr = {"liability": 0}
    except Exception:
        _fr = {"liability": 0}
    
    rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != ''")
    total_income = 0.0
    for row in rows:
        note = (row.get("notes") or "")
        if note.startswith("Topup") or note.startswith("New member"): continue
        net_amount = float(row.get("net") or 0)
        total_income += net_amount
    
    opex = _mq("SELECT COALESCE(SUM(amount),0) as t FROM opex")
    total_expense = float(opex[0]["t"] or 0) if opex else 0
    
    dep = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets")
    total_dep = float(dep[0]["t"] or 0) if dep else 0
    
    cogs_sum = 0.0
    cogs = _mq("SELECT unit_cost, quantity FROM stock_in WHERE unit_cost IS NOT NULL AND unit_cost > 0")
    for c in cogs:
        cogs_sum += float(c["unit_cost"] or 0) * float(c["quantity"] or 0)
    
    net_profit = total_income - total_expense - cogs_sum - total_dep - float(_fr.get("liability", 0))
    
    mgmt_fee = round(net_profit * 0.10, 0) if net_profit > 0 else 0
    distributable = round(net_profit * 0.90, 0) if net_profit > 0 else 0
    
    sh = _mq("SELECT id, name, ownership_pct FROM shareholders ORDER BY id")
    shares = []
    for s in sh:
        pct = float(s["ownership_pct"] or 0)
        share_amt = round(distributable * pct / 100, 0) if distributable > 0 else 0
        shares.append({"shareholder_id": s["id"], "name": s["name"], "ownership_pct": pct, "share_amount": share_amt})
    
    return {"success": True, "data": {
        "net_profit": round(net_profit, 0),
        "management_fee_pct": 10,
        "management_fee": mgmt_fee,
        "management_fee_to": "Aung Chan Myint",
        "distributable_amount": distributable,
        "shareholders": shares,
        "period": datetime.now().strftime("%Y-%m"),
        "is_profitable": net_profit > 0
    }}

@router.post('/profit-distribution/record')
async def record_profit_distribution(req: dict, user: dict = Depends(get_current_user)):
    """Record a profit distribution"""
    from mysql_db import execute as _me, query as _mq
    from datetime import date
    
    calc = req.get("calculation", {})
    if not calc:
        return {"success": False, "error": "Calculation data required"}
    
    net_profit = float(calc.get("net_profit", 0))
    mgmt_fee = float(calc.get("management_fee", 0))
    distributable = float(calc.get("distributable_amount", 0))
    shares = calc.get("shareholders", [])
    
    if net_profit <= 0:
        return {"success": False, "error": "No profit to distribute"}
    
    period_label = calc.get("period", date.today().strftime("%Y-%m"))
    
    _me("INSERT INTO profit_distributions (distribution_date,period_label,net_profit,management_fee,distributable_amount,status) VALUES (%s,%s,%s,%s,%s,'distributed')",
        (date.today(), period_label, net_profit, mgmt_fee, distributable))
    dist_id = _mq("SELECT LAST_INSERT_ID() as id")[0]["id"]
    
    if mgmt_fee > 0:
        _me("INSERT INTO profit_distribution_details (distribution_id,shareholder_id,shareholder_name,share_amount,share_pct,is_management_fee) VALUES (%s,%s,%s,%s,%s,TRUE)",
            (dist_id, 1, "Aung Chan Myint", mgmt_fee, 10.0))
    
    for sh in shares:
        amt = float(sh.get("share_amount", 0))
        if amt > 0:
            _me("INSERT INTO profit_distribution_details (distribution_id,shareholder_id,shareholder_name,share_amount,share_pct,is_management_fee) VALUES (%s,%s,%s,%s,%s,FALSE)",
                (dist_id, sh["shareholder_id"], sh["name"], amt, sh["ownership_pct"]))
    
    return {"success": True, "data": {"distribution_id": dist_id, "period": period_label, "net_profit": net_profit, "management_fee": mgmt_fee}}

@router.get('/profit-distribution/history')
async def get_profit_distribution_history(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    import json
    rows = _mq("SELECT pd.*, (SELECT JSON_ARRAYAGG(JSON_OBJECT('shareholder_name',pdd.shareholder_name,'share_amount',pdd.share_amount,'share_pct',pdd.share_pct,'is_management_fee',pdd.is_management_fee)) FROM profit_distribution_details pdd WHERE pdd.distribution_id = pd.id) as details FROM profit_distributions pd ORDER BY pd.created_at DESC")
    result = []
    for r in rows:
        entry = dict(r)
        if isinstance(entry.get("details"), str):
            try: entry["details"] = json.loads(entry["details"])
            except: entry["details"] = []
        else:
            entry["details"] = []
        result.append(entry)
    return {"success": True, "data": result}


# ── Dividends ──
@router.get('/dividends/list')
async def get_dividends(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq("""SELECT d.*, s.name AS shareholder_name
                  FROM dividends d
                  JOIN shareholders s ON d.shareholder_id = s.id
                  ORDER BY d.dividend_date DESC, d.created_at DESC""")
    return {'success': True, 'data': rows}

@router.post('/dividends/record')
async def record_dividend(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me, query as _mq
    sid = int(req.get('shareholder_id', 0))
    amount = float(req.get('amount', 0))
    div_date = req.get('dividend_date', '') or datetime.now().strftime('%Y-%m-%d')
    pm = (req.get('payment_method', '') or '').strip() or 'cash'
    status = req.get('status', 'paid') or 'paid'
    notes = (req.get('notes', '') or '').strip()
    recorded_by = (req.get('recorded_by', '') or '').strip() or 'system'
    if not sid or amount <= 0:
        return {'success': False, 'error': 'Valid shareholder_id and amount required'}
    sh = _mq('SELECT id, name FROM shareholders WHERE id=%s', (sid,))
    if not sh:
        return {'success': False, 'error': 'Shareholder not found'}
    _me("""INSERT INTO dividends (shareholder_id, amount, dividend_date, payment_method, status, notes, recorded_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (sid, amount, div_date, pm, status, notes, recorded_by))
    
    pm_map = {'kpay':'KPay','cash':'Cash','wavepay':'WavePay','wave':'WavePay','aya_pay':'AYA Pay','kbz_bank':'KBZ Bank','acm_acc':"ACM's Acc"}
    db_account = pm_map.get(pm.lower().replace(' ','_'), pm) if pm else 'Cash'
    full_note = 'Dividend: ' + sh[0]['name'] + (' (' + notes + ')' if notes else '')
    _me('INSERT INTO cash_movements (account, movement_type, amount, note, created_at)'
        ' VALUES (%s, %s, %s, %s, NOW())',
        (db_account, 'dividend', amount, full_note))
    return {'success': True, 'message': f'Dividend of {amount:,.0f} Ks recorded for {sh[0]["name"]}'}
@router.get('/dividends/summary')
async def get_dividends_summary(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq("""SELECT s.id AS shareholder_id, s.name AS shareholder_name,
                          COALESCE(SUM(d.amount), 0) AS total_dividends,
                          COUNT(d.id) AS dividend_count
                   FROM shareholders s
                   LEFT JOIN dividends d ON s.id = d.shareholder_id
                   GROUP BY s.id, s.name
                   ORDER BY total_dividends DESC""")
    total = sum(float(r['total_dividends'] or 0) for r in rows)
    return {'success': True, 'data': {'shareholders': rows, 'total_paid': total}}
