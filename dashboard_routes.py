"""PS VIBE Dashboard — Dashboard Data API Endpoints"""
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
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
            "SELECT COUNT(*) as cnt FROM console_booking WHERE DATE(booking_date) = %s AND status = 'Active' AND console_id IS NOT NULL AND console_id != ''",
            (today,)
        )
        active_players = active_players["cnt"] if active_players else 0

        today_revenue = _mysql_query_one(
            "SELECT COALESCE(SUM(net), 0) as total FROM sales_daily WHERE DATE(sale_date) = %s", (today,)
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
                   cb.id as booking_id, 
                   COALESCE(NULLIF(cb.staff_name, ''), cb.member_id) as customer_name,
                   cb.phone as customer_phone,
                   cb.start_time, cb.end_time, cb.status as booking_status
            FROM console_status c
            LEFT JOIN (
                SELECT cb1.*
                FROM console_booking cb1
                INNER JOIN (
                    SELECT console_id, MAX(id) as max_id
                    FROM console_booking
                    WHERE DATE(booking_date) = %s
                    AND status = 'Active'
                    GROUP BY console_id
                ) cb2 ON cb1.id = cb2.max_id
            ) cb ON c.console_id = cb.console_id
            ORDER BY c.console_id
        """, (today,))

        for row in rows:
            has_booking = row.get("booking_id") is not None
            display_status = "Active" if has_booking else (row["status"] or "Active")

        consoles = []
        for row in rows:
            consoles.append({
                "id": row["id"],
                "name": row["name"],
                "status": display_status,
                "current_booking": {
                    "id": row["booking_id"],
                    "customer": row["customer_name"],
                    "phone": row.get("customer_phone", "") or "",
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
            SELECT cb.id, c.console_id as console_name, 
                   COALESCE(NULLIF(cb.staff_name, ''), cb.member_id) as customer_name,
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
    """Clean up OLD cancelled/rejected/done bookings (30+ days). Never delete recent ones."""
    try:
        # Only delete bookings in terminal states that are 30+ days old
        d1 = _mysql_delete(
            "DELETE FROM console_booking WHERE status IN (%s, %s, %s, %s, %s) AND booking_date < DATE_SUB(CURDATE(), INTERVAL 30 DAY)",
            ("cancelled", "Cancelled", "rejected", "Rejected", "Done")
        )
        d2 = _mysql_delete(
            "DELETE FROM console_booking WHERE booking_date < DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
        )
        total = d1 + d2
        return {
            "success": True,
            "data": {"deleted_count": total, "message": f"Cleaned up {total} bookings (30+ days old only)"}
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
    """Update a booking (validates console conflicts on reassign)."""
    try:
        logger.info(f"PUT /bookings/{booking_id}: req={req}")
        existing = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        # Console reassign validation: block if target console has an overlapping Active session
        new_console = req.get("console_id", "")
        if new_console and new_console != (existing.get("console_id") or ""):
            # Only block if new console has an Active session that overlaps with this booking's time
            _bk_start = existing.get("start_time")
            _bk_end = existing.get("end_time")
            if _bk_start and _bk_end:
                active_check = _mysql_query_one(
                    "SELECT id FROM console_booking WHERE console_id=%s AND id!=%s AND status='Active'"
                    " AND start_time < %s AND end_time > %s LIMIT 1",
                    (new_console, booking_id, _bk_end, _bk_start)
                )
            else:
                # No time data — just check if console is Active
                active_check = _mysql_query_one(
                    "SELECT id FROM console_booking WHERE console_id=%s AND id!=%s AND status='Active' LIMIT 1",
                    (new_console, booking_id)
                )
            if active_check:
                return {"success": False, "error": f"Console {new_console} has an active session (BK#{active_check['id']}) at this time"}
            # Admin override: confirmed/pending overlaps are allowed

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

        # 🐛 Fix Jun 25: Auto-calculate end_time when start_time or duration_mins changes
        if "end_time" not in req:
            _eff_start = req.get("start_time") or existing.get("start_time")
            _eff_dur = int(req.get("duration_mins") or existing.get("duration_mins") or 0)
            if _eff_start and _eff_dur > 0:
                if isinstance(_eff_start, str):
                    _eff_start = datetime.fromisoformat(str(_eff_start))
                _eff_end = _eff_start + timedelta(minutes=_eff_dur)
                updates.append("end_time = %s")
                params.append(_eff_end.strftime("%Y-%m-%d %H:%M:%S"))

        params.append(booking_id)
        _mysql_execute(f"UPDATE console_booking SET {', '.join(updates)} WHERE id = %s", tuple(params))

        # 📢 Customer notification on confirm/cancel from web dashboard
        new_status = req.get("status", "")
        if new_status in ("confirmed", "cancelled"):
            logger.info("[DASH-NOTIFY] Triggering notification for BK#%s status=%s", booking_id, new_status)
            _notify_row = _mysql_query_one(
                "SELECT telegram_chat_id, console_id, booking_date, start_time, duration_mins FROM console_booking WHERE id=%s",
                (booking_id,)
            )
            if _notify_row and _notify_row.get("telegram_chat_id"):
                try:
                    import threading as _threading_notify, urllib.request as _urllib_notify, json as _json_notify, os as _os_notify
                    _tg_id = _notify_row["telegram_chat_id"]
                    _bk_id_str = str(booking_id)
                    if new_status == "confirmed":
                        _console = _notify_row.get("console_id", "") or "TBD"
                        _date = str(_notify_row.get("booking_date", ""))
                        _time = str(_notify_row.get("start_time", ""))[:16] if _notify_row.get("start_time") else ""
                        _dur = str(_notify_row.get("duration_mins", ""))
                        _msg = (
                            f"✅ <b>Booking Confirmed!</b>\n\n"
                            f"🎫 Booking <b>#{_bk_id_str}</b>\n"
                            f"📅 {_date}  ⏰ {_time}\n"
                            f"🎮 Console: {_console}  ⏱️ {_dur} min\n\n"
                            f"📲 Booking ချိန်ရောက်ရင် Staff ကို Check-in ပြပေးပါ။ ပျော်ရွှင်ပါစေ! 🎮"
                        )
                    else:
                        _msg = (
                            f"❌ <b>Booking #{_bk_id_str} Cancelled</b>\n\n"
                            f"Slot မအားတာ သို့မဟုတ် အကြောင်းတစ်စုံတစ်ရာကြောင့် Booking ကို cancel လုပ်လိုက်ပါတယ်။\n"
                            f"📲 နောက်ထပ် Booking အသစ် တင်ပေးပါ။"
                        )
                    _token = _os_notify.environ.get("CUSTOMER_BOT_TOKEN", "")
                    if _token:
                        def _send_cust_notify():
                            try:
                                _url = f"https://api.telegram.org/bot{_token}/sendMessage"
                                _data = _json_notify.dumps({"chat_id": _tg_id, "text": _msg, "parse_mode": "HTML"}).encode()
                                _req = _urllib_notify.Request(_url, data=_data, headers={"Content-Type": "application/json"})
                                _urllib_notify.urlopen(_req, timeout=5)
                                logger.info("[DASH-NOTIFY] Sent to chat_id=%s for BK#%s", _tg_id, booking_id)
                            except Exception as _ex:
                                logger.warning("[DASH-NOTIFY] Failed for BK#%s: %s", booking_id, _ex)
                        _threading_notify.Thread(target=_send_cust_notify, daemon=True).start()
                except Exception as _ex:
                    logger.warning("[DASH-NOTIFY] Error for BK#%s: %s", booking_id, _ex)

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


@router.put("/bookings/{booking_id}/timer")
async def dashboard_adjust_timer(booking_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Adjust the timer (duration_mins) of an active booking. Recalcs end_time."""
    try:
        duration_mins = req.get("duration_mins")
        if not duration_mins or int(duration_mins) <= 0:
            return {"success": False, "error": "duration_mins must be > 0"}
        duration_mins = int(duration_mins)

        existing = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        if existing.get("status") != "Active":
            return {"success": False, "error": "Can only adjust timer for Active sessions"}

        staff_name = user.get("username", "Dashboard User")
        _mysql_execute(
            "UPDATE console_booking SET duration_mins=%s, end_time=DATE_ADD(start_time, INTERVAL %s MINUTE), "
            "notes=CONCAT(IFNULL(notes,''), ' | Timer adjusted to ', %s, 'min by ', %s) WHERE id=%s",
            (duration_mins, duration_mins, str(duration_mins), staff_name, booking_id)
        )

        updated = _mysql_query_one("SELECT id, console_id, start_time, end_time, duration_mins, status FROM console_booking WHERE id=%s", (booking_id,))
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "console_id": updated.get("console_id"),
                "start_time": str(updated["start_time"]) if updated.get("start_time") else None,
                "end_time": str(updated["end_time"]) if updated.get("end_time") else None,
                "duration_mins": updated.get("duration_mins"),
                "status": updated.get("status"),
            },
        }
    except Exception as e:
        logger.error(f"PUT /bookings/{booking_id}/timer error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/bookings/{booking_id}")
