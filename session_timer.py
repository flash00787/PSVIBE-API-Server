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
from typing import Any, Dict, List, Optional, Union
import mysql.connector

logger = logging.getLogger(__name__)

# In-memory active session timers
# Format: { booking_id: { "task": asyncio.Task, "console_id": str, "member_id": str, "end_time": datetime } }
_active_timers: Dict[int, Dict[str, Any]] = {}

# Telegram config (loaded from env)
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
STAFF_NOTIFY_CHAT: str = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")

CALLBACK_BASE: str = os.environ.get("API_BASE_URL", "http://localhost:8000")

# MySQL config
MYSQL_CFG: Dict[str, Union[str, int]] = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "psvibe_api"),
    "charset": "utf8mb4",
    "autocommit": False,
}

# ── Helper: send Telegram message ──────────────────────────────────────
def _send_telegram(chat_id: str, text: str, keyboard: Optional[List[List[Dict[str, str]]]] = None) -> bool:
    """Send Telegram message with optional inline keyboard."""
    if not BOT_TOKEN:
        logger.warning("No BOT_TOKEN set, cannot send Telegram")
        return False
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard and len(keyboard) > 0:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        data = json.dumps(payload).encode()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        try:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            if not body.get("ok"):
                logger.warning(
                    f"_send_telegram: Telegram returned ok=false — {body.get('description', 'no description')}"
                )
                return False
        except Exception as _parse_err:
            logger.warning(f"_send_telegram: response parse failed: {_parse_err}")
        return True
    except Exception as e:
        logger.warning(f"_send_telegram: {e}")
        return False

def _edit_telegram(chat_id: str, message_id: str, text: str, keyboard: Optional[List[List[Dict[str, str]]]] = None) -> bool:
    """Edit existing Telegram message."""
    if not BOT_TOKEN:
        return False
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if keyboard and len(keyboard) > 0:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        data = json.dumps(payload).encode()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        try:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            if not body.get("ok"):
                logger.warning(
                    f"_edit_telegram: Telegram returned ok=false — {body.get('description', 'no description')}"
                )
                return False
        except Exception as _parse_err:
            logger.warning(f"_edit_telegram: response parse failed: {_parse_err}")
        return True
    except Exception as e:
        logger.warning(f"_edit_telegram: {e}")
        return False

def _answer_callback(callback_id: str, text: str = "") -> None:
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

# ── MySQL helpers (local, no circular dependency) ─────────────────────
def _mysql_exec(sql: str, params: Optional[tuple] = None) -> int:
    """Execute SQL using mysql.connector directly — avoids circular import with app.py."""
    conn = mysql.connector.connect(**MYSQL_CFG)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid

def _mysql_query(sql: str, params: Optional[tuple] = None) -> List[dict]:
    """Query using mysql.connector directly — avoids circular import with app.py."""
    conn = mysql.connector.connect(**MYSQL_CFG)
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    conn.close()
    return rows

