"""Session Timer Module — 5-min-before-end reminder with extend/end inline buttons.

Integrates with app.py via patch_routes pattern.
Auto-starts background scheduler for active sessions on server boot.
"""
import asyncio
import logging
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
import mysql.connector

logger = logging.getLogger(__name__)

# In-memory active session timers
# Format: { booking_id: { "task": asyncio.Task, "console_id": str, "member_id": str, "end_time": datetime } }
_active_timers = {}

# Telegram config (loaded from env)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STAFF_NOTIFY_CHAT = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")

CALLBACK_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")

# MySQL config
MYSQL_CFG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "psvibe_api"),
    "charset": "utf8mb4",
    "autocommit": False,
}

# ── Helper: send Telegram message ──────────────────────────────────────
def _send_telegram(chat_id: str, text: str, keyboard: list = None) -> bool:
    """Send Telegram message with optional inline keyboard."""
    if not BOT_TOKEN:
        logger.warning("No BOT_TOKEN set, cannot send Telegram")
        return False
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if keyboard and len(keyboard) > 0:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        data = json.dumps(payload).encode()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.warning(f"_send_telegram: {e}")
        return False

def _edit_telegram(chat_id: str, message_id: str, text: str, keyboard: list = None) -> bool:
    """Edit existing Telegram message."""
    if not BOT_TOKEN:
        return False
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
        if keyboard and len(keyboard) > 0:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        data = json.dumps(payload).encode()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.warning(f"_edit_telegram: {e}")
        return False

def _answer_callback(callback_id: str, text: str = ""):
    """Answer Telegram callback query (loading spinner stop)."""
    if not BOT_TOKEN:
        return
    try:
        data = json.dumps({"callback_query_id": callback_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning(f"_answer_callback: {e}")

# ── MySQL access (lazy import from app) ────────────────────────────────
def _mysql_exec(sql, params=None):
    from app import _mysql_exec as me
    return me(sql, params)

def _mysql_query(sql, params=None):
    from app import _mysql_query as mq
    return mq(sql, params)

# ── Session reminder timer ─────────────────────────────────────────────
async def _session_timer_task(booking_id: int, console_id: str, member_id: str,
                               start_time: datetime, duration_mins: int,
                               telegram_msg_id: str = None):
    """Background task: wait until 5 min before end, then send reminder.
    
    If extend happens, this task gets cancelled and a new one is scheduled.
    """
    try:
        # Calculate end time
        end_time = start_time + timedelta(minutes=duration_mins)
        reminder_time = end_time - timedelta(minutes=5)
        now = datetime.now(timezone.utc)
        
        # If reminder time is already past, send immediately
        sleep_seconds = (reminder_time - now).total_seconds()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)
        
        # Check if session is still active
        booking = _mysql_query("SELECT status, telegram_chat_id FROM console_booking WHERE id=%s", (booking_id,))
        if not booking or booking[0]["status"] != "Active":
            logger.info(f"Session {booking_id} no longer active, skipping reminder")
            return
        
        # Skip API-side reminder if booking was created via bot (has telegram_chat_id).
        # The sales bot runs its own _remind_loop and would duplicate the notification.
        if booking[0].get("telegram_chat_id"):
            logger.info(f"Session {booking_id} has telegram_chat_id — bot handles reminders, skipping API timer")
            return
        
        # Recalculate remaining time (may have extended)
        now = datetime.now(timezone.utc)
        booking_data = _mysql_query(
            "SELECT end_time, duration_mins, console_id, member_id FROM console_booking WHERE id=%s",
            (booking_id,))
        if not booking_data:
            return
        
        b = booking_data[0]
        actual_end = b.get("end_time") or (start_time + timedelta(minutes=duration_mins))
        if isinstance(actual_end, str):
            actual_end = datetime.fromisoformat(actual_end)
        
        remaining = int((actual_end - now).total_seconds() / 60)
        if remaining <= 0:
            return
        
        bk_name = member_id or booking
        lines = [
            f"⏰ *Timer Alert — Session Ending Soon!*\n",
            f"🎮 Console: `{console_id}`",
            f"👤 Member: `{bk_name}`",
            f"⏱ Remaining: *{remaining} min*",
            f"",
            f"Use sales bot to extend or end session.",
        ]
        text = "\n".join(lines)
        
        # Inline keyboard: (disabled - sales bot needs CallbackQueryHandler)
        # Uncomment when callback handler is added to sales bot
        keyboard = []  # TODO: enable inline buttons after adding CallbackQueryHandler
        
        if telegram_msg_id:
            _edit_telegram(STAFF_NOTIFY_CHAT, telegram_msg_id, text, keyboard)
        else:
            _send_telegram(STAFF_NOTIFY_CHAT, text, keyboard)
        
        logger.info(f"Timer reminder sent for booking {booking_id} ({remaining} min left)")
        
    except asyncio.CancelledError:
        logger.info(f"Timer for booking {booking_id} cancelled (extended/ended)")
    except Exception as e:
        logger.error(f"Session timer error for booking {booking_id}: {e}", exc_info=True)


# ── API: Schedule session timer ──────────────────────────────────────
def schedule_session_timer(booking_id: int, console_id: str, member_id: str,
                           start_time: datetime, duration_mins: int):
    """Schedule a background timer for the session.
    
    Called from checkin/create_booking endpoints.
    If a timer already exists for this booking, it's cancelled first.
    """
    # Cancel existing timer if any
    if booking_id in _active_timers:
        old = _active_timers[booking_id]
        old["task"].cancel()
        logger.info(f"Cancelled old timer for booking {booking_id}")
    
    loop = asyncio.get_event_loop()
    task = loop.create_task(
        _session_timer_task(booking_id, console_id, member_id, start_time, duration_mins)
    )
    _active_timers[booking_id] = {
        "task": task,
        "console_id": console_id,
        "member_id": member_id,
        "end_time": start_time + timedelta(minutes=duration_mins),
    }
    end_str = (start_time + timedelta(minutes=duration_mins)).strftime("%H:%M")
    logger.info(f"Scheduled timer for booking {booking_id} (ends ~{end_str}, console={console_id})")


# ── API: Extend session ──────────────────────────────────────────────
def extend_session(booking_id: int, extra_mins: int) -> dict:
    """Extend a session by N minutes. Returns updated info."""
    try:
        booking = _mysql_query(
            "SELECT id, console_id, member_id, start_time, duration_mins, status FROM console_booking WHERE id=%s",
            (booking_id,))
        if not booking:
            return {"ok": False, "error": "Booking not found"}
        
        b = booking[0]
        if b["status"] != "Active":
            return {"ok": False, "error": "Session is not active"}
        
        new_duration = (b["duration_mins"] or 0) + extra_mins
        _mysql_exec(
            "UPDATE console_booking SET duration_mins=%s, end_time=DATE_ADD(start_time, INTERVAL %s MINUTE) WHERE id=%s",
            (new_duration, new_duration, booking_id))
        
        console_id = b["console_id"]
        member_id = b["member_id"]
        start_time = b["start_time"]
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time)
        
        # Reschedule timer
        schedule_session_timer(booking_id, console_id, member_id, start_time, new_duration)
        
        # Notify admin group
        _send_telegram(STAFF_NOTIFY_CHAT,
            f"✅ *Session Extended!*\n"
            f"🎮 Console: `{console_id}`\n"
            f"👤 Member: `{member_id}`\n"
            f"⏱️ +{extra_mins} min (Total: {new_duration} min)")
        
        return {"ok": True, "new_duration": new_duration, "console_id": console_id}
    except Exception as e:
        logger.error(f"extend_session: {e}")
        return {"ok": False, "error": str(e)}


