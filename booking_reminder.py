"""
Booking Reminder Service — Restart-proof 30-min advance reminders.

Replaces n8n webhook: POST /webhook/booking-reminder

On receiving a booking, schedules a Telegram notification 30 min before
the booking start time to STAFF_NOTIFY_CHAT.

Persistence: saves reminders to /root/psvibe_api_server/data/booking_reminders.json
On startup, restores all pending reminders.
"""
import asyncio
import json
import logging
import os
import threading
import time as _time
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

REMINDER_FILE = "/root/psvibe_api_server/data/booking_reminders.json"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CUSTOMER_BOT_TOKEN = os.environ.get("CUSTOMER_BOT_TOKEN", "")
STAFF_NOTIFY_CHAT = os.environ.get("DEPOSIT_NOTIFY_CHAT", os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747"))
STAFF_NOTIFY_THREAD = int(os.environ.get("STAFF_NOTIFY_THREAD", "0") or 0)
MMT = timezone(timedelta(hours=6, minutes=30))

# In-memory state
_pending_reminders: dict[str, dict] = {}  # key → reminder dict
_lock = threading.RLock()  # Reentrant lock — allows nested acquire


def _load_reminders() -> dict:
    """Load persisted reminders from disk."""
    try:
        if os.path.isfile(REMINDER_FILE):
            with open(REMINDER_FILE) as f:
                data = json.load(f)
            logger.info("booking_reminder: loaded %d pending reminders", len(data))
            return data
    except Exception as e:
        logger.error("booking_reminder load: %s", e)
    return {}


def _save_reminders() -> None:
    """Persist current reminders to disk."""
    try:
        os.makedirs(os.path.dirname(REMINDER_FILE), exist_ok=True)
        with _lock:
            data = dict(_pending_reminders)
        with open(REMINDER_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error("booking_reminder save: %s", e)


def add_booking_reminder(data: dict) -> str:
    """Add a booking reminder and schedule background task.

    Expected keys:
        bk_id, customer_name, phone, console_id, console_type,
        date, time_slot, duration_mins, staff_notify_chat, telegram_chat_id
    """
    bk_id = str(data.get("bk_id", ""))
    if not bk_id:
        return "missing bk_id"

    key = f"bk_{bk_id}"
    with _lock:
        # Parse booking time in MMT
        date_str = data.get("date", "")
        time_str = data.get("time_slot", "")
        try:
            import re as _re
            m = _re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
            if not m:
                return f"bad date format: {date_str}"
            mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h, mi = map(int, time_str.split(":"))
            booking_dt = datetime(yr, mo, da, h, mi, 0, tzinfo=MMT)
        except Exception as e:
            return f"parse error: {e}"

        remind_dt = booking_dt - timedelta(minutes=30)
        now_mmt = datetime.now(MMT)

        if remind_dt <= now_mmt:
            if booking_dt <= now_mmt:
                return "booking already started — skipped"
            # Less than 30 min away — fire immediately
            delay_seconds = 1
        else:
            delay_seconds = (remind_dt - now_mmt).total_seconds()

        reminder = {
            "bk_id": bk_id,
            "customer_name": data.get("customer_name", "?"),
            "phone": data.get("phone", ""),
            "console_id": data.get("console_id", ""),
            "console_type": data.get("console_type", ""),
            "date": date_str,
            "time_slot": time_str,
            "duration_mins": data.get("duration_mins", 60),
            "tg_chat": data.get("telegram_chat_id", ""),
            "booking_iso": booking_dt.isoformat(),
            "remind_iso": remind_dt.isoformat(),
            "delay_seconds": delay_seconds,
            "created_at": now_mmt.isoformat(),
        }
        _pending_reminders[key] = reminder

    _save_reminders()
    # Start background task
    threading.Thread(target=_reminder_thread, args=(key, delay_seconds), daemon=True).start()
    logger.info("booking_reminder: bk#%s scheduled (delay=%ds, remind @ %s)", bk_id, delay_seconds, remind_dt.strftime("%H:%M"))
    return "ok"


def _reminder_thread(key: str, delay_seconds: float) -> None:
    """Sleep until reminder time, then send notification."""
    _time.sleep(max(1, delay_seconds))

    with _lock:
        reminder = _pending_reminders.pop(key, None)
    if not reminder:
        return  # Already cancelled or already sent

    _save_reminders()
    _send_reminder_notification(reminder)


def _send_reminder_notification(rem: dict) -> None:
    """Send 30-min advance reminder to admin group + customer via Telegram."""
    if not BOT_TOKEN or not STAFF_NOTIFY_CHAT:
        logger.warning("booking_reminder: BOT_TOKEN or STAFF_NOTIFY_CHAT not configured")
        return

    bk_id = rem["bk_id"]
    name = rem["customer_name"]
    phone = rem["phone"]
    console_id = rem["console_id"]
    console_type = rem["console_type"]
    date_str = rem["date"]
    time_str = rem["time_slot"]
    dur = rem["duration_mins"]
    tg_chat = rem.get("tg_chat", "")

    notify_text = (
        f"⏰ <b>Booking #{bk_id} Reminder!</b>\n"
        f"⏱️ <b>30 မိနစ်ခန့်အလို</b> ကြိုမှတ်ပါ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⬇ Customer: <b>{name}</b>  ℹ️ {phone}\n"
        f"🕹️ Console: <b>{console_id}</b> ({console_type})\n"
        f"📅 {date_str}  ⏰ {time_str}\n"
        f"⏱️ Duration: <b>{dur} mins</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Staff — ပြင်ဆင်ထားရန် သတိပေးချက်"
    )

    try:
        payload = json.dumps({
            "chat_id": int(STAFF_NOTIFY_CHAT),
            "text": notify_text,
            "parse_mode": "HTML",
        })
        if STAFF_NOTIFY_THREAD:
            payload_dict = json.loads(payload)
            payload_dict["message_thread_id"] = STAFF_NOTIFY_THREAD
            payload = json.dumps(payload_dict)
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("booking_reminder: bk#%s reminder sent to admin group", bk_id)
    except Exception as e:
        logger.error("booking_reminder send: %s", e)

    # 📱 Also notify customer if they have a Telegram chat ID
    if tg_chat and CUSTOMER_BOT_TOKEN:
        try:
            cust_text = (
                f"\u23f0 <b>PS VIBE — Booking Reminder!</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\u267f {date_str}  \u23f0 {time_str}\n"
                f"\u267f Duration: <b>{dur} mins</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\u23f1\ufe0f <b>30 \u1019\u102d\u1014\u1037\u1001\u103a\u1021\u101c\u102d\u102f</b> \u1000\u103c\u102d\u102f\u1010\u1000\u103a \u101b\u1031\u102c\u1000\u103a\u1015\u102b\n"
                f"\u26a0\ufe0f \u1021\u1001\u103b\u102d\u1014\u103a\u1019\u103e\u102c \u101c\u102c\u101c\u102c\u1019\u103b\u1010\u103a \u1015\u103c\u1004\u103a\u1006\u1004\u103a\u1015\u102b!"
            )
            payload2 = json.dumps({"chat_id": tg_chat, "text": cust_text, "parse_mode": "HTML"})
            req2 = urllib.request.Request(
                f"https://api.telegram.org/bot{CUSTOMER_BOT_TOKEN}/sendMessage",
                data=payload2.encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req2, timeout=10)
            logger.info("booking_reminder: bk#%s customer reminder sent to %s", bk_id, tg_chat)
        except Exception as e:
            logger.warning("booking_reminder customer send: %s", e)


def cancel_booking_reminder(bk_id: str) -> bool:
    """Remove a pending booking reminder (e.g., when booking is cancelled)."""
    key = f"bk_{bk_id}"
    removed = False
    with _lock:
        if key in _pending_reminders:
            del _pending_reminders[key]
            removed = True
    if removed:
        _save_reminders()
        logger.info("booking_reminder: bk#%s cancelled", bk_id)
        return True
    return False


def restore_reminders() -> None:
    """Restore pending reminders from disk on startup."""
    data = _load_reminders()
    now_mmt = datetime.now(MMT)
    restored = 0
    for key, rem in list(data.items()):
        try:
            bk_id = rem.get("bk_id", "")
            booking_iso = rem.get("booking_iso", "")
            if booking_iso:
                booking_dt = datetime.fromisoformat(booking_iso)
                if booking_dt <= now_mmt:
                    logger.info("booking_reminder: bk#%s already passed — skipping restore", bk_id)
                    continue
            # Recalculate delay
            remind_iso = rem.get("remind_iso", "")
            if remind_iso:
                remind_dt = datetime.fromisoformat(remind_iso)
                delay = max(1, (remind_dt - now_mmt).total_seconds())
            else:
                delay = max(1, rem.get("delay_seconds", 300))
            with _lock:
                _pending_reminders[key] = rem
            threading.Thread(target=_reminder_thread, args=(key, delay), daemon=True).start()
            restored += 1
            logger.info("booking_reminder: restored bk#%s (delay=%ds)", bk_id, delay)
        except Exception as e:
            logger.error("booking_reminder restore bk#%s: %s", rem.get("bk_id", "?"), e)
    _save_reminders()
    logger.info("booking_reminder: restored %d reminders", restored)