# ── Session reminder timer ─────────────────────────────────────────────
async def _session_timer_task(booking_id: int, console_id: str, member_id: str,
                               start_time: datetime, duration_mins: int,
                               telegram_msg_id: Optional[str] = None) -> None:
    """Background task: wait until 5 min before end, then send reminder.
    
    If extend happens, this task gets cancelled and a new one is scheduled.
    """
    logger.info(f"Timer task STARTED for booking {booking_id}")
    try:
        # Normalize: MySQL DATETIME is stored in MMT (UTC+6:30) without timezone.
        # When passed from move-session or other endpoints, start_time may be naive.
        MMT = timezone(timedelta(hours=6, minutes=30))
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=MMT)
        
        # Calculate end time
        end_time = start_time + timedelta(minutes=duration_mins)
        reminder_time = end_time - timedelta(minutes=5)
        now = datetime.now(timezone.utc)
        
        # If reminder time is already past, send immediately
        sleep_seconds = (reminder_time - now).total_seconds()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)
        
        # Check if session is still active
        booking = _mysql_query("SELECT id, status, telegram_chat_id FROM console_bookings WHERE id=%s", (booking_id,))
        if not booking or booking[0]["status"] != "Active":
            logger.info(f"Session {booking_id} no longer active, skipping reminder")
            return
        
        # ⚠️ Race-condition fix: Give bot 5 seconds to call persist_reminder() before
        # checking. Without this delay, both bot and API can fire the same reminder.
        await asyncio.sleep(5)
        
        # Skip API-side reminder if booking was created via bot (has non-empty telegram_chat_id).
        # The sales bot runs its own _remind_loop and would duplicate the notification.
        # Note: empty string "" means no Telegram (walk-in), bot should handle via its own reminder_store.
        tg_id = booking[0].get("telegram_chat_id", "")
        # Only skip if bot has actually registered a persistent reminder for this console
        if tg_id and str(tg_id).strip() and _bot_has_persistent_reminder(console_id, tg_id):
            logger.info(f"Session {booking_id} has bot reminder — skipping API timer")
            return
        
        # Recalculate remaining time (may have extended)
        now = datetime.now(timezone.utc)
        booking_data = _mysql_query(
            "SELECT COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
            "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
            "cb.console_id, cb.member_id "
            "FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE cb.id=%s",
            (booking_id,))
        if not booking_data:
            return
        
        b = booking_data[0]
        actual_end = b.get("end_time") or (start_time + timedelta(minutes=duration_mins))
        if isinstance(actual_end, str):
            actual_end = datetime.fromisoformat(actual_end)
        # Normalize: DB returns naive datetime in MMT
        if actual_end is not None and actual_end.tzinfo is None:
            actual_end = actual_end.replace(tzinfo=MMT)
        
        remaining = int((actual_end - now).total_seconds() / 60)
        
        bk_name = member_id or booking
        
        # Inline keyboard: mirror bot _extend_timer_kb format (ext:{extra}:{cid}|{member}|{chat})
        # Sales bot has CallbackQueryHandler cb_extend_timer with pattern ^ext: — buttons work
        try:
            _tag_chat = int(STAFF_NOTIFY_CHAT) if STAFF_NOTIFY_CHAT else 0
        except (ValueError, TypeError):
            _tag_chat = 0
        _tag = f"{console_id}|{bk_name}|{_tag_chat}"
        keyboard = [
            [
                {"text": "➕ +30 min", "callback_data": f"ext:30:{_tag}"},
                {"text": "➕ +60 min", "callback_data": f"ext:60:{_tag}"},
                {"text": "➕ +90 min", "callback_data": f"ext:90:{_tag}"},
            ],
            [{"text": "✏️ Custom (မိနစ် ကိုယ်တိုင်ထည့်)", "callback_data": f"ext:custom:{_tag}"}],
            [{"text": "⏹️ Sale Bot → Session End", "callback_data": f"ext:redirect:{_tag}"}],
        ]
        
        # ── Repeat reminder loop (mirror bot _remind_loop) ──
        # fire=1 → 5-min-before; fire=2 → session end reached; fire>=3 → overdue (every 5 min)
        # First fire: always send (even if remaining<=0 — overdue case must still alert).
        fire_count = 0
        max_fires = 30  # ⚠️ Safety cap (~150 min overdue) — prevents infinite spam if end fails
        while True:
            fire_count += 1
            now = datetime.now(timezone.utc)
            # Re-fetch actual end (may have been extended via Extend button while we slept)
            booking_data = _mysql_query(
                "SELECT COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
                "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
                "cb.console_id, cb.member_id "
                "FROM console_bookings cb "
                "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
                "WHERE cb.id=%s",
                (booking_id,))
            if not booking_data:
                logger.info(f"Session {booking_id} gone — stopping reminder loop")
                break
            b = booking_data[0]
            actual_end = b.get("end_time") or (start_time + timedelta(minutes=duration_mins))
            if isinstance(actual_end, str):
                actual_end = datetime.fromisoformat(actual_end)
            if actual_end is not None and actual_end.tzinfo is None:
                actual_end = actual_end.replace(tzinfo=MMT)
            remaining = int((actual_end - now).total_seconds() / 60) if actual_end else 0
            
            if remaining > 0:
                _warn_line = f"⏱ Remaining: <b>{remaining} min</b>"
                _title = "⏰ <b>Timer Alert — Session ဆုံးခါနီး!</b>"
            else:
                # Past end — show ACTUAL overdue minutes (works for resumed timers too)
                _overdue_mins = max(1, int(abs(remaining)))
                _warn_line = f"🚨 <b>Session ကျော်လွန်နေပြီ! ({_overdue_mins} မိနစ်)</b>"
                _title = "⏰ <b>Timer Alert — Session Overdue</b>"
            
            lines = [
                f"{_title}\n",
                f"🎮 Console: <code>{console_id}</code>",
                f"👤 Member: <code>{bk_name}</code>",
                f"{_warn_line}",
                f"",
                f"⚠️ <b>Manual End လုပ်ပါ</b> — Session auto-end မဖြစ်ပါ",
                f"End Session နှိပ်မှသာ session ဆုံးမည်။",
            ]
            text = "\n".join(lines)
            
            if telegram_msg_id:
                _edit_telegram(STAFF_NOTIFY_CHAT, telegram_msg_id, text, keyboard)
            else:
                _send_telegram(STAFF_NOTIFY_CHAT, text, keyboard)
            logger.info(f"Timer reminder sent for booking {booking_id} (fire={fire_count}, remaining={remaining} min)")

            # 📲 Staff App FCM push (ADDITIVE — must never break the timer loop).
            #    Wired 2026-09-19 (Staff App Stage 4): mirrors the Telegram timer alert.
            try:
                import fcm
                _push_body = (
                    f"Console {console_id}: session ending in {remaining} min"
                    if remaining > 0 else
                    f"Console {console_id}: session {max(1, abs(remaining))} min overdue"
                )
                fcm.push_session_reminder(
                    console_id=console_id,
                    minutes_left=(remaining if remaining > 0 else 0),
                    member_id=bk_name,
                    body=_push_body,
                )
            except Exception as _push_err:
                logger.warning(f"session_timer: FCM push failed (non-critical): {_push_err}")
            
            # Stop repeating if session no longer active
            chk = _mysql_query("SELECT status FROM console_bookings WHERE id=%s", (booking_id,))
            if not chk or chk[0].get("status") != "Active":
                logger.info(f"Session {booking_id} ended — stopping reminder loop")
                break
            
            if fire_count >= max_fires:
                logger.warning(f"Timer for booking {booking_id} reached max_fires={max_fires} — stopping loop")
                break
            
            await asyncio.sleep(5 * 60)  # next reminder in 5 min (while overdue / near end)
        
    except asyncio.CancelledError:
        logger.info(f"Timer for booking {booking_id} cancelled (extended/ended)")
    except Exception as e:
        logger.error(f"Session timer error for booking {booking_id}: {e}", exc_info=True)


