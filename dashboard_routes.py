"""PS VIBE Dashboard — Dashboard Data API Endpoints"""
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from console_status import sync_console_status as _sync_console_status
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


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
                   cb.planned_start AS start_time, cb.planned_end AS end_time, cb.status as booking_status
            FROM console_status c
            LEFT JOIN (
                SELECT cb1.*
                FROM console_bookings cb1
                INNER JOIN (
                    SELECT console_id, MAX(id) as max_id
                    FROM console_bookings
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
                   cb.planned_start AS start_time, cb.planned_end AS end_time, cb.status,
                   cb.planned_duration_mins AS duration_mins, cb.phone
            FROM console_bookings cb
            JOIN console_status c ON cb.console_id = c.console_id
            WHERE DATE(cb.booking_date) = %s
            ORDER BY cb.planned_start AS start_time ASC
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
                   cb.planned_start AS start_time, cb.planned_end AS end_time, cb.status, cb.staff_name,
                   cb.notes, cb.planned_duration_mins AS duration_mins, cb.phone, cb.game_name, cb.created_at
            FROM console_bookings cb
            WHERE {' AND '.join(where)}
            ORDER BY cb.id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM console_bookings cb WHERE {' AND '.join(where)}",
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
            "DELETE FROM console_bookings WHERE status IN (%s, %s, %s, %s, %s) AND booking_date < DATE_SUB(CURDATE(), INTERVAL 30 DAY)",
            ("cancelled", "Cancelled", "rejected", "Rejected", "Done")
        )
        d2 = _mysql_delete(
            "DELETE FROM console_bookings WHERE booking_date < DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
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
                      cb.planned_start AS start_time, cb.planned_end AS end_time, cb.status, cb.staff_name,
                      cb.notes, cb.planned_duration_mins AS duration_mins, cb.phone, cb.game_name, cb.created_at,
                      cb.telegram_chat_id
               FROM console_bookings cb WHERE cb.id = %s""",
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
        existing = _mysql_query_one("SELECT * FROM console_bookings WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        # Console reassign validation: block if target console has an overlapping Active session
        new_console = req.get("console_id", "")
        if new_console and new_console != (existing.get("console_id") or ""):
            # Use requested times (if provided) for overlap check, otherwise existing times
            _bk_start = req.get("start_time") or existing.get("planned_start")
            _bk_end = req.get("end_time") or None
            _req_dur = req.get("duration_mins") or existing.get("planned_duration_mins") or 0
            # Compute end_time from requested start + duration if not explicitly provided
            if not _bk_end and _bk_start and int(_req_dur) > 0:
                from datetime import datetime as _dt, timedelta as _td
                try:
                    _st_dt = _dt.fromisoformat(str(_bk_start)) if isinstance(_bk_start, str) else _bk_start
                    _bk_end = _st_dt + _td(minutes=int(_req_dur))
                except Exception:
                    _bk_end = existing.get("planned_end")
            if not _bk_end:
                _bk_end = existing.get("planned_end")
            if _bk_start and _bk_end:
                active_check = _mysql_query_one(
                    "SELECT id FROM console_bookings WHERE console_id=%s AND id!=%s AND status='Active'"
                    " AND planned_start < %s AND planned_end > %s LIMIT 1",
                    (new_console, booking_id, _bk_end, _bk_start)
                )
            else:
                # No time data — just check if console is Active
                active_check = _mysql_query_one(
                    "SELECT id FROM console_bookings WHERE console_id=%s AND id!=%s AND status='Active' LIMIT 1",
                    (new_console, booking_id)
                )
            if active_check:
                logger.warning(f"PUT /bookings/{booking_id}: BLOCKED — C-{new_console} has Active BK#{active_check['id']} (req times: {_bk_start}–{_bk_end})")
                return {"success": False, "error": f"Console {new_console} has an active session (BK#{active_check['id']}) at this time"}
            # Admin override: confirmed/pending overlaps are allowed

        updates = []
        params = []
        for field in ["console_id", "member_id", "status", "staff_name", "notes",
                       "planned_duration_mins", "phone", "game_name"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])
        if "booking_date" in req and req["booking_date"]:
            updates.append("booking_date = %s")
            params.append(req["booking_date"])
        if "start_time" in req and req["start_time"]:
            updates.append("planned_start = %s")
            params.append(req["start_time"])
        if "end_time" in req and req["end_time"]:
            updates.append("planned_end = %s")
            params.append(req["end_time"])
        if "duration_mins" in req and req["duration_mins"]:
            updates.append("planned_duration_mins = %s")
            params.append(req["duration_mins"])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        # 🐛 Fix Jun 25: Auto-calculate end_time when start_time or duration_mins changes
        if "end_time" not in req:
            _eff_start = req.get("start_time") or existing.get("planned_start")
            _eff_dur = int(req.get("duration_mins") or existing.get("planned_duration_mins") or 0)
            if _eff_start and _eff_dur > 0:
                if isinstance(_eff_start, str):
                    _eff_start = datetime.fromisoformat(str(_eff_start))
                _eff_end = _eff_start + timedelta(minutes=_eff_dur)
                updates.append("planned_end = %s")
                params.append(_eff_end.strftime("%Y-%m-%d %H:%M:%S"))

        params.append(booking_id)
        _mysql_execute(f"UPDATE console_bookings SET {', '.join(updates)} WHERE id = %s", tuple(params))

        # 🐛 Fix Jul 1: Sync console_status when console_id or status changes
        try:
            _old_cid = existing.get("console_id", "") or ""
            _new_cid = new_console if new_console else _old_cid
            if _old_cid and _old_cid != _new_cid:
                _sync_console_status(_old_cid)
            if _new_cid:
                _sync_console_status(_new_cid)
            elif new_status in ("cancelled", "Done", "rejected") and _old_cid:
                _sync_console_status(_old_cid)
        except Exception as _sync_err:
            logger.warning("dashboard_update_booking: _sync_console_status failed: %s", _sync_err)

        # 📢 Customer notification on confirm/cancel from web dashboard
        new_status = req.get("status", "")
        if new_status in ("confirmed", "cancelled"):
            logger.info("[DASH-NOTIFY] Triggering notification for BK#%s status=%s", booking_id, new_status)
            _notify_row = _mysql_query_one(
                "SELECT telegram_chat_id, console_id, booking_date, planned_start, planned_duration_mins FROM console_bookings WHERE id=%s",
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
                        _time = str(_notify_row.get("planned_start", ""))[:16] if _notify_row.get("planned_start") else ""
                        _dur = str(_notify_row.get("planned_duration_mins", ""))
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

        updated = _mysql_query_one("SELECT * FROM console_bookings WHERE id = %s", (booking_id,))
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "console_id": updated.get("console_id"),
                "member_id": updated.get("member_id"),
                "booking_date": str(updated["booking_date"]) if updated.get("booking_date") else None,
                "start_time": str(updated["planned_start"]) if updated.get("planned_start") else None,
                "end_time": str(updated["planned_end"]) if updated.get("planned_end") else None,
                "status": updated.get("status"),
                "staff_name": updated.get("staff_name"),
                "notes": updated.get("notes"),
                "duration_mins": updated.get("planned_duration_mins"),
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

        existing = _mysql_query_one("SELECT * FROM console_bookings WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        if existing.get("status") != "Active":
            return {"success": False, "error": "Can only adjust timer for Active sessions"}

        staff_name = user.get("username", "Dashboard User")
        _mysql_execute(
            "UPDATE console_sessions SET actual_duration_mins=%s, actual_end=DATE_ADD(actual_start, INTERVAL %s MINUTE), "
            "notes=CONCAT(IFNULL(notes,''), ' | Timer adjusted to ', %s, 'min by ', %s) WHERE booking_id=%s AND status='active'",
            (duration_mins, duration_mins, str(duration_mins), staff_name, booking_id)
        )
        # Also update bookings table
        _mysql_execute(
            "UPDATE console_bookings SET planned_duration_mins=%s, planned_end=DATE_ADD(planned_start, INTERVAL %s MINUTE) WHERE id=%s",
            (duration_mins, duration_mins, booking_id)
        )

        # ── Reschedule API-side session timer (fix: old timer used stale end time) ──
        try:
            from session_timer import schedule_session_timer
            from datetime import datetime as _dt
            sess = _mysql_query_one(
                "SELECT actual_start, console_id, COALESCE(member_id,'') AS member_id FROM console_sessions WHERE booking_id=%s AND status='active'",
                (booking_id,))
            if sess:
                start_dt = sess.get("actual_start")
                if isinstance(start_dt, str):
                    start_dt = _dt.fromisoformat(start_dt)
                if start_dt:
                    schedule_session_timer(booking_id, sess["console_id"], sess["member_id"], start_dt, duration_mins)
                    logger.info(f"Timer rescheduled for booking {booking_id} (new duration={duration_mins}min)")
                    # Also update bot's session_reminders.json so bot picks up the change
                    try:
                        from routes.console_routes import _persist_session_for_bot
                        _persist_session_for_bot(sess["console_id"], sess["member_id"], duration_mins, start_dt)
                    except Exception:
                        pass
        except Exception as timer_err:
            logger.warning(f"Failed to reschedule API timer for booking {booking_id}: {timer_err}")

        updated = _mysql_query_one("SELECT id, console_id, planned_start AS start_time, planned_end AS end_time, planned_duration_mins AS duration_mins, status FROM console_bookings WHERE id=%s", (booking_id,))
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
        existing = _mysql_query_one("SELECT * FROM console_bookings WHERE id = %s", (booking_id,))
        if not existing:
            return {"success": False, "error": "Booking not found"}

        # Soft-delete: never physically delete bookings — preserve history
        staff_name = user.get("username", "Dashboard User")
        _mysql_execute(
            "UPDATE console_bookings SET status='cancelled', notes=CONCAT(IFNULL(notes,''), ' | Cancelled by ', %s, ' via dashboard') WHERE id=%s",
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
#  SALES DAILY — List
# ═══════════════════════════════════════
# ═══════════════════════════════════════
#  FINANCIAL REPORT
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
        except Exception:
            member_balance_before = 0  # non-critical: fallback

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
        except Exception:
            pass  # non-critical: members table may not exist

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
                FROM console_bookings
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
        except Exception:
            pass  # non-critical: members table may not exist

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
                   FROM console_bookings
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
    """Return all customer feedback with optional date filter. All timestamps in MMT."""
    try:
        if date:
            rows = _mysql_query(
                "SELECT cf.*, cb.member_id, cb.staff_name, cb.console_id as booking_console_id, "
                "cb.booking_date, cb.planned_duration_mins AS duration_mins, cb.game_name "
                "FROM customer_feedback cf "
                "LEFT JOIN console_bookings cb ON cf.booking_id = cb.id "
                "WHERE DATE(cf.created_at) = %s "
                "ORDER BY cf.created_at DESC LIMIT %s",
                (date, limit)
            )
        else:
            rows = _mysql_query(
                "SELECT cf.*, cb.member_id, cb.staff_name, cb.console_id as booking_console_id, "
                "cb.booking_date, cb.planned_duration_mins AS duration_mins, cb.game_name "
                "FROM customer_feedback cf "
                "LEFT JOIN console_bookings cb ON cf.booking_id = cb.id "
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
            + (" WHERE DATE(DATE_ADD(created_at, INTERVAL 390 MINUTE)) = %s" if date else ""),
            (date,) if date else ()
        )

        # Daily breakdown (last 14 days) — data now stored in MMT
        daily_rows = _mysql_query(
            "SELECT DATE(created_at) AS day, COUNT(*) AS cnt, ROUND(AVG(rating), 1) AS avg_rating "
            "FROM customer_feedback "
            "WHERE created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY) "
            "GROUP BY DATE(created_at) ORDER BY day DESC"
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
                "created_at": (r["created_at"]).strftime("%Y-%m-%d %H:%M") if r.get("created_at") else "",
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
#  CONSOLE SWAP
# ══════════════════════════════════════════════════════════════════


@router.get("/swap/confirmed")
async def get_swap_confirmed_bookings(user: dict = Depends(get_current_user)):
    """List confirmed future bookings eligible for console swap."""
    try:
        now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")
        rows = _mysql_query("""
            SELECT id, console_id, booking_date,
                   planned_start, planned_end, planned_duration_mins,
                   member_id, game_name, staff_name, phone, notes
            FROM console_bookings
            WHERE status = 'confirmed'
              AND planned_start > %s
            ORDER BY planned_start ASC
        """, (now_mmt,))

        return {"success": True, "data": [
            {
                "id": r["id"],
                "console_id": r["console_id"],
                "booking_date": str(r["booking_date"]) if r.get("booking_date") else None,
                "planned_start": str(r["planned_start"]) if r.get("planned_start") else None,
                "planned_end": str(r["planned_end"]) if r.get("planned_end") else None,
                "planned_duration_mins": r.get("planned_duration_mins"),
                "member_id": r["member_id"],
                "game_name": r.get("game_name", "") or "",
                "staff_name": r.get("staff_name", "") or "",
                "phone": r.get("phone", "") or "",
                "notes": r.get("notes", "") or "",
            }
            for r in rows
        ]}
    except Exception as e:
        logger.error(f"swap/confirmed GET error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/swap/confirmed")
async def swap_confirmed_bookings(body: dict, user: dict = Depends(get_current_user)):
    """Swap consoles between two confirmed future bookings."""
    try:
        booking_1_id = body.get("booking_1_id")
        booking_2_id = body.get("booking_2_id")
        if not booking_1_id or not booking_2_id:
            raise HTTPException(400, "booking_1_id and booking_2_id are required")
        if booking_1_id == booking_2_id:
            raise HTTPException(400, "Cannot swap a booking with itself")

        # Fetch both bookings
        b1 = _mysql_query_one(
            "SELECT id, console_id, planned_start, planned_end, planned_duration_mins, status FROM console_bookings WHERE id=%s",
            (booking_1_id,))
        b2 = _mysql_query_one(
            "SELECT id, console_id, planned_start, planned_end, planned_duration_mins, status FROM console_bookings WHERE id=%s",
            (booking_2_id,))

        if not b1:
            raise HTTPException(404, f"Booking {booking_1_id} not found")
        if not b2:
            raise HTTPException(404, f"Booking {booking_2_id} not found")
        if b1["status"] != "confirmed":
            raise HTTPException(400, f"Booking {booking_1_id} is not confirmed (status: {b1['status']})")
        if b2["status"] != "confirmed":
            raise HTTPException(400, f"Booking {booking_2_id} is not confirmed (status: {b2['status']})")

        # Check no active session on target consoles
        for bid, target_console in [(booking_1_id, b2["console_id"]), (booking_2_id, b1["console_id"])]:
            active = _mysql_query_one(
                "SELECT cs.id FROM console_sessions cs JOIN console_bookings cb ON cs.booking_id=cb.id WHERE cb.console_id=%s AND cs.status='active' LIMIT 1",
                (target_console,))
            if active:
                raise HTTPException(409, f"Target console {target_console} (for booking {bid}) has an active session — cannot swap")

        # Swap using transaction
        _mysql_execute("START TRANSACTION")
        try:
            _mysql_execute(
                "UPDATE console_bookings SET console_id=%s, planned_start=%s, planned_end=%s, planned_duration_mins=%s WHERE id=%s",
                (b2["console_id"], b2["planned_start"], b2["planned_end"], b2["planned_duration_mins"], booking_1_id))
            _mysql_execute(
                "UPDATE console_bookings SET console_id=%s, planned_start=%s, planned_end=%s, planned_duration_mins=%s WHERE id=%s",
                (b1["console_id"], b1["planned_start"], b1["planned_end"], b1["planned_duration_mins"], booking_2_id))
            _mysql_execute("COMMIT")
        except Exception:
            _mysql_execute("ROLLBACK")
            raise

        logger.info(f"Console swap: booking {booking_1_id} ↔ {booking_2_id} by user {user.get('user_id','?')}")
        return {"success": True, "message": f"Successfully swapped consoles for bookings {booking_1_id} and {booking_2_id}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"swap/confirmed POST error: {e}")
        try:
            _mysql_execute("ROLLBACK")
        except Exception:
            pass
        return {"success": False, "error": str(e)}


@router.get("/swap/active")
async def get_swap_active_sessions(user: dict = Depends(get_current_user)):
    """List active console sessions eligible for swap."""
    try:
        rows = _mysql_query("""
            SELECT cb.id AS booking_id, cb.console_id, cb.member_id,
                   cb.game_name, cb.staff_name,
                   cs.actual_start, cs.actual_duration_mins
            FROM console_bookings cb
            JOIN console_sessions cs ON cs.booking_id = cb.id
            WHERE cs.status = 'active'
            ORDER BY cs.actual_start ASC
        """)

        return {"success": True, "data": [
            {
                "booking_id": r["booking_id"],
                "console_id": r["console_id"],
                "member_id": r["member_id"],
                "game_name": r.get("game_name", "") or "",
                "staff_name": r.get("staff_name", "") or "",
                "actual_start": str(r["actual_start"]) if r.get("actual_start") else None,
                "actual_duration_mins": r.get("actual_duration_mins"),
            }
            for r in rows
        ]}
    except Exception as e:
        logger.error(f"swap/active GET error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/swap/active")
async def swap_active_sessions(body: dict, user: dict = Depends(get_current_user)):
    """Swap consoles between two active sessions."""
    try:
        booking_1_id = body.get("booking_1_id")
        booking_2_id = body.get("booking_2_id")
        if not booking_1_id or not booking_2_id:
            raise HTTPException(400, "booking_1_id and booking_2_id are required")
        if booking_1_id == booking_2_id:
            raise HTTPException(400, "Cannot swap a session with itself")

        # Validate both have active sessions
        s1 = _mysql_query_one(
            "SELECT cs.id AS session_id, cb.console_id FROM console_sessions cs JOIN console_bookings cb ON cs.booking_id=cb.id WHERE cb.id=%s AND cs.status='active'",
            (booking_1_id,))
        s2 = _mysql_query_one(
            "SELECT cs.id AS session_id, cb.console_id FROM console_sessions cs JOIN console_bookings cb ON cs.booking_id=cb.id WHERE cb.id=%s AND cs.status='active'",
            (booking_2_id,))

        if not s1:
            raise HTTPException(404, f"No active session found for booking {booking_1_id}")
        if not s2:
            raise HTTPException(404, f"No active session found for booking {booking_2_id}")

        # Swap console_id in BOTH tables
        _mysql_execute("START TRANSACTION")
        try:
            _mysql_execute(
                "UPDATE console_bookings SET console_id=%s WHERE id=%s",
                (s2["console_id"], booking_1_id))
            _mysql_execute(
                "UPDATE console_bookings SET console_id=%s WHERE id=%s",
                (s1["console_id"], booking_2_id))
            _mysql_execute(
                "UPDATE console_sessions SET console_id=%s WHERE id=%s",
                (s2["console_id"], s1["session_id"]))
            _mysql_execute(
                "UPDATE console_sessions SET console_id=%s WHERE id=%s",
                (s1["console_id"], s2["session_id"]))
            _mysql_execute("COMMIT")
        except Exception:
            _mysql_execute("ROLLBACK")
            raise

        logger.info(f"Active session swap: booking {booking_1_id} ↔ {booking_2_id} by user {user.get('user_id','?')}")
        return {"success": True, "message": f"Successfully swapped active sessions for bookings {booking_1_id} and {booking_2_id}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"swap/active POST error: {e}")
        try:
            _mysql_execute("ROLLBACK")
        except Exception:
            pass
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
#  ALL-TIME CUMULATIVE STATS — Business Overview
# ══════════════════════════════════════════════════════════════════

@router.get("/cumulative")
async def get_cumulative_stats(user: dict = Depends(require_admin)):
    """All-time cumulative business stats for the dashboard overview. [Admin only]
    
    Sections:
      - summary: key metrics (revenue, profit, bookings, members, hours)
      - monthly: revenue/profit/bookings by month
      - top_members: top 15 by total spend
      - top_games: top 10 by play count
      - revenue_breakdown: gaming vs food vs topup
      - console_utilization: pct busy time per console type
      - member_growth: new members per month
      - hourly_distribution: bookings by hour of day
    """
    try:
        result = {}

        # ── 1. Summary Cards ──
        summary = _mysql_query_one("""
            SELECT
                COUNT(DISTINCT cb.id) AS total_bookings,
                COUNT(DISTINCT CASE WHEN cb.status IN ('Done','Active') THEN cb.id END) AS completed_sessions,
                COUNT(DISTINCT CASE WHEN cb.status = 'cancelled' THEN cb.id END) AS cancelled_bookings,
                COUNT(DISTINCT CASE WHEN cb.member_id IS NOT NULL AND cb.member_id != '' AND cb.member_id != 'Guest' THEN cb.member_id END) AS unique_members,
                COALESCE(SUM(CASE WHEN cb.status IN ('Done','Active') THEN COALESCE(cs.actual_duration_mins, cb.planned_duration_mins, 0) END), 0) AS total_play_mins,
                COALESCE(AVG(CASE WHEN cb.status IN ('Done','Active') THEN COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) END), 0) AS avg_session_mins
            FROM console_bookings cb
            LEFT JOIN console_sessions cs ON cs.booking_id = cb.id
        """)

        # Revenue from sales_daily (gaming) + food_cart + topup_log
        rev = _mysql_query_one("""
            SELECT
                COALESCE((SELECT SUM(amount) FROM sales_daily), 0) AS total_gaming_rev,
                COALESCE((SELECT SUM(quantity * unit_price) FROM food_cart), 0) AS total_food_rev,
                COALESCE((SELECT SUM(amount) FROM topup_log), 0) AS total_topup_rev,
                COALESCE((SELECT SUM(amount) FROM sales_daily), 0) + COALESCE((SELECT SUM(quantity * unit_price) FROM food_cart), 0) AS total_revenue
        """)

        result["summary"] = {
            "total_bookings": summary.get("total_bookings", 0) or 0,
            "completed_sessions": summary.get("completed_sessions", 0) or 0,
            "cancelled_bookings": summary.get("cancelled_bookings", 0) or 0,
            "cancellation_rate": round(
                (summary.get("cancelled_bookings", 0) or 0) / max(1, summary.get("total_bookings", 1) or 1) * 100, 1
            ),
            "unique_members": summary.get("unique_members", 0) or 0,
            "total_play_hours": round((summary.get("total_play_mins", 0) or 0) / 60, 1),
            "avg_session_mins": round(summary.get("avg_session_mins", 0) or 0, 1),
            "total_gaming_revenue": int(rev.get("total_gaming_rev", 0) or 0),
            "total_food_revenue": int(rev.get("total_food_rev", 0) or 0),
            "total_topup_revenue": int(rev.get("total_topup_rev", 0) or 0),
            "total_revenue": int(rev.get("total_revenue", 0) or 0),
        }

        # ── 2. Monthly Trends ──
        monthly_rows = _mysql_query("""
            SELECT
                d.month,
                COALESCE(s.gaming_rev, 0) AS gaming_rev,
                COALESCE(f.food_rev, 0) AS food_rev,
                COALESCE(t.topup_rev, 0) AS topup_rev,
                COALESCE(s.gaming_rev, 0) + COALESCE(f.food_rev, 0) AS total_rev,
                COALESCE(s.session_count, 0) AS session_count,
                COALESCE(s.play_mins, 0) AS play_mins
            FROM (
                SELECT DISTINCT DATE_FORMAT(sale_date, '%%Y-%%m') AS month FROM sales_daily
                UNION SELECT DISTINCT DATE_FORMAT(created_at, '%%Y-%%m') FROM food_cart
                UNION SELECT DISTINCT DATE_FORMAT(topup_date, '%%Y-%%m') FROM topup_log
                UNION SELECT DISTINCT DATE_FORMAT(booking_date, '%%Y-%%m') FROM console_bookings
            ) d
            LEFT JOIN (
                SELECT DATE_FORMAT(sale_date, '%%Y-%%m') AS m, SUM(amount) AS gaming_rev,
                       COUNT(*) AS session_count, SUM(0) AS play_mins
                FROM sales_daily GROUP BY m
            ) s ON s.m = d.month
            LEFT JOIN (
                SELECT DATE_FORMAT(created_at, '%%Y-%%m') AS m, SUM(quantity * unit_price) AS food_rev
                FROM food_cart GROUP BY m
            ) f ON f.m = d.month
            LEFT JOIN (
                SELECT DATE_FORMAT(topup_date, '%%Y-%%m') AS m, SUM(amount) AS topup_rev
                FROM topup_log GROUP BY m
            ) t ON t.m = d.month
            ORDER BY d.month ASC
        """)
        result["monthly"] = [
            {
                "month": r["month"],
                "gaming_revenue": int(r.get("gaming_rev", 0) or 0),
                "food_revenue": int(r.get("food_rev", 0) or 0),
                "topup_revenue": int(r.get("topup_rev", 0) or 0),
                "total_revenue": int(r.get("total_rev", 0) or 0),
                "sessions": int(r.get("session_count", 0) or 0),
                "play_hours": round((r.get("play_mins", 0) or 0) / 60, 1),
            }
            for r in monthly_rows
        ]

        # ── 3. Top 15 Members by Spend ──
        top_members_rows = _mysql_query("""
            SELECT
                cb.member_id AS member,
                COUNT(DISTINCT cb.id) AS visits,
                COALESCE(SUM(COALESCE(cs.actual_duration_mins, cb.planned_duration_mins)), 0) AS total_mins,
                COALESCE((SELECT SUM(sd2.amount) FROM sales_daily sd2 WHERE sd2.member_id = cb.member_id), 0) AS gaming_spend,
                COALESCE((SELECT SUM(fc2.quantity * fc2.unit_price) FROM food_cart fc2 JOIN console_bookings cb3 ON cb3.id = fc2.booking_id WHERE cb3.member_id = cb.member_id), 0) AS food_spend
            FROM console_bookings cb
            LEFT JOIN console_sessions cs ON cs.booking_id = cb.id
            WHERE cb.member_id IS NOT NULL AND cb.member_id != '' AND cb.member_id != 'Guest'
            GROUP BY cb.member_id
            ORDER BY (COALESCE((SELECT SUM(sd2.amount) FROM sales_daily sd2 WHERE sd2.member_id = cb.member_id), 0) + COALESCE((SELECT SUM(fc2.quantity * fc2.unit_price) FROM food_cart fc2 JOIN console_bookings cb3 ON cb3.id = fc2.booking_id WHERE cb3.member_id = cb.member_id), 0)) DESC
            LIMIT 15
        """)
        result["top_members"] = [
            {
                "member": r["member"],
                "visits": int(r.get("visits", 0) or 0),
                "total_hours": round((r.get("total_mins", 0) or 0) / 60, 1),
                "gaming_spend": int(r.get("gaming_spend", 0) or 0),
                "food_spend": int(r.get("food_spend", 0) or 0),
                "total_spend": int(r.get("gaming_spend", 0) or 0) + int(r.get("food_spend", 0) or 0),
            }
            for r in top_members_rows
        ]

        # ── 4. Top 10 Games ──
        top_games_rows = _mysql_query("""
            SELECT
                COALESCE(cb.game_name, 'Unknown') AS game,
                COUNT(*) AS play_count,
                COALESCE(SUM(cs.actual_duration_mins), SUM(cb.planned_duration_mins), 0) AS total_mins
            FROM console_bookings cb
            LEFT JOIN console_sessions cs ON cs.booking_id = cb.id
            WHERE cb.status IN ('Done','Active')
            GROUP BY cb.game_name
            ORDER BY play_count DESC
            LIMIT 10
        """)
        result["top_games"] = [
            {
                "game": r["game"],
                "play_count": int(r.get("play_count", 0) or 0),
                "total_hours": round((r.get("total_mins", 0) or 0) / 60, 1),
            }
            for r in top_games_rows
        ]

        # ── 5. Revenue Breakdown (pie chart data) ──
        result["revenue_breakdown"] = [
            {"category": "Gaming", "amount": result["summary"]["total_gaming_revenue"]},
            {"category": "Food & Drinks", "amount": result["summary"]["total_food_revenue"]},
            {"category": "Top-ups", "amount": result["summary"]["total_topup_revenue"]},
        ]

        # ── 6. Console Utilization ──
        util_rows = _mysql_query("""
            SELECT
                SUBSTRING_INDEX(cb.console_id, '-', 1) AS console_type,
                COUNT(DISTINCT cb.console_id) AS console_count,
                COUNT(DISTINCT cb.id) AS sessions,
                COALESCE(SUM(cs.actual_duration_mins), SUM(cb.planned_duration_mins), 0) AS total_mins
            FROM console_bookings cb
            LEFT JOIN console_sessions cs ON cs.booking_id = cb.id
            WHERE cb.status IN ('Done','Active')
              AND cb.console_id IS NOT NULL AND cb.console_id != ''
            GROUP BY SUBSTRING_INDEX(cb.console_id, '-', 1)
            ORDER BY total_mins DESC
        """)
        # Estimate: days since first booking * 12h/day * console_count
        first_day = _mysql_query_one("SELECT MIN(booking_date) AS d FROM console_bookings")
        days_since = 30  # fallback
        if first_day and first_day.get("d"):
            from datetime import date as _date
            try:
                _fd = first_day["d"]
                if isinstance(_fd, str):
                    _fd = _date.fromisoformat(_fd)
                days_since = max(1, (_get_mmt_now().date() - _fd).days + 1)
            except Exception:
                pass
        result["console_utilization"] = [
            {
                "type": r["console_type"],
                "count": int(r.get("console_count", 1) or 1),
                "sessions": int(r.get("sessions", 0) or 0),
                "total_hours": round((r.get("total_mins", 0) or 0) / 60, 1),
                "utilization_pct": round(
                    ((r.get("total_mins", 0) or 0) / 60) / max(1, int(r.get("console_count", 1) or 1) * 12 * days_since) * 100, 1
                ),
            }
            for r in util_rows
        ]

        # ── 7. Member Growth (new members per month) ──
        growth_rows = _mysql_query("""
            SELECT
                DATE_FORMAT(MIN(cb.created_at), '%%Y-%%m') AS month,
                COUNT(DISTINCT cb.member_id) AS new_members
            FROM (
                SELECT member_id, MIN(created_at) AS created_at
                FROM console_bookings
                WHERE member_id IS NOT NULL AND member_id != '' AND member_id != 'Guest'
                GROUP BY member_id
            ) cb
            GROUP BY DATE_FORMAT(cb.created_at, '%%Y-%%m')
            ORDER BY month ASC
        """)
        result["member_growth"] = [
            {
                "month": r["month"],
                "new_members": int(r.get("new_members", 0) or 0),
            }
            for r in growth_rows
        ]

        # ── 8. Hourly Distribution (popular hours) ──
        hourly_rows = _mysql_query("""
            SELECT
                HOUR(cb.planned_start) AS hour_of_day,
                COUNT(*) AS booking_count
            FROM console_bookings cb
            WHERE cb.planned_start IS NOT NULL
            GROUP BY HOUR(cb.planned_start)
            ORDER BY hour_of_day ASC
        """)
        result["hourly_distribution"] = [
            {
                "hour": r["hour_of_day"],
                "label": f"{r['hour_of_day']:02d}:00",
                "count": int(r.get("booking_count", 0) or 0),
            }
            for r in hourly_rows
        ]

        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"cumulative GET error: {e}")
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


