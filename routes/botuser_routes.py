"""PS VIBE API — Bot User Routes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import ok, error_response
from deps import verify_api_key
from models import GenericResponse
import logging
import os
from datetime import datetime, timedelta
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Bot Users'])

# ═══════════════════════════════════════
@router.post("/log/ai-interaction", response_model=GenericResponse, tags=["Logging"], summary="Log AI interaction")
async def api_log_ai_interaction(req: dict, auth=Depends(verify_api_key)):
    """Fire-and-forget: log an AI interaction. No GSheet dependency."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        query = req.get("query", "")[:300]
        response = req.get("response", "")[:500]
        sentiment = req.get("sentiment", "neutral")
        logger.info("AI-LOG: user=%s query=%s sentiment=%s", user_name, query[:60], sentiment)
        return ok({"logged": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/bot-users/track", response_model=GenericResponse, tags=["Bot Users"], summary="Track bot user interaction")
async def api_bot_users_track(req: dict, auth=Depends(verify_api_key)):
    """Track bot user interaction - MySQL upsert (GSheets fallback removed)."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        action = req.get("action", "")
        member_id = req.get("member_id", "")
        phone = req.get("phone", "")
        detail = req.get("detail", "")
        member_name = req.get("member_name", "")
        balance_mins = req.get("balance_mins", 0)
        logger.info("BOT-USER: tg=%s user=%s action=%s", tg_id, user_name, action)

        # MySQL: upsert user + log activity
        try:
            # Build dynamic UPSERT for optional fields
            upsert_sets = [
                "username = VALUES(username)",
                "first_name = VALUES(first_name)",
                "total_interactions = total_interactions + 1",
                "last_seen = NOW()",
                "last_command = VALUES(last_command)",
                "last_activity_type = VALUES(last_activity_type)",
            ]
            insert_cols = ["tg_id", "username", "first_name", "total_interactions", "last_command", "last_activity_type"]
            insert_vals = [tg_id, username, user_name, 1, action, action]

            # Optionally include member_id, phone, member_name, balance_mins
            if member_id:
                insert_cols.append("member_id")
                insert_vals.append(member_id)
                upsert_sets.append("member_id = VALUES(member_id)")
            if phone:
                insert_cols.append("phone")
                insert_vals.append(phone)
                upsert_sets.append("phone = VALUES(phone)")
            if member_name:
                insert_cols.append("member_name")
                insert_vals.append(member_name)
                upsert_sets.append("member_name = VALUES(member_name)")
            if balance_mins:
                insert_cols.append("balance_mins")
                insert_vals.append(balance_mins)
                upsert_sets.append("balance_mins = VALUES(balance_mins)")

            # Track session/book events
            if action == "session_start":
                upsert_sets.append("session_count = session_count + 1")
                upsert_sets.append("last_session_date = NOW()")
            if action.startswith("book_step_"):
                upsert_sets.append("last_funnel_step = VALUES(last_funnel_step)")
                insert_cols.append("last_funnel_step")
                insert_vals.append(action)
            if action == "booking_created":
                upsert_sets.append("booking_count = booking_count + 1")
                upsert_sets.append("last_booking_date = NOW()")

            placeholders = ", ".join(["%s"] * len(insert_vals))
            sql = (
                f"INSERT INTO customer_bot_users ({', '.join(insert_cols)}) "
                f"VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {', '.join(upsert_sets)}"
            )
            _mysql_exec(sql, insert_vals)

            # Log activity
            if action:
                _mysql_exec(
                    "INSERT INTO customer_bot_activity_log (tg_id, activity_type, detail) VALUES (%s, %s, %s)",
                    (tg_id, action, detail[:500] if detail else "")
                )

            # ── Auto-segmentation (Task 6) ──
            user = _mysql_query_one(
                "SELECT booking_count, total_interactions, last_seen, total_spend_ks, frustration_count, tags "
                "FROM customer_bot_users WHERE tg_id=%s", (tg_id,)
            )
            if user:
                tags = set(t.strip() for t in (user.get("tags", "") or "").split(",") if t.strip())
                bc = user.get("booking_count", 0) or 0
                ti = user.get("total_interactions", 0) or 0
                sp = user.get("total_spend_ks", 0) or 0
                fc = user.get("frustration_count", 0) or 0
                last_seen = user.get("last_seen")

                if bc >= 3:
                    tags.add("active_booker")
                if ti > 50 and bc == 0:
                    tags.add("chat_only")
                if last_seen:
                    days_ago = (datetime.now() - last_seen).days if isinstance(last_seen, datetime) else 999
                    if days_ago > 30:
                        tags.add("lapsed_30d")
                if sp >= 100000:
                    tags.add("high_value")
                if fc >= 3:
                    tags.add("needs_attention")

                new_tags = ",".join(sorted(tags))
                _mysql_exec("UPDATE customer_bot_users SET tags=%s WHERE tg_id=%s", (new_tags, tg_id))

        except Exception as dbe:
            logger.warning("bot-users MySQL write failed: %s", dbe)

        return ok({"tracked": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Customer Feedback ─────────────────────────────────────────────────────────


@router.get("/bot-users/stats", response_model=GenericResponse, tags=["Bot Users"], summary="Bot user stats")
async def api_bot_users_stats(auth=Depends(verify_api_key)):
    """Return aggregated bot user statistics with conversion rate, tag distribution, and peak hours."""
    try:
        total = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users")
        active_today = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE DATE(last_seen) = CURDATE()")
        active_week = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE last_seen >= DATE_SUB(NOW(), INTERVAL 7 DAY)")
        ai_chats = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_activity_log WHERE activity_type IN ('ai_chat','ai_chat_cached')")
        top_cmds = _mysql_query(
            "SELECT activity_type, COUNT(*) AS cnt FROM customer_bot_activity_log "
            "WHERE activity_type NOT IN ('ai_chat','ai_chat_cached','message') "
            "GROUP BY activity_type ORDER BY cnt DESC LIMIT 10"
        )
        by_day = _mysql_query(
            "SELECT DATE(first_seen) AS d, COUNT(*) AS cnt FROM customer_bot_users "
            "WHERE first_seen >= DATE_SUB(NOW(), INTERVAL 7 DAY) "
            "GROUP BY DATE(first_seen) ORDER BY d"
        )

        # Booking conversion rate
        users_with_booking = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE booking_count > 0")
        total_users = total["c"] if total else 0
        booking_cr = round((users_with_booking["c"] / total_users * 100), 1) if total_users > 0 else 0

        # Tag distribution
        tag_rows = _mysql_query("SELECT tags FROM customer_bot_users WHERE tags != '' AND tags IS NOT NULL")
        tag_dist: dict = {}
        for r in (tag_rows or []):
            for t in r["tags"].split(","):
                t = t.strip()
                if t:
                    tag_dist[t] = tag_dist.get(t, 0) + 1

        # Peak hours (hours with most activity from activity_log)
        peak_hours = _mysql_query(
            "SELECT HOUR(created_at) AS h, COUNT(*) AS cnt FROM customer_bot_activity_log "
            "WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            "GROUP BY HOUR(created_at) ORDER BY cnt DESC LIMIT 12"
        )

        return ok({
            "total_users": total_users,
            "active_today": active_today["c"] if active_today else 0,
            "active_this_week": active_week["c"] if active_week else 0,
            "total_ai_chats": ai_chats["c"] if ai_chats else 0,
            "top_commands": [{"action": r["activity_type"], "count": r["cnt"]} for r in (top_cmds or [])],
            "users_by_day": [{"date": str(r["d"]), "count": r["cnt"]} for r in (by_day or [])],
            "booking_conversion_rate_pct": booking_cr,
            "users_with_bookings": users_with_booking["c"] if users_with_booking else 0,
            "tag_distribution": tag_dist,
            "peak_hours": [{"hour": r["h"], "count": r["cnt"]} for r in (peak_hours or [])],
        })
    except Exception as e:
        return error_response(message=str(e))

@router.get("/bot-users/booking-success-rate", response_model=GenericResponse, tags=["Bot Users"], summary="Customer Bot booking success rate")
async def api_bot_booking_success_rate(since: str = Query("2026-06-02", description="Filter bookings from this date (YYYY-MM-DD). Default: 2026-06-02 (all data). Use 2026-06-25 for post-bind-fix only."), auth=Depends(verify_api_key)):
    """Return booking success rate for Customer Bot users: Done vs Cancelled vs Rejected.
    Excludes test bookings (6296803251) and rebooked cancellations (cancelled then rebooked → Done).
    Query param: since — filter from date (default 2026-06-02 = all data; 2026-06-25 = post-bind-fix).
    """
    try:
        _admin_chat = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")
        _base = f"WHERE telegram_chat_id IS NOT NULL AND telegram_chat_id != '' AND telegram_chat_id != '{_admin_chat}' AND telegram_chat_id != '6296803251' AND booking_date >= %s"

        # Count rebooked cancellations: cancelled bookings that were rebooked and completed (Done)
        # Match: same phone + same booking_date + same game_name → has a Done/Active booking
        rebooked = _mysql_query_one(f"""
            SELECT COUNT(*) AS cnt FROM console_bookings c
            {_base} AND c.status = 'cancelled'
            AND c.phone IS NOT NULL AND c.phone != ''
            AND c.game_name IS NOT NULL AND c.game_name != ''
            AND EXISTS (
                SELECT 1 FROM console_bookings d
                WHERE d.phone = c.phone
                  AND d.booking_date = c.booking_date
                  AND d.game_name = c.game_name
                  AND d.status IN ('Done','Active')
                  AND d.id != c.id
                  AND d.telegram_chat_id IS NOT NULL AND d.telegram_chat_id != ''
            )
        """, (since,))
        rebooked_count = rebooked["cnt"] if rebooked else 0

        # Totals by status (raw)
        totals = _mysql_query(f"SELECT status, COUNT(*) AS cnt FROM console_bookings {_base} GROUP BY status", (since,))
        total_map = {r["status"]: r["cnt"] for r in (totals or [])}
        done = total_map.get("Done", 0)
        active = total_map.get("Active", 0)
        confirmed = total_map.get("confirmed", 0)
        cancelled_raw = total_map.get("cancelled", 0)
        rejected = total_map.get("rejected", 0)
        pending = total_map.get("pending", 0)
        # Adjusted: rebooked cancellations don't count as negative
        cancelled_adj = cancelled_raw - rebooked_count
        total = sum(total_map.values())
        # Success rate: (Done + Active) / Total (rebooked exclusion doesn't change total, only affects interpretation)
        success_rate = round((done + active) / total * 100, 1) if total > 0 else 0
        # Adjusted success rate excluding rebooked cancellations from the failure pool
        adjusted_base = total - rebooked_count
        success_rate_adj = round((done + active) / adjusted_base * 100, 1) if adjusted_base > 0 else 0

        # Daily trend — last 14 days (raw counts, rebook flag separate)
        daily = _mysql_query(f"""
            SELECT booking_date AS d,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='Done' THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                   SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END) AS active
            FROM console_bookings {_base}
            AND booking_date >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
            GROUP BY booking_date ORDER BY d DESC
        """, (since,))

        # Weekly trend — last 4 weeks
        weekly = _mysql_query(f"""
            SELECT YEARWEEK(booking_date,1) AS w,
                   MIN(booking_date) AS week_start,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='Done' THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                   SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected
            FROM console_bookings {_base}
            AND booking_date >= DATE_SUB(CURDATE(), INTERVAL 4 WEEK)
            GROUP BY YEARWEEK(booking_date,1) ORDER BY w DESC
        """, (since,))

        # Console breakdown
        console = _mysql_query(f"""
            SELECT console_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('Done','Active') THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled
            FROM console_bookings {_base} AND console_id != ''
            GROUP BY console_id ORDER BY console_id
        """, (since,))

        # All users with booking success (no minimum — includes 1-booking users), with Telegram details
        top_users = _mysql_query(f"""
            SELECT b.staff_name AS name, b.phone, b.telegram_chat_id AS tg_id,
                   u.username, u.first_name,
                   COUNT(*) AS total_bookings,
                   SUM(CASE WHEN b.status IN ('Done','Active') THEN 1 ELSE 0 END) AS success_count,
                   SUM(CASE WHEN b.status='cancelled' THEN 1 ELSE 0 END) AS cancelled_count,
                   SUM(CASE WHEN b.status='rejected' THEN 1 ELSE 0 END) AS rejected_count,
                   ROUND(SUM(CASE WHEN b.status IN ('Done','Active') THEN 1 ELSE 0 END)/(COUNT(*) - SUM(CASE WHEN b.status='cancelled' AND b.phone IS NOT NULL AND b.phone!='' AND b.game_name IS NOT NULL AND b.game_name!='' AND EXISTS (SELECT 1 FROM console_bookings d WHERE d.phone=b.phone AND d.booking_date=b.booking_date AND d.game_name=b.game_name AND d.status IN ('Done','Active') AND d.id!=b.id AND d.telegram_chat_id IS NOT NULL AND d.telegram_chat_id!='') THEN 1 ELSE 0 END))*100, 1) AS success_pct
            FROM console_bookings b
            LEFT JOIN customer_bot_users u ON b.telegram_chat_id = u.tg_id
            {_base}
            GROUP BY b.staff_name, b.phone, b.telegram_chat_id, u.username, u.first_name
            ORDER BY total_bookings DESC, success_pct DESC
        """, (since,))

        return ok({
            "totals": {"total": total, "done": done, "active": active, "confirmed": confirmed,
                       "cancelled_raw": cancelled_raw, "cancelled_adj": cancelled_adj,
                       "rebooked": rebooked_count, "rejected": rejected, "pending": pending},
            "success_rate_pct": success_rate,
            "success_rate_adj_pct": success_rate_adj,
            "rebooked_count": rebooked_count,
            "note": f"{rebooked_count} cancelled bookings were rebooked and completed (excluded from failure count). Adjusted success rate: {success_rate_adj}%",
            "daily_trend": [{"date": str(r["d"]), "total": r["total"], "done": r["done"],
                            "cancelled": r["cancelled"], "rejected": r["rejected"], "active": r["active"]}
                           for r in (daily or [])],
            "weekly_trend": [{"week": r["w"], "week_start": str(r["week_start"]), "total": r["total"],
                              "done": r["done"], "cancelled": r["cancelled"], "rejected": r["rejected"]}
                             for r in (weekly or [])],
            "console_breakdown": [{"console_id": r["console_id"], "total": r["total"],
                                    "success": r["success"], "cancelled": r["cancelled"],
                                    "rate_pct": round(r["success"]/r["total"]*100,1) if r["total"]>0 else 0}
                                   for r in (console or [])],
            "top_users": [{"name": r["name"] or "Unknown", "phone": r["phone"] or "",
                           "tg_id": r["tg_id"] or "", "username": r["username"] or "",
                           "first_name": r["first_name"] or "",
                           "total_bookings": r["total_bookings"], "success_count": r["success_count"],
                           "cancelled_count": r["cancelled_count"], "rejected_count": r["rejected_count"],
                           "success_pct": r["success_pct"]}
                          for r in (top_users or [])],
        })
    except Exception as e:
        return error_response(message=str(e))


@router.get("/bot-users/list", response_model=GenericResponse, tags=["Bot Users"], summary="List bot users")
async def api_bot_users_list(limit: int = 50, offset: int = 0, search: str = "", auth=Depends(verify_api_key)):
    """Return paginated bot user list with all tracking columns."""
    base_cols = "tg_id, username, first_name, first_seen, last_seen, total_interactions, ai_chat_count, last_command, last_activity_type, member_id, member_name, phone, balance_mins, is_active, session_count, last_session_date, booking_count, total_spend_ks, last_booking_date, tags, last_funnel_step, frustration_count"
    try:
        if search:
            like = f"%{search}%"
            total = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE username LIKE %s OR first_name LIKE %s OR tg_id LIKE %s", (like, like, like))
            rows = _mysql_query(
                f"SELECT {base_cols} FROM customer_bot_users WHERE username LIKE %s OR first_name LIKE %s OR tg_id LIKE %s "
                "ORDER BY last_seen DESC LIMIT %s OFFSET %s",
                (like, like, like, limit, offset)
            )
        else:
            total = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users")
            rows = _mysql_query(
                f"SELECT {base_cols} FROM customer_bot_users ORDER BY last_seen DESC LIMIT %s OFFSET %s",
                (limit, offset)
            )
        users = []
        for r in (rows or []):
            users.append({
                "tg_id": r["tg_id"],
                "username": r["username"],
                "first_name": r["first_name"],
                "first_seen": str(r["first_seen"]) if r["first_seen"] else "",
                "last_seen": str(r["last_seen"]) if r["last_seen"] else "",
                "total_interactions": r["total_interactions"],
                "ai_chat_count": r["ai_chat_count"],
                "last_command": r["last_command"] or "",
                "last_activity": r["last_activity_type"] or "",
                "member_id": r["member_id"] or "",
                "member_name": r["member_name"] or "",
                "phone": r["phone"] or "",
                "balance_mins": r["balance_mins"],
                "is_active": bool(r["is_active"]),
                "session_count": r["session_count"],
                "last_session_date": str(r["last_session_date"]) if r["last_session_date"] else "",
                "booking_count": r["booking_count"],
                "total_spend_ks": str(r["total_spend_ks"]) if r["total_spend_ks"] is not None else "0",
                "last_booking_date": str(r["last_booking_date"]) if r["last_booking_date"] else "",
                "tags": r["tags"] or "",
                "last_funnel_step": r["last_funnel_step"] or "",
                "frustration_count": r["frustration_count"],
            })
        return ok({"users": users, "total": total["c"] if total else 0})
    except Exception as e:
        return error_response(message=str(e))


@router.get("/bot-users/activity", response_model=GenericResponse, tags=["Bot Users"], summary="Bot user activity log")
async def api_bot_users_activity(tg_id: str, limit: int = 50, auth=Depends(verify_api_key)):
    """Return activity log for a specific user."""
    try:
        rows = _mysql_query(
            "SELECT id, activity_type, detail, created_at FROM customer_bot_activity_log "
            "WHERE tg_id=%s ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        activities = []
        for r in (rows or []):
            activities.append({
                "id": r["id"],
                "activity_type": r["activity_type"],
                "detail": r["detail"] or "",
                "created_at": str(r["created_at"]) if r["created_at"] else "",
            })
        return ok({"activities": activities})
    except Exception as e:
        return error_response(message=str(e))


@router.get("/bot-users/timeline/{tg_id}", response_model=GenericResponse, tags=["Bot Users"], summary="Bot user timeline")
async def api_bot_users_timeline(tg_id: str, limit: int = 100, auth=Depends(verify_api_key)):
    """Return a user's complete timeline: bookings, activity log, and feedback merged chronologically."""
    try:
        events = []

        # User info
        user = _mysql_query_one(
            "SELECT * FROM customer_bot_users WHERE tg_id=%s", (tg_id,)
        )

        # Activity log entries
        act_rows = _mysql_query(
            "SELECT id, activity_type, detail, created_at FROM customer_bot_activity_log "
            "WHERE tg_id=%s ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        for r in (act_rows or []):
            events.append({
                "type": "activity",
                "id": r["id"],
                "subtype": r["activity_type"],
                "detail": r["detail"] or "",
                "timestamp": str(r["created_at"]) if r["created_at"] else "",
            })

        # Bookings
        bk_rows = _mysql_query(
            "SELECT id, member_id, booking_date, console_id, staff_name, phone, "
            "duration_mins, game_name, status, created_at "
            "FROM console_bookings WHERE telegram_chat_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        for r in (bk_rows or []):
            events.append({
                "type": "booking",
                "id": r["id"],
                "subtype": r.get("status", ""),
                "detail": f"{r.get('console_id','')} | {r.get('member_id','')} | {r.get('staff_name','')} | {r.get('duration_mins',0)}min | {r.get('game_name','')}",
                "timestamp": str(r["created_at"]) if r["created_at"] else "",
                "booking_date": str(r.get("booking_date", "")) if r.get("booking_date") else "",
                "console_id": r.get("console_id", ""),
                "status": r.get("status", ""),
            })

        # Feedback entries
        fb_rows = _mysql_query(
            "SELECT id, tg_id, username, booking_id, rating, comment, console_id, created_at "
            "FROM customer_feedback WHERE tg_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        for r in (fb_rows or []):
            events.append({
                "type": "feedback",
                "id": r["id"],
                "subtype": str(r.get("rating", 0)),
                "detail": r.get("comment", "") or "",
                "timestamp": str(r["created_at"]) if r["created_at"] else "",
                "booking_id": r.get("booking_id", ""),
            })

        # Sort merged timeline by timestamp descending
        events.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        result = {
            "tg_id": tg_id,
            "events": events[:limit],
            "total_events": len(events),
        }
        if user:
            result["user"] = {
                "username": user.get("username", ""),
                "first_name": user.get("first_name", ""),
                "total_interactions": user.get("total_interactions", 0),
                "booking_count": user.get("booking_count", 0),
                "tags": user.get("tags", ""),
            }
        return ok(result)
    except Exception as e:
        return error_response(message=str(e))