# ── API: Schedule session timer ──────────────────────────────────────
def schedule_session_timer(booking_id: int, console_id: str, member_id: str,
                           start_time: datetime, duration_mins: int) -> None:
    """Schedule a background timer for the session.
    
    Called from checkin/create_booking endpoints.
    If a timer already exists for this booking, it's cancelled first.
    """
    # Cancel existing timer if any
    if booking_id in _active_timers:
        old = _active_timers[booking_id]
        old["task"].cancel()
        logger.info(f"Cancelled old timer for booking {booking_id}")
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
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
def extend_session(booking_id: int, extra_mins: int) -> Dict[str, Any]:
    """Extend a session by N minutes. Returns updated info."""
    try:
        booking = _mysql_query(
            "SELECT id, console_id, member_id, actual_start AS start_time, actual_duration_mins AS duration_mins, status FROM console_sessions WHERE booking_id=%s AND status='active' ORDER BY id DESC LIMIT 1",
            (booking_id,))
        if not booking:
            return {"ok": False, "error": "Booking not found"}
        
        b = booking[0]
        # NOTE (2026-09-20): the DB stores lowercase 'active'; the old check compared
        # against 'Active' and therefore ALWAYS failed (dead extend path).
        if str(b["status"] or "").strip().lower() != "active":
            return {"ok": False, "error": "Session is not active"}
        
        new_duration = (b["duration_mins"] or 0) + extra_mins
        _mysql_exec(
            "UPDATE console_sessions SET actual_duration_mins=%s, actual_end=DATE_ADD(actual_start, INTERVAL %s MINUTE) WHERE booking_id=%s AND status='active'",
            (new_duration, new_duration, booking_id))
        # Also update console_bookings planned_duration_mins
        _mysql_exec(
            "UPDATE console_bookings SET planned_duration_mins=%s, planned_end=DATE_ADD(planned_start, INTERVAL %s MINUTE) WHERE id=%s",
            (new_duration, new_duration, booking_id))
        
        console_id = b["console_id"]
        member_id = b["member_id"]
        start_time = b["start_time"]
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time)
        
        # Reschedule timer
        schedule_session_timer(booking_id, console_id, member_id, start_time, new_duration)
        
        # ⚠️ Admin notification REMOVED — the bot's _do_extend already sends a richer
        # notification with inline buttons. Keeping both causes double messages.
        # _send_telegram(STAFF_NOTIFY_CHAT,
        #     f"✅ *Session Extended!*\n"
        #     f"🎮 Console: `{console_id}`\n"
        #     f"👤 Member: `{member_id}`\n"
        #     f"⏱️ +{extra_mins} min (Total: {new_duration} min)")
        
        return {"ok": True, "new_duration": new_duration, "console_id": console_id}
    except Exception as e:
        logger.error(f"extend_session: {e}")
        return {"ok": False, "error": str(e)}