# ── API: End session (from timer button) ─────────────────────────────
def end_session_now(booking_id: int) -> dict:
    """End a session immediately. Returns updated info."""
    try:
        booking = _mysql_query(
            "SELECT id, console_id, member_id FROM console_booking WHERE id=%s",
            (booking_id,))
        if not booking:
            return {"ok": False, "error": "Booking not found"}
        
        b = booking[0]
        console_id = b["console_id"]
        member_id = b["member_id"]
        
        # Transactional: ensure both tables stay in sync
        conn = mysql.connector.connect(**MYSQL_CFG)
        conn.start_transaction()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("UPDATE console_booking SET status='Done', end_time=NOW() WHERE id=%s", (booking_id,))
            cur.execute(
                "UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s",
                (console_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
        
        # Cancel timer
        if booking_id in _active_timers:
            _active_timers[booking_id]["task"].cancel()
            del _active_timers[booking_id]
        
        _send_telegram(STAFF_NOTIFY_CHAT,
            f"⏹️ *Session Ended*\n"
            f"🎮 Console: `{console_id}`\n"
            f"👤 Member: `{member_id}`")
        
        return {"ok": True, "console_id": console_id, "member_id": member_id}
    except Exception as e:
        logger.error(f"end_session_now: {e}")
        return {"ok": False, "error": str(e)}


# ── API: Handle Telegram callback query ──────────────────────────────
def handle_callback(callback_data: str, callback_id: str = None,
                    chat_id: str = None, message_id: str = None) -> dict:
    """Process Telegram inline button callback.
    
    callback_data format:
      extend:<booking_id>:<mins>   → extend session
      endsession:<booking_id>      → end session immediately
    """
    parts = callback_data.split(":")
    action = parts[0]
    
    if action == "extend" and len(parts) >= 3:
        booking_id = int(parts[1])
        mins = int(parts[2])
        result = extend_session(booking_id, mins)
        if callback_id:
            if result.get("ok"):
                _answer_callback(callback_id, f"✅ +{mins} min added!")
            else:
                _answer_callback(callback_id, f"❌ {result.get('error', 'Failed')}")
        return result
    
    elif action == "endsession" and len(parts) >= 2:
        booking_id = int(parts[1])
        result = end_session_now(booking_id)
        if callback_id:
            if result.get("ok"):
                _answer_callback(callback_id, "✅ Session ended")
            else:
                _answer_callback(callback_id, f"❌ {result.get('error', 'Failed')}")
        if chat_id and message_id:
            _edit_telegram(chat_id, message_id, f"⏹️ *Session Ended*\nConsole: `{result.get('console_id','')}`")
        return result
    
    return {"ok": False, "error": "Unknown action"}


# ── Resume active sessions on startup ────────────────────────────────
def resume_active_timers():
    """On API server restart, find all Active bookings and resume timers."""
    try:
        rows = _mysql_query(
            "SELECT id, console_id, member_id, start_time, duration_mins, telegram_chat_id FROM console_booking WHERE status='Active' AND start_time IS NOT NULL")
        if not rows:
            logger.info("No active sessions to resume timers for")
            return
        
        now = datetime.now(timezone.utc)
        resumed = 0
        for r in rows:
            start_time = r["start_time"]
            if isinstance(start_time, str):
                start_time = datetime.fromisoformat(start_time)
            elif hasattr(start_time, 'isoformat'):
                # MySQL connector may return datetime objects — ensure it's a datetime
                start_time = datetime.fromisoformat(start_time.isoformat())
            duration = r["duration_mins"]
            # No timer mode (duration=0 or NULL): skip timer scheduling, just ensure console_status sync
            if not duration:
                logger.info(f"Booking {r['id']} has no timer (duration=0), skipping timer schedule")
                continue
            
            # Normalize: MySQL DATETIME is stored in MMT (UTC+6:30) without timezone.
            # Make it timezone-aware so comparisons with UTC `now` work correctly.
            try:
                if start_time is not None and start_time.tzinfo is None:
                    MMT = timezone(timedelta(hours=6, minutes=30))
                    start_time = start_time.replace(tzinfo=MMT)
                elif start_time is not None and start_time.tzinfo is not None:
                    # Already timezone-aware — keep as-is
                    pass
                else:
                    logger.warning(f"Skipping booking {r['id']}: start_time is None")
                    continue
                # Convert to UTC for safe comparison
                start_time_utc = start_time.astimezone(timezone.utc)
                end_time = start_time_utc + timedelta(minutes=duration)
                if end_time < now:
                    # Session expired — log but DO NOT auto-end (by Boss request).
                    # Staff must manually end sessions. The sales bot's _remind_loop
                    # will continue sending reminders until staff clicks "End Session".
                    logger.warning(
                        f"resume_active_timers: booking {r['id']} expired "
                        f"(end={end_time.isoformat()}, now={now.isoformat()}) — "
                        f"NOT auto-ending (manual-only policy)"
                    )
                    continue
            except Exception as _tz_err:
                logger.error(f"resume_active_timers timezone error for booking {r['id']}: {_tz_err} — skipping (manual-only policy)")
                continue
            
            # Skip API-side timer if booking was created via bot (has telegram_chat_id).
            # The sales bot runs its own _remind_loop and would duplicate the notification.
            # NOTE: expired session cleanup above runs BEFORE this skip.
            if r.get("telegram_chat_id"):
                logger.info(f"Booking {r['id']} has telegram_chat_id — bot handles reminders, skipping API timer")
                continue
            
            schedule_session_timer(r["id"], r["console_id"], r["member_id"], start_time, duration)
            resumed += 1
        
        logger.info(f"Resumed {resumed} session timers")
    except Exception as e:
        logger.error(f"resume_active_timers: {e}")


# ── Flask/FastAPI route handler for Telegram callback ───────────────
# This is called by the API server when Telegram sends a callback query
async def api_telegram_callback(req: dict):
    """Handle Telegram callback query from inline button.
    
    POST /api/telegram/callback
    Body: { "callback_query": { "id": "...", "data": "...", "message": {...} } }
    """
    try:
        cq = req.get("callback_query", {})
        if not cq:
            return {"ok": False, "error": "No callback_query"}
        
        callback_id = cq.get("id")
        callback_data = cq.get("data", "")
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        message_id = str(cq.get("message", {}).get("message_id", ""))
        
        result = handle_callback(callback_data, callback_id, chat_id, message_id)
        return {"ok": True, "result": result.get("ok", False)}
    except Exception as e:
        logger.error(f"api_telegram_callback: {e}")
        return {"ok": False, "error": str(e)}