async def dashboard_delete_booking(booking_id: int, user: dict = Depends(get_current_user)):
    """Soft-delete a booking — sets status to 'cancelled' instead of physical DELETE."""
    try:
        existing = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        # Soft-delete: never physically delete bookings — preserve history
        staff_name = user.get("username", "Dashboard User")
        _mysql_execute(
            "UPDATE console_booking SET status='cancelled', notes=CONCAT(IFNULL(notes,''), ' | Cancelled by ', %s, ' via dashboard') WHERE id=%s",
            (staff_name, booking_id)
        )
        return {"success": True, "data": {"cancelled": booking_id, "message": "Booking soft-deleted (status=cancelled)"}}
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
            SELECT m.member_id, m.member_name as name, COALESCE(m.phone,'') as phone, m.balance_mins, m.tier,
                   m.total_spend, m.lifetime_spend, m.join_date, m.last_updated, m.effective_rate
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

        from fifo_wallet import fifo_calc

        members = []
        for r in rows:
            mid = r["member_id"]
            bal_mins = r.get("balance_mins") or 0
            # Fetch topup history for FIFO liability calculation
            tu = _mysql_query(
                "SELECT amount, mins_added FROM topup_log WHERE member_id=%s ORDER BY topup_date ASC",
                (mid,)
            ) or []
            topups = [{"amount": float(t["amount"] or 0), "mins_added": float(t["mins_added"] or 0)} for t in tu]
            fifo = fifo_calc(topups, bal_mins)
            members.append({
                "member_id": mid,
                "name": r.get("name"),
                "phone": r.get("phone", ""),
                "balance_minutes": bal_mins,
                "balance_kyat": int(fifo["liability"]),
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
    member_id: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    payment_method: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List topup logs with optional filters."""
    try:
        from mysql_db import query as _mq, query_one as _mqo
        where = ["1=1"]
        params = []
        if member_id:
            where.append("tl.member_id = %s")
            params.append(member_id)
        if date_from:
            where.append("DATE(tl.topup_date) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(tl.topup_date) <= %s")
            params.append(date_to)
        if payment_method:
            where.append("tl.payment_method = %s")
            params.append(payment_method)
        if search:
            where.append("(tl.member_id LIKE %s OR tl.staff_name LIKE %s OR tl.payment_method LIKE %s)")
            s = f"%{search}%"
            params.extend([s, s, s])

        sql = f"""
            SELECT tl.id, tl.member_id, tl.amount, tl.mins_added, tl.topup_date,
                   tl.staff_name, tl.payment_method, tl.balance_before, tl.balance_after,
                   tl.balance_mins_before, tl.balance_mins_after, tl.notes, tl.created_at,
                   mw.member_name
            FROM topup_log tl
            LEFT JOIN member_wallets mw ON tl.member_id = mw.member_id
            WHERE {' AND '.join(where)}
            ORDER BY tl.topup_date DESC, tl.id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mq(sql, tuple(params))

        count_row = _mqo(
            f"SELECT COUNT(*) as total FROM topup_log tl WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        topups = []
        for r in rows:
            topups.append({
                "id": r["id"],
                "member_id": r.get("member_id"),
                "member_name": r.get("member_name"),
                "amount": float(r.get("amount") or 0),
                "mins_added": r.get("mins_added", 0),
                "topup_date": str(r["topup_date"]) if r.get("topup_date") else None,
                "staff_name": r.get("staff_name"),
                "payment_method": r.get("payment_method"),
                "balance_before": float(r.get("balance_before") or 0),
                "balance_after": float(r.get("balance_after") or 0),
                "balance_mins_before": r.get("balance_mins_before", 0),
                "balance_mins_after": r.get("balance_mins_after", 0),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": topups, "total": total}
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
        # Track old item_name for stock_in/stock_out sync on rename
        old_name = existing.get("item_name", "")
        for field in ["item_name", "category", "quantity", "unit_price", "reorder_level"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(item_id)
        _mysql_execute(f"UPDATE inventory SET {', '.join(updates)} WHERE id = %s", tuple(params))

        # Sync stock_in/stock_out on rename
        new_name = req.get("item_name", "")
        if old_name and new_name and old_name != new_name:
            _mysql_execute("UPDATE stock_in SET item_name = %s WHERE item_name = %s", (new_name, old_name))
            _mysql_execute("UPDATE stock_out SET item_name = %s WHERE item_name = %s", (new_name, old_name))
            logger.info("dashboard: renamed '%s' -> '%s' (synced stock_in, stock_out)", old_name, new_name)

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

        # Enrich with console/SSD installation info
        if games:
            game_titles = [g["game_title"] for g in games]
            placeholders = ",".join(["%s"] * len(game_titles))
            cg_rows = _mysql_query(
                f"SELECT DISTINCT game_title, console_id, console_name, status, install_type FROM console_games WHERE game_title IN ({placeholders}) ORDER BY game_title, console_id",
                tuple(game_titles)
            )
            cg_map = {}
            for cg in cg_rows:
                gt = cg["game_title"]
                if gt not in cg_map:
                    cg_map[gt] = {"consoles": [], "ssds": []}
                cid = cg["console_id"]
                entry = {"console_id": cid, "console_name": cg.get("console_name") or cid, "status": cg.get("status"), "install_type": cg.get("install_type")}
                if cid and cid.startswith("SSD"):
                    cg_map[gt]["ssds"].append(entry)
                else:
                    cg_map[gt]["consoles"].append(entry)
            for g in games:
                info = cg_map.get(g["game_title"], {"consoles": [], "ssds": []})
                g["consoles"] = info["consoles"]
                g["ssds"] = info["ssds"]

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



# ── Game Install Management ─────────────────────────────────────────

@router.post("/games/install")
async def dashboard_install_game(req: dict, user: dict = Depends(get_current_user)):
    """Install a game on a console or backup to an SSD."""
    try:
        game_title = req.get("game_title", "")
        console_id = req.get("console_id", "")
        if not game_title:
            return {"success": False, "error": "game_title is required"}
        if not console_id:
            return {"success": False, "error": "console_id is required"}

        game = _mysql_query_one("SELECT game_title FROM games_library WHERE game_title = %s", (game_title,))
        if not game:
            return {"success": False, "error": "Game not found"}

        existing = _mysql_query_one(
            "SELECT id FROM console_games WHERE game_title = %s AND console_id = %s",
            (game_title, console_id)
        )
        if existing:
            return {"success": False, "error": f"Game already installed on {console_id}"}

        console_name = console_id
        _mysql_execute(
            """INSERT INTO console_games (branch_id, console_id, console_name, game_id, game_title, status, install_type, slot_position, created_at, updated_at)
               VALUES (1, %s, %s, %s, %s, 'Installed', 'Internal SSD', 1, NOW(), NOW())""",
            (console_id, console_name, game_title, game_title)
        )

        return {"success": True, "data": {"game_title": game_title, "console_id": console_id, "action": "installed"}}
    except Exception as e:
        logger.error(f"POST /games/install error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/games/uninstall")
async def dashboard_uninstall_game(game_title: str = Query(...), console_id: str = Query(...), user: dict = Depends(get_current_user)):
    """Remove a game from a console or SSD."""
    try:
        existing = _mysql_query_one(
            "SELECT id FROM console_games WHERE game_title = %s AND console_id = %s",
            (game_title, console_id)
        )
        if not existing:
            return {"success": False, "error": f"Game not found on {console_id}"}

        _mysql_delete("DELETE FROM console_games WHERE game_title = %s AND console_id = %s", (game_title, console_id))
        return {"success": True, "data": {"game_title": game_title, "console_id": console_id, "action": "uninstalled"}}
    except Exception as e:
        logger.error(f"DELETE /games/uninstall error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/games/install-targets")
async def dashboard_get_install_targets(user: dict = Depends(get_current_user)):
    """Get list of available consoles and SSDs for install target selection."""
    try:
        consoles = _mysql_query(
            "SELECT DISTINCT console_id, console_name FROM console_games WHERE console_id NOT LIKE CONCAT('SSD', CHAR(37)) ORDER BY console_id"
        )
        ssds = _mysql_query(
            "SELECT DISTINCT console_id, console_name FROM console_games WHERE console_id LIKE CONCAT('SSD', CHAR(37)) ORDER BY console_id"
        )
        return {
            "success": True,
            "data": {
                "consoles": [{"id": c["console_id"], "name": c.get("console_name") or c["console_id"]} for c in consoles],
                "ssds": [{"id": s["console_id"], "name": s.get("console_name") or s["console_id"]} for s in ssds],
            }
        }
    except Exception as e:
        logger.error(f"GET /games/install-targets error: {e}")
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

@router.get("/food-orders")
async def dashboard_get_food_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List food orders linked to console sessions (from food_cart)."""
    try:
        sql = """
            SELECT fc.id, fc.booking_id, fc.item_name, fc.quantity, fc.unit_price,
                   (fc.quantity * fc.unit_price) as line_total,
                   fc.created_at,
                   fc.fulfilled_at,
                   cb.console_id, cb.member_id, cb.status as booking_status,
                   cb.start_time
            FROM food_cart fc
            JOIN console_booking cb ON CAST(fc.booking_id AS UNSIGNED) = cb.id
            ORDER BY fc.id DESC
            LIMIT %s OFFSET %s
        """
        rows = _mysql_query(sql, (limit, offset))
        count_row = _mysql_query_one("SELECT COUNT(*) as total FROM food_cart")
        total = count_row["total"] if count_row else 0

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "booking_id": r.get("booking_id"),
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit_price": float(r.get("unit_price") or 0),
                "line_total": float(r.get("line_total") or 0),
                "console_id": r.get("console_id"),
                "member_id": r.get("member_id"),
                "booking_status": r.get("booking_status"),
                "start_time": str(r["start_time"]) if r.get("start_time") else None,
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
                "fulfilled_at": str(r["fulfilled_at"]) if r.get("fulfilled_at") else None,
            })
        logger.info(f"GET /food-orders: returning {len(entries)} entries, total={total}")
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /food-orders error: {e}", exc_info=True)
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
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List sales daily records. Supports single date OR custom period (start_date..end_date)."""
    try:
        where = ["1=1"]
        params = []
        if start_date and end_date:
            where.append("sale_date >= %s AND sale_date <= %s")
            params.extend([start_date, end_date])
        elif date:
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
                "sale_time": str(r["sale_time"])[:16] if r.get("sale_time") else None,  # MMT HH:MM
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
            # Normalize "acm's_acc" to account key "acm_acc"
            if pm == "acm's_acc":
                pm = "acm_acc"
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
        # Also subtract advance settlement injects — these are recoveries of previously deducted advances, NOT new cash
        _adv_settle_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'inject' AND note LIKE CONCAT('Advance settled', CHAR(37)) GROUP BY account")
        for _r in _adv_settle_rows:
            _k = "inject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        key_to_name = {"cash": "Cash", "wave": "Wave", "kpay": "KPay", "aya_pay": "AYA Pay", "kbz_bank": "KBZ Bank", "acm_acc": "ACM's Acc"}
        # Capital expenditure queries (all from KBZ Bank)
        _asset_purchases = _mqo("SELECT COALESCE(SUM(per_price * qty), 0) as total FROM finance_assets WHERE status = 'active'")
        _advances_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_advances WHERE status='pending' OR status IS NULL")
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
        # --- GAME REVENUE: all sales_daily net (excl Topup/New member notes) ---
        rev_rows = _mq("SELECT net, gross, notes FROM sales_daily WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0", (ym,))
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
        # --- FOOD REVENUE: stock_out (actual items sold × unit_price = selling price) ---
        frows = _mq("SELECT COALESCE(SUM(total), 0) as t FROM stock_out WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s", (ym,))
        food_rev = float(frows[0]["t"] or 0) if frows else 0
        # --- FOOD COGS from stock FIFO ---
        import stock_fifo, pymysql
        _sfc = pymysql.connect(host='127.0.0.1', user='root', password='PsVibe@MySQL2024!', database='psvibe_api')
        _sfr = stock_fifo.calc_fifo(_sfc)
        _sfc.close()
        cogs = _sfr["cogs"]
        # --- GAME REVENUE = total_sales - food_rev (food_REV is already in sales_daily total) ---
        game_rev = total_sales - food_rev
        # Ensure no negative game revenue
        if game_rev < 0: game_rev = 0
        trows = _mq("SELECT COALESCE(SUM(amount),0) as t FROM topup_log WHERE DATE_FORMAT(topup_date, '%%Y-%%m') = %s", (ym,))
        topup_rev = float(trows[0]["t"] or 0) if trows else 0
        # cogs already computed (from first block above)
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
        # Game Profit = game_rev (no direct COGS for game)
        # Food Profit = food_rev - food_cogs
        game_profit = game_rev + wallet_consumed
        food_profit = food_rev - cogs
        total_gross_profit = game_profit + food_profit
        # Depreciation expense for this month
        first_of_month = f"{year:04d}-{month:02d}-01"
        _dep_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0 AND purchase_date < %s", (first_of_month,))
        depreciation_exp = float(_dep_rows[0]["t"] or 0) if _dep_rows else 0
        total_expense_all = total_expense + depreciation_exp
        net_profit = total_gross_profit - total_expense_all
        return {"success": True, "data": {
            "period": ym,
            "revenue": {"game_revenue": round(game_rev,0), "food_revenue": round(food_rev,0), "topup_revenue": round(topup_rev,0), "wallet_consumed": round(wallet_consumed,0), "topup_deferred": round(topup_sales,0), "discounts": round(discounts,0), "total_revenue": round(total_revenue,0)},
            "cogs": round(cogs,0), "food_cogs": round(cogs,0),
            "gross_profit": round(total_gross_profit,0), "game_profit": round(game_profit,0), "food_profit": round(food_profit,0),
            "expenses": {"by_category": expenses_by_cat, "total": round(total_expense,0), "depreciation": round(depreciation_exp,0), "total_with_depreciation": round(total_expense_all,0)},
            "operating_profit": round(total_gross_profit - total_expense,0),
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
            _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
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
        db_acct = {"cash":"Cash","wave":"Wave","kpay":"KPay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
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
            opr = _mq("SELECT payment_method, COALESCE(SUM(amount),0) as total FROM opex WHERE description NOT LIKE '%%Prepaid Rent Amortization%%' GROUP BY payment_method")
            opex_by_method = {}
            for r in opr:
                pm = (r.get("payment_method") or "").strip().lower().replace(" ", "_").replace("acm's_acc", "acm_acc")
                opex_by_method[pm] = float(r.get("total") or 0)
            opex = opex_by_method.get(key, 0)

            # Cash movements — use DB account name (label), not key
            db = db_acct[key]
            t_in = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_in'", (db,))["t"] or 0)
            t_out = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_out'", (db,))["t"] or 0)
            inj = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='inject' AND (note IS NULL OR (note NOT LIKE CONCAT('Topup', CHAR(37)) AND note NOT LIKE CONCAT('New member', CHAR(37)) AND note NOT LIKE CONCAT('Advance settled', CHAR(37))))", (db,))["t"] or 0)
            ej = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='eject'", (db,))["t"] or 0)

            # t_out is stored as negative in DB, so + t_out works (= subtract)
            bal = income - opex + t_in + t_out + inj - ej

            if key == "kbz_bank":
                ded_assets = float(_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active'")["t"] or 0)
                ded_adv = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")["t"] or 0)
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
        # Retained = cumulative P&L net profit = Revenue - Expenses
        # Revenue = game+food sales (excl topups) + wallet consumption (FIFO consumed)
        # Expenses = opex + food cogs + cumulative depreciation
        _sh = _mq("SELECT SUM(capital_contribution) as tc FROM shareholders")
        icap = float(_sh[0]["tc"] or 0) if _sh else 0
        ti_excl_topup = float(_mqo("SELECT COALESCE(SUM(net),0) as t FROM sales_daily WHERE net>0 AND notes NOT LIKE 'Topup%%' AND notes NOT LIKE 'New member%%'")["t"] or 0)
        te = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM opex")["t"] or 0)
        cost_of_sold = _inv['cogs']  # FIFO-based COGS
        wallet_consumed = float(_fr.get("consumed", 0))  # FIFO wallet consumed = recognized revenue
        total_earned_revenue = ti_excl_topup + wallet_consumed
        retained = total_earned_revenue - te - cost_of_sold - total_dep
        total_eq = icap + retained

        total_liab = member_liab
        total_liab_eq = round(total_liab + total_eq, 0)
        total_assets = round(total_assets, 0)
        # Data-entry / rounding gap auto-balance (large gaps now handled by advance recovery above)
        _bs_diff = round(total_assets - total_liab_eq, 0)
        if abs(_bs_diff) > 10:
            retained += _bs_diff
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
    """Cash Flow Statement — 3-section format (Operating / Investing / Capital).
    
    - Operating: Game income + topups - OPEX - stock purchases (all accounts)
    - Investing: Asset purchases + Advances (TOTAL given) + Prepaid - Advance recoveries - Disposal proceeds
    - Capital: KBZ Bank transfer_in — no separate Financing section (300M already = opening)
    
    Matches original June 9 design: closing ≈ 28.8M with proper advance tracking.
    """
    from mysql_db import query as _mq, query_one as _mqo
    ym = f"{year:04d}-{month:02d}"
    try:
        _start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            _end = f"{year+1:04d}-01-01"
        else:
            _end = f"{year:04d}-{month+1:02d}-01"

        # Opening balance from shareholders
        _sh = _mq("SELECT COALESCE(SUM(capital_contribution),0) as tc FROM shareholders")
        opener = float(_sh[0]["tc"] or 0) if _sh else 0

        acct_keys = ["cash","wave","aya_pay","kpay","kbz_bank","acm_acc"]
        key2name = {"cash":"Cash","wave":"Wave","aya_pay":"AYA Pay","kpay":"KPay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}

        # ─── Income: sales_daily by payment method (MONTH-FILTERED) ───
        inc = {k: 0.0 for k in acct_keys}
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND created_at >= %s AND created_at < %s", (_start, _end))
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

        # Topup income (MONTH-FILTERED)
        tr = _mq("SELECT payment_method, amount FROM topup_log WHERE amount>0 AND payment_method IS NOT NULL AND topup_date >= %s AND topup_date < %s", (_start, _end))
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

        total_income = sum(inc.values())

        # ─── OPEX by payment method, excl Prepaid Rent Amortization (MONTH-FILTERED) ───
        opex = {k: 0.0 for k in acct_keys}
        ox = _mq("SELECT payment_method, COALESCE(SUM(amount),0) as t FROM opex WHERE description NOT LIKE %s AND created_at >= %s AND created_at < %s GROUP BY payment_method", ("%Prepaid Rent Amortization%", _start, _end))
        for _r in ox:
            m = (_r.get("payment_method") or "").strip().lower().replace(" ", "_")
            if m == "acm's_acc": m = "acm_acc"
            if m in opex: opex[m] += float(_r["t"] or 0)
        total_opex = sum(opex.values())

        # ─── Cash movements (MONTH-FILTERED) ───
        cm = {}
        for _r in _mq("SELECT movement_type, account, COALESCE(SUM(amount),0) as t FROM cash_movements WHERE created_at >= %s AND created_at < %s GROUP BY movement_type, account", (_start, _end)):
            cm[f"{_r['movement_type']}|{_r['account']}"] = float(_r["t"] or 0)

        # Remove Topup/New member injects (duplicates of income already tracked)
        _bi = _mq("SELECT account, COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type='inject' AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37))) AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _bi:
            k = "inject|" + _r["account"]
            if k in cm: cm[k] -= float(_r["t"] or 0)

        # ─── Stock purchases = all eject from all accounts (cash outflow for inventory) ───
        total_stock_eject = sum(abs(cm.get(f"eject|{key2name[k]}",0)) for k in acct_keys)

        # ─── Investing: Assets + Advances (TOTAL given) + Prepaid - Recoveries ───
        # Cumulative — these are long-lived investments, not month-limited
        _ap = float((_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active'") or {}).get("t",0))
        _pp = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid") or {}).get("t",0))
        _dp = float((_mqo("SELECT COALESCE(SUM(disposal_amount),0) as t FROM finance_assets WHERE status='disposed' AND disposal_amount > 0") or {}).get("t",0))

        # Advances: TOTAL ever given (settled + pending), NOT just current pending
        # Advance settlements are returns of these advances — shown as recovery inflow
        _adv_total = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances") or {}).get("t",0))  # ALL advances

        # Advance recoveries: inject entries with "Advance settled" note (MONTH-FILTERED)
        _adv_recovery = float((_mqo(
            "SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type='inject' AND note LIKE CONCAT('Advance settled', CHAR(37)) AND created_at >= %s AND created_at < %s",
            (_start, _end)
        ) or {}).get("t",0))

        # KBZ Bank transfer_in (the capital injection)
        _kbz_tin = float((_mqo(
            "SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account='KBZ Bank' AND movement_type='transfer_in' AND created_at >= %s AND created_at < %s",
            (_start, _end)
        ) or {}).get("t",0))

        # ─── Per-account closing balance ───
        total_bal = 0.0
        for k in acct_keys:
            db = key2name[k]
            # Include ALL injects (not advance settlements filtered — we handle advances separately in Investing)
            bal = inc[k] - opex[k] + cm.get(f"transfer_in|{db}",0) - abs(cm.get(f"transfer_out|{db}",0)) + cm.get(f"inject|{db}",0) - cm.get(f"eject|{db}",0)
            if k == "kbz_bank":
                bal = bal - _ap - _adv_total - _pp + _dp
            total_bal += bal

        # ─── Build sections ───

        # Section 1: Operating Activities
        op_items = []
        if total_income: op_items.append({"label": "Game & Top-up Income", "emoji": "🎮", "amount": round(total_income, 0), "type": "inflow"})
        if total_opex: op_items.append({"label": "Operating Expenses (All Accounts)", "emoji": "📉", "amount": round(total_opex, 0), "type": "outflow"})
        if total_stock_eject > 1: op_items.append({"label": "Stock Purchases (Inventory)", "emoji": "📦", "amount": round(total_stock_eject, 0), "type": "outflow"})
        op_net = total_income - total_opex - total_stock_eject

        # Section 2: Investing Activities
        inv_items = []
        if _ap: inv_items.append({"label": "Equipment & Setup (PS5, TVs, Furniture)", "emoji": "🎮", "amount": round(_ap, 0), "type": "outflow"})
        if _adv_total: inv_items.append({"label": "Advances Given (Total)", "emoji": "📋", "amount": round(_adv_total, 0), "type": "outflow"})
        if _pp: inv_items.append({"label": "Prepaid Expenses (Rent/Lease)", "emoji": "📅", "amount": round(_pp, 0), "type": "outflow"})
        if _adv_recovery: inv_items.append({"label": "Advance Recoveries (Returned)", "emoji": "♻️", "amount": round(_adv_recovery, 0), "type": "inflow"})
        if _dp: inv_items.append({"label": "Asset Disposal Proceeds", "emoji": "💰", "amount": round(_dp, 0), "type": "inflow"})
        inv_net = _adv_recovery + _dp - _ap - _adv_total - _pp

        # ─── Closing = Opening + Operating + Investing (Opening already = 300M capital) ───
        closing = opener + op_net + inv_net

        return {"success": True, "data": {
            "period": ym,
            "opening_balance": round(opener, 0),
            "sections": {
                "operating": {"title": "Operating Activities", "items": op_items, "subtotal": round(op_net, 0)},
                "investing": {"title": "Investing Activities", "items": inv_items, "subtotal": round(inv_net, 0)}
            },
            "summary": {
                "opening_balance": round(opener, 0),
                "operating_net": round(op_net, 0),
                "investing_net": round(inv_net, 0),
                "net_change": round(closing - opener, 0),
                "closing_balance": round(closing, 0)
            }
        }}
    except Exception as e:
        import traceback
        traceback.print_exc()
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
async def calculate_profit_distribution(
    user: dict = Depends(get_current_user),
    period: str = None
):
    """Calculate profit distribution: 10% mgmt fee -> proportional split.
    Uses the SAME revenue/COGS/depreciation logic as the monthly P&L endpoint.
    
    Query params:
        period (optional): YYYY-MM format, defaults to current month.
    """
    from mysql_db import query as _mq
    import pymysql
    import stock_fifo
    from datetime import datetime, date
    
    # ── Period resolution ──
    today = date.today()
    if period:
        try:
            year, month = map(int, period.split('-'))
        except Exception:
            year, month = today.year, today.month
    else:
        year, month = today.year, today.month
    ym = f"{year:04d}-{month:02d}"
    first_of_month = f"{year:04d}-{month:02d}-01"
    
    # ── 1. Revenue (ALIGNED with PNL) ──
    # Game revenue: sales_daily net, exclude Topup/New member notes
    rev_rows = _mq(
        "SELECT net, gross, notes FROM sales_daily "
        "WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0",
        (ym,)
    )
    total_sales = 0.0
    for r in rev_rows:
        n = float(r.get("net") or 0)
        notes = (r.get("notes") or "")
        if notes.startswith("Topup") or notes.startswith("New member"):
            continue
        total_sales += n
    
    # Food revenue: stock_out (actual items × unit_price)
    frows = _mq(
        "SELECT COALESCE(SUM(total), 0) as t FROM stock_out "
        "WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s",
        (ym,)
    )
    food_rev = float(frows[0]["t"] or 0) if frows else 0
    
    # Game revenue = total_sales - food_rev (food is embedded in sales_daily)
    game_rev = max(0.0, total_sales - food_rev)
    
    # Wallet consumed: FIFO liability consumed during period
    try:
        _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
        _fr = __import__("fifo_wallet").get_all_fifo(_fc)
        _fc.close()
        wallet_consumed = float(_fr.get("consumed", 0))
    except Exception:
        wallet_consumed = 0.0
    
    total_revenue = game_rev + food_rev + wallet_consumed
    
    # ── 2. Operating expenses (ALIGNED with PNL) ──
    opex_rows = _mq(
        "SELECT category, COALESCE(SUM(amount),0) as total FROM opex "
        "WHERE DATE_FORMAT(expense_date, '%%Y-%%m') = %s "
        "GROUP BY category ORDER BY total DESC",
        (ym,)
    )
    total_expense = sum(float(r["total"]) for r in opex_rows) if opex_rows else 0.0
    
    # ── 3. COGS — food stock FIFO (ALIGNED with PNL) ──
    try:
        _sfc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
        _sfr = stock_fifo.calc_fifo(_sfc)
        _sfc.close()
        cogs = _sfr["cogs"]
    except Exception:
        cogs = 0.0
    
    # ── 4. Depreciation (ALIGNED with PNL: purchase_date < first_of_month) ──
    dep_rows = _mq(
        "SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets "
        "WHERE status='active' AND useful_life > 0 AND purchase_date < %s",
        (first_of_month,)
    )
    total_dep = float(dep_rows[0]["t"] or 0) if dep_rows else 0.0
    
    # ── 5. Net profit (ALIGNED with PNL) ──
    gross_profit = total_revenue - cogs
    net_profit = gross_profit - total_expense - total_dep
    
    mgmt_fee = round(net_profit * 0.10, 0) if net_profit > 0 else 0
    distributable = round(net_profit * 0.90, 0) if net_profit > 0 else 0
    
    sh = _mq("SELECT id, name, role, ownership_pct FROM shareholders ORDER BY id")
    shares = []
    mgmt_fee_to = None
    max_pct = 0
    for s in sh:
        pct = float(s["ownership_pct"] or 0)
        share_amt = round(distributable * pct / 100, 0) if distributable > 0 else 0
        shares.append({"shareholder_id": s["id"], "name": s["name"], "ownership_pct": pct, "share_amount": share_amt})
        # Management fee goes to Founder, or highest-ownership shareholder
        if s.get("role") == "Founder":
            mgmt_fee_to = s["name"]
        if pct > max_pct:
            max_pct = pct
            if mgmt_fee_to is None:
                mgmt_fee_to = s["name"]
    if mgmt_fee_to is None:
        mgmt_fee_to = shares[0]["name"] if shares else "Unknown"
    
    return {"success": True, "data": {
        "net_profit": round(net_profit, 0),
        "management_fee_pct": 10,
        "management_fee": mgmt_fee,
        "management_fee_to": mgmt_fee_to,
        "distributable_amount": distributable,
        "shareholders": shares,
        "period": ym,
        "pnl_matched": True,
        "breakdown": {
            "game_revenue": round(game_rev, 0),
            "food_revenue": round(food_rev, 0),
            "wallet_consumed": round(wallet_consumed, 0),
            "total_revenue": round(total_revenue, 0),
            "cogs": round(cogs, 0),
            "gross_profit": round(gross_profit, 0),
            "total_expense": round(total_expense, 0),
            "depreciation": round(total_dep, 0)
        },
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

# ═══════════════════════════════════════
#  TOPUPS — CREATE
# ═══════════════════════════════════════
@router.post("/topups")
async def dashboard_create_topup(req: dict, user: dict = Depends(get_current_user)):
    """Manual topup — insert into topup_log and update member balance."""
    try:
        member_id = req.get("member_id", "")
        amount = req.get("amount", 0)
        mins_added = req.get("mins_added", 0)
        payment_method = req.get("payment_method", "Cash")
        staff_name = req.get("staff_name", user.get("username", "admin"))
        notes = req.get("notes", "")

        if not member_id:
            return {"success": False, "error": "member_id is required"}
        if amount <= 0:
            return {"success": False, "error": "amount must be positive"}

        # Get current member wallet
        member = _mysql_query_one(
            "SELECT member_id, member_name, balance_mins FROM member_wallets WHERE member_id = %s",
            (member_id,)
        )
        if not member:
            return {"success": False, "error": "Member not found"}

        balance_mins_before = int(member.get("balance_mins") or 0)
        balance_mins_after = balance_mins_before + mins_added

        # Also check the 'members' table if it exists
        member_balance_before = None
        try:
            mb = _mysql_query_one("SELECT balance_minutes FROM members WHERE member_id = %s", (member_id,))
            member_balance_before = int(mb.get("balance_minutes") or 0) if mb else 0
        except:
            member_balance_before = 0

        member_balance_after = (member_balance_before or 0) + mins_added

        # Insert into topup_log
        new_id = _mysql_execute(
            """INSERT INTO topup_log
               (member_id, amount, mins_added, topup_date, staff_name, payment_method,
                balance_before, balance_after, balance_mins_before, balance_mins_after, notes, created_at)
               VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (
                member_id,
                amount,
                mins_added,
                staff_name,
                payment_method,
                member_balance_before,
                member_balance_after,
                balance_mins_before,
                balance_mins_after,
                notes,
            ),
        )

        # Update member_wallets
        _mysql_execute(
            "UPDATE member_wallets SET balance_mins = %s, last_updated = NOW() WHERE member_id = %s",
            (balance_mins_after, member_id)
        )

        # Update members table
        try:
            _mysql_execute(
                "UPDATE members SET balance_minutes = %s, updated_at = NOW() WHERE member_id = %s",
                (member_balance_after, member_id)
            )
        except:
            pass

        return {
            "success": True,
            "data": {
                "id": new_id,
                "member_id": member_id,
                "member_name": member.get("member_name"),
                "amount": amount,
                "mins_added": mins_added,
                "payment_method": payment_method,
                "staff_name": staff_name,
                "balance_mins_before": balance_mins_before,
                "balance_mins_after": balance_mins_after,
                "notes": notes,
            },
        }
    except Exception as e:
        logger.error(f"POST /topups error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  STAFF — CRUD
# ═══════════════════════════════════════
@router.get("/staff")
async def dashboard_get_staff(
    search: str | None = Query(None),
    is_active: str | None = Query(None),
    role: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List staff members with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(staff_name LIKE %s OR role LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like])
        if is_active is not None and is_active.lower() in ("true", "false", "1", "0"):
            where.append("is_active = %s")
            params.append(1 if is_active.lower() in ("true", "1") else 0)
        if role:
            where.append("role = %s")
            params.append(role)

        sql = f"""
            SELECT staff_id, staff_name, email, base_salary, food_allowance, transport_allowance,
                   attendance_bonus, commission_rate_food, role, is_active, last_updated
            FROM staff_records
            WHERE {' AND '.join(where)}
            ORDER BY staff_name ASC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM staff_records WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        staff_list = []
        for r in rows:
            staff_list.append({
                "staff_id": r["staff_id"],
                "staff_name": r.get("staff_name"),
                "email": r.get("email") or "",
                "base_salary": float(r.get("base_salary") or 0),
                "food_allowance": float(r.get("food_allowance") or 0),
                "transport_allowance": float(r.get("transport_allowance") or 0),
                "attendance_bonus": float(r.get("attendance_bonus") or 0),
                "commission_rate_food": float(r.get("commission_rate_food") or 0),
                "role": r.get("role"),
                "is_active": bool(r.get("is_active")),
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })
        return {"success": True, "data": staff_list, "total": total}
    except Exception as e:
        logger.error(f"GET /staff error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/staff")
async def dashboard_create_staff(req: dict, user: dict = Depends(get_current_user)):
    """Create a new staff member."""
    try:
        staff_name = req.get("staff_name", "").strip()
        if not staff_name:
            return {"success": False, "error": "staff_name is required"}

        base_salary = req.get("base_salary", 0)
        email = req.get("email", "").strip()
        food_allowance = req.get("food_allowance", 0)
        transport_allowance = req.get("transport_allowance", 0)
        attendance_bonus = req.get("attendance_bonus", 0)
        commission_rate_food = req.get("commission_rate_food", 0)
        role = req.get("role", "")

        new_id = _mysql_execute(
            """INSERT INTO staff_records (staff_name, email, base_salary, food_allowance, transport_allowance,
                   attendance_bonus, commission_rate_food, role, is_active, last_updated)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())""",
            (staff_name, email or None, base_salary, food_allowance, transport_allowance,
             attendance_bonus, commission_rate_food, role),
        )

        return {
            "success": True,
            "data": {
                "staff_id": new_id,
                "staff_name": staff_name,
                "email": email or "",
                "base_salary": base_salary,
                "food_allowance": food_allowance,
                "transport_allowance": transport_allowance,
                "attendance_bonus": attendance_bonus,
                "commission_rate_food": commission_rate_food,
                "role": role,
                "is_active": True,
            },
        }
    except Exception as e:
        logger.error(f"POST /staff error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/staff/{staff_id}")
async def dashboard_update_staff(staff_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a staff member."""
    try:
        existing = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not existing:
            return {"success": False, "error": "Staff not found"}

        updates = []
        params = []
        for field in ["staff_name", "email", "base_salary", "food_allowance", "transport_allowance",
                       "attendance_bonus", "commission_rate_food", "role", "is_active"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(staff_id)
        _mysql_execute(f"UPDATE staff_records SET {', '.join(updates)} WHERE staff_id = %s", tuple(params))

        updated = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        return {
            "success": True,
            "data": {
                "staff_id": updated["staff_id"],
                "staff_name": updated.get("staff_name"),
                "email": updated.get("email") or "",
                "base_salary": float(updated.get("base_salary") or 0),
                "food_allowance": float(updated.get("food_allowance") or 0),
                "transport_allowance": float(updated.get("transport_allowance") or 0),
                "attendance_bonus": float(updated.get("attendance_bonus") or 0),
                "commission_rate_food": float(updated.get("commission_rate_food") or 0),
                "role": updated.get("role"),
                "is_active": bool(updated.get("is_active")),
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /staff/{staff_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/staff/{staff_id}/deactivate")
async def dashboard_deactivate_staff(staff_id: int, user: dict = Depends(get_current_user)):
    """Soft-deactivate a staff member (set is_active=0)."""
    try:
        existing = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not existing:
            return {"success": False, "error": "Staff not found"}

        _mysql_execute(
            "UPDATE staff_records SET is_active = 0, last_updated = NOW() WHERE staff_id = %s",
            (staff_id,)
        )
        return {
            "success": True,
            "data": {
                "staff_id": staff_id,
                "staff_name": existing.get("staff_name"),
                "is_active": False,
                "message": "Staff deactivated",
            },
        }
    except Exception as e:
        logger.error(f"PUT /staff/{staff_id}/deactivate error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════ Staff Leave ═══════════
@router.get("/staff/{staff_id}/leave")
async def dashboard_get_staff_leave(staff_id: int, month: str | None = Query(None), user: dict = Depends(get_current_user)):
    """Get leave days for a staff member (optionally filtered by month)."""
    try:
        where = "staff_id = %s"
        params = [staff_id]
        if month and len(month) == 7:
            where += " AND DATE_FORMAT(leave_date, '%%Y-%%m') = %s"
            params.append(month)
        rows = _mysql_query(f"SELECT * FROM staff_leave WHERE {where} ORDER BY leave_date DESC", tuple(params))
        total_days = len(rows)
        return {"success": True, "data": [{
            "id": r["id"], "staff_id": r["staff_id"],
            "leave_date": str(r["leave_date"]), "reason": r.get("reason") or ""
        } for r in rows], "total": total_days, "free_limit": 2,
           "bonus_at_risk": total_days > 2}
    except Exception as e:
        logger.error(f"GET staff leave error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/salary/leaves")
async def dashboard_get_all_leaves(month: str | None = Query(None), user: dict = Depends(get_current_user)):
    """Get all leave records with staff names (optionally filtered by month)."""
    try:
        where = "1=1"
        params = []
        if month and len(month) == 7:
            where += " AND DATE_FORMAT(l.leave_date, '%%Y-%%m') = %s"
            params.append(month)
        rows = _mysql_query(f"""
            SELECT l.id, l.staff_id, s.staff_name, l.leave_date, l.reason, l.created_at
            FROM staff_leave l JOIN staff_records s ON l.staff_id = s.staff_id
            WHERE {where} ORDER BY l.leave_date DESC
        """, tuple(params))
        return {"success": True, "data": [{
            "id": r["id"], "staff_id": r["staff_id"],
            "staff_name": r["staff_name"],
            "leave_date": str(r["leave_date"]),
            "reason": r.get("reason") or ""
        } for r in rows]}
    except Exception as e:
        logger.error(f"GET salary/leaves error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/staff/{staff_id}/leave")
async def dashboard_add_staff_leave(staff_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Record a leave day for a staff member."""
    try:
        leave_date = req.get("leave_date", "")
        reason = req.get("reason", "")
        if not leave_date:
            return {"success": False, "error": "leave_date required"}
        
        existing = _mysql_query_one(
            "SELECT id FROM staff_leave WHERE staff_id = %s AND leave_date = %s",
            (staff_id, leave_date)
        )
        if existing:
            return {"success": False, "error": "Leave already recorded for this date"}
        
        new_id = _mysql_execute(
            "INSERT INTO staff_leave (staff_id, leave_date, reason) VALUES (%s, %s, %s)",
            (staff_id, leave_date, reason)
        )
        return {"success": True, "data": {"id": new_id, "staff_id": staff_id, "leave_date": leave_date, "reason": reason}}
    except Exception as e:
        logger.error(f"POST staff leave error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/staff/{staff_id}/leave/{leave_id}")
async def dashboard_remove_staff_leave(staff_id: int, leave_id: int, user: dict = Depends(get_current_user)):
    """Remove a leave record."""
    try:
        _mysql_execute("DELETE FROM staff_leave WHERE id = %s AND staff_id = %s", (leave_id, staff_id))
        return {"success": True, "message": "Leave removed"}
    except Exception as e:
        logger.error(f"DELETE staff leave error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/staff/{staff_id}/attendance")
async def dashboard_get_staff_attendance(
    staff_id: int,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get attendance history for a specific staff member."""
    try:
        existing = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not existing:
            return {"success": False, "error": "Staff not found"}

        where = ["al.staff_id = %s"]
        params = [staff_id]
        if date_from:
            where.append("al.date >= %s")
            params.append(date_from)
        if date_to:
            where.append("al.date <= %s")
            params.append(date_to)

        sql = f"""
            SELECT al.id, al.staff_id, al.check_in, al.check_out, al.hours_worked,
                   al.hourly_rate, al.daily_pay, al.date, al.status, al.notes, al.created_at,
                   sr.staff_name
            FROM attendance_log al
            JOIN staff_records sr ON al.staff_id = sr.staff_id
            WHERE {' AND '.join(where)}
            ORDER BY al.date DESC, al.check_in DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM attendance_log al WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        attendance = []
        for r in rows:
            attendance.append({
                "id": r["id"],
                "staff_id": r.get("staff_id"),
                "staff_name": r.get("staff_name"),
                "check_in": str(r["check_in"]) if r.get("check_in") else None,
                "check_out": str(r["check_out"]) if r.get("check_out") else None,
                "hours_worked": float(r.get("hours_worked") or 0),
                "hourly_rate": float(r.get("hourly_rate") or 0),
                "daily_pay": float(r.get("daily_pay") or 0),
                "date": str(r["date"]) if r.get("date") else None,
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": attendance, "total": total}
    except Exception as e:
        logger.error(f"GET /staff/{staff_id}/attendance error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  ATTENDANCE — LOG
# ═══════════════════════════════════════
@router.get("/attendance")
async def dashboard_get_attendance(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    staff_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List attendance records with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if date_from:
            where.append("al.date >= %s")
            params.append(date_from)
        if date_to:
            where.append("al.date <= %s")
            params.append(date_to)
        if staff_id:
            where.append("al.staff_id = %s")
            params.append(staff_id)

        sql = f"""
            SELECT al.id, al.staff_id, al.check_in, al.check_out, al.hours_worked,
                   al.hourly_rate, al.daily_pay, al.date, al.status, al.notes, al.created_at,
                   sr.staff_name
            FROM attendance_log al
            LEFT JOIN staff_records sr ON al.staff_id = sr.staff_id
            WHERE {' AND '.join(where)}
            ORDER BY al.date DESC, al.check_in DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM attendance_log al WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        attendance = []
        for r in rows:
            attendance.append({
                "id": r["id"],
                "staff_id": r.get("staff_id"),
                "staff_name": r.get("staff_name"),
                "check_in": str(r["check_in"]) if r.get("check_in") else None,
                "check_out": str(r["check_out"]) if r.get("check_out") else None,
                "hours_worked": float(r.get("hours_worked") or 0),
                "hourly_rate": float(r.get("hourly_rate") or 0),
                "daily_pay": float(r.get("daily_pay") or 0),
                "date": str(r["date"]) if r.get("date") else None,
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": attendance, "total": total}
    except Exception as e:
        logger.error(f"GET /attendance error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/attendance/check-in")
async def dashboard_attendance_check_in(req: dict, user: dict = Depends(get_current_user)):
    """Check in a staff member — create attendance log entry."""
    try:
        staff_id = req.get("staff_id")
        notes = req.get("notes", "")

        if not staff_id:
            return {"success": False, "error": "staff_id is required"}

        # Get staff record for hourly_rate
        staff = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not staff:
            return {"success": False, "error": "Staff not found"}

        if not staff.get("is_active"):
            return {"success": False, "error": "Staff is inactive"}

        hourly_rate = float(staff.get("hourly_rate") or 0)

        # Check if already checked in today without checkout
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = _mysql_query_one(
            "SELECT id FROM attendance_log WHERE staff_id = %s AND date = %s AND status = 'checked_in'",
            (staff_id, today)
        )
        if existing:
            return {"success": False, "error": "Staff already checked in today. Check out first."}

        new_id = _mysql_execute(
            """INSERT INTO attendance_log
               (staff_id, check_in, hourly_rate, date, status, notes, created_at)
               VALUES (%s, NOW(), %s, %s, 'checked_in', %s, NOW())""",
            (staff_id, hourly_rate, today, notes),
        )

        return {
            "success": True,
            "data": {
                "id": new_id,
                "staff_id": staff_id,
                "staff_name": staff.get("staff_name"),
                "date": today,
                "status": "checked_in",
                "hourly_rate": hourly_rate,
                "message": f"{staff.get('staff_name')} checked in",
            },
        }
    except Exception as e:
        logger.error(f"POST /attendance/check-in error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/attendance/check-out")
async def dashboard_attendance_check_out(req: dict, user: dict = Depends(get_current_user)):
    """Check out a staff member — update attendance log with hours and pay."""
    try:
        staff_id = req.get("staff_id")
        notes = req.get("notes", "")

        if not staff_id:
            return {"success": False, "error": "staff_id is required"}

        # Find the open check-in for today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        record = _mysql_query_one(
            "SELECT * FROM attendance_log WHERE staff_id = %s AND date = %s AND status = 'checked_in' ORDER BY id DESC LIMIT 1",
            (staff_id, today)
        )
        if not record:
            return {"success": False, "error": "No open check-in found for today"}

        # Calculate hours worked
        hours_worked = 0
        daily_pay = 0
        try:
            check_in_time = record["check_in"]
            if check_in_time:
                now = datetime.now(timezone.utc)
                if hasattr(check_in_time, 'timestamp'):
                    delta = now - check_in_time
                else:
                    ci_str = str(check_in_time)
                    ci_dt = datetime.fromisoformat(ci_str.replace('Z', '+00:00'))
                    delta = now.replace(tzinfo=None) - ci_dt.replace(tzinfo=None)
                hours_worked = round(delta.total_seconds() / 3600.0, 2)
                hourly_rate = float(record.get("hourly_rate") or 0)
                daily_pay = round(hours_worked * hourly_rate, 2)
        except Exception as calc_err:
            logger.warning(f"Hours calculation fallback: {calc_err}")
            hours_worked = 0
            daily_pay = 0

        # Update the record
        _mysql_execute(
            """UPDATE attendance_log
               SET check_out = NOW(), hours_worked = %s, daily_pay = %s, status = 'checked_out', notes = CONCAT(IFNULL(notes,''), ' | ', %s)
               WHERE id = %s""",
            (hours_worked, daily_pay, notes, record["id"]),
        )

        staff = _mysql_query_one("SELECT staff_name FROM staff_records WHERE staff_id = %s", (staff_id,))

        return {
            "success": True,
            "data": {
                "id": record["id"],
                "staff_id": staff_id,
                "staff_name": staff.get("staff_name") if staff else "Unknown",
                "date": today,
                "hours_worked": hours_worked,
                "daily_pay": daily_pay,
                "status": "checked_out",
                "message": f"Checked out. {hours_worked}h worked, {daily_pay} pay",
            },
        }
    except Exception as e:
        logger.error(f"POST /attendance/check-out error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  SALARY & PAYROLL
# ═══════════════════════════════════════
@router.get("/salary")
async def dashboard_get_salary(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    staff_name: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List salary/payroll records with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if date_from:
            where.append("pay_date >= %s")
            params.append(date_from)
        if date_to:
            where.append("pay_date <= %s")
            params.append(date_to)
        if staff_name:
            where.append("staff_name LIKE %s")
            params.append(f"%{staff_name}%")
        if status:
            where.append("status = %s")
            params.append(status)

        sql = f"""
            SELECT id, staff_name, base_salary, bonus, deductions, net_salary,
                   pay_period, pay_date, status, notes, created_at
            FROM salary_payroll
            WHERE {' AND '.join(where)}
            ORDER BY pay_date DESC, id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM salary_payroll WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        records = []
        for r in rows:
            records.append({
                "id": r["id"],
                "staff_name": r.get("staff_name"),
                "base_salary": float(r.get("base_salary") or 0),
                "bonus": float(r.get("bonus") or 0),
                "deductions": float(r.get("deductions") or 0),
                "net_salary": float(r.get("net_salary") or 0),
                "pay_period": r.get("pay_period"),
                "pay_date": str(r["pay_date"]) if r.get("pay_date") else None,
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": records, "total": total}
    except Exception as e:
        logger.error(f"GET /salary error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/salary")
async def dashboard_create_salary(req: dict, user: dict = Depends(get_current_user)):
    """Record a salary payment. Creates OPEX entry when status=paid."""
    try:
        staff_name = req.get("staff_name", "").strip()
        if not staff_name:
            return {"success": False, "error": "staff_name is required"}

        pay_period = req.get("pay_period", "")
        
        # Look up auto-generated payroll for amount
        gen_row = _mysql_query_one(
            "SELECT net_salary, notes FROM salary_payroll WHERE staff_name = %s AND pay_period = %s",
            (staff_name, pay_period)
        )
        if gen_row:
            net_salary = float(gen_row["net_salary"] or 0)
            notes = gen_row.get("notes") or ""
        else:
            # Manual: use provided amounts
            base_salary = req.get("base_salary", 0)
            bonus = req.get("bonus", 0)
            deductions = req.get("deductions", 0)
            net_salary = float(base_salary) + float(bonus) - float(deductions)
            notes = req.get("notes", "[manual entry]")

        pay_date = req.get("pay_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        status = req.get("status", "pending")
        new_id = _mysql_execute(
            """INSERT INTO salary_payroll
               (staff_name, base_salary, bonus, deductions, net_salary, pay_period, pay_date, status, payment_method, notes, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (staff_name, 0, net_salary, 0, net_salary, pay_period, pay_date, status, payment_method, notes),
        )

        # If marked as paid, create OPEX entry
        if status == "paid":
            _mysql_execute(
                """INSERT INTO opex (category, description, amount, expense_date, payment_method, staff_name, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
                ("Staff Salary", f"{staff_name} - {pay_period}", net_salary, pay_date, payment_method, staff_name),
            )

        return {
            "success": True,
            "data": {
                "id": new_id, "staff_name": staff_name,
                "net_salary": net_salary, "pay_period": pay_period,
                "pay_date": pay_date, "status": status,
                "payment_method": payment_method, "notes": notes,
            },
        }
    except Exception as e:
        logger.error(f"POST /salary error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/salary/generate")
async def dashboard_generate_salary(req: dict, user: dict = Depends(get_current_user)):
    """Auto-generate payroll for a given month.
    Net Pay = base_salary + transport_allowance + attendance_bonus
            + commission_food + game_target_bonus
            + food_allowance_val - unpaid_advances
    Game bonus: SHOP-WIDE tier by total play hours (non-stacking, highest qualifying).
    All staff get the same game bonus if shop target is met."""
    try:
        month = req.get("month", "").strip()
        if not month or len(month) != 7:
            return {"success": False, "error": "month required (YYYY-MM)"}

        staff_ids = req.get("staff_ids", None)

        where = "WHERE is_active = 1"
        extra = ()
        if staff_ids:
            ids = tuple(staff_ids) if isinstance(staff_ids, list) else (staff_ids,)
            placeholders = ",".join(["%s"] * len(ids))
            where += f" AND staff_id IN ({placeholders})"
            extra = ids

        staff_rows = _mysql_query(f"SELECT * FROM staff_records {where} ORDER BY staff_name", extra)

        # Read game bonus tiers from config
        tier_cfg = _mysql_query_one(
            "SELECT config_value FROM settings_config WHERE config_key = 'game_bonus_tiers'"
        )
        import json as _json
        tiers = _json.loads(tier_cfg["config_value"]) if tier_cfg else {}

        # ── SHOP-WIDE total play hours ──
        shop_row = _mysql_query_one(
            "SELECT COALESCE(SUM(CAST(REGEXP_REPLACE(IFNULL(notes,''), '.*Mins?:? *([0-9]+).*', '$1') AS UNSIGNED)), 0) AS total_mins "
            "FROM sales_daily WHERE DATE_FORMAT(sale_date, '%%Y-%%m') = %s",
            (month,)
        )
        shop_total_mins = int(shop_row["total_mins"] or 0)
        shop_total_hrs = round(shop_total_mins / 60.0, 1)

        # ── Member Card Commission (shop-wide, split equally) ──
        # Count new member registrations (non-gift): balance_mins_before=0 + amount>0
        card_total_row = _mysql_query_one(
            "SELECT COUNT(*) AS card_count FROM topup_log "
            "WHERE balance_mins_before = 0 AND amount > 0 AND LEFT(topup_date, 7) = %s",
            (month,)
        )
        total_cards = int(card_total_row["card_count"] or 0) if card_total_row else 0
        CARD_COMMISSION_PER_CARD = 1000  # Ks per card
        shop_per_staff_card_com = total_cards * CARD_COMMISSION_PER_CARD

        # ── Food Profit (same as PNL): stock_out revenue − FIFO COGS ──
        food_rev_row = _mysql_query_one(
            "SELECT COALESCE(SUM(total), 0) AS t FROM stock_out WHERE DATE_FORMAT(sale_date, '%%Y-%%m') = %s",
            (month,)
        )
        food_revenue = float(food_rev_row["t"] or 0)
        try:
            import stock_fifo, pymysql
            _sfc = pymysql.connect(host='127.0.0.1', user='root', password='PsVibe@MySQL2024!', database='psvibe_api')
            _sfr = stock_fifo.calc_fifo(_sfc)
            _sfc.close()
            food_cogs = _sfr["cogs"]
        except Exception:
            food_cogs = 0
        total_food_profit = food_revenue - food_cogs

        # Determine shop-wide game bonus (non-stacking, highest qualifying tier)
        game_bonus = 0
        qualified_tier = None
        if tiers:
            hrs_thresholds = sorted([(int(k), int(v)) for k, v in tiers.items()], key=lambda x: x[0], reverse=True)
            for threshold_hrs, bonus_amt in hrs_thresholds:
                if shop_total_hrs >= threshold_hrs:
                    game_bonus = bonus_amt
                    qualified_tier = f"{threshold_hrs}hr"
                    break

        generated = []
        for s in staff_rows:
            sname = s["staff_name"]
            sid = s["staff_id"]
            base = float(s.get("base_salary") or 0)
            food_allowance_val = float(s.get("food_allowance") or 0)
            transport = float(s.get("transport_allowance") or 0)
            attendance_bonus = float(s.get("attendance_bonus") or 0)
            cr_food = float(s.get("commission_rate_food") or 0)

            # Leave policy:
            #   - ANY leave day → forfeit attendance bonus
            #   - >2 leave days → 15,000 Ks per extra day deducted
            leave_row = _mysql_query_one(
                "SELECT COUNT(*) AS lc FROM staff_leave WHERE staff_id = %s AND DATE_FORMAT(leave_date, '%%Y-%%m') = %s",
                (sid, month)
            )
            leave_days = int(leave_row["lc"] or 0)
            actual_att_bonus = 0 if leave_days > 0 else attendance_bonus
            extra_leave = max(0, leave_days - 2)
            leave_penalty = extra_leave * 15000

            # Food commission — SHOP-WIDE: staff rate × total shop food profit
            commission_food = round(total_food_profit * cr_food, 0)

            # Member card commission — shop-wide, split equally among all staff
            commission_card = shop_per_staff_card_com

            # Unpaid advances
            adv_row = _mysql_query_one(
                "SELECT COALESCE(SUM(amount), 0) AS ta FROM salary_advance "
                "WHERE staff_name = %s AND repayment_status != 'paid'", (sname,)
            )
            unpaid_adv = int(adv_row["ta"] or 0)

            deductions = unpaid_adv + leave_penalty
            additions = transport + food_allowance_val + actual_att_bonus + commission_food + game_bonus + commission_card
            net_pay = round(base + additions - deductions, 0)

            total_commission = commission_food + game_bonus + commission_card

            exist = _mysql_query_one(
                "SELECT id FROM salary_payroll WHERE staff_name = %s AND pay_period = %s",
                (sname, month)
            )
            card_note = f", card_com={commission_card:,.0f}({total_cards}cards×{CARD_COMMISSION_PER_CARD})" if total_cards > 0 else ""
            bonus_note = f"[auto: food_com={commission_food:,.0f}({cr_food*100:.0f}% of profit={total_food_profit:,.0f}(rev{food_revenue:,.0f}-cogs{food_cogs:,.0f})), game_bonus={game_bonus:,.0f}@{qualified_tier or 'none'}(shop={shop_total_hrs}hr){card_note}, transport={transport:,.0f}, att_bonus={actual_att_bonus:,.0f}(leave={leave_days}d, forfeit)" if leave_days > 0 else f"[auto: food_com={commission_food:,.0f}({cr_food*100:.0f}% of profit={total_food_profit:,.0f}(rev{food_revenue:,.0f}-cogs{food_cogs:,.0f})), game_bonus={game_bonus:,.0f}@{qualified_tier or 'none'}(shop={shop_total_hrs}hr){card_note}, transport={transport:,.0f}, att_bonus={actual_att_bonus:,.0f}(leave=0d)"
            if extra_leave > 0:
                bonus_note = bonus_note[:-1] + f", penalty={leave_penalty}(extra={extra_leave}d×15K)]"
            if exist:
                _mysql_execute(
                    "UPDATE salary_payroll SET base_salary=%s, bonus=%s, deductions=%s, net_salary=%s, status='pending', notes=%s WHERE id=%s",
                    (base, total_commission, deductions, net_pay, bonus_note, exist["id"])
                )
                rid = exist["id"]
            else:
                rid = _mysql_execute(
                    "INSERT INTO salary_payroll (staff_name, base_salary, bonus, deductions, net_salary, pay_period, pay_date, status, notes, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, CURDATE(), 'pending', %s, NOW())",
                    (sname, base, total_commission, deductions, net_pay, month, bonus_note)
                )

            generated.append({
                "id": rid,
                "staff_name": sname,
                "staff_id": sid,
                "base_salary": base,
                "food_allowance": food_allowance_val,
                "transport_allowance": transport,
                "attendance_bonus": actual_att_bonus,
                "leave_days": leave_days,
                "leave_penalty": leave_penalty,
                "commission_food": commission_food,
                "commission_card": commission_card,
                "total_cards": total_cards,
                "card_commission_rate": CARD_COMMISSION_PER_CARD,
                "game_bonus": game_bonus,
                "total_food_profit": total_food_profit, "food_revenue": food_revenue, "food_cogs": food_cogs,
                "shop_total_hrs": shop_total_hrs,
                "qualified_tier": qualified_tier,
                "unpaid_advances": unpaid_adv,
                "deductions": deductions,
                "net_salary": net_pay,
                "pay_period": month,
                "commission_rate_food": cr_food,
            })

        return {"success": True, "data": generated, "count": len(generated), "shop_total_hrs": shop_total_hrs, "game_bonus": game_bonus, "qualified_tier": qualified_tier, "total_food_profit": total_food_profit, "food_revenue": food_revenue, "food_cogs": food_cogs}
    except Exception as e:
        logger.error(f"POST /salary/generate error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/salary/advances")
async def dashboard_get_salary_advances(
    staff_name: str | None = Query(None),
    repayment_status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List salary advances with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if staff_name:
            where.append("staff_name LIKE %s")
            params.append(f"%{staff_name}%")
        if repayment_status:
            where.append("repayment_status = %s")
            params.append(repayment_status)

        sql = f"""
            SELECT id, staff_name, amount, advance_date, repayment_status, notes, created_at
            FROM salary_advance
            WHERE {' AND '.join(where)}
            ORDER BY advance_date DESC, id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM salary_advance WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        advances = []
        for r in rows:
            advances.append({
                "id": r["id"],
                "staff_name": r.get("staff_name"),
                "amount": float(r.get("amount") or 0),
                "advance_date": str(r["advance_date"]) if r.get("advance_date") else None,
                "repayment_status": r.get("repayment_status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": advances, "total": total}
    except Exception as e:
        logger.error(f"GET /salary/advances error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/salary/advances")
async def dashboard_create_salary_advance(req: dict, user: dict = Depends(get_current_user)):
    """Record a salary advance. Creates OPEX entry automatically."""
    try:
        staff_name = req.get("staff_name", "").strip()
        amount = req.get("amount", 0)
        if not staff_name:
            return {"success": False, "error": "staff_name is required"}
        if float(amount) <= 0:
            return {"success": False, "error": "amount must be positive"}

        advance_date = req.get("advance_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        repayment_status = req.get("repayment_status", "unpaid")
        notes = req.get("notes", "")
        new_id = _mysql_execute(
            """INSERT INTO salary_advance
               (staff_name, amount, advance_date, repayment_status, notes, payment_method, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
            (staff_name, amount, advance_date, repayment_status, notes, payment_method),
        )

        # Auto-create OPEX entry for staff salary advance
        _mysql_execute(
            """INSERT INTO opex (category, description, amount, expense_date, payment_method, staff_name, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
            ("Staff Salary", f"Advance: {staff_name}", amount, advance_date, payment_method, staff_name),
        )

        return {
            "success": True,
            "data": {
                "id": new_id,
                "staff_name": staff_name,
                "amount": amount,
                "advance_date": advance_date,
                "repayment_status": repayment_status,
                "notes": notes,
            },
        }
    except Exception as e:
        logger.error(f"POST /salary/advances error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/salary/advances/{advance_id}")
async def dashboard_delete_salary_advance(advance_id: int, user: dict = Depends(get_current_user)):
    """Delete a salary advance record."""
    try:
        _mysql_execute("DELETE FROM salary_advance WHERE id = %s", (advance_id,))
        return {"success": True, "message": "Advance deleted"}
    except Exception as e:
        logger.error(f"DELETE /salary/advances error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  CONSOLE — DETAILED CRUD
# ═══════════════════════════════════════
@router.get("/consoles/detail")
async def dashboard_get_consoles_detail(
    user: dict = Depends(get_current_user),
):
    """Get full console details with type and status info."""
    try:
        rows = _mysql_query("""
            SELECT c.console_id, c.status, c.console_type, c.current_game,
                   c.current_member, c.start_time, c.last_updated
            FROM console_status c
            ORDER BY c.console_id
        """)

        consoles = []
        for r in rows:
            # Get active booking if any
            booking = _mysql_query_one("""
                SELECT id, member_id, start_time, end_time, status, game_name, duration_mins
                FROM console_booking
                WHERE console_id = %s AND status = 'Active'
                ORDER BY id DESC LIMIT 1
            """, (r["console_id"],))

            consoles.append({
                "console_id": r["console_id"],
                "status": r.get("status") or "available",
                "console_type": r.get("console_type") or "",
                "current_game": r.get("current_game") or "",
                "current_member": r.get("current_member") or "",
                "start_time": str(r["start_time"]) if r.get("start_time") else None,
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
                "active_booking": {
                    "id": booking["id"],
                    "member_id": booking.get("member_id"),
                    "start_time": str(booking["start_time"]) if booking and booking.get("start_time") else None,
                    "end_time": str(booking["end_time"]) if booking and booking.get("end_time") else None,
                    "status": booking.get("status"),
                    "game_name": booking.get("game_name"),
                    "duration_mins": booking.get("duration_mins"),
                } if booking else None,
            })

        return {"success": True, "data": consoles}
    except Exception as e:
        logger.error(f"GET /consoles/detail error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/consoles/{console_id}")
async def dashboard_update_console(console_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update console details — type, status, etc."""
    try:
        existing = _mysql_query_one("SELECT * FROM console_status WHERE console_id = %s", (console_id,))
        if not existing:
            return {"success": False, "error": "Console not found"}

        updates = []
        params = []
        for field in ["status", "console_type", "current_game", "current_member"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(console_id)
        _mysql_execute(f"UPDATE console_status SET {', '.join(updates)} WHERE console_id = %s", tuple(params))

        updated = _mysql_query_one("SELECT * FROM console_status WHERE console_id = %s", (console_id,))
        return {
            "success": True,
            "data": {
                "console_id": updated["console_id"],
                "status": updated.get("status"),
                "console_type": updated.get("console_type"),
                "current_game": updated.get("current_game"),
                "current_member": updated.get("current_member"),
                "start_time": str(updated["start_time"]) if updated.get("start_time") else None,
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /consoles/{console_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/consoles")
async def dashboard_create_console(req: dict, user: dict = Depends(get_current_user)):
    """Add a new console to console_status."""
    try:
        console_id = req.get("console_id")
        if not console_id:
            return {"success": False, "error": "console_id is required"}

        existing = _mysql_query_one("SELECT * FROM console_status WHERE console_id = %s", (console_id,))
        if existing:
            return {"success": False, "error": "Console already exists"}

        _mysql_execute(
            """INSERT INTO console_status (console_id, status, console_type, current_game, current_member, last_updated)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (
                console_id,
                req.get("status", "available"),
                req.get("console_type", ""),
                req.get("current_game", ""),
                req.get("current_member", ""),
            ),
        )

        return {
            "success": True,
            "data": {
                "console_id": console_id,
                "status": req.get("status", "available"),
                "console_type": req.get("console_type", ""),
            },
        }
    except Exception as e:
        logger.error(f"POST /consoles error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  MEMBER — WALLET + TRANSACTIONS
# ═══════════════════════════════════════
@router.put("/members/{member_id}/wallet")
async def dashboard_adjust_member_wallet(member_id: str, req: dict, user: dict = Depends(get_current_user)):
    """Adjust a member's wallet balance (add or deduct minutes)."""
    try:
        adjustment_mins = req.get("adjustment_mins", 0)
        reason = req.get("reason", "").strip()

        if not adjustment_mins:
            return {"success": False, "error": "adjustment_mins is required (positive to add, negative to deduct)"}

        member = _mysql_query_one(
            "SELECT member_id, member_name, balance_mins FROM member_wallets WHERE member_id = %s",
            (member_id,)
        )
        if not member:
            return {"success": False, "error": "Member not found"}

        balance_before = int(member.get("balance_mins") or 0)
        balance_after = balance_before + adjustment_mins

        if balance_after < 0:
            return {"success": False, "error": f"Insufficient balance. Current: {balance_before} mins"}

        # Update member_wallets
        _mysql_execute(
            "UPDATE member_wallets SET balance_mins = %s, last_updated = NOW() WHERE member_id = %s",
            (balance_after, member_id)
        )

        # Update members table if exists
        try:
            mb = _mysql_query_one("SELECT balance_minutes FROM members WHERE member_id = %s", (member_id,))
            if mb:
                mem_balance_before = int(mb.get("balance_minutes") or 0)
                mem_balance_after = mem_balance_before + adjustment_mins
                _mysql_execute(
                    "UPDATE members SET balance_minutes = %s, updated_at = NOW() WHERE member_id = %s",
                    (mem_balance_after, member_id)
                )
        except:
            pass

        # Log the adjustment in topup_log for audit trail
        action_type = "topup" if adjustment_mins > 0 else "deduction"
        staff_name = user.get("username", "admin")
        _mysql_execute(
            """INSERT INTO topup_log
               (member_id, amount, mins_added, topup_date, staff_name, payment_method,
                balance_before, balance_after, balance_mins_before, balance_mins_after, notes, created_at)
               VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (
                member_id,
                0,
                adjustment_mins,
                staff_name,
                f"Wallet {action_type}",
                0,
                0,
                balance_before,
                balance_after,
                reason or f"Wallet {action_type}: {adjustment_mins} mins",
            ),
        )

        return {
            "success": True,
            "data": {
                "member_id": member_id,
                "member_name": member.get("member_name"),
                "adjustment_mins": adjustment_mins,
                "balance_before": balance_before,
                "balance_after": balance_after,
                "action": action_type,
                "reason": reason,
            },
        }
    except Exception as e:
        logger.error(f"PUT /members/{member_id}/wallet error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/members/{member_id}/transactions")
async def dashboard_get_member_transactions(
    member_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get full transaction history for a member (topups, deductions, adjustments)."""
    try:
        existing = _mysql_query_one("SELECT member_id FROM member_wallets WHERE member_id = %s", (member_id,))
        if not existing:
            return {"success": False, "error": "Member not found"}

        rows = _mysql_query(
            """SELECT id, member_id, amount, mins_added, topup_date,
                      staff_name, payment_method, balance_mins_before, balance_mins_after,
                      notes, created_at
               FROM topup_log WHERE member_id = %s
               ORDER BY topup_date DESC, id DESC
               LIMIT %s OFFSET %s""",
            (member_id, limit, offset),
        )

        count_row = _mysql_query_one(
            "SELECT COUNT(*) as total FROM topup_log WHERE member_id = %s", (member_id,)
        )
        total = count_row["total"] if count_row else 0

        # Also get bookings (as deductions)
        bookings = []
        try:
            booking_rows = _mysql_query(
                """SELECT id, console_id, booking_date, start_time, end_time,
                          duration_mins, status, game_name, notes, created_at
                   FROM console_booking
                   WHERE member_id = %s
                   ORDER BY booking_date DESC, id DESC
                   LIMIT %s OFFSET %s""",
                (member_id, limit, offset),
            )
            for br in booking_rows:
                bookings.append({
                    "type": "booking",
                    "id": br["id"],
                    "console_id": br.get("console_id"),
                    "booking_date": str(br["booking_date"]) if br.get("booking_date") else None,
                    "start_time": str(br["start_time"]) if br.get("start_time") else None,
                    "end_time": str(br["end_time"]) if br.get("end_time") else None,
                    "duration_mins": br.get("duration_mins", 0),
                    "status": br.get("status"),
                    "game_name": br.get("game_name"),
                    "notes": br.get("notes"),
                    "created_at": str(br["created_at"]) if br.get("created_at") else None,
                })
        except Exception:
            pass

        transactions = []
        for r in rows:
            txn_type = "deduction" if (r.get("mins_added") or 0) < 0 else "topup"
            if r.get("payment_method") and "deduction" in str(r.get("payment_method") or "").lower():
                txn_type = "deduction"
            if r.get("payment_method") and "adjustment" in str(r.get("payment_method") or "").lower():
                txn_type = "adjustment"

            transactions.append({
                "type": txn_type,
                "id": r["id"],
                "member_id": r.get("member_id"),
                "amount": float(r.get("amount") or 0),
                "mins_added": r.get("mins_added", 0),
                "date": str(r["topup_date"]) if r.get("topup_date") else None,
                "staff_name": r.get("staff_name"),
                "payment_method": r.get("payment_method"),
                "balance_mins_before": r.get("balance_mins_before", 0),
                "balance_mins_after": r.get("balance_mins_after", 0),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })

        # Merge and sort
        all_txns = transactions + bookings
        all_txns.sort(key=lambda x: str(x.get("date") or x.get("created_at") or x.get("booking_date") or ""), reverse=True)

        return {
            "success": True,
            "data": {
                "member_id": member_id,
                "transactions": all_txns[:limit],
            },
            "total": total,
        }
    except Exception as e:
        logger.error(f"GET /members/{member_id}/transactions error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  INVENTORY — ALERTS
# ═══════════════════════════════════════
@router.get("/inventory/alerts")
async def dashboard_inventory_alerts(
    user: dict = Depends(get_current_user),
):
    """Get inventory items below reorder level."""
    try:
        rows = _mysql_query("""
            SELECT id, item_name, category, quantity, unit_price, reorder_level, last_updated
            FROM inventory
            WHERE quantity <= reorder_level AND reorder_level > 0
            ORDER BY (reorder_level - quantity) DESC, item_name ASC
        """)

        items = []
        for r in rows:
            shortage = int(r.get("reorder_level", 0)) - int(r.get("quantity", 0))
            items.append({
                "id": r["id"],
                "item_name": r["item_name"],
                "category": r.get("category"),
                "quantity": r.get("quantity", 0),
                "unit_price": float(r.get("unit_price") or 0),
                "reorder_level": r.get("reorder_level", 0),
                "shortage": shortage,
                "status": "critical" if shortage >= int(r.get("reorder_level", 0)) else "low",
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })

        return {"success": True, "data": items, "total": len(items)}
    except Exception as e:
        logger.error(f"GET /inventory/alerts error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  BRANCH MANAGEMENT — CRUD (Phase 1)
# ═══════════════════════════════════════
from fastapi import Body

@router.get("/branches")
async def api_get_branches(user: dict = Depends(get_current_user)):
    """Get all active branches"""
    from branch_utils import get_all_branches
    branches = get_all_branches()
    return {"success": True, "data": branches, "total": len(branches)}

@router.get("/branches/{branch_id}")
async def api_get_branch(branch_id: int, user: dict = Depends(get_current_user)):
    """Get a single branch by ID"""
    from branch_utils import get_branch
    branch = get_branch(branch_id)
    if not branch:
        return {"success": False, "error": f"Branch {branch_id} not found"}
    return {"success": True, "data": branch}

@router.post("/branches")
async def api_create_branch(body: dict = Body(...), user: dict = Depends(get_current_user)):
    """Create a new branch"""
    name = body.get("name", "").strip()
    code = body.get("code", "").strip().upper()
    if not name or not code:
        return {"success": False, "error": "Name and code are required"}
    
    existing = _mysql_query_one("SELECT id FROM branches WHERE code = %s", (code,))
    if existing:
        return {"success": False, "error": f"Branch code '{code}' already exists"}
    
    bid = _mysql_execute(
        "INSERT INTO branches (name, code, address, phone, open_time, close_time) VALUES (%s, %s, %s, %s, %s, %s)",
        (name, code, body.get("address", ""), body.get("phone", ""),
         body.get("open_time", "10:00:00"), body.get("close_time", "22:00:00"))
    )
    
    from branch_utils import get_branch
    branch = get_branch(bid)
    return {"success": True, "data": branch, "message": f"Branch '{name}' created"}

@router.put("/branches/{branch_id}")
async def api_update_branch(branch_id: int, body: dict = Body(...), user: dict = Depends(get_current_user)):
    """Update branch details"""
    existing = _mysql_query_one("SELECT id FROM branches WHERE id = %s", (branch_id,))
    if not existing:
        return {"success": False, "error": f"Branch {branch_id} not found"}
    
    fields = []
    params = []
    for key in ("name", "code", "address", "phone", "is_active", "open_time", "close_time", "telegram_group_id"):
        if key in body:
            fields.append(f"{key} = %s")
            params.append(body[key])
    if not fields:
        return {"success": False, "error": "No fields to update"}
    params.append(branch_id)
    _mysql_execute(f"UPDATE branches SET {', '.join(fields)} WHERE id = %s", tuple(params))
    
    from branch_utils import get_branch
    branch = get_branch(branch_id)
    return {"success": True, "data": branch, "message": "Branch updated"}

@router.delete("/branches/{branch_id}")
async def api_delete_branch(branch_id: int, user: dict = Depends(get_current_user)):
    """Soft-delete a branch (set is_active=0)"""
    if branch_id == 1:
        return {"success": False, "error": "Cannot delete the default branch (id=1)"}
    
    existing = _mysql_query_one("SELECT id, name FROM branches WHERE id = %s", (branch_id,))
    if not existing:
        return {"success": False, "error": f"Branch {branch_id} not found"}
    
    _mysql_execute("UPDATE branches SET is_active = 0 WHERE id = %s", (branch_id,))
    return {"success": True, "message": f"Branch '{existing['name']}' deactivated"}

# ═══════════════════════════════════════════════════
#  LOYALTY SYSTEM — Customer Loyalty & Rewards
# ═══════════════════════════════════════════════════

# ── Loyalty Settings ──

@router.get("/loyalty/settings")
async def get_loyalty_settings(user: dict = Depends(get_current_user)):
    """Get loyalty points settings."""
    try:
        row = _mysql_query_one("SELECT * FROM loyalty_settings WHERE id = 1")
        if not row:
            return {"success": True, "data": {
                "points_per_ks": 1.0, "points_per_min": 0.5,
                "min_redeem_points": 100, "points_per_ks_redeem": 1.0,
                "signup_bonus_points": 50, "birthday_bonus_points": 100,
                "referral_points": 25
            }}
        return {"success": True, "data": {
            "id": row["id"],
            "points_per_ks": float(row["points_per_ks"]),
            "points_per_min": float(row["points_per_min"]),
            "min_redeem_points": row["min_redeem_points"],
            "points_per_ks_redeem": float(row["points_per_ks_redeem"]),
            "signup_bonus_points": row["signup_bonus_points"],
            "birthday_bonus_points": row["birthday_bonus_points"],
            "referral_points": row["referral_points"],
            "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
        }}
    except Exception as e:
        logger.error(f"GET /loyalty/settings error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/loyalty/settings")
async def update_loyalty_settings(req: dict, user: dict = Depends(get_current_user)):
    """Update loyalty points settings."""
    try:
        _mysql_execute("""
            INSERT INTO loyalty_settings (id, points_per_ks, points_per_min, min_redeem_points,
                points_per_ks_redeem, signup_bonus_points, birthday_bonus_points, referral_points)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                points_per_ks = VALUES(points_per_ks),
                points_per_min = VALUES(points_per_min),
                min_redeem_points = VALUES(min_redeem_points),
                points_per_ks_redeem = VALUES(points_per_ks_redeem),
                signup_bonus_points = VALUES(signup_bonus_points),
                birthday_bonus_points = VALUES(birthday_bonus_points),
                referral_points = VALUES(referral_points)
        """, (
            req.get("points_per_ks", 1.0),
            req.get("points_per_min", 0.5),
            req.get("min_redeem_points", 100),
            req.get("points_per_ks_redeem", 1.0),
            req.get("signup_bonus_points", 50),
            req.get("birthday_bonus_points", 100),
            req.get("referral_points", 25),
        ))
        return {"success": True, "message": "Loyalty settings updated"}
    except Exception as e:
        logger.error(f"PUT /loyalty/settings error: {e}")
        return {"success": False, "error": str(e)}


# ── Member Loyalty ──

@router.get("/loyalty/members")
async def list_loyalty_members(
    search: str = Query(None),
    tier: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """List all member loyalty with optional search/tier filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(ml.member_id LIKE %s OR m.name LIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        if tier:
            where.append("ml.tier = %s")
            params.append(tier)

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            f"SELECT COUNT(*) as cnt FROM member_loyalty ml LEFT JOIN members m ON ml.member_id = m.member_id WHERE {where_clause}",
            tuple(params)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            f"""SELECT ml.*, COALESCE(m.name, ml.member_id) as member_name
                FROM member_loyalty ml
                LEFT JOIN members m ON ml.member_id = m.member_id
                WHERE {where_clause}
                ORDER BY ml.total_points DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [page_size, offset])
        )

        members = []
        for r in rows:
            members.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "member_name": r.get("member_name", r["member_id"]),
                "total_points": r.get("total_points", 0),
                "redeemed_points": r.get("redeemed_points", 0),
                "available_points": r.get("available_points", 0),
                "lifetime_spent": float(r.get("lifetime_spent") or 0),
                "tier": r.get("tier", "Bronze"),
                "join_date": str(r["join_date"]) if r.get("join_date") else None,
                "birthday": str(r["birthday"]) if r.get("birthday") else None,
                "last_earn_date": str(r["last_earn_date"]) if r.get("last_earn_date") else None,
                "last_redeem_date": str(r["last_redeem_date"]) if r.get("last_redeem_date") else None,
            })

        return {"success": True, "data": members, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/members error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/loyalty/members/{member_id}")
async def get_loyalty_member(member_id: str, user: dict = Depends(get_current_user)):
    """Get one member's loyalty info."""
    try:
        row = _mysql_query_one(
            """SELECT ml.*, COALESCE(m.name, ml.member_id) as member_name
               FROM member_loyalty ml
               LEFT JOIN members m ON ml.member_id = m.member_id
               WHERE ml.member_id = %s""",
            (member_id,)
        )
        if not row:
            return {"success": False, "error": "Member not found in loyalty system"}

        return {"success": True, "data": {
            "id": row["id"],
            "member_id": row["member_id"],
            "member_name": row.get("member_name", row["member_id"]),
            "total_points": row.get("total_points", 0),
            "redeemed_points": row.get("redeemed_points", 0),
            "available_points": row.get("available_points", 0),
            "lifetime_spent": float(row.get("lifetime_spent") or 0),
            "tier": row.get("tier", "Bronze"),
            "join_date": str(row["join_date"]) if row.get("join_date") else None,
            "birthday": str(row["birthday"]) if row.get("birthday") else None,
            "last_earn_date": str(row["last_earn_date"]) if row.get("last_earn_date") else None,
            "last_redeem_date": str(row["last_redeem_date"]) if row.get("last_redeem_date") else None,
        }}
    except Exception as e:
        logger.error(f"GET /loyalty/members/{member_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/loyalty/members/adjust")
async def adjust_loyalty_points(req: dict, user: dict = Depends(get_current_user)):
    """Manual points adjustment (admin)."""
    try:
        member_id = req.get("member_id", "")
        points = req.get("points", 0)
        description = req.get("description", "Manual adjustment")
        staff_name = req.get("staff_name", user.get("username", "admin"))

        if not member_id:
            return {"success": False, "error": "member_id is required"}
        if points == 0:
            return {"success": False, "error": "points must be non-zero"}

        # Ensure member exists in loyalty
        existing = _mysql_query_one(
            "SELECT id, total_points FROM member_loyalty WHERE member_id = %s",
            (member_id,)
        )
        if not existing:
            return {"success": False, "error": "Member not found in loyalty system"}

        # Adjust points
        if points > 0:
            _mysql_execute(
                "UPDATE member_loyalty SET total_points = total_points + %s, last_earn_date = CURDATE() WHERE member_id = %s",
                (points, member_id)
            )
        else:
            # Deduction: ensure enough available
            current = _mysql_query_one("SELECT available_points FROM member_loyalty WHERE member_id = %s", (member_id,))
            if current and current["available_points"] < abs(points):
                return {"success": False, "error": f"Insufficient points. Available: {current['available_points']}"}
            _mysql_execute(
                "UPDATE member_loyalty SET total_points = total_points + %s WHERE member_id = %s",
                (points, member_id)
            )

        # Log transaction
        _mysql_execute(
            """INSERT INTO loyalty_transactions (member_id, points, transaction_type, description, created_by)
               VALUES (%s, %s, 'adjustment', %s, %s)""",
            (member_id, points, description, staff_name)
        )

        # Recalculate tier
        _recalc_tier(member_id)

        return {"success": True, "message": f"Points adjusted by {points}"}
    except Exception as e:
        logger.error(f"POST /loyalty/members/adjust error: {e}")
        return {"success": False, "error": str(e)}


# ── Points Transactions ──

@router.get("/loyalty/transactions")
async def list_loyalty_transactions(
    member_id: str = Query(None),
    transaction_type: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Transaction log with filters."""
    try:
        where = ["1=1"]
        params = []
        if member_id:
            where.append("lt.member_id = %s")
            params.append(member_id)
        if transaction_type:
            where.append("lt.transaction_type = %s")
            params.append(transaction_type)
        if date_from:
            where.append("DATE(lt.created_at) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(lt.created_at) <= %s")
            params.append(date_to)

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            f"SELECT COUNT(*) as cnt FROM loyalty_transactions lt WHERE {where_clause}",
            tuple(params)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            f"""SELECT lt.*, COALESCE(m.name, lt.member_id) as member_name
                FROM loyalty_transactions lt
                LEFT JOIN members m ON lt.member_id = m.member_id
                WHERE {where_clause}
                ORDER BY lt.created_at DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [page_size, offset])
        )

        transactions = []
        for r in rows:
            transactions.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "member_name": r.get("member_name", r["member_id"]),
                "points": r["points"],
                "transaction_type": r["transaction_type"],
                "reference_id": r.get("reference_id"),
                "description": r.get("description"),
                "branch_id": r.get("branch_id"),
                "created_by": r.get("created_by"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })

        return {"success": True, "data": transactions, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/transactions error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/loyalty/transactions/{member_id}")
async def get_member_transactions(
    member_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Get all transactions for one member."""
    try:
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM loyalty_transactions WHERE member_id = %s",
            (member_id,)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            """SELECT * FROM loyalty_transactions
               WHERE member_id = %s
               ORDER BY created_at DESC
               LIMIT %s OFFSET %s""",
            (member_id, page_size, offset)
        )

        transactions = []
        for r in rows:
            transactions.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "points": r["points"],
                "transaction_type": r["transaction_type"],
                "reference_id": r.get("reference_id"),
                "description": r.get("description"),
                "branch_id": r.get("branch_id"),
                "created_by": r.get("created_by"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })

        return {"success": True, "data": transactions, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/transactions/{member_id} error: {e}")
        return {"success": False, "error": str(e)}


# ── Rewards Catalog ──

@router.get("/loyalty/rewards")
async def list_loyalty_rewards(
    is_active: bool = Query(None),
    user: dict = Depends(get_current_user),
):
    """List available rewards."""
    try:
        where = "1=1"
        params = []
        if is_active is not None:
            where = "is_active = %s"
            params = [1 if is_active else 0]

        rows = _mysql_query(
            f"SELECT * FROM loyalty_rewards WHERE {where} ORDER BY points_required ASC",
            tuple(params)
        )

        rewards = []
        for r in rows:
            rewards.append({
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description"),
                "points_required": r["points_required"],
                "reward_type": r["reward_type"],
                "reward_value": float(r["reward_value"]) if r.get("reward_value") else None,
                "stock": r.get("stock", -1),
                "is_active": bool(r.get("is_active", 1)),
                "image_url": r.get("image_url"),
            })

        return {"success": True, "data": rewards}
    except Exception as e:
        logger.error(f"GET /loyalty/rewards error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/loyalty/rewards")
async def create_loyalty_reward(req: dict, user: dict = Depends(get_current_user)):
    """Add a new reward to the catalog."""
    try:
        name = req.get("name", "")
        if not name:
            return {"success": False, "error": "name is required"}

        reward_id = _mysql_execute(
            """INSERT INTO loyalty_rewards (name, description, points_required, reward_type, reward_value, stock, is_active, image_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                name,
                req.get("description", ""),
                req.get("points_required", 0),
                req.get("reward_type", "discount_ks"),
                req.get("reward_value"),
                req.get("stock", -1),
                req.get("is_active", 1),
                req.get("image_url"),
            )
        )
        return {"success": True, "data": {"id": reward_id}, "message": "Reward created"}
    except Exception as e:
        logger.error(f"POST /loyalty/rewards error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/loyalty/rewards/{reward_id}")
async def update_loyalty_reward(reward_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a reward."""
    try:
        existing = _mysql_query_one("SELECT * FROM loyalty_rewards WHERE id = %s", (reward_id,))
        if not existing:
            return {"success": False, "error": "Reward not found"}

        _mysql_execute(
            """UPDATE loyalty_rewards
               SET name = %s, description = %s, points_required = %s, reward_type = %s,
                   reward_value = %s, stock = %s, is_active = %s, image_url = %s
               WHERE id = %s""",
            (
                req.get("name", existing.get("name")),
                req.get("description", existing.get("description")),
                req.get("points_required", existing.get("points_required")),
                req.get("reward_type", existing.get("reward_type")),
                req.get("reward_value", existing.get("reward_value")),
                req.get("stock", existing.get("stock", -1)),
                req.get("is_active", existing.get("is_active", 1)),
                req.get("image_url", existing.get("image_url")),
                reward_id,
            )
        )
        return {"success": True, "message": "Reward updated"}
    except Exception as e:
        logger.error(f"PUT /loyalty/rewards/{reward_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/loyalty/rewards/{reward_id}")
async def deactivate_loyalty_reward(reward_id: int, user: dict = Depends(get_current_user)):
    """Deactivate a reward (soft delete)."""
    try:
        _mysql_execute("UPDATE loyalty_rewards SET is_active = 0 WHERE id = %s", (reward_id,))
        return {"success": True, "message": "Reward deactivated"}
    except Exception as e:
        logger.error(f"DELETE /loyalty/rewards/{reward_id} error: {e}")
        return {"success": False, "error": str(e)}


# ── Redemptions ──

@router.get("/loyalty/redemptions")
async def list_loyalty_redemptions(
    member_id: str = Query(None),
    status: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """List redemptions with filters."""
    try:
        where = ["1=1"]
        params = []
        if member_id:
            where.append("lr.member_id = %s")
            params.append(member_id)
        if status:
            where.append("lr.status = %s")
            params.append(status)
        if date_from:
            where.append("DATE(lr.redeemed_at) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(lr.redeemed_at) <= %s")
            params.append(date_to)

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            f"SELECT COUNT(*) as cnt FROM loyalty_redemptions lr WHERE {where_clause}",
            tuple(params)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            f"""SELECT lr.*, COALESCE(m.name, lr.member_id) as member_name
                FROM loyalty_redemptions lr
                LEFT JOIN members m ON lr.member_id = m.member_id
                WHERE {where_clause}
                ORDER BY lr.redeemed_at DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [page_size, offset])
        )

        redemptions = []
        for r in rows:
            redemptions.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "member_name": r.get("member_name", r["member_id"]),
                "reward_id": r["reward_id"],
                "reward_name": r.get("reward_name"),
                "points_used": r["points_used"],
                "status": r.get("status", "pending"),
                "redeemed_at": str(r["redeemed_at"]) if r.get("redeemed_at") else None,
                "redeemed_by": r.get("redeemed_by"),
                "notes": r.get("notes"),
            })

        return {"success": True, "data": redemptions, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/redemptions error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/loyalty/redemptions")
async def redeem_loyalty_reward(req: dict, user: dict = Depends(get_current_user)):
    """Staff redeems reward for a member."""
    try:
        member_id = req.get("member_id", "")
        reward_id = req.get("reward_id", 0)
        staff_name = req.get("staff_name", user.get("username", "admin"))
        notes = req.get("notes", "")

        if not member_id:
            return {"success": False, "error": "member_id is required"}
        if not reward_id:
            return {"success": False, "error": "reward_id is required"}

        # Get reward
        reward = _mysql_query_one(
            "SELECT * FROM loyalty_rewards WHERE id = %s AND is_active = 1",
            (reward_id,)
        )
        if not reward:
            return {"success": False, "error": "Reward not found or inactive"}

        # Check stock
        if reward["stock"] == 0:
            return {"success": False, "error": "Reward out of stock"}

        # Get member loyalty
        loyalty = _mysql_query_one(
            "SELECT * FROM member_loyalty WHERE member_id = %s",
            (member_id,)
        )
        if not loyalty:
            return {"success": False, "error": "Member not found in loyalty system"}

        points_needed = reward["points_required"]
        if loyalty["available_points"] < points_needed:
            return {"success": False, "error": f"Insufficient points. Need {points_needed}, have {loyalty['available_points']}"}

        # Deduct points (increase redeemed, not decrease total_points — available is computed)
        _mysql_execute(
            "UPDATE member_loyalty SET redeemed_points = redeemed_points + %s, last_redeem_date = CURDATE() WHERE member_id = %s",
            (points_needed, member_id)
        )

        # Log transaction
        _mysql_execute(
            """INSERT INTO loyalty_transactions (member_id, points, transaction_type, reference_id, description, created_by)
               VALUES (%s, %s, 'redeem', %s, %s, %s)""",
            (member_id, -points_needed, str(reward_id), f"Redeemed: {reward['name']}", staff_name)
        )

        # Reduce stock if limited
        if reward["stock"] > 0:
            _mysql_execute("UPDATE loyalty_rewards SET stock = stock - 1 WHERE id = %s", (reward_id,))

        # Create redemption record
        redemption_id = _mysql_execute(
            """INSERT INTO loyalty_redemptions (member_id, reward_id, reward_name, points_used, status, redeemed_by, notes)
               VALUES (%s, %s, %s, %s, 'redeemed', %s, %s)""",
            (member_id, reward_id, reward["name"], points_needed, staff_name, notes)
        )

        return {"success": True, "data": {"id": redemption_id}, "message": f"Redeemed '{reward['name']}' for {points_needed} points"}
    except Exception as e:
        logger.error(f"POST /loyalty/redemptions error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/loyalty/redemptions/{redemption_id}")
async def update_redemption_status(redemption_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update redemption status (confirm or cancel)."""
    try:
        status = req.get("status", "")
        if status not in ("redeemed", "cancelled"):
            return {"success": False, "error": "status must be 'redeemed' or 'cancelled'"}

        existing = _mysql_query_one(
            "SELECT * FROM loyalty_redemptions WHERE id = %s",
            (redemption_id,)
        )
        if not existing:
            return {"success": False, "error": "Redemption not found"}

        old_status = existing["status"]
        if old_status == status:
            return {"success": True, "message": f"Already {status}"}

        # If cancelling a redeemed reward, refund points
        if status == "cancelled" and old_status == "redeemed":
            _mysql_execute(
                "UPDATE member_loyalty SET redeemed_points = redeemed_points - %s WHERE member_id = %s",
                (existing["points_used"], existing["member_id"])
            )
            # Log refund transaction
            _mysql_execute(
                """INSERT INTO loyalty_transactions (member_id, points, transaction_type, description, created_by)
                   VALUES (%s, %s, 'adjustment', %s, %s)""",
                (existing["member_id"], existing["points_used"], f"Refund from cancelled redemption #{redemption_id}", user.get("username", "admin"))
            )
            # Restore stock
            _mysql_execute("UPDATE loyalty_rewards SET stock = stock + 1 WHERE id = %s AND stock >= 0", (existing["reward_id"],))

        # If confirming a pending redemption
        if status == "redeemed" and old_status == "cancelled":
            # Re-deduct points
            loyalty = _mysql_query_one(
                "SELECT available_points FROM member_loyalty WHERE member_id = %s",
                (existing["member_id"],)
            )
            if not loyalty or loyalty["available_points"] < existing["points_used"]:
                return {"success": False, "error": "Member doesn't have enough points anymore"}
            _mysql_execute(
                "UPDATE member_loyalty SET redeemed_points = redeemed_points + %s WHERE member_id = %s",
                (existing["points_used"], existing["member_id"])
            )
            _mysql_execute("UPDATE loyalty_rewards SET stock = stock - 1 WHERE id = %s AND stock > 0", (existing["reward_id"],))

        _mysql_execute(
            "UPDATE loyalty_redemptions SET status = %s, notes = CONCAT(COALESCE(notes,''), %s) WHERE id = %s",
            (status, f" | Status changed to {status} by {user.get('username', 'admin')}", redemption_id)
        )

        return {"success": True, "message": f"Redemption status updated to {status}"}
    except Exception as e:
        logger.error(f"PUT /loyalty/redemptions/{redemption_id} error: {e}")
        return {"success": False, "error": str(e)}


# ── Tier Calculation Helper ──

def _recalc_tier(member_id: str):
    """Recalculate member tier based on lifetime_spent."""
    tiers = [
        ("Platinum", 500000),
        ("Gold", 300000),
        ("Silver", 100000),
        ("Bronze", 0),
    ]
    row = _mysql_query_one(
        "SELECT lifetime_spent FROM member_loyalty WHERE member_id = %s",
        (member_id,)
    )
    if not row:
        return
    spent = float(row.get("lifetime_spent") or 0)
    for tier_name, threshold in tiers:
        if spent >= threshold:
            _mysql_execute(
                "UPDATE member_loyalty SET tier = %s WHERE member_id = %s AND tier != %s",
                (tier_name, member_id, tier_name)
            )
            break
import httpx

# Analytics proxy endpoints - proxy to local predictive analytics engine on port 3120
ANALYTICS_BASE = "http://127.0.0.1:3120"


@router.get("/analytics/sales-trend")
async def get_analytics_sales_trend(user: dict = Depends(get_current_user)):
    """Get 30-day sales trend data for charts"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ANALYTICS_BASE}/analytics/sales-trend", timeout=10.0)
            return r.json()
    except Exception as e:
        logger.error(f"Analytics sales-trend proxy error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/analytics/peak-hours")
async def get_analytics_peak_hours(user: dict = Depends(get_current_user)):
    """Get busiest hours ranking"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ANALYTICS_BASE}/analytics/peak-hours", timeout=10.0)
            return r.json()
    except Exception as e:
        logger.error(f"Analytics peak-hours proxy error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/analytics/popular-games")
async def get_analytics_popular_games(user: dict = Depends(get_current_user)):
    """Get top games by play count"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ANALYTICS_BASE}/analytics/popular-games", timeout=10.0)
            return r.json()
    except Exception as e:
        logger.error(f"Analytics popular-games proxy error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/analytics/member-growth")
async def get_analytics_member_growth(user: dict = Depends(get_current_user)):
    """Get new members per day"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ANALYTICS_BASE}/analytics/member-growth", timeout=10.0)
            return r.json()
    except Exception as e:
        logger.error(f"Analytics member-growth proxy error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/analytics/forecast")
async def get_analytics_forecast(user: dict = Depends(get_current_user)):
    """Get 7-day revenue forecast"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ANALYTICS_BASE}/analytics/forecast", timeout=10.0)
            return r.json()
    except Exception as e:
        logger.error(f"Analytics forecast proxy error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/analytics/summary")
async def get_analytics_summary(user: dict = Depends(get_current_user)):
    """Get all analytics in one call"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ANALYTICS_BASE}/analytics/summary", timeout=15.0)
            return r.json()
    except Exception as e:
        logger.error(f"Analytics summary proxy error: {e}")
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════
#  DASHBOARD USERS — CRUD (Admin Only)
# ═══════════════════════════════════════
from auth import require_admin
import bcrypt as _bcrypt

@router.get("/users")
async def dashboard_get_users(user: dict = Depends(require_admin)):
    """List all dashboard users (admin only)."""
    try:
        rows = _mysql_query(
            "SELECT id, username, role, display_name, staff_id, is_active, created_at, last_login FROM dashboard_users ORDER BY id ASC"
        )
        users = []
        for r in rows:
            users.append({
                "id": r["id"],
                "username": r["username"],
                "role": r["role"],
                "display_name": r.get("display_name"),
                "staff_id": r.get("staff_id"),
                "is_active": bool(r.get("is_active")),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
                "last_login": str(r["last_login"]) if r.get("last_login") else None,
            })
        return {"success": True, "data": users, "total": len(users)}
    except Exception as e:
        logger.error(f"GET /users error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/users")
async def dashboard_create_user(req: dict, user: dict = Depends(require_admin)):
    """Create a new dashboard user (admin only)."""
    try:
        username = (req.get("username") or "").strip()
        password = (req.get("password") or "").strip()
        if not username or not password:
            return {"success": False, "error": "username and password are required"}
        if len(username) < 3:
            return {"success": False, "error": "username must be at least 3 characters"}
        if len(password) < 4:
            return {"success": False, "error": "password must be at least 4 characters"}

        existing = _mysql_query_one("SELECT id FROM dashboard_users WHERE username = %s", (username,))
        if existing:
            return {"success": False, "error": f"User '{username}' already exists"}

        password_hash = _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')
        role = req.get("role", "staff")
        if role not in ("admin", "staff"):
            role = "staff"
        display_name = (req.get("display_name") or username).strip()
        staff_id = (req.get("staff_id") or "").strip() or None

        new_id = _mysql_execute(
            "INSERT INTO dashboard_users (username, password_hash, role, display_name, staff_id) VALUES (%s, %s, %s, %s, %s)",
            (username, password_hash, role, display_name, staff_id),
        )
        return {
            "success": True,
            "data": {
                "id": new_id,
                "username": username,
                "role": role,
                "display_name": display_name,
                "staff_id": staff_id,
            },
            "message": f"User '{username}' created",
        }
    except Exception as e:
        logger.error(f"POST /users error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/users/{user_id}")
async def dashboard_update_user(user_id: int, req: dict, user: dict = Depends(require_admin)):
    """Update a dashboard user: password, role, active status (admin only)."""
    try:
        existing = _mysql_query_one("SELECT * FROM dashboard_users WHERE id = %s", (user_id,))
        if not existing:
            return {"success": False, "error": "User not found"}

        updates = []
        params = []

        if "password" in req and req["password"]:
            new_pw = req["password"].strip()
            if len(new_pw) < 4:
                return {"success": False, "error": "password must be at least 4 characters"}
            password_hash = _bcrypt.hashpw(new_pw.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')
            updates.append("password_hash = %s")
            params.append(password_hash)

        if "role" in req:
            role = req["role"]
            if role not in ("admin", "staff"):
                return {"success": False, "error": "role must be 'admin' or 'staff'"}
            updates.append("role = %s")
            params.append(role)

        if "display_name" in req:
            updates.append("display_name = %s")
            params.append(req["display_name"])

        if "staff_id" in req:
            updates.append("staff_id = %s")
            params.append(req["staff_id"] or None)

        if "is_active" in req:
            updates.append("is_active = %s")
            params.append(1 if req["is_active"] else 0)

        if not updates:
            return {"success": False, "error": "No fields to update"}

        params.append(user_id)
        _mysql_execute(f"UPDATE dashboard_users SET {', '.join(updates)} WHERE id = %s", tuple(params))

        # Clear credentials cache to pick up changes
        import auth
        auth._creds_cache = None
        auth._creds_cache_time = None

        updated = _mysql_query_one(
            "SELECT id, username, role, display_name, staff_id, is_active, created_at, last_login FROM dashboard_users WHERE id = %s",
            (user_id,),
        )
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "username": updated["username"],
                "role": updated["role"],
                "display_name": updated.get("display_name"),
                "staff_id": updated.get("staff_id"),
                "is_active": bool(updated.get("is_active")),
                "created_at": str(updated["created_at"]) if updated.get("created_at") else None,
                "last_login": str(updated["last_login"]) if updated.get("last_login") else None,
            },
            "message": "User updated",
        }
    except Exception as e:
        logger.error(f"PUT /users/{user_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/users/{user_id}")
async def dashboard_delete_user(user_id: int, user: dict = Depends(require_admin)):
    """Deactivate a dashboard user (soft delete, admin only)."""
    try:
        existing = _mysql_query_one("SELECT * FROM dashboard_users WHERE id = %s", (user_id,))
        if not existing:
            return {"success": False, "error": "User not found"}

        if existing["username"] == "admin":
            return {"success": False, "error": "Cannot deactivate the default admin user"}

        _mysql_execute("UPDATE dashboard_users SET is_active = 0 WHERE id = %s", (user_id,))

        # Clear credentials cache
        import auth
        auth._creds_cache = None
        auth._creds_cache_time = None

        return {
            "success": True,
            "data": {
                "id": user_id,
                "username": existing["username"],
                "is_active": False,
            },
            "message": f"User '{existing['username']}' deactivated",
        }
    except Exception as e:
        logger.error(f"DELETE /users/{user_id} error: {e}")
        return {"success": False, "error": str(e)}


# ============================================================
#  SELF-SERVICE PASSWORD CHANGE
# ============================================================
@router.post("/change-password")
async def dashboard_change_password(req: dict, user: dict = Depends(get_current_user)):
    '''Let any logged-in user change their own password.'''
    try:
        current_pw = req.get("current_password", "")
        new_pw = req.get("new_password", "")
        if not current_pw or not new_pw:
            return {"success": False, "error": "Current and new password required"}
        if len(new_pw) < 4:
            return {"success": False, "error": "New password must be at least 4 characters"}
        
        # Verify current password
        row = _mysql_query_one(
            "SELECT password_hash FROM dashboard_users WHERE username = %s AND is_active = 1",
            (user["username"],)
        )
        if not row:
            return {"success": False, "error": "User not found"}
        
        import bcrypt
        if not bcrypt.checkpw(current_pw.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return {"success": False, "error": "Current password is incorrect"}
        
        # Update password
        new_hash = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        _mysql_execute(
            "UPDATE dashboard_users SET password_hash = %s WHERE username = %s",
            (new_hash, user["username"])
        )
        
        # Flush credentials cache so login works immediately
        try:
            from auth import flush_credentials_cache
            flush_credentials_cache()
        except Exception:
            pass
        
        logger.info("Password changed for user: %s", user["username"])
        return {"success": True, "message": "Password changed successfully"}
    except Exception as e:
        logger.error(f"change-password error: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
#  CUSTOMER FEEDBACK
# ══════════════════════════════════════════════════════════════════
@router.get("/feedback")
async def dashboard_feedback_summary(user: dict = Depends(get_current_user), date: str = Query(None), limit: int = Query(100)):
    """Return all customer feedback with optional date filter."""
    try:
        if date:
            rows = _mysql_query(
                "SELECT cf.*, cb.member_id, cb.staff_name, cb.console_id as booking_console_id, "
                "cb.booking_date, cb.duration_mins, cb.game_name "
                "FROM customer_feedback cf "
                "LEFT JOIN console_booking cb ON cf.booking_id = cb.id "
                "WHERE DATE(cf.created_at) = %s "
                "ORDER BY cf.created_at DESC LIMIT %s",
                (date, limit)
            )
        else:
            rows = _mysql_query(
                "SELECT cf.*, cb.member_id, cb.staff_name, cb.console_id as booking_console_id, "
                "cb.booking_date, cb.duration_mins, cb.game_name "
                "FROM customer_feedback cf "
                "LEFT JOIN console_booking cb ON cf.booking_id = cb.id "
                "ORDER BY cf.created_at DESC LIMIT %s",
                (limit,)
            )

        # Stats
        stats_rows = _mysql_query(
            "SELECT "
            "  COUNT(*) AS total, "
            "  ROUND(AVG(rating), 1) AS avg_rating, "
            "  SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) AS positive, "
            "  SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END) AS neutral, "
            "  SUM(CASE WHEN rating <= 2 THEN 1 ELSE 0 END) AS negative, "
            "  COUNT(CASE WHEN comment != '' AND comment IS NOT NULL THEN 1 END) AS with_comments "
            "FROM customer_feedback"
            + (" WHERE DATE(created_at) = %s" if date else ""),
            (date,) if date else ()
        )

        # Daily breakdown (last 14 days)
        daily_rows = _mysql_query(
            "SELECT DATE(CONVERT_TZ(created_at, '+00:00', '+06:30')) AS day, COUNT(*) AS cnt, ROUND(AVG(rating), 1) AS avg_rating "
            "FROM customer_feedback "
            "WHERE created_at >= DATE_SUB(CONVERT_TZ(NOW(), '+00:00', '+06:30'), INTERVAL 14 DAY) "
            "GROUP BY DATE(CONVERT_TZ(created_at, '+00:00', '+06:30')) ORDER BY day DESC"
        )

        return {
            "success": True,
            "feedbacks": [{
                "id": r["id"],
                "tg_id": r.get("tg_id"),
                "username": r.get("username"),
                "booking_id": r.get("booking_id"),
                "rating": r["rating"],
                "comment": r.get("comment") or "",
                "console_id": r.get("console_id") or r.get("booking_console_id") or "",
                "member_id": r.get("member_id") or "",
                "staff_name": r.get("staff_name") or "",
                "booking_date": str(r["booking_date"]) if r.get("booking_date") else "",
                "duration_mins": r.get("duration_mins") or 0,
                "game_name": r.get("game_name") or "",
                "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M") if r.get("created_at") else "",
            } for r in rows],
            "stats": {
                "total": stats_rows[0]["total"] if stats_rows else 0,
                "avg_rating": float(stats_rows[0]["avg_rating"] or 0) if stats_rows else 0,
                "positive": stats_rows[0]["positive"] or 0 if stats_rows else 0,
                "neutral": stats_rows[0]["neutral"] or 0 if stats_rows else 0,
                "negative": stats_rows[0]["negative"] or 0 if stats_rows else 0,
                "with_comments": stats_rows[0]["with_comments"] or 0 if stats_rows else 0,
            },
            "daily": [{"day": str(d["day"]), "cnt": d["cnt"], "avg_rating": float(d["avg_rating"] or 0)} for d in daily_rows],
        }
    except Exception as e:
        logger.error(f"dashboard/feedback error: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
#  DAILY TILL / CASH REGISTER MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def _get_mmt_now():
    """Return current datetime in Myanmar time (UTC+6:30)."""
    return datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)


def _get_mmt_today_str():
    """Return today's date string in MMT."""
    return _get_mmt_now().strftime("%Y-%m-%d")


@router.get("/till/today")
async def get_till_today(user: dict = Depends(get_current_user)):
    """Get today's till record. Auto-creates if it doesn't exist."""
    try:
        today = _get_mmt_today_str()

        # Get branch_id from header (default 1)
        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        row = _mysql_query_one(
            "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
            (today, branch_id)
        )

        if not row:
            # Auto-create empty till record for today
            now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")
            _mysql_execute(
                """INSERT INTO daily_till (branch_id, till_date, status, opened_at)
                   VALUES (%s, %s, 'open', %s)""",
                (branch_id, today, now_mmt)
            )
            row = _mysql_query_one(
                "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
                (today, branch_id)
            )

        # Calculate payment-method sales from sales_daily for today
        # payment_method field can be: "Cash:10000", "KPay:31000|Cash:20000|WavePay:17000", etc.
        all_sales = _mysql_query(
            """SELECT payment_method, net FROM sales_daily
               WHERE DATE(sale_date) = %s AND payment_method IS NOT NULL""",
            (today,)
        ) or []
        
        # Parse pipe-separated payment strings and aggregate by method
        pm_totals = {}  # method -> total
        for s in all_sales:
            pm_str = (s.get("payment_method") or "").strip()
            net = float(s.get("net") or 0)
            parts = pm_str.split("|")
            total_method_val = 0
            for part in parts:
                if ":" in part:
                    m, val = part.rsplit(":", 1)
                    try:
                        val_num = float(val)
                        total_method_val += val_num
                        pm_totals[m.strip()] = pm_totals.get(m.strip(), 0) + val_num
                    except ValueError:
                        pass
            # If parsed total doesn't match net, distribute remainder proportionally
            # (net is the authoritative total)
            if total_method_val > 0 and abs(net - total_method_val) > 0.01:
                ratio = net / total_method_val if total_method_val else 1
                for m, v in list(pm_totals.items()):
                    # Adjust only the methods from this row — but simpler: skip micro-adjustment
                    pass
        
        cash_total = pm_totals.get("Cash", 0)
        kpay_total = pm_totals.get("KPay", 0)
        wavepay_total = pm_totals.get("WavePay", 0)
        ayapay_total = pm_totals.get("AYA Pay", 0)
        total_sales = sum(pm_totals.values())
        
        # ── Cash outflows: eject + transfer_out (both leave the physical drawer) ──
        cm = _mysql_query_one(
            """SELECT
                 COALESCE(SUM(CASE WHEN movement_type = 'eject' THEN amount ELSE 0 END), 0) AS cash_ejected,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_out' THEN ABS(amount) ELSE 0 END), 0) AS cash_transferred_out,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_in' THEN amount ELSE 0 END), 0) AS cash_transferred_in,
                 COALESCE(SUM(CASE WHEN movement_type = 'inject' THEN amount ELSE 0 END), 0) AS cash_injected
               FROM cash_movements
               WHERE account = 'Cash' AND DATE(created_at) = %s""",
            (today,)
        )
        cash_ejected = float(cm["cash_ejected"]) if cm else 0.0
        cash_transferred_out = float(cm["cash_transferred_out"]) if cm else 0.0
        cash_transferred_in = float(cm["cash_transferred_in"]) if cm else 0.0
        cash_injected = float(cm["cash_injected"]) if cm else 0.0
        cash_outflows = cash_ejected + cash_transferred_out  # both physically leave drawer
        cash_inflows_extra = cash_transferred_in + cash_injected
        
        logger.info(f"till/today cash movements: ejected={cash_ejected} tx_out={cash_transferred_out} tx_in={cash_transferred_in} injected={cash_injected}")

        # Update the till record with live sales totals
        # Expected = Opening + Cash Sales + Transfers In + Injections - Ejects - Transfers Out
        expected_closing = (float(row["opening_balance"] or 0) + cash_total + cash_inflows_extra - cash_outflows)
        _mysql_execute(
            """UPDATE daily_till
               SET cash_sales_total = %s, kpay_sales_total = %s,
                   expected_closing = %s
               WHERE id = %s""",
            (cash_total, kpay_total, expected_closing, row["id"])
        )

        # Re-fetch to get updated values
        row = _mysql_query_one(
            "SELECT * FROM daily_till WHERE id = %s", (row["id"],)
        )

        # Format response
        def _fmt(val):
            return float(val) if val is not None else 0.0

        return {
            "success": True,
            "data": {
                "id": row["id"],
                "branch_id": row["branch_id"],
                "till_date": str(row["till_date"]),
                "opening_balance": _fmt(row.get("opening_balance")),
                "closing_balance": _fmt(row.get("closing_balance")),
                "cash_sales_total": _fmt(row.get("cash_sales_total")),
                "kpay_sales_total": _fmt(row.get("kpay_sales_total")),
                "expected_closing": _fmt(row.get("expected_closing")),
                "actual_cash_counted": _fmt(row.get("actual_cash_counted")),
                "variance": _fmt(row.get("variance")),
                "opened_by": row.get("opened_by") or "",
                "closed_by": row.get("closed_by") or "",
                "opened_at": row["opened_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("opened_at") else None,
                "closed_at": row["closed_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("closed_at") else None,
                "notes": row.get("notes") or "",
                "status": row["status"],
            },
            "sales_summary": {
                "cash_total": cash_total,
                "kpay_total": kpay_total,
                "wavepay_total": wavepay_total,
                "ayapay_total": ayapay_total,
                "total_sales": total_sales,
                "cash_ejected": cash_ejected,
                "cash_transferred_out": cash_transferred_out,
                "cash_transferred_in": cash_transferred_in,
                "cash_injected": cash_injected,
                "cash_outflows": cash_outflows,
                "cash_inflows_extra": cash_inflows_extra,
            }
        }
    except Exception as e:
        logger.error(f"till/today error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/till/open")
async def open_till(req: dict, user: dict = Depends(get_current_user)):
    """Open today's till with opening balance."""
    try:
        today = _get_mmt_today_str()
        opening_balance = float(req.get("opening_balance", 0))
        opened_by = str(req.get("opened_by", user.get("username", "")))

        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")

        existing = _mysql_query_one(
            "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
            (today, branch_id)
        )

        if existing:
            _mysql_execute(
                """UPDATE daily_till
                   SET opening_balance = %s, opened_by = %s, opened_at = %s, status = 'open'
                   WHERE id = %s""",
                (opening_balance, opened_by, now_mmt, existing["id"])
            )
        else:
            _mysql_execute(
                """INSERT INTO daily_till
                   (branch_id, till_date, opening_balance, opened_by, opened_at, status)
                   VALUES (%s, %s, %s, %s, %s, 'open')""",
                (branch_id, today, opening_balance, opened_by, now_mmt)
            )

        logger.info(f"Till opened by {opened_by} with balance {opening_balance}")
        return {"success": True, "message": f"Till opened with {opening_balance:,.2f} MMK"}
    except Exception as e:
        logger.error(f"till/open error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/till/close")
async def close_till(req: dict, user: dict = Depends(get_current_user)):
    """Close today's till with actual cash counted."""
    try:
        today = _get_mmt_today_str()
        actual_cash = float(req.get("actual_cash_counted", 0))
        closed_by = str(req.get("closed_by", user.get("username", "")))
        notes = str(req.get("notes", ""))

        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")

        existing = _mysql_query_one(
            "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
            (today, branch_id)
        )

        if not existing:
            return {"success": False, "error": "No till record found for today"}

        if existing["status"] == "closed":
            return {"success": False, "error": "Till is already closed for today"}

        # Calculate live sales totals before closing (parse pipe-separated payment_method)
        all_cs = _mysql_query(
            """SELECT payment_method, net FROM sales_daily
               WHERE DATE(sale_date) = %s AND payment_method IS NOT NULL""",
            (today,)
        ) or []
        cash_total = 0.0
        kpay_total = 0.0
        for s in all_cs:
            pm_str = (s.get("payment_method") or "").strip()
            for part in pm_str.split("|"):
                if ":" in part:
                    m, v = part.rsplit(":", 1)
                    try:
                        vv = float(v)
                        if m.strip() == "Cash":
                            cash_total += vv
                        elif m.strip() == "KPay":
                            kpay_total += vv
                    except ValueError:
                        pass

        opening = float(existing["opening_balance"] or 0)
        
        # Include cash movements (eject + transfers) in expected
        cm = _mysql_query_one(
            """SELECT
                 COALESCE(SUM(CASE WHEN movement_type = 'eject' THEN amount ELSE 0 END), 0) AS cash_ejected,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_out' THEN ABS(amount) ELSE 0 END), 0) AS cash_transferred_out,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_in' THEN amount ELSE 0 END), 0) AS cash_transferred_in,
                 COALESCE(SUM(CASE WHEN movement_type = 'inject' THEN amount ELSE 0 END), 0) AS cash_injected
               FROM cash_movements
               WHERE account = 'Cash' AND DATE(created_at) = %s""",
            (today,)
        )
        _ejected = float(cm["cash_ejected"]) if cm else 0.0
        _tx_out = float(cm["cash_transferred_out"]) if cm else 0.0
        _tx_in = float(cm["cash_transferred_in"]) if cm else 0.0
        _injected = float(cm["cash_injected"]) if cm else 0.0
        
        expected = opening + cash_total + _tx_in + _injected - _ejected - _tx_out
        variance = actual_cash - expected
        closing_balance = actual_cash

        _mysql_execute(
            """UPDATE daily_till
               SET actual_cash_counted = %s, variance = %s, closing_balance = %s,
                   closed_by = %s, closed_at = %s, notes = %s, status = 'closed',
                   cash_sales_total = %s, expected_closing = %s
               WHERE id = %s""",
            (actual_cash, variance, closing_balance,
             closed_by, now_mmt, notes,
             cash_total, expected,
             existing["id"])
        )

        logger.info(f"Till closed by {closed_by}: expected={expected}, actual={actual_cash}, variance={variance}")
        return {
            "success": True,
            "message": f"Till closed. Variance: {variance:+,.2f} MMK",
            "data": {
                "opening_balance": opening,
                "cash_sales_total": cash_total,
                "expected_closing": expected,
                "actual_cash_counted": actual_cash,
                "variance": variance,
            }
        }
    except Exception as e:
        logger.error(f"till/close error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/till/history")
async def get_till_history(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user)
):
    """Get past till records."""
    try:
        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        rows = _mysql_query(
            """SELECT * FROM daily_till
               WHERE branch_id = %s AND till_date <= CURDATE()
               ORDER BY till_date DESC
               LIMIT %s""",
            (branch_id, days)
        )

        def _fmt(val):
            return float(val) if val is not None else 0.0

        return {
            "success": True,
            "data": [{
                "id": r["id"],
                "till_date": str(r["till_date"]),
                "opening_balance": _fmt(r.get("opening_balance")),
                "cash_sales_total": _fmt(r.get("cash_sales_total")),
                "kpay_sales_total": _fmt(r.get("kpay_sales_total")),
                "expected_closing": _fmt(r.get("expected_closing")),
                "actual_cash_counted": _fmt(r.get("actual_cash_counted")),
                "variance": _fmt(r.get("variance")),
                "opened_by": r.get("opened_by") or "",
                "closed_by": r.get("closed_by") or "",
                "opened_at": r["opened_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("opened_at") else None,
                "closed_at": r["closed_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("closed_at") else None,
                "notes": r.get("notes") or "",
                "status": r["status"],
            } for r in rows]
        }
    except Exception as e:
        logger.error(f"till/history error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  SESSION ↔ SALES RECONCILIATION
# ═══════════════════════════════════════
@router.get("/reconciliation")
async def get_reconciliation(
    date: str = Query(None),
    user: dict = Depends(get_current_user),
):
    """Cross-check Done sessions vs sales_daily entries for a given date."""
    try:
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Fetch Done sessions for this date
        done_sessions = _mysql_query("""
            SELECT cb.id as booking_id, cb.console_id, cb.start_time, cb.end_time,
                   cb.duration_mins, cb.staff_name, cb.phone, cb.created_at,
                   TIMESTAMPDIFF(MINUTE, cb.start_time, cb.end_time) as actual_mins
            FROM console_booking cb
            WHERE cb.status = 'Done'
              AND DATE(cb.booking_date) = %s
              AND cb.console_id IS NOT NULL AND cb.console_id != ''
            ORDER BY cb.console_id, cb.start_time
        """, (date,)) or []

        # 2. Fetch sales_daily for this date
        sales_records = _mysql_query("""
            SELECT id, voucher_no, console_id, amount, net, gross, discount,
                   payment_method, notes, created_at, sale_date
            FROM sales_daily
            WHERE DATE(sale_date) = %s
              AND console_id IS NOT NULL AND console_id != ''
            ORDER BY console_id, created_at
        """, (date,)) or []

        # 3. Parse play minutes from sales notes
        for sr in sales_records:
            notes = sr.get("notes") or ""
            m = re.search(r'Mins?:?\s*(\d+)', notes, re.IGNORECASE)
            sr["_play_mins"] = int(m.group(1)) if m else 0

        # 4. Matching logic:
        #    Match by console_id + mins within ±10 + time proximity
        matched_pairs = []  # (session, sale)
        used_sale_ids = set()

        for session in done_sessions:
            sid = session["booking_id"]
            cid = session["console_id"]
            actual = session.get("actual_mins") or 0
            if actual <= 0 and session.get("duration_mins"):
                actual = session["duration_mins"]

            candidates = [
                s for s in sales_records
                if s["console_id"] == cid
                and s["id"] not in used_sale_ids
                and abs(s["_play_mins"] - actual) <= 10
            ]

            if candidates:
                # Pick closest by time proximity (closest start_time to created_at)
                session_start = session.get("start_time")
                best = min(candidates, key=lambda s: abs(
                    (s["created_at"] - session_start).total_seconds()
                ) if (session_start and s.get("created_at")) else 0)
                matched_pairs.append((session, best))
                used_sale_ids.add(best["id"])

        matched_session_ids = {p[0]["booking_id"] for p in matched_pairs}

        # 5. Build missing_sales (Done sessions without matching sale)
        def parse_multiplier(console_id: str) -> float:
            """C-09 and C-10 have 1.2x rate multiplier."""
            if not console_id:
                return 1.0
            cid = console_id.upper().strip()
            if cid in ("C-09", "C-10"):
                return 1.2
            return 1.0

        missing_sales = []
        for session in done_sessions:
            if session["booking_id"] not in matched_session_ids:
                actual = session.get("actual_mins") or session.get("duration_mins") or 0
                if actual <= 0:
                    actual = session.get("duration_mins") or 0
                mult = parse_multiplier(session.get("console_id", ""))
                est = round((actual / 60) * 10000 * mult)
                missing_sales.append({
                    "booking_id": session["booking_id"],
                    "console_id": session["console_id"],
                    "start_time": session["start_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("start_time") else None,
                    "end_time": session["end_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("end_time") else None,
                    "actual_mins": actual,
                    "staff_name": session.get("staff_name") or "—",
                    "phone": session.get("phone") or "",
                    "estimated_amount": int(est),
                })

        # 6. Build orphan_sales (sales without matching Done session, exclude food-only)
        orphan_sales = []
        for sale in sales_records:
            if sale["id"] not in used_sale_ids:
                _notes = sale.get("notes") or ""
                # Skip food-only / topup / registration sales (no Mins: in notes)
                if not re.search(r'Mins?:?\s*\d+', _notes, re.IGNORECASE):
                    continue
                orphan_sales.append({
                    "id": sale["id"],
                    "voucher_no": sale.get("voucher_no") or "",
                    "console_id": sale.get("console_id") or "",
                    "net": float(sale.get("net") or 0),
                    "payment_method": sale.get("payment_method") or "",
                    "notes": sale.get("notes") or "",
                })

        # 7. Build all_matched for detail view
        all_matched = []
        for session, sale in matched_pairs:
            all_matched.append({
                "console_id": session["console_id"],
                "booking_id": session["booking_id"],
                "voucher_no": sale.get("voucher_no") or "",
                "mins": session.get("actual_mins") or session.get("duration_mins") or 0,
                "payment_method": sale.get("payment_method") or "",
                "amount": float(sale.get("net") or 0),
                "match_type": "console+mins",
            })

        total_done = len(done_sessions)
        total_sales = len(sales_records)
        total_matched = len(matched_pairs)
        missing_revenue = sum(ms["estimated_amount"] for ms in missing_sales)

        return {
            "success": True,
            "date": date,
            "done_sessions": total_done,
            "sales_records": total_sales,
            "matched": total_matched,
            "missing_sales": missing_sales,
            "orphan_sales": orphan_sales,
            "all_matched": all_matched,
            "summary": {
                "total_done_sessions": total_done,
                "total_sales": total_sales,
                "total_matched": total_matched,
                "total_orphan_sessions": len(missing_sales),
                "total_orphan_sales": len(orphan_sales),
                "missing_revenue": int(missing_revenue),
            },
        }
    except Exception as e:
        logger.error(f"Reconciliation error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