# ── API: End session (from timer button) ─────────────────────────────
def end_session_now(booking_id: int) -> Dict[str, Any]:
    """End a session immediately. Returns updated info."""
    try:
        booking = _mysql_query(
            "SELECT id, console_id, member_id FROM console_sessions WHERE booking_id=%s ORDER BY id DESC LIMIT 1",
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
            cur.execute("UPDATE console_bookings SET status='Done' WHERE id=%s", (booking_id,))
            now_mmt = datetime.now(timezone(timedelta(hours=6, minutes=30)))
            cur.execute("UPDATE console_sessions SET status='done', actual_end=%s WHERE booking_id=%s", (now_mmt, booking_id))
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
def handle_callback(callback_data: str, callback_id: Optional[str] = None,
                    chat_id: Optional[str] = None, message_id: Optional[str] = None) -> Dict[str, Any]:
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

def _bot_has_persistent_reminder(console_id: str, chat_id: Optional[str] = None) -> bool:
    """Check if the sales bot has registered a persistent reminder for this console.
    
    Uses console_id prefix match (ignores chat_id) since the bot's chat_id
    (admin group) differs from the booking's telegram_chat_id (customer).
    """
    try:
        reminder_file = "/opt/psvibe/sales-bot/data/session_reminders.json"
        if not os.path.isfile(reminder_file):
            return False
        with open(reminder_file) as f:
            store = json.load(f)
        if not isinstance(store, dict):
            return False
        # Match by console_id prefix (e.g., "C-01|") regardless of chat_id
        prefix = f"{console_id}|"
        for key in store:
            if key.startswith(prefix):
                return True
        return False
    except Exception:
        return False

def resume_active_timers() -> None:
    """On API server restart, find all Active bookings and resume timers."""
    try:
        rows = _mysql_query(
            "SELECT cb.id, cb.console_id, cb.member_id, "
            "COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
            "cb.telegram_chat_id "
            "FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE cb.status='Active' AND cb.planned_start IS NOT NULL")
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
                    # Session expired — do NOT auto-end (by Boss request), but DO resume
                    # the reminder loop so staff keeps getting overdue alerts until they
                    # manually end the session. The loop's first fire = overdue alert.
                    logger.warning(
                        f"resume_active_timers: booking {r['id']} expired "
                        f"(end={end_time.isoformat()}, now={now.isoformat()}) — "
                        f"NOT auto-ending (manual-only policy); resuming overdue reminders"
                    )
            except Exception as _tz_err:
                logger.error(f"resume_active_timers timezone error for booking {r['id']}: {_tz_err} — skipping (manual-only policy)")
                continue
            
            # Skip API-side timer only if bot has registered a persistent reminder
            tg_id = r.get("telegram_chat_id", "")
            bot_reminder = _bot_has_persistent_reminder(r["console_id"], tg_id) if tg_id and str(tg_id).strip() else False
            if bot_reminder:
                logger.info(f"Booking {r['id']} has bot reminder for {r['console_id']} — skipping API timer")
                continue
            elif tg_id and str(tg_id).strip():
                logger.info(f"Booking {r['id']} has telegram_chat_id={tg_id} but no bot reminder — API timer handles it")
            
            schedule_session_timer(r["id"], r["console_id"], r["member_id"], start_time, duration)
            resumed += 1
        
        logger.info(f"Resumed {resumed} session timers")
    except Exception as e:
        logger.error(f"resume_active_timers: {e}")


# ── Flask/FastAPI route handler for Telegram callback ───────────────
# This is called by the API server when Telegram sends a callback query
async def api_telegram_callback(req: Dict[str, Any]) -> Dict[str, Any]:
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
