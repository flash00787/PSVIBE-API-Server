"""PS VIBE API — Booking & Receipt Routes."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Body
from config import MMT_HOURS, MMT_MINUTES
from fastapi.responses import JSONResponse, HTMLResponse, Response
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
import mysql.connector as _mc
from helpers import _mysql_get_setting, _mysql_get_settings_dict, ok, error_response, success_response, _parse_pm_and_inject, rate_limit_10pm, rate_limit_5pm, rate_limit_30pm, insert_cash_movement_if_absent, resolve_sale_game
from field_utils import normalize_list
from deps import verify_api_key, verify_webhook_secret
import staff_audit
from models import GenericResponse
from session_timer import schedule_session_timer, _active_timers
from booking_reminder import add_booking_reminder, restore_reminders, cancel_booking_reminder
from console_status import sync_console_status as _sync_console_status
import logging, os, re
import json
import json as _json_notify
import threading as _threading_notify
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
import asyncio
logger = logging.getLogger(__name__)
import json as _sjson
import os as _sos
_SUGGEST_FILE = "/root/psvibe-discord-bot/suggestions.json"

import json as _sjson
import os as _sos

_SUGGEST_FILE = "/root/psvibe-discord-bot/suggestions.json"

def _load_suggestions():
    """Load suggestions from JSON file."""
    try:
        if _sos.path.exists(_SUGGEST_FILE):
            with open(_SUGGEST_FILE, 'r') as f:
                data = _sjson.load(f)
                if isinstance(data, dict) and 'suggestions' in data:
                    return data.get('suggestions', [])
                return []
    except Exception:
        pass
    return []

def _save_suggestions(suggestions_list):
    """Save suggestions list to JSON file."""
    try:
        _sos.makedirs(_sos.path.dirname(_SUGGEST_FILE), exist_ok=True)
        with open(_SUGGEST_FILE, 'w') as f:
            _sjson.dump({'suggestions': suggestions_list}, f, indent=2, default=str)
        return True
    except Exception:
        pass
    return False


router = APIRouter(prefix="/api", tags=['Bookings'])

# ── Animated (custom) emoji ───────────────────────────────────────────────
# Reused from the customer bot's fancy.py CUSTOM_EMOJI_ID (verified animated
# TGS custom emoji). Applied to every customer notification so the Booking
# Confirmed / Refund / Cancel messages animate (owner 2026-10-02).
_ANIM_EMOJI_ID = {
    "✅": "5413721644277441416",
    "❌": "5032973497861669622",
    "🔄": "5244758760429213978",
    "📋": "5033080906403808074",
    "📝": "5033080906403808074",
    "🎮": "5431755876945904647",
    "💰": "5287231198098117669",
    "📅": "5429242943120623604",
    "⏰": "4967525849902350972",
    "⚠️": "5328254530981214461",
    "🚫": "5418159410646099061",
    "📞": "4969971262546772590",
    "🔔": "4969746438188696430",
    "🙏": "5431411862950388510",
    "⏱️": "5382194935057372936",
    "📌": "4969923081603645985",
    "⭐": "4970181191958266458",
    "👤": "4967667085606912536",
    "⚡️": "5429633836684157942",
    "🔥": "4970107898341360413",
    "🎉": "5429197605445847745",
    "🔒": "5030643469513655027",
    "🆕": "5429511915447536335",
    "📊": "5190806721286657692",
    "💳": "4967518033061872209",
    "🔑": "5346023639613466677",
    "🏆": "5429387503129875330",
}


def _anim(text: str) -> str:
    """Wrap known emoji in Telegram <tg-emoji> animated-custom-emoji entities."""
    if not text:
        return text
    out, i, n = [], 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two in _ANIM_EMOJI_ID:
            out.append('<tg-emoji emoji-id="%s">%s</tg-emoji>' % (_ANIM_EMOJI_ID[two], two))
            i += 2
        elif text[i] in _ANIM_EMOJI_ID:
            out.append('<tg-emoji emoji-id="%s">%s</tg-emoji>' % (_ANIM_EMOJI_ID[text[i]], text[i]))
            i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _send_telegram_customer(chat_id: str, message: str):
    """Send Telegram message to a customer via the Customer Bot token."""
    _log = logging.getLogger("psvibe_api")
    if not chat_id:
        _log.warning("[NOTIFY] No chat_id, skipping")
        return
    token = os.environ.get("CUSTOMER_BOT_TOKEN", "")
    _log.info("[NOTIFY] chat_id=%s token_len=%s", chat_id, len(token) if token else 0)
    if not token:
        _log.warning("[NOTIFY] CUSTOMER_BOT_TOKEN not configured in env")
        return
    message = _anim(message)   # animated custom emoji (owner 2026-10-02)
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = _json_notify.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        body = resp.read()
        _log.info("[NOTIFY] Sent to %s: %s", chat_id, body.decode()[:200])
    except Exception as e:
        _log.warning("[NOTIFY] Failed to %s: %s", chat_id, e)


# Owner 2026-10-02: the Booking Cancel & Refund terms image is sent to the
# customer when their booking becomes CONFIRMED (deposit verified).
PSVIBE_TERMS_IMAGE_URL = os.environ.get(
    "PSVIBE_TERMS_IMAGE_URL",
    "https://ps-vibe.com/psvibe-assets/psvibe-booking-terms-v3.png",
)


def _terms_image_enabled() -> bool:
    """Feature flag — auto-send the terms image on booking confirmation.

    Default OFF (owner 2026-10-02: "လောလောဆယ် မပို့သေးနဲ့"). Enable later with
    env PSVIBE_TERMS_IMAGE_ENABLED=1 (no code change).
    """
    return str(os.environ.get("PSVIBE_TERMS_IMAGE_ENABLED", "0")).strip().lower() \
        in ("1", "true", "yes", "on")


def _terms_image_local_path() -> str:
    """Local path of the served terms image, derived from PSVIBE_TERMS_IMAGE_URL."""
    try:
        from urllib.parse import urlparse
        name = os.path.basename(urlparse(PSVIBE_TERMS_IMAGE_URL).path)
        if not name:
            return ""
        return os.path.join(os.path.dirname(__file__), "..", "dashboard-dist", "assets", name)
    except Exception:
        return ""


def _send_telegram_customer_photo(chat_id: str, photo: str, caption: str = ""):
    """Send the terms image to a customer via the Customer Bot.

    Uploads the LOCAL file (multipart) when available — Telegram caches an image
    by URL (serving a stale copy when the file at that URL is replaced) and its
    URL fetcher can fail outright. Falls back to URL sendPhoto.
    """
    _log = logging.getLogger("psvibe_api")
    if not chat_id:
        return
    token = os.environ.get("CUSTOMER_BOT_TOKEN", "")
    if not token:
        _log.warning("[NOTIFY-PHOTO] CUSTOMER_BOT_TOKEN not configured in env")
        return
    api = f"https://api.telegram.org/bot{token}"
    caption = _anim(caption)  # animated custom emoji (owner 2026-10-02)
    local = _terms_image_local_path()
    try:
        if local and os.path.isfile(local):
            import requests as _rq
            with open(local, "rb") as fh:
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = caption
                    # caption goes through _anim() -> <tg-emoji> HTML tags,
                    # so Telegram must parse it as HTML (raw tags leaked before).
                    data["parse_mode"] = "HTML"
                r = _rq.post(f"{api}/sendPhoto", data=data,
                             files={"photo": (os.path.basename(local), fh, "image/png")},
                             timeout=90)
            _log.info("[NOTIFY-PHOTO] upload -> %s: %s", chat_id, r.text[:160])
            if r.ok:
                return
    except Exception as e:
        _log.warning("[NOTIFY-PHOTO] upload failed (%s) - falling back to URL", e)
    try:
        if not photo:
            return
        payload = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        data = _json_notify.dumps(payload).encode()
        req = urllib.request.Request(f"{api}/sendPhoto", data=data,
                                     headers={"Content-Type": "application/json"})
        _log.info("[NOTIFY-PHOTO] url -> %s: %s", chat_id,
                  urllib.request.urlopen(req).read().decode()[:160])
    except Exception as e:
        _log.warning("[NOTIFY-PHOTO] Failed to %s: %s", chat_id, e)


def _log_booking_event(booking_id, event_type, field=None, old_value=None, new_value=None,
                       actor=None, source="system", note=None):
    """Append-only audit trail for booking lifecycle changes (added 2026-10-20).

    The free-text ``console_bookings.notes`` column proved unreliable (12h time
    formatting, missing move/swap entries), so every status/deposit/reschedule
    change now leaves a structured row in ``booking_events``. Never raises.
    """
    try:
        _mysql_exec(
            "INSERT INTO booking_events (booking_id, event_type, field, old_value, new_value, actor, source, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (int(booking_id), str(event_type)[:40], (str(field)[:48] if field else None),
             (str(old_value)[:160] if old_value is not None else None),
             (str(new_value)[:160] if new_value is not None else None),
             (str(actor)[:80] if actor else None), str(source)[:24], (str(note)[:2000] if note else None))
        )
    except Exception as _ev_err:
        logger.warning("booking_event log failed BK#%s: %s", booking_id, _ev_err)


def _notify_deposit_forfeited(bk_id: int, row: dict, deposit_amount: int):
    """Notify deposit group when a deposit is forfeited (no-show with paid deposit)."""
    import json, urllib.request
    token = os.environ.get("BOT_TOKEN", "")
    chat_id = os.environ.get("DEPOSIT_NOTIFY_CHAT", os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747"))
    if not token:
        return
    date = str(row.get('booking_date', ''))[:10] if row.get('booking_date') else '\u2014'
    start = str(row.get('start_time', ''))[:16] if row.get('start_time') else '\u2014'
    console = str(row.get('console_id', 'PS5'))
    game = str(row.get('game_name', '\u2014'))
    member = str(row.get('member_id', '\u2014'))
    phone = str(row.get('phone', '\u2014'))
    msg = (
        f"\u26a0\ufe0f <b>Deposit Forfeited — BK#{bk_id}</b>\n\n"
        f"\U0001f464 {member}\n"
        f"\U0001f4de {phone}\n"
        f"\U0001f4c5 {date}  \u23f0 {start}\n"
        f"\U0001f3ae {console}  \U0001f579 {game}\n"
        f"\U0001f4b0 {deposit_amount:,} Ks — <b>Forfeited</b>\n\n"
        f"<i>Customer did not check in. Deposit is forfeited as revenue.</i>"
    )
    try:
        payload = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"}
            ),
            timeout=5
        )
    except Exception as _e:
        logger.warning(f"[_notify_deposit_forfeited] bk#{bk_id} notify failed: {_e}")


def _notify_deposit_refunded(bk_id: int, row: dict, deposit_amount: int, method: str = ""):
    """Notify deposit group when a deposit is refunded."""
    import json, urllib.request
    token = os.environ.get("BOT_TOKEN", "")
    chat_id = os.environ.get("DEPOSIT_NOTIFY_CHAT", os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747"))
    if not token:
        return
    date = str(row.get('booking_date', ''))[:10] if row.get('booking_date') else '\u2014'
    start = str(row.get('start_time', ''))[:16] if row.get('start_time') else '\u2014'
    console = str(row.get('console_id', 'PS5'))
    member = str(row.get('member_id', '\u2014'))
    phone = str(row.get('phone', '\u2014'))
    method_label = f" ({method})" if method else ""
    msg = (
        f"\U0001f504 <b>Deposit Refunded — BK#{bk_id}</b>\n\n"
        f"\U0001f464 {member}\n"
        f"\U0001f4de {phone}\n"
        f"\U0001f4c5 {date}  \u23f0 {start}\n"
        f"\U0001f3ae {console}\n"
        f"\U0001f4b0 {deposit_amount:,} Ks{method_label} — <b>Refunded</b>\n\n"
        f"<i>Deposit has been refunded to customer.</i>"
    )
    try:
        payload = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"}
            ),
            timeout=5
        )
    except Exception as _e:
        logger.warning(f"[_notify_deposit_refunded] bk#{bk_id} notify failed: {_e}")


def _notify_no_show_cancel(bk_id: int, row: dict):
    """Notify customer their booking was auto-cancelled (no-show)."""
    tg_chat_id = str(row.get('telegram_chat_id', ''))
    if not tg_chat_id:
        return
    date = str(row.get('booking_date', ''))[:10] if row.get('booking_date') else '—'
    start = str(row.get('start_time', ''))[:16] if row.get('start_time') else '—'
    console = str(row.get('console_id', 'PS5'))
    game = str(row.get('game_name', '—'))
    msg = (
        f"⏰ <b>Booking #{bk_id} — Auto-Cancelled</b>\n\n"
        f"သတ်မှတ်ချိန်အတွင်း Check-in မပြုလုပ်သောကြောင့်\n"
        f"သင့် Booking အား auto-cancel ပြုလုပ်လိုက်ပါသည်။\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {date}  ⏰ {start}\n"
        f"🕹️ {console}  🎮 {game}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"📲 နောက်ထပ် Booking အသစ် တင်ပေးပါ။\n"
        f"ဆက်သွယ်ရန် @psvibeofficial"
    )
    _send_telegram_customer(tg_chat_id, msg)


def _notify_booking_rescheduled(bk_id, booking, new_console, new_date, new_time, new_dur, staff, reason):
    """Notify staff group + customer that a confirmed booking was rescheduled."""
    # Staff group
    try:
        token = os.environ.get("BOT_TOKEN", "")
        chat_id = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")
        if token and chat_id:
            old_date = str(booking.get("booking_date", ""))[:10] if booking.get("booking_date") else "\u2014"
            old_start = booking.get("planned_start")
            old_time = ""
            if old_start:
                try:
                    from datetime import datetime as _dt
                    old_time = (_dt.fromisoformat(str(old_start)) if isinstance(old_start, str) else old_start).strftime("%H:%M")
                except Exception:
                    old_time = str(old_start)[:5] if old_start else ""
            old_console = booking.get("console_id") or "\u2014"
            old_dur = booking.get("planned_duration_mins") or ""
            member = booking.get("member_id") or booking.get("customer_name") or booking.get("phone") or "Guest"
            msg = (
                f"\U0001f504 <b>Booking #{bk_id} \u2014 Rescheduled</b>\n\n"
                f"\U0001f464 {member}\n"
                f"\U0001f579\ufe0f {old_console} \u2192 <b>{new_console}</b>\n"
                f"\U0001f4c5 {old_date} \u23f0 {old_time} ({old_dur}m)\n"
                f"   \u21b3 <b>{new_date} \u23f0 {new_time} ({new_dur}m)</b>\n"
                + (f"\U0001f464 By: {staff}\n" if staff else "")
                + (f"\U0001f4dd Reason: {reason}\n" if reason else "")
                + f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
            )
            payload = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload, headers={"Content-Type": "application/json"}), timeout=5)
    except Exception as _e:
        logger.warning("[reschedule] staff notify failed: %s", _e)

    # Customer
    try:
        tg = str(booking.get("telegram_chat_id", "") or "")
        if tg:
            _send_telegram_customer(tg, (
                f"\U0001f504 <b>Booking #{bk_id} \u2014 Rescheduled</b>\n\n"
                f"\U0001f4c5 {new_date}  \u23f0 {new_time}\n"
                f"\U0001f579\ufe0f Console: {new_console}  \u23f1\ufe0f {new_dur} min\n\n"
                f"Booking \u1015\u103c\u1031\u102c\u1004\u103a\u1038\u101c\u1032\u1015\u103c\u102e\u1038\u1015\u102b\u104b\n"
                f"\u1006\u1000\u103a\u101e\u1000\u1039\u101c\u102c\u101b\u103e\u1014\u1039 @psvibeofficial"
            ))
    except Exception as _e:
        logger.warning("[reschedule] customer notify failed: %s", _e)


# ═══════════════════════════════════════
@router.post("/bookings/extend-duration", response_model=GenericResponse, tags=["Bookings"], summary="Extend active booking duration [MySQL]")
async def api_extend_booking_duration(req: dict, auth=Depends(verify_api_key)):
    """Extend active booking duration by extra_mins; updates console_sessions."""
    try:
        console_id = req.get("console_id")
        extra_mins = int(req.get("extra_mins", 0))
        if not console_id or extra_mins <= 0:
            return error_response(message="console_id and extra_mins required")
        row = _mysql_query_one(
            "SELECT cs.booking_id AS id, cs.actual_duration_mins AS duration_mins FROM console_sessions cs WHERE cs.console_id=%s AND cs.status='active' ORDER BY cs.id DESC LIMIT 1",
            (console_id,)
        )
        if not row:
            return error_response(message="No active booking found")
        new_duration = (row["duration_mins"] or 0) + extra_mins
        _dur_end = 360 if new_duration == 0 else new_duration
        _booking_id = row["id"]
        _mysql_exec("UPDATE console_sessions SET actual_duration_mins=%s, actual_end=DATE_ADD(actual_start, INTERVAL %s MINUTE) WHERE booking_id=%s AND status='active'", (new_duration, _dur_end, _booking_id))
        # Also update console_bookings planned_end/duration for conflict checks
        _mysql_exec("UPDATE console_bookings SET planned_duration_mins=%s, planned_end=DATE_ADD(planned_start, INTERVAL %s MINUTE) WHERE id=%s AND status='Active'", (new_duration, _dur_end, _booking_id))
        # Update console_status to reflect extended session end time
        _mysql_exec("UPDATE console_status SET last_updated=NOW() WHERE console_id=%s", (console_id,))
        # ── Reschedule API session timer to the NEW end ──
        # 🐛 Fix (2026-09-26): extend-duration previously updated the DB duration/end
        # but never re-armed the API session_timer. The running timer kept its OLD
        # anchor (old_end - 5min), so after an extend it woke EARLY, re-fetched the
        # (correctly extended) actual_end, and fired "Session ending soon" every 5 min
        # from the stale anchor — the reported "reminder ကြိုကျ + duplicate" bug.
        try:
            _trow = _mysql_query_one(
                "SELECT actual_start, member_id FROM console_sessions WHERE booking_id=%s AND status='active'",
                (_booking_id,)
            )
            if _trow and _trow.get("actual_start") and new_duration and new_duration > 0:
                _st = _trow["actual_start"]
                if hasattr(_st, "tzinfo") and _st.tzinfo is None:
                    _st = _st.replace(tzinfo=timezone(timedelta(hours=6, minutes=30)))
                schedule_session_timer(
                    _booking_id, console_id,
                    _trow.get("member_id") or "Guest",
                    _st, new_duration,
                )
                logger.info("extend-duration: rescheduled timer for booking %s → dur=%d", _booking_id, new_duration)
        except Exception as _tm_err:
            logger.warning("extend-duration: timer reschedule failed (non-critical): %s", _tm_err)
        # ── Sync updated duration to bot's session_reminders.json ──
        try:
            _br_file = "/opt/psvibe/sales-bot/data/session_reminders.json"
            if os.path.isfile(_br_file):
                with open(_br_file, "r") as _brf:
                    _br_store = _json_notify.load(_brf)
                _br_updated = False
                for _br_key, _br_val in list(_br_store.items()):
                    if _br_val.get("cid") == console_id:
                        # Recalculate new end time from DB
                        _br_row = _mysql_query_one(
                            "SELECT actual_start, actual_duration_mins FROM console_sessions WHERE booking_id=%s AND status='active'",
                            (_booking_id,)
                        )
                        if _br_row and _br_row.get("actual_start") and _br_row.get("actual_duration_mins"):
                            from datetime import timedelta as _td
                            _br_new_end = _br_row["actual_start"] + _td(minutes=_br_row["actual_duration_mins"])
                            if hasattr(_br_new_end, 'tzinfo') and _br_new_end.tzinfo is None:
                                _br_new_end = _br_new_end.replace(tzinfo=timezone(timedelta(hours=6, minutes=30)))
                            _br_val["end_t"] = _br_new_end.strftime("%H:%M")
                            _br_val["end_dt_iso"] = _br_new_end.isoformat()
                            _br_val["planned_mins"] = new_duration
                            _br_val["total_plan_mins"] = new_duration
                            _br_val["updated_at"] = datetime.now(timezone.utc).isoformat()
                            _br_updated = True
                            logger.info("extend-duration: synced %s → end_t=%s (dur=%d)", _br_key, _br_val["end_t"], new_duration)
                if _br_updated:
                    _br_tmp = _br_file + ".tmp"
                    with open(_br_tmp, "w") as _brf:
                        _json_notify.dump(_br_store, _brf, indent=2)
                    os.replace(_br_tmp, _br_file)
        except Exception as _br_err:
            logger.warning("extend-duration: bot reminder sync failed: %s", _br_err)
        return ok({"booking_id": _booking_id, "new_duration_mins": new_duration})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/bookings/mark-bot-reminder", response_model=GenericResponse, tags=["Bookings"], summary="Mark booking for bot-managed reminders (prevents API timer duplication)")
async def api_mark_bot_reminder(req: dict, auth=Depends(verify_api_key)):
    """Called by the Sales Bot when _remind_loop starts handling a session.
    Updates telegram_chat_id so the API-side session_timer will skip this booking."""
    try:
        console_id = req.get("console_id", "")
        telegram_chat_id = req.get("telegram_chat_id", "")
        if not console_id or not telegram_chat_id:
            return error_response(message="console_id and telegram_chat_id required")
        row = _mysql_query_one(
            "SELECT cs.booking_id AS id, cb.telegram_chat_id FROM console_sessions cs JOIN console_bookings cb ON cb.id=cs.booking_id WHERE cs.console_id=%s AND cs.status='active' ORDER BY cs.id DESC LIMIT 1",
            (console_id,)
        )
        if not row:
            return error_response(message="No active booking found for this console")
        # Only update if telegram_chat_id is currently empty (avoid overwriting real customer chat IDs)
        existing_tg = (row.get("telegram_chat_id") or "").strip()
        if not existing_tg:
            _mysql_exec("UPDATE console_bookings SET telegram_chat_id=%s WHERE id=%s",
                       (str(telegram_chat_id), row["id"]))
            logger.info(f"Booking {row['id']}: set telegram_chat_id={telegram_chat_id} (bot handles reminders)")
        else:
            logger.debug(f"Booking {row['id']}: telegram_chat_id already set to {existing_tg}, skipping")
        return ok({"booking_id": row["id"]})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  WEBHOOK — Booking Reminder (30-min advance)
# ═══════════════════════════════════════

# ═══════════════════════════════════════
@router.get("/next_voucher", response_model=GenericResponse, tags=["Utility"], summary="Get next voucher number [MySQL]")
async def api_next_voucher(auth=Depends(verify_api_key)):
    """Get next available voucher number from MySQL."""
    try:
        rows = _mysql_query("SELECT receipt_no FROM receipts ORDER BY id DESC LIMIT 1")
        if rows and rows[0]["receipt_no"]:
            parts = rows[0]["receipt_no"].rsplit("-", 1)
            next_no = int(parts[1]) + 1 if len(parts) == 2 and parts[1].isdigit() else 1
        else:
            next_no = 1
        return ok(f"V-{next_no:03d}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))






@router.post("/bookings/checkin", response_model=GenericResponse, tags=["Bookings"], summary="Check in a customer [MySQL]")
async def api_booking_checkin(req: dict, request: Request, auth=Depends(verify_api_key)):
    """Mark a confirmed booking as checked in (Active). Updates booking status and console.
    Accepts optional consoleId to assign/override the console at checkin time.
    Staff App: check-in staff is taken from the JWT when staff_name is omitted."""
    try:
        booking_id = req.get("id") or req.get("booking_id")
        if not booking_id:
            return error_response(message="booking id required")
        
        rows = _mysql_query(
            "SELECT id, console_id, branch_id, member_id, booking_date, "
            "planned_start AS start_time, planned_end AS end_time, "
            "planned_duration_mins AS duration_mins, "
            "status, staff_name, notes, telegram_chat_id, phone, "
            "game_name, admin_notify_msg_id, created_at, cancelled_at "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not rows:
            return error_response(message="Booking not found")
        
        booking = rows[0]
        
        # Optional console override from checkin payload
        override_console = req.get("consoleId") or req.get("console_id") or ""
        console_id = override_console if override_console else (booking.get("console_id", "") or "")
        # If console was overridden at checkin, update the booking's console_id
        if override_console and override_console != booking.get("console_id", ""):
            _mysql_exec("UPDATE console_bookings SET console_id=%s WHERE id=%s", (override_console, booking_id))
        telegram_chat_id = booking.get("telegram_chat_id", "")
        
        # Read the actual staff who is performing the check-in (optional, from bot/API caller);
        # Staff App sends no name — fall back to the JWT identity for the audit trail.
        _checkin_staff = (req.get("staff_name", "") or req.get("staff", "")
                          or staff_audit.identity_from_request(request, req))
        
        # CI-3: Wrap status update in transaction (checked_in only marks arrival, NO timer start)
        _now_ref = now_mmt()
        _chk_conn = _mc.connect(
            host=os.environ.get('MYSQL_HOST', 'localhost'),
            user=os.environ.get('MYSQL_USER', 'root'),
            password=os.environ.get('MYSQL_PASSWORD', ''),
            database=os.environ.get('MYSQL_DATABASE', 'psvibe'),
            port=int(os.environ.get('MYSQL_PORT', '3306')),
            charset='utf8mb4'
        )
        _chk_conn.start_transaction()
        _chk_cur = _chk_conn.cursor()
        try:
            # Mark as checked_in (customer arrived, but session NOT started yet)
            # Insert a session row for checked_in (new schema: sessions track checked_in)
            # Use the checkin staff's name if provided, otherwise fall back to booking's staff_name
            if _checkin_staff:
                _chk_cur.execute(
                    "INSERT INTO console_sessions (booking_id, branch_id, console_id, member_id, actual_start, actual_duration_mins, status, game_name, staff_name, customer_name, notes, created_at, source) "
                    "SELECT %s, COALESCE(branch_id,1), console_id, member_id, planned_start, planned_duration_mins, 'checked_in', game_name, %s, customer_name, notes, %s, 'booking' "
                    "FROM console_bookings WHERE id=%s",
                    (booking_id, _checkin_staff, datetime.now(timezone(timedelta(hours=6, minutes=30))), booking_id)
                )
            else:
                _chk_cur.execute(
                    "INSERT INTO console_sessions (booking_id, branch_id, console_id, member_id, actual_start, actual_duration_mins, status, game_name, staff_name, customer_name, notes, created_at, source) "
                    "SELECT %s, COALESCE(branch_id,1), console_id, member_id, planned_start, planned_duration_mins, 'checked_in', game_name, staff_name, customer_name, notes, %s, 'booking' "
                    "FROM console_bookings WHERE id=%s",
                    (booking_id, datetime.now(timezone(timedelta(hours=6, minutes=30))), booking_id)
                )
            # Don't start timer — session start is a separate action
            # Don't set console_status to Active — console remains Free/Reserved until session starts
            
            # Also update booking status to checked_in
            _chk_cur.execute(
                "UPDATE console_bookings SET status=%s WHERE id=%s",
                ("checked_in", booking_id)
            )

            _chk_conn.commit()

            # Restore deposit if previously forfeited (auto-cancel then reactivate)
            _dep_check = _mysql_query_one(
                "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
                (booking_id,)
            )
            if _dep_check and _dep_check.get('deposit_status') == 'forfeited' and int(_dep_check.get('deposit_amount', 0) or 0) > 0:
                _mysql_exec(
                    "UPDATE console_bookings SET deposit_status='verified' WHERE id=%s AND deposit_status='forfeited' AND deposit_amount > 0",
                    (booking_id,)
                )
                # Reverse forfeit cash_movement — insert eject (🐛 2026-07-31: checkin restore was missing this,
                # leaving phantom forfeit injects for redeemed deposits)
                _dep_amt = int(_dep_check.get('deposit_amount', 0) or 0)
                _dep_meth = (_dep_check.get('deposit_method', '') or 'kpay').lower()
                _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(_dep_meth, "KPay")
                if _dep_amt > 0:
                    insert_cash_movement_if_absent(
                        "eject", _meth_label, _dep_amt,
                        f"Refund forfeit BK#{booking_id}%",
                        f"Refund forfeit BK#{booking_id} (deposit reverse - reactivated via checkin) via {_meth_label}", "system", "deposit_refund")
                    logger.info(f"Checkin BK#{booking_id}: deposit forfeit {_dep_amt}Ks reversed (eject)")
        except Exception:
            _chk_conn.rollback()
            raise
        finally:
            try:
                _chk_conn.close()
            except Exception:
                pass  # non-critical: connection close
        
        
        # ⏰ Timer is now handled by Sale Bot's _remind_loop (admin_bookings.py schedules it after checkin)
        
        # 🐛 Fix: Cancel pending booking reminders after check-in (prevents stale "Booking Confirmed" messages)
        try:
            from booking_reminder import cancel_booking_reminder
            cancel_booking_reminder(str(booking_id))
            logger.info("Cancelled booking reminders for checked-in booking %s", booking_id)
        except Exception as _ce:
            logger.debug("Cancel booking reminder failed (non-critical): %s", _ce)
        
        # 📢 Send Telegram notification to admin group
        STAFF_NOTIFY_CHAT = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")
        BOT_TOKEN = os.environ.get("BOT_TOKEN", "8545665013:AAFgEuw4V_715Q9yzGOYloinIdbdYXYb8zU")
        try:
            _session_msg = (
                "✅ *Customer Checked In!*\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 Member: `{booking.get('member_id', '')}`\n"
                f"🎮 Console: `{console_id}`\n"
                f"⏱ Time: `{now_mmt().strftime('%H:%M')} MMT`\n"
                f"📋 Status: Checked In (Session မစသေးပါ)\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            _tel_payload = json.dumps({
                "chat_id": STAFF_NOTIFY_CHAT,
                "text": _session_msg,
                "parse_mode": "Markdown"
            }).encode()
            _tel_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            _tel_req = urllib.request.Request(
                _tel_url, data=_tel_payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(_tel_req, timeout=5)
            logger.info("Check-in notification sent for booking %s", booking_id)
        except Exception as e:
            logger.warning("Session notification error: %s", e)
        
        return ok({
            "console_id": console_id,
            "telegram_chat_id": telegram_chat_id,
            "duration_mins": booking.get("duration_mins", 60),
            "member_id": booking.get("member_id", ""),
        })
    except Exception as e:
        return error_response(message=str(e))

# ═══════════════════════════════════════
#  MUTATION — create_booking
# ═══════════════════════════════════════
@router.post("/create_booking", response_model=GenericResponse, tags=["Bookings"], summary="[DEPRECATED] Create new console booking — use POST /api/bookings instead [MySQL]", deprecated=True)
async def api_create_booking(req: dict, auth=Depends(verify_api_key)):
    """DEPRECATED: Use POST /api/bookings instead. This endpoint is now hard-disabled."""
    return JSONResponse(content={"success": False, "error": "This endpoint is deprecated. Use POST /api/bookings instead."}, status_code=410)
    # --- LEGACY CODE BELOW — NO LONGER EXECUTED ---
    try:
        now = now_mmt()
        console_id = req.get("console_id", "")
        member_id = req.get("member_id", "")
        staff = req.get("staff", "")
        notes = req.get("notes", "")
        bk_id = f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ','').replace('-','')}-{now.strftime('%H%M')}"
        _mysql_exec(
            "INSERT INTO console_bookings (console_id, member_id, booking_date, planned_start, status, staff_name, customer_name, notes, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'booking')",
            (console_id, member_id, now.date(), now, "Active", staff, staff, notes))
        # Extract game name from notes (format: "GameName" or "GameName [BK#123]")
        _game_name = notes
        if " [BK#" in _game_name:
            _game_name = _game_name.split(" [BK#")[0]
        # Sync console_status: mark console as Active (prevent duplicate sessions)
        _mysql_exec(
            "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
            (member_id, _game_name, now, console_id))
        # 📢 Send Telegram booking notification to admin group
        STAFF_NOTIFY_CHAT = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")
        BOT_TOKEN = os.environ.get("BOT_TOKEN", "8545665013:AAFgEuw4V_715Q9yzGOYloinIdbdYXYb8zU")
        try:
            _bk_msg = (
                "📅 *New Booking Created!*\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 Member: `{member_id}`\n"
                f"🎮 Console: `{console_id}`\n"
                f"⏰ Time: `{now.strftime('%H:%M')} MMT`\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            _bk_payload = json.dumps({
                "chat_id": STAFF_NOTIFY_CHAT,
                "text": _bk_msg,
                "parse_mode": "Markdown"
            }).encode()
            _bk_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            _bk_req = urllib.request.Request(
                _bk_url, data=_bk_payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(_bk_req, timeout=5)
            logger.info("Booking notification sent for %s", bk_id)
        except Exception as e:
            logger.warning("Booking notification error: %s", e)
        
        return ok({"booking_id": bk_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — create_booking (NEW — Web Dashboard)
# ═══════════════════════════════════════
@router.post("/bookings", response_model=GenericResponse, tags=["Bookings"], summary="Create booking [MySQL] — Web Dashboard + Customer Bot merged")
async def api_bookings_create(req: dict, auth=Depends(verify_api_key), _rl=Depends(rate_limit_10pm)) -> JSONResponse:
    """Create a booking. Supports both Web Dashboard format and Customer Bot format.
    Detect by checking if 'customerName' (bot) or 'customer_name' (web dashboard) is present.
    """
    try:
        customer_name_bot = req.get("customerName", "").strip()
        customer_name_web = req.get("customer_name", "").strip()

        # Detect format: web dashboard sends snake_case fields (customer_name, booking_date, start_time)
        # Customer bot sends camelCase fields (customerName, timeSlot, date)
        is_web_format = bool(customer_name_web) or bool(req.get("booking_date", "")) or bool(req.get("start_time", ""))

        # ── WEB DASHBOARD FORMAT (check first to avoid bot branch conflict) ──
        if is_web_format:
            customer_name = customer_name_web or customer_name_bot
            phone = (req.get("phone") or "").strip()
            member_id = (req.get("member_id") or "").strip() or None
            console_id = (req.get("console_id") or "").strip()
            game_name = (req.get("game_name") or "").strip()
            booking_date = (req.get("booking_date") or "").strip()
            start_time_str = (req.get("start_time") or "").strip()
            duration_mins = int(req.get("duration_mins", 60))
            notes = (req.get("notes") or "").strip()
            add_deposit = req.get("add_deposit", False)
            deposit_amount = int(req.get("deposit_amount", 0) or 0)
            deposit_method = (req.get("deposit_method") or "").strip().lower()
            if deposit_method == "mmqr":
                deposit_method = "mmqr_cb"

            # ── Validation ──
            if not console_id:
                return error_response(message="Console is required")
            if not booking_date:
                return error_response(message="Booking date is required")
            if not start_time_str:
                return error_response(message="Start time is required")
            if duration_mins < 30 or duration_mins > 480:
                return error_response(message="Duration must be between 30 and 480 minutes")
            if not customer_name and not member_id:
                return error_response(message="Customer name or member is required")

            # ── Deposit validation ──
            if add_deposit:
                if deposit_amount <= 0:
                    return error_response(message="Deposit amount must be greater than 0")
                if deposit_method not in ("kpay", "wavepay", "aya_pay", "mmqr_cb"):
                    return error_response(message="Invalid deposit_method. Must be kpay, wavepay, aya_pay, or mmqr_cb")

            # ── Parse date/time ──
            from datetime import datetime as _dt, timedelta as _td
            try:
                start_dt = _dt.strptime(f"{booking_date} {start_time_str}:00", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return error_response(message="Invalid date/time format. Use YYYY-MM-DD for date and HH:MM for time.")

            end_dt = start_dt + _td(minutes=duration_mins)
            start_mmt = now_mmt()

            # ── Web dashboard bookings always auto-confirm ──
            _bk_status = "confirmed"

            # ── Generate booking ID for reference ──
            bk_date_str = start_dt.strftime("%Y%m%d")
            bk_cid_slug = console_id.replace(" ", "").replace("-", "")[:12]
            bk_ref = f"BK-{bk_date_str}-{bk_cid_slug}-{start_dt.strftime('%H%M')}"

            # ── Insert with MySQL transaction ──
            _conn = _mc.connect(
                host=os.environ.get('MYSQL_HOST', 'localhost'),
                user=os.environ.get('MYSQL_USER', 'root'),
                password=os.environ.get('MYSQL_PASSWORD', ''),
                database=os.environ.get('MYSQL_DATABASE', 'psvibe'),
                port=int(os.environ.get('MYSQL_PORT', '3306')),
                charset='utf8mb4'
            )
            _conn.start_transaction()
            _cur = _conn.cursor(dictionary=True)
            try:
                # Staff name from auth context (the logged-in dashboard user)
                _staff_name = customer_name if not member_id else member_id

                # Build INSERT columns
                _cols = ["console_id", "booking_date", "planned_start", "planned_end",
                         "planned_duration_mins", "status", "staff_name", "customer_name", "notes", "phone", "game_name",
                         "created_at", "source"]
                _vals = [console_id, booking_date, start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                         end_dt.strftime('%Y-%m-%d %H:%M:%S'),
                         duration_mins, _bk_status, _staff_name, _staff_name, notes, phone, game_name,
                         start_mmt.strftime('%Y-%m-%d %H:%M:%S'), "booking"]

                if member_id:
                    _cols.append("member_id")
                    _vals.append(member_id)

                if add_deposit:
                    _cols.extend(["deposit_status", "deposit_method", "deposit_amount"])
                    _vals.extend(["paid", deposit_method, deposit_amount])

                _placeholders = ", ".join("%s" for _ in _vals)
                _sql = f"INSERT INTO console_bookings ({', '.join(_cols)}) VALUES ({_placeholders})"
                _cur.execute(_sql, tuple(_vals))
                bk_id = _cur.lastrowid

                _conn.commit()
            except Exception as _tx_err:
                _conn.rollback()
                _conn.close()
                raise _tx_err
            finally:
                try:
                    _conn.close()
                except Exception:
                    pass

            # ── Sync console status ──
            try:
                _sync_console_status(console_id)
            except Exception:
                pass

            return ok({
                "booking_id": bk_id,
                "ref": bk_ref,
                "console_id": console_id,
                "status": _bk_status,
                "start_time": start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                "duration_mins": duration_mins,
                "deposit_status": "paid" if add_deposit else "none",
            })

        # ── CUSTOMER BOT FORMAT ──
        elif customer_name_bot:
            now = now_mmt()
            # FIX 2.7: Server-side MAX_SESSION_MINS validation
            MAX_SESSION_MINS = 1440  # 24 hours
            duration_mins_raw = req.get("durationMins", req.get("duration_mins", 0))
            try:
                duration_mins_raw = int(duration_mins_raw) if duration_mins_raw else 0
            except (ValueError, TypeError):
                duration_mins_raw = 0
            if not duration_mins_raw or duration_mins_raw <= 0 or duration_mins_raw > MAX_SESSION_MINS:
                return JSONResponse(content={"success": False, "message": f"duration_mins must be 1-{MAX_SESSION_MINS}"}, status_code=400)
            logging.warning("api_bookings_create [bot]: req keys=%s", list(req.keys()))

            telegram_chat_id = req.get("telegramChatId", "")
            phone = req.get("phone", "")
            # Fallback: look up telegram_chat_id from phone if not provided in request
            if not telegram_chat_id and phone:
                _tg = _mysql_query_one(
                    "SELECT telegram_chat_id FROM console_bookings WHERE phone=%s AND telegram_chat_id IS NOT NULL AND telegram_chat_id != '' ORDER BY id DESC LIMIT 1",
                    (phone,)
                )
                if _tg:
                    telegram_chat_id = _tg["telegram_chat_id"]
            booking_date_str = req.get("date", "")
            time_slot = req.get("timeSlot", "")
            console_type = req.get("consoleType", "PS5")
            console_id = req.get("console_id", "")
            duration_mins = int(req.get("durationMins", 60))
            game_name = req.get("gameName", "")
            username = req.get("username", "")

            # MI-4: Validate duration
            if duration_mins < 1 or duration_mins > 1440:
                return JSONResponse(content={"success": False, "message": "Duration must be 1-1440 minutes"}, status_code=400)

            from datetime import datetime, timedelta
            # Handle multiple date formats (bot sends M/D/YYYY, API expects YYYY-MM-DD)
            _parsed_date = booking_date_str
            for _fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d"):
                try:
                    _parsed_date = datetime.strptime(booking_date_str, _fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

            # HI-4: Validate date/time — return error instead of silently defaulting to now
            try:
                _mmt_tz = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))
                start_dt = datetime.strptime(f"{_parsed_date} {time_slot}", "%Y-%m-%d %H:%M").replace(tzinfo=_mmt_tz)
                if start_dt < now_mmt():
                    return JSONResponse(content={"success": False, "message": "Cannot book for a past date/time"}, status_code=400)
            except ValueError:
                return JSONResponse(content={"success": False, "message": f"Invalid time format: {time_slot}"}, status_code=400)

            end_dt = start_dt + timedelta(minutes=duration_mins)

            # CI-1 & CI-3: Wrap conflict check + INSERT in a transaction with row-level locking
            conn = _mc.connect(
            host=os.environ.get('MYSQL_HOST', 'localhost'),
            user=os.environ.get('MYSQL_USER', 'root'),
            password=os.environ.get('MYSQL_PASSWORD', ''),
            database=os.environ.get('MYSQL_DATABASE', 'psvibe'),
            port=int(os.environ.get('MYSQL_PORT', '3306')),
            charset='utf8mb4'
        )
            conn.start_transaction()
            cur = conn.cursor(dictionary=True)
            try:
                # ── Slot conflict check with FOR UPDATE locking ──
                # CI-5: Conflict check — find ALL busy consoles (not just first)
                _conflict_ids = set()
                if console_id:
                    # Specific console: check this one directly
                    cur.execute(
                        "SELECT id FROM console_bookings WHERE console_id=%s AND (status IN ('confirmed','pending_check_in','checked_in','Active') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != '') OR created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)))) AND planned_start < %s AND planned_end > %s LIMIT 1 FOR UPDATE",
                        (console_id, end_dt, start_dt)
                    )
                    if cur.fetchone():
                        _conflict_ids.add(console_id)
                else:
                    # No console: find ALL matching consoles, identify which are busy
                    _matching_consoles = cur.execute(
                        "SELECT console_id FROM console_status WHERE console_type=%s", (console_type,)
                    )
                    _matching_consoles = cur.fetchall()
                    _cids = [c["console_id"] for c in (_matching_consoles or [])]
                    if _cids:
                        _placeholders = ",".join(["%s"] * len(_cids))
                        cur.execute(
                            f"SELECT DISTINCT console_id FROM console_bookings WHERE console_id IN ({_placeholders}) AND (status IN ('confirmed','pending_check_in','checked_in','Active') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != '') OR created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)))) AND planned_start < %s AND planned_end > %s FOR UPDATE",
                            tuple(_cids) + (end_dt, start_dt)
                        )
                        _conflict_ids = {r["console_id"] for r in cur.fetchall()}
                    else:
                        _cids = []

                # Specific console requested and busy → reject
                if console_id and console_id in _conflict_ids:
                    conn.rollback()
                    conn.close()
                    return JSONResponse(content={"success": False, "message": f"\u23f0 Console {console_id} \u101e\u100a\u103a {_parsed_date} {time_slot} \u1010\u103d\u1004\u103a \u1000\u103c\u102d\u102f\u1010\u1004\u103a\u1005\u102c\u101b\u1004\u103a\u1038\u101b\u103e\u102d\u1015\u103c\u102e\u1038\u1016\u103c\u1005\u103a\u1015\u102b\u101e\u100a\u103a"}, status_code=409)

                # Auto-assign: pick first available console from matching type
                if not console_id:
                    _available = [cid for cid in _cids if cid not in _conflict_ids]
                    if _available:
                        console_id = _available[0]
                    else:
                        conn.rollback()
                        conn.close()
                        return JSONResponse(content={"success": False, "message": f"\u23f0 {_parsed_date} {time_slot} \u1010\u103d\u1004\u103a {console_type} \u1005\u1000\u103a\u1021\u102c\u1038\u101c\u102f\u1036\u1038 \u1019\u101b\u1010\u1031\u102c\u1037\u1015\u102b"}, status_code=409)

                notes = f"Game: {game_name} | Phone: {phone} | User: {username}"

                # Staff bookings auto-confirm; customer bookings need approval
                _bk_status = "confirmed" if req.get("source") == "staff" else "pending"

                # ── H8 FIX: Resolve member_id from payload, phone, telegram_chat_id ──
                bk_member_id = req.get("memberId", req.get("member_id", ""))

                # If member_id from payload, verify it exists in member_wallets
                if bk_member_id:
                    cur.execute("SELECT member_id FROM member_wallets WHERE member_id=%s", (bk_member_id,))
                    if not cur.fetchone():
                        bk_member_id = ""  # Invalid — clear it

                # Fallback 1: resolve from phone (member_wallets)
                if not bk_member_id and phone:
                    cur.execute("SELECT member_id FROM member_wallets WHERE phone=%s AND member_id IS NOT NULL AND member_id != ''", (phone,))
                    _pm_rows = cur.fetchall()
                    if _pm_rows:
                        bk_member_id = _pm_rows[0]["member_id"]

                # Fallback 2: telegram_chat_id → member_wallets (direct match)
                if not bk_member_id and telegram_chat_id:
                    cur.execute("SELECT member_id FROM member_wallets WHERE member_id=%s", (telegram_chat_id,))
                    _pm_rows = cur.fetchall()
                    if _pm_rows:
                        bk_member_id = _pm_rows[0]["member_id"]

                # Fallback 3: telegram_chat_id → customer_bot_users → member_wallets
                if not bk_member_id and telegram_chat_id:
                    cur.execute("SELECT member_id FROM customer_bot_users WHERE tg_id=%s AND member_id IS NOT NULL AND member_id != ''", (telegram_chat_id,))
                    _cbu_rows = cur.fetchall()
                    if _cbu_rows:
                        _cbu_member = _cbu_rows[0]["member_id"]
                        # Verify this member_id actually exists in member_wallets
                        cur.execute("SELECT member_id FROM member_wallets WHERE member_id=%s", (_cbu_member,))
                        if cur.fetchone():
                            bk_member_id = _cbu_member

                # Fallback 4: auto-create member record if phone provided but no member found
                if not bk_member_id and phone:
                    _auto_member_id = f"MEM{phone.replace('+','').replace(' ','')[-8:]}"
                    try:
                        cur.execute(
                            "INSERT INTO member_wallets (member_id, phone, member_name, balance_mins, tier, created_at) "
                            "VALUES (%s, %s, %s, 0, 'standard', NOW()) "
                            "ON DUPLICATE KEY UPDATE phone=VALUES(phone)",
                            (_auto_member_id, phone, customer_name_bot or "Unknown")
                        )
                        bk_member_id = _auto_member_id
                        logger.info("H8: Auto-created member %s for phone=%s during booking", _auto_member_id, phone)
                    except Exception as _ac_err:
                        logger.warning("H8: Auto-create member failed for phone=%s: %s", phone, _ac_err)

                # H8: Convert empty string to None (NULL in DB) + log warning for monitoring
                if not bk_member_id:
                    logger.warning("H8: booking created without member_id for phone=%s telegram_chat_id=%s", phone, telegram_chat_id)
                    bk_member_id = None  # Store NULL instead of ""

                cur.execute(
                    "INSERT INTO console_bookings (console_id, member_id, booking_date, planned_start, planned_end, status, staff_name, customer_name, notes, telegram_chat_id, planned_duration_mins, phone, game_name, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'booking')",
                    (console_id, bk_member_id, _parsed_date, start_dt, end_dt, _bk_status, customer_name_bot, customer_name_bot, notes, telegram_chat_id, duration_mins, phone, game_name)
                )
                bk_id = cur.lastrowid

                # console_status sync deferred to after commit — _sync_console_status()
                # derives the correct status (Active > Reserved > Free) safely.

                conn.commit()
            except Exception as _tx_err:
                conn.rollback()
                conn.close()
                raise _tx_err
            finally:
                try:
                    conn.close()
                except Exception:
                    pass  # non-critical: connection close

            # Sync console_status after commit (derives correct status: Active > Reserved > Free)
            if console_id:
                try:
                    _sync_console_status(console_id)
                except Exception:
                    pass

            # Store admin_notify_msg_id if provided (Sale Bot pre-sends notification)
            admin_notify_msg_id = req.get("admin_notify_msg_id", "")
            if admin_notify_msg_id:
                from mysql_db import execute as _mysql_execute
                try:
                    _mysql_execute(
                        "UPDATE console_bookings SET admin_notify_msg_id=%s WHERE id=%s",
                        (str(admin_notify_msg_id), bk_id)
                    )
                except Exception:
                    pass
            # Send admin notification ONLY if Sale Bot didn't already send one
            if not admin_notify_msg_id:
                import threading
                threading.Thread(target=_notify_booking_received, args=(customer_name_bot, booking_date_str, time_slot, console_type, duration_mins, game_name, phone, bk_id, console_id), daemon=True).start()

            return ok({"id": bk_id, "message": "Booking created"})

        # ── UNKNOWN FORMAT FALLBACK ──
        else:
            return JSONResponse(content={"success": False, "message": "Invalid request format. Use customer_name (web) or customerName (bot)."}, status_code=400)
    except Exception as e:
        logger.error(f"api_bookings_create error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))





def _raw_mysql_conn():
    """Open a raw MySQL connection (write paths) with a short retry on transient drops.

    2026-10-15: a momentary "MySQL server has gone away" (2006) on the booking-status
    endpoint surfaced as a 500 in the Staff App (BK reject). Every other endpoint
    self-heals through the pooled connection (mysql_db, DBUtils ping=1); this path
    opens its own connection, so retry briefly here instead of failing the user.
    """
    import time as _time
    _last = None
    for _attempt in range(3):
        try:
            return _mc.connect(
                host=os.environ.get('MYSQL_HOST', 'localhost'),
                user=os.environ.get('MYSQL_USER', 'root'),
                password=os.environ.get('MYSQL_PASSWORD', ''),
                database=os.environ.get('MYSQL_DATABASE', 'psvibe'),
                port=int(os.environ.get('MYSQL_PORT', '3306')),
                charset='utf8mb4',
                connection_timeout=10,
            )
        except Exception as _e:  # only transient drops are retried; others re-raise
            _last = _e
            _m = str(_e).lower()
            if ('gone away' in _m) or ('lost connection' in _m) or ('connection reset' in _m) or ("can't connect" in _m) or ('cannot connect' in _m):
                _time.sleep(0.25 * (_attempt + 1))
                continue
            raise
    raise _last


@router.patch("/bookings/{booking_id}/status", response_model=GenericResponse, tags=["Bookings"], summary="Update booking status (approve/reject) [MySQL]")
async def api_update_booking_status(booking_id: int, req: dict, request: Request, auth=Depends(verify_api_key)):
    """Update a booking's status and optionally assign a console (approve/reject/cancel).
    Staff App: the acting staff is taken from the JWT when staffName is omitted."""
    try:
        status = req.get("status", "")
        # Audit: explicit staffName wins, else JWT name/sub (Staff App never sends a name).
        _actor = staff_audit.identity_from_request(request, req, keys=("staffName", "staff_name", "staff"))
        if status not in ("pending", "confirmed", "rejected", "pending_check_in", "checked_in", "Active", "cancelled", "Done"):
            return error_response(message=f"Invalid status: {status}")
        # HI-1: Optimistic lock — only approve pending bookings
        _app_conn = _raw_mysql_conn()
        _app_conn.start_transaction()
        _app_cur = _app_conn.cursor()
        try:
            # Allow cancel from pending/confirmed/pending_check_in/checked_in
            # Allow Active from checked_in (session start for checked-in customer)
            # All other transitions from pending only
            if status == "cancelled":
                _allowed_from = "('pending','confirmed','pending_check_in','checked_in')"
            elif status == "Active":
                _allowed_from = "('pending','checked_in','cancelled')"
            else:
                _allowed_from = "('pending')"
            # For cancelled status, also stamp cancelled_at
            if status == "cancelled":
                # Fetch deposit state before cancelling (Boss policy 2026-07-31)
                _dep_bk = _mysql_query_one(
                    "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
                    (booking_id,)
                ) or {}
                _dep_status = _dep_bk.get('deposit_status') or ''
                _app_cur.execute(
                    f"UPDATE console_bookings SET status=%s, cancelled_at=DATE_ADD(NOW(), INTERVAL 390 MINUTE), "
                    f"deposit_status=CASE WHEN %s='verified' THEN 'forfeited' WHEN %s='paid' THEN 'none' ELSE deposit_status END, "
                    f"staff_name=COALESCE(NULLIF(%s, ''), staff_name), notes=COALESCE(NULLIF(%s, ''), notes) "
                    f"WHERE id=%s AND status IN {_allowed_from}",
                    (status, _dep_status, _dep_status, _actor or req.get("staffName", ""), req.get("staffNote", ""), booking_id))
            else:
                _app_cur.execute(
                    f"UPDATE console_bookings SET status=%s, staff_name=COALESCE(NULLIF(%s, ''), staff_name), notes=COALESCE(NULLIF(%s, ''), notes) WHERE id=%s AND status IN {_allowed_from}",
                    (status, _actor or req.get("staffName", ""), req.get("staffNote", ""), booking_id))
            affected = _app_cur.rowcount
            if affected == 0:
                _app_conn.rollback()
                _app_conn.close()
                return JSONResponse(content={"success": False, "message": "Booking already processed or cannot be cancelled"}, status_code=409)
            # ✅ Overlap check BEFORE confirming — prevent double-booking (2026-06-19)
            # 🐛 Fix Jun 25: Use requested console_id for overlap check, not existing one
            if status == "confirmed":
                _bk = _mysql_query_one(
                    "SELECT console_id, planned_start AS start_time, planned_end AS end_time FROM console_bookings WHERE id=%s",
                    (booking_id,)
                )
                if _bk and _bk.get("start_time") and _bk.get("end_time"):
                    _check_cid = req.get("consoleId") or _bk.get("console_id", "")
                    if _check_cid:
                        _app_cur.execute(
                            "SELECT id FROM console_bookings WHERE console_id=%s"
                            " AND (status IN ('confirmed','pending_check_in','checked_in','Active') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != '') OR created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE))))"
                            " AND id != %s"
                            " AND NOT (planned_end <= %s OR planned_start >= %s)"
                            " LIMIT 1 FOR UPDATE",
                            (_check_cid, booking_id, _bk["start_time"], _bk["end_time"])
                        )
                        _conflict = _app_cur.fetchone()
                        if _conflict:
                            _app_conn.rollback()
                            _app_conn.close()
                            return JSONResponse(content={
                                "success": False,
                                "error": f"⏰ Console {_check_cid} already has a confirmed booking (BK#{_conflict['id']}) at this time slot",
                                "conflict_id": _conflict["id"]
                            }, status_code=409)
            _old_console_id = None  # track old console for status sync after move
            if req.get("consoleId"):
                _bk_old = _mysql_query_one("SELECT console_id FROM console_bookings WHERE id=%s", (booking_id,))
                if _bk_old:
                    _old_console_id = _bk_old.get("console_id", "")
                _app_cur.execute("UPDATE console_bookings SET console_id=%s WHERE id=%s", (req["consoleId"], booking_id))
                # Also update console_sessions if active session exists
                _app_cur.execute("UPDATE console_sessions SET console_id=%s WHERE booking_id=%s AND status IN ('active','checked_in')", (req["consoleId"], booking_id))
            # Sync console_status in same transaction — prevent drift on partial failure
            _cid = req.get("consoleId") or _mysql_query_one("SELECT console_id FROM console_bookings WHERE id=%s", (booking_id,))
            if _cid:
                _console_id = _cid if isinstance(_cid, str) else _cid.get("console_id", "")
                if status == "confirmed":
                    # Booking confirmed — do NOT hold the console (keep Free for walk-ins)
                    # The booking itself shows as "Reserved" in displays, but the physical
                    # console stays Free so walk-in customers can still use it until booked time.
                    pass
                elif status == "Active":
                    # C2: Checkin race condition fix — FOR UPDATE lock on console_status
                    # Serialize console activation to prevent double-Active race on same console
                    _app_cur.execute(
                        "SELECT status FROM console_status WHERE console_id=%s FOR UPDATE",
                        (_console_id,)
                    )
                    _cs_row = _app_cur.fetchone()
                    if _cs_row and _cs_row.get('status') == 'Active':
                        _app_conn.rollback()
                        _app_conn.close()
                        return JSONResponse(content={
                            "success": False,
                            "message": f"Console {_console_id} already has an active session"
                        }, status_code=409)
                    # Checkin bind / session start — mark console as Active
                    _bk_info = _mysql_query_one(
                        "SELECT staff_name, customer_name, member_id, game_name FROM console_bookings WHERE id=%s",
                        (booking_id,)
                    ) or {}
                    _member_name = _bk_info.get("customer_name", "") or _bk_info.get("staff_name", "") or _bk_info.get("member_id", "") or ""
                    _now_mmt_str = now_mmt().strftime('%Y-%m-%d %H:%M:%S')
                    _app_cur.execute(
                        "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                        (_member_name, _bk_info.get("game_name", "") or "", _now_mmt_str, _console_id)
                    )
                elif status in ("rejected", "cancelled"):
                    _app_cur.execute("UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s", (_console_id,))
            _app_conn.commit()

            # Verified deposit cancelled -> forfeit as revenue (Boss policy 2026-07-31)
            if status == "cancelled" and _dep_status == "verified":
                _dep_amt = int(_dep_bk.get('deposit_amount') or 0)
                _dep_meth = (_dep_bk.get('deposit_method') or 'kpay').lower()
                _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(_dep_meth, "KPay")
                if _dep_amt > 0:
                    try:
                        insert_cash_movement_if_absent(
                            "inject", _meth_label, _dep_amt,
                            f"Forfeit BK#{booking_id}%",
                            f"Forfeit BK#{booking_id} via {_meth_label}", "system", "deposit_forfeit")
                        logger.info(f"api_update_booking_status forfeit: BK#{booking_id} {_dep_amt}Ks {_meth_label}")
                    except Exception as fe:
                        logger.warning(f"api_update_booking_status forfeit cash_movements BK#{booking_id}: {fe}")

            # Restore deposit if previously forfeited (auto-cancel then reactivate) — 🐛 2026-07-28
            if status not in ("cancelled", "rejected"):
                _dep_row = _mysql_query_one(
                    "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
                    (booking_id,)
                )
                if _dep_row and _dep_row.get('deposit_status') == 'forfeited' and int(_dep_row.get('deposit_amount', 0) or 0) > 0:
                    _mysql_exec(
                        "UPDATE console_bookings SET deposit_status='verified', cancelled_at=NULL WHERE id=%s AND deposit_status='forfeited' AND deposit_amount > 0",
                        (booking_id,)
                    )
                    # Reverse forfeit cash_movement — insert eject entry (🐛 2026-07-28)
                    _dep_amt = int(_dep_row.get('deposit_amount', 0) or 0)
                    _dep_meth = (_dep_row.get('deposit_method', '') or 'kpay').lower()
                    _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(_dep_meth, "KPay")
                    if _dep_amt > 0:
                        insert_cash_movement_if_absent(
                            "eject", _meth_label, _dep_amt,
                            f"Refund forfeit BK#{booking_id}%",
                            f"Refund forfeit BK#{booking_id} (reactivated) via {_meth_label}", "system", "deposit_refund")
                        logger.info(f"BK#{booking_id}: deposit forfeit {_dep_amt}Ks reversed (eject), cancelled_at cleared")

            # 📢 Customer notification on confirm/cancel (fire-and-forget)
            if status in ("confirmed", "cancelled"):
                logger.info("[NOTIFY] Triggering notification for BK#%s status=%s", booking_id, status)
                _notify_row = _mysql_query_one(
                    "SELECT telegram_chat_id, console_id, booking_date, planned_start AS start_time, planned_duration_mins AS duration_mins FROM console_bookings WHERE id=%s",
                    (booking_id,)
                )
                if _notify_row and _notify_row.get("telegram_chat_id"):
                    _tg_id = _notify_row["telegram_chat_id"]
                    _bk_id = str(booking_id)
                    if status == "confirmed":
                        _console = _notify_row.get("console_id", "") or "TBD"
                        _date = str(_notify_row.get("booking_date", ""))
                        _time = str(_notify_row.get("start_time", ""))[:16] if _notify_row.get("start_time") else ""
                        _dur = str(_notify_row.get("duration_mins", ""))
                        _msg = (
                            f"✅ <b>Booking Confirmed!</b>\n\n"
                            f"🎫 Booking <b>#{_bk_id}</b>\n"
                            f"📅 {_date}  ⏰ {_time}\n"
                            f"🎮 Console: {_console}  ⏱️ {_dur} min\n\n"
                            f"📲 Booking ချိန်ရောက်ရင် Staff ကို Check-in ပြပေးပါ။ 🎮"
                        )
                    else:
                        _msg = (
                            f"❌ <b>Booking #{_bk_id} Cancelled</b>\n\n"
                            f"Slot မအားတာ သို့မဟုတ် အကြောင်းတစ်စုံတစ်ရာကြောင့် Booking ကို cancel လုပ်လိုက်ပါတယ်။\n"
                            f"📲 နောက်ထပ် Booking အသစ် တင်ပေးပါ။"
                        )
                    _threading_notify.Thread(target=_send_telegram_customer, args=(_tg_id, _msg), daemon=True).start()

            # Safety: sync console_status in case of edge-case drift
            if _cid:
                _console_id = _cid if isinstance(_cid, str) else _cid.get("console_id", "")
                if _console_id:
                    try:
                        _sync_console_status(_console_id)
                    except Exception:
                        pass
            # 🐛 Fix Jun 25: Also sync old console when booking was moved to a new console
            if _old_console_id and _old_console_id != (_cid if isinstance(_cid, str) else _cid.get("console_id", "") if _cid else ""):
                try:
                    _sync_console_status(_old_console_id)
                except Exception:
                    pass
        except Exception:
            _app_conn.rollback()
            raise
        finally:
            try:
                _app_conn.close()
            except Exception:
                pass  # non-critical: connection close
        return ok({"booking_id": booking_id, "status": status})
    except Exception as e:
        logger.exception("api_update_booking_status failed BK#%s status=%s: %s", booking_id, req.get("status"), e)
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/bookings/broadcast-targets ── (must be BEFORE {booking_id} catch-all)
@router.get("/bookings/broadcast-targets", response_model=GenericResponse, tags=["Bookings"], summary="Get broadcast targets [MySQL]")
async def api_bookings_broadcast_targets(auth=Depends(verify_api_key)):
    """Get broadcast targets (distinct Telegram chat IDs from customer bot bookings)."""
    try:
        rows = _mysql_query(
            "SELECT DISTINCT telegram_chat_id FROM console_bookings "
            "WHERE telegram_chat_id IS NOT NULL AND telegram_chat_id != ''"
        )
        telegram_ids = [r["telegram_chat_id"] for r in rows]
        return ok({"telegram_ids": telegram_ids, "count": len(telegram_ids)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/bookings/{booking_id} ──
@router.get("/bookings/{booking_id}", response_model=GenericResponse, tags=["Bookings"], summary="Get single booking by ID [MySQL]")
async def api_get_booking(booking_id: int, request: Request, auth=Depends(verify_api_key)):
    """Fetch a single booking's full details.

    ``deposit_ref`` (the payment proof) is only returned to trusted staff/service
    callers (see ``staff_audit.can_view_payment_ref``)."""
    try:
        row = _mysql_query_one(
            "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, "
            "COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
            "cb.status, cb.staff_name, cb.customer_name, cb.notes, cb.telegram_chat_id, "
            "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
            "cb.phone, cb.game_name, cb.deposit_status, cb.deposit_amount, "
            "cb.deposit_method, cb.deposit_ref, cb.deposit_ref_type "
            "FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id=cb.id WHERE cb.id=%s",
            (booking_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Booking not found")
        start = row.get("start_time")
        time_slot = ""
        if start:
            try:
                from datetime import datetime as _dt
                start_dt = _dt.fromisoformat(start) if isinstance(start, str) else start
                time_slot = start_dt.strftime("%H:%M")
            except Exception:
                time_slot = str(start)[:5] if start else ""
        bd = row.get("booking_date")
        bd_str = str(bd)[:10] if bd else ""
        _cid = row.get("console_id", "")
        _ctype = _cid
        if _cid and not any(t in _cid.lower() for t in ("ps5", "ps4", "ps3", "xbox", "switch", "pc")):
            try:
                _crows = _mysql_query("SELECT console_type FROM console_status WHERE console_id=%s LIMIT 1", (_cid,))
                if _crows and _crows[0].get("console_type"):
                    _ctype = _crows[0]["console_type"]
            except Exception:
                pass
        _customer_name = row.get("customer_name", "") or row.get("staff_name", "") or ""
        _phone = row.get("phone", "") or ""
        _member_id = row.get("member_id", "") or ""
        _can_see_ref = staff_audit.can_view_payment_ref(request)
        # customerName priority: customer_name → member_id → staff_name → phone → 'Guest'
        if not _customer_name.strip():
            _customer_name = _member_id.strip() if _member_id and _member_id.strip() and _member_id.strip() != "Guest" else (_phone.strip() or "Guest")
            _customer_name = _phone.strip()
        else:
            _customer_name = _member_id.strip() if _member_id.strip() else "Guest"
        return ok({
            "booking": {
                "id": row.get("id"),
                "customerName": _customer_name,
                "phone": _phone,
                "date": bd_str,
                "timeSlot": time_slot,
                "consoleType": _ctype,
                "durationMins": row.get("duration_mins", 60),
                "gameName": row.get("game_name", ""),
                "console_id": row.get("console_id", ""),
                "status": row.get("status", ""),
                "notes": row.get("notes", ""),
                "telegramChatId": row.get("telegram_chat_id", ""),
                "depositAmount": row.get("deposit_amount", 0),
                "depositStatus": row.get("deposit_status", ""),
                "deposit_amount": row.get("deposit_amount", 0),
                "deposit_status": row.get("deposit_status", ""),
                "deposit_method": row.get("deposit_method", "") or "",
                # Payment proof — staff/service only.
                "deposit_ref": (row.get("deposit_ref") or "") if _can_see_ref else "",
                "deposit_ref_type": (row.get("deposit_ref_type") or "") if _can_see_ref else "",
                "can_view_proof": _can_see_ref,
            }
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/bookings/{booking_id}/deposit/proof — stream the transfer screenshot ──
# console_bookings.deposit_ref holds EITHER an http(s) URL OR a Telegram file_id
# (saved by the CUSTOMER bot, so the id is scoped to CUSTOMER_BOT_TOKEN).  An
# <img src> can never load a bare file_id, so we resolve it here, server-side,
# and stream the bytes.  The bot token is NEVER written to the response, logs,
# or any error message.
_DEPOSIT_PROOF_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads", "deposits")
_DEPOSIT_PROOF_TTL = 24 * 3600          # re-download after 24h (Telegram file paths expire)
_DEPOSIT_PROOF_MAX = 8 * 1024 * 1024    # soft cap (8 MB) — deposit screenshots are small
_IMG_CT_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                  ".webp": "image/webp", ".gif": "image/gif"}


def _proof_ct(ext: str) -> str:
    return _IMG_CT_BY_EXT.get((ext or "").lower(), "image/jpeg")


@router.get("/bookings/{booking_id}/deposit/proof", tags=["Deposits"],
            summary="Stream a booking's deposit proof image (staff-only) [MySQL]")
async def api_booking_deposit_proof(booking_id: int, request: Request, auth=Depends(verify_api_key)):
    """Stream the deposit-payment proof (transfer screenshot) for one booking.

    The ref is only served to trusted staff/service callers
    (``staff_audit.can_view_payment_ref``); the bot token used to resolve a
    Telegram ``file_id`` never appears in the response, logs, or errors."""
    if not staff_audit.can_view_payment_ref(request):
        raise HTTPException(status_code=403, detail="Not allowed to view payment proof")
    try:
        row = _mysql_query_one(
            "SELECT id, deposit_ref, deposit_ref_type FROM console_bookings WHERE id=%s",
            (booking_id,))
    except Exception:
        raise HTTPException(status_code=503, detail="Proof temporarily unavailable — please retry")
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    ref = (row.get("deposit_ref") or "").strip()
    rtype = (row.get("deposit_ref_type") or "").strip().lower()
    if not ref:
        raise HTTPException(status_code=404, detail="No deposit proof on this booking")
    if rtype not in ("image", "both"):
        raise HTTPException(status_code=404, detail="This booking has no image proof")

    _hdr = {"Cache-Control": "private, max-age=300"}

    # (a) Plain http(s) URL → stream it through (no redirect, no token leakage).
    if re.match(r"^https?://", ref, re.I):
        try:
            _rq = urllib.request.Request(ref, headers={"User-Agent": "PSVIBE/1.0"})
            with urllib.request.urlopen(_rq, timeout=10) as _up:
                body = _up.read(_DEPOSIT_PROOF_MAX)
                ctype = (_up.headers.get_content_type() or "image/jpeg")
        except Exception:
            logger.warning("deposit proof URL fetch failed (booking %s)", booking_id)
            raise HTTPException(status_code=502, detail="Could not load the proof — please retry")
        if not str(ctype).startswith("image/"):
            ctype = "image/jpeg"
        return Response(content=body, media_type=ctype, headers=_hdr)

    # (b) Telegram file_id → getFile (CUSTOMER bot token) → download → cache.
    token = os.environ.get("CUSTOMER_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
    if not token:
        raise HTTPException(status_code=503, detail="Proof service is not configured")

    # Serve a fresh cached copy when present (cache dir is outside every static mount).
    try:
        for _fn in os.listdir(_DEPOSIT_PROOF_DIR):
            if _fn.startswith("bk%d." % booking_id):
                _cp = os.path.join(_DEPOSIT_PROOF_DIR, _fn)
                if os.path.isfile(_cp) and (datetime.now().timestamp() - os.path.getmtime(_cp)) < _DEPOSIT_PROOF_TTL:
                    try:
                        with open(_cp, "rb") as _f:
                            return Response(content=_f.read(),
                                            media_type=_proof_ct(os.path.splitext(_fn)[1]), headers=_hdr)
                    except Exception:
                        pass
    except Exception:
        pass

    import urllib.parse as _uparse
    try:
        _gu = ("https://api.telegram.org/bot" + token + "/getFile?file_id="
               + _uparse.quote(ref, safe=""))
        with urllib.request.urlopen(_gu, timeout=10) as _gf:
            _info = json.load(_gf)
        _fp = ((_info or {}).get("result") or {}).get("file_path") if (_info and _info.get("ok")) else ""
        if not _fp:
            raise HTTPException(status_code=404, detail="Proof image is no longer available on Telegram")
        _ext = os.path.splitext(_fp)[1].lower()
        if _ext not in _IMG_CT_BY_EXT:
            _ext = ".jpg"
        _du = "https://api.telegram.org/file/bot" + token + "/" + _fp
        with urllib.request.urlopen(_du, timeout=10) as _df:
            body = _df.read(_DEPOSIT_PROOF_MAX)
    except HTTPException:
        raise
    except Exception:
        logger.warning("deposit proof download failed (booking %s)", booking_id)
        raise HTTPException(status_code=502, detail="Could not load the proof — please retry")

    # Best-effort cache write — never fails the request.
    try:
        os.makedirs(_DEPOSIT_PROOF_DIR, exist_ok=True)
        with open(os.path.join(_DEPOSIT_PROOF_DIR, "bk%d%s" % (booking_id, _ext)), "wb") as _cf:
            _cf.write(body)
    except Exception:
        pass
    return Response(content=body, media_type=_proof_ct(_ext), headers=_hdr)


# ── Shared booking-list helpers (Staff App Wave 4: status=open + /api/timeline) ──
# console_sessions can hold >1 row per booking_id (re-opened / duplicated sessions),
# so the list/timeline joins below resolve exactly ONE (latest) session per booking so a
# booking can never appear twice in one response.
_LATEST_SESSION_JOIN = (
    " LEFT JOIN ("
    "   SELECT s.booking_id, s.status, s.actual_start, s.actual_end, s.actual_duration_mins"
    "   FROM console_sessions s"
    "   INNER JOIN ("
    "     SELECT booking_id, MAX(id) AS max_id FROM console_sessions"
    "     WHERE booking_id IS NOT NULL GROUP BY booking_id"
    "   ) latest ON latest.max_id = s.id"
    " ) cs ON cs.booking_id = cb.id"
)

# Live booking statuses the Staff App "Open" filter must show in ONE request.
_LIVE_BOOKING_STATUSES = ("pending", "confirmed", "pending_check_in", "checked_in", "active")

# Single-query "open bookings" list (no status-filter fan-out, no N+1, no dup rows).
_OPEN_BOOKINGS_SQL = (
    "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, "
    "DATE_FORMAT(COALESCE(cs.actual_start, cb.planned_start), '%%H:%%i') AS start_time, "
    "DATE_FORMAT(COALESCE(cs.actual_end, cb.planned_end), '%%H:%%i') AS end_time, "
    "CASE WHEN LOWER(COALESCE(cs.status, '')) IN ('active','checked_in') THEN LOWER(cs.status) "
    "     ELSE LOWER(cb.status) END AS status, "
    "cb.staff_name, cb.customer_name, cb.notes, cb.telegram_chat_id, "
    "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
    "cb.phone, cb.game_name, cb.created_at, cb.cancelled_at, "
    "cb.deposit_status, cb.deposit_method, cb.deposit_amount, cb.deposit_ref, cb.deposit_ref_type "
    "FROM console_bookings cb" + _LATEST_SESSION_JOIN +
    " WHERE (LOWER(cb.status) IN ('pending','confirmed','pending_check_in','checked_in','active')"
    "        OR LOWER(COALESCE(cs.status, '')) IN ('active','checked_in'))"
    " ORDER BY cb.booking_date ASC, COALESCE(cs.actual_start, cb.planned_start) ASC"
)

# One query for a single day's grid (cancelled included so the UI can dim them).
_TIMELINE_SQL = (
    "SELECT cb.id, cb.console_id, cb.member_id, cb.customer_name, "
    "LOWER(cb.status) AS booking_status, LOWER(COALESCE(cs.status, '')) AS session_status, "
    "COALESCE(cs.actual_start, cb.planned_start) AS start_dt, "
    "COALESCE(cs.actual_end, cb.planned_end) AS end_dt, "
    "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
    "cb.deposit_status "
    "FROM console_bookings cb" + _LATEST_SESSION_JOIN +
    " WHERE cb.booking_date=%s "
    "AND (LOWER(cb.status) <> 'pending' OR cb.deposit_status IN ('paid','verified') "
    "     OR (cb.member_id IS NOT NULL AND cb.member_id != '')) "
    "ORDER BY start_dt ASC"
)


def _console_type_map(rows):
    """Batch-resolve console_id -> console_type (N+1 fix, mirrors /api/bookings)."""
    all_cids = set()
    for r in rows:
        _cid = r.get("console_id", "")
        if _cid and not any(t in _cid.lower() for t in ("ps5", "ps4", "ps3", "xbox", "switch", "pc")):
            all_cids.add(_cid)
    ctype_map = {}
    if all_cids:
        cid_list = sorted(all_cids)
        placeholders = ",".join("%s" for _ in cid_list)
        try:
            crows = _mysql_query(
                f"SELECT console_id, console_type FROM console_status WHERE console_id IN ({placeholders})",
                tuple(cid_list),
            )
            for cr in crows or []:
                ctype_map[cr.get("console_id")] = cr.get("console_type", cr.get("console_id"))
        except Exception:
            pass
    return ctype_map


def _normalize_booking_rows(rows, can_see_ref):
    """Normalise raw console_bookings rows into the Staff App booking shape.

    Mirrors GET /api/bookings exactly (customerName, timeSlot, deposit_*, console_id,
    durationMins, ...).  ``deposit_ref`` is only exposed when ``can_see_ref`` is True
    (trusted staff / service caller) — never to a non-staff token.
    """
    ctype_map = _console_type_map(rows)
    from datetime import datetime as _dt, timedelta
    normalized = []
    for r in rows:
        _cid = r.get("console_id", "")
        _ctype = ctype_map.get(_cid, _cid)
        start = r.get("start_time")
        time_slot = ""
        end_time_str = ""
        start_time_full = ""
        if start:
            try:
                if isinstance(start, str) and len(start) <= 5 and ':' in start and '-' not in start:
                    start_dt = _dt.strptime(start, "%H:%M")
                else:
                    start_dt = _dt.fromisoformat(start) if isinstance(start, str) else start
                time_slot = start_dt.strftime("%H:%M")
                start_time_full = start_dt.strftime("%H:%M")
                dur = int(r.get("duration_mins", 0) or 0)  # 0 = No Timer
                end_dt = start_dt + timedelta(minutes=dur)
                end_time_str = end_dt.strftime("%H:%M")
            except Exception:
                time_slot = str(start)[:5] if start else ""
        bd = r.get("booking_date")
        bd_str = ""
        if bd:
            try:
                bd_o = _dt.fromisoformat(bd) if isinstance(bd, str) else bd
                bd_str = bd_o.strftime("%Y-%m-%d")
            except Exception:
                bd_str = str(bd)[:10]
        _phone = r.get("phone", "") or ""
        _customer_name = r.get("customer_name", "") or r.get("staff_name", "") or ""
        _member_id = r.get("member_id", "") or ""
        # customerName priority: customer_name -> member_id -> phone -> 'Guest'
        if not _customer_name.strip():
            _customer_name = _member_id.strip() if _member_id and _member_id.strip() and _member_id.strip() != "Guest" else (_phone.strip() or "Guest")
        _created = r.get("created_at")
        _created_str = ""
        if _created:
            try:
                if hasattr(_created, 'isoformat'):
                    if _created.tzinfo is None:
                        _created_str = _created.replace(tzinfo=timezone.utc).isoformat()
                    else:
                        _created_str = _created.isoformat()
                else:
                    _created_str = str(_created)
            except Exception:
                _created_str = str(_created)
        normalized.append({
            "id": r.get("id", ""),
            "customerName": _customer_name,
            "phone": _phone,
            "date": bd_str,
            "timeSlot": time_slot,
            "startTime": start_time_full,
            "endTime": end_time_str,
            "consoleType": _ctype,
            "durationMins": r.get("duration_mins", 60),
            "gameName": r.get("game_name", ""),
            "console_id": r.get("console_id", ""),
            "consoleId": r.get("console_id", ""),
            "member_id": r.get("member_id", ""),
            "telegram_chat_id": r.get("telegram_chat_id", ""),
            "telegramChatId": r.get("telegram_chat_id", ""),
            "status": r.get("status", ""),
            "created_at": _created_str,
            "end_time": str(r.get("end_time", "") or ""),
            "cancelled_at": str(r.get("cancelled_at", "") or ""),
            "cancelledAt": str(r.get("cancelled_at", "") or ""),
            "deposit_status": r.get("deposit_status", "") or "",
            "deposit_method": r.get("deposit_method", "") or "",
            "deposit_amount": int(r.get("deposit_amount", 0) or 0),
            "deposit_ref": (r.get("deposit_ref") or "") if can_see_ref else "",
            "depositRef": (r.get("deposit_ref") or "") if can_see_ref else "",
            "deposit_ref_type": (r.get("deposit_ref_type") or "") if can_see_ref else "",
            "can_view_proof": can_see_ref,
            "notes": r.get("notes", "") or ""
        })
    return normalized


def _branch_hm(v):
    """Format a branches.open_time/close_time (TIME or timedelta) as HH:MM."""
    if v is None:
        return None
    try:
        if hasattr(v, "total_seconds"):
            s = int(v.total_seconds())
            return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"
    except Exception:
        pass
    return str(v)[:5]


def _hm(v):
    """Format a datetime / TIME value as HH:MM ("" when empty)."""
    if not v:
        return ""
    try:
        return (v if hasattr(v, "strftime") else datetime.fromisoformat(str(v))).strftime("%H:%M")
    except Exception:
        s = str(v)
        return s[11:16] if len(s) >= 16 else s[:5]


# ================================================================
#  GET /api/bookings - list bookings by status
# ================================================================
@router.get("/bookings", response_model=GenericResponse, tags=["Bookings"], summary="List bookings by status [MySQL]")
async def api_get_bookings(request: Request, status: str = "", member_id: str = Query(""), date: str = Query(""), auth=Depends(verify_api_key)):
    """List bookings with optional status, member_id, and date filters.

    ``deposit_ref`` (payment proof) is only included for trusted staff/service callers.
    """
    try:
        _can_see_ref = staff_audit.can_view_payment_ref(request)
        # Staff App Wave 4: ONE request for all live bookings (replaces the 4-call fan-out).
        if (status or "").lower() == "open":
            _rows = _mysql_query(_OPEN_BOOKINGS_SQL)
            _bookings = _normalize_booking_rows(_rows, _can_see_ref)
            return ok({"bookings": _bookings, "total": len(_bookings)})
        sql = "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, cb.source, " \
              "DATE_FORMAT(COALESCE(cs.actual_start, cb.planned_start), '%%H:%%i') AS start_time, " \
              "DATE_FORMAT(COALESCE(cs.actual_end, cb.planned_end), '%%H:%%i') AS end_time, " \
              "cb.status, cb.staff_name, cb.customer_name, cb.notes, cb.telegram_chat_id, " \
              "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, " \
              "cb.phone, cb.game_name, cb.created_at, cb.cancelled_at, " \
              "cb.deposit_status, cb.deposit_method, cb.deposit_amount, " \
              "cb.deposit_ref, cb.deposit_ref_type " \
              "FROM console_bookings cb" + _LATEST_SESSION_JOIN
        where_clauses = []
        params = []
        status_lower = status.lower() if status else ""
        if status:
            # Session statuses live in console_sessions, booking statuses in console_bookings
            session_statuses = {'active', 'checked_in', 'done'}
            status_lower = status.lower()
            if status_lower in session_statuses:
                where_clauses.append("LOWER(cs.status)=%s")
                params.append(status_lower)
            else:
                where_clauses.append("LOWER(cb.status)=%s")
                params.append(status_lower)
        if member_id:
            where_clauses.append("cb.member_id=%s")
            params.append(member_id)
        if date:
            # Normalize date formats (MM/DD/YYYY → YYYY-MM-DD) for MySQL DATE compatibility
            _clean_date = date
            try:
                from datetime import datetime as _parse_dt
                for _fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y"):
                    try:
                        _clean_date = _parse_dt.strptime(date, _fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
            except Exception:
                _clean_date = date
            where_clauses.append("cb.booking_date=%s")
            params.append(_clean_date)
        # Osmo: Only show pending bookings that have submitted deposit on timeline
        # Customer bot bookings without deposit (deposit_status='none') stay hidden until deposit is paid
        # Member bookings skip deposit — show them on timeline immediately
        if status_lower == "pending":
            where_clauses.append("(cb.deposit_status IN ('paid', 'verified') OR (cb.member_id IS NOT NULL AND cb.member_id != ''))")
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        # Server-side filter: cancelled bookings — forfeited deposits show all day, regular only 2 hours
        # Server-side filter: cancelled bookings — forfeited deposits show all day, regular only 2 hours
        if status == "cancelled":
            sql += " AND " if where_clauses else " WHERE "
            sql += "(cb.deposit_status = 'forfeited' OR cb.cancelled_at >= DATE_ADD(NOW(), INTERVAL 390 MINUTE) - INTERVAL 2 HOUR)"
        sql += " ORDER BY cb.booking_date DESC, COALESCE(cs.actual_start, cb.planned_start) DESC"
        rows = _mysql_query(sql, tuple(params)) if params else _mysql_query(sql)
        if not rows:
            return ok({"bookings": [], "total": 0})
        # Batch resolve console_types (N+1 fix)
        all_cids = set()
        for r in rows:
            _cid = r.get("console_id", "")
            if _cid and not any(t in _cid.lower() for t in ("ps5", "ps4", "ps3", "xbox", "switch", "pc")):
                all_cids.add(_cid)
        ctype_map = {}
        if all_cids:
            cid_list = sorted(all_cids)
            placeholders = ",".join("%s" for _ in cid_list)
            try:
                crows = _mysql_query(f"SELECT console_id, console_type FROM console_status WHERE console_id IN ({placeholders})", cid_list)
                for cr in crows or []:
                    ctype_map[cr.get("console_id")] = cr.get("console_type", cr.get("console_id"))
            except Exception:
                pass
        from datetime import datetime as _dt, timedelta
        normalized = []
        for r in rows:
            _cid = r.get("console_id", "")
            _ctype = ctype_map.get(_cid, _cid)
            start = r.get("start_time")
            time_slot = ""
            end_time_str = ""
            start_time_full = ""
            if start:
                try:
                    # Handle both 'HH:MM' and 'YYYY-MM-DD HH:MM:SS' formats
                    if isinstance(start, str) and len(start) <= 5 and ':' in start and '-' not in start:
                        start_dt = _dt.strptime(start, "%H:%M")
                    else:
                        start_dt = _dt.fromisoformat(start) if isinstance(start, str) else start
                    time_slot = start_dt.strftime("%H:%M")
                    start_time_full = start_dt.strftime("%H:%M")
                    dur = int(r.get("duration_mins", 0))  # 0=No Timer
                    end_dt = start_dt + timedelta(minutes=dur)
                    end_time_str = end_dt.strftime("%H:%M")
                except Exception:
                    time_slot = str(start)[:5] if start else ""
            bd = r.get("booking_date")
            bd_str = ""
            if bd:
                try:
                    bd_o = _dt.fromisoformat(bd) if isinstance(bd, str) else bd
                    bd_str = bd_o.strftime("%Y-%m-%d")
                except Exception:
                    bd_str = str(bd)[:10]
            _phone = r.get("phone", "") or ""
            _customer_name = r.get("customer_name", "") or r.get("staff_name","") or ""
            _member_id = r.get("member_id", "") or ""
            # customerName priority: customer_name → member_id → phone → 'Guest'
            if not _customer_name.strip():
                _customer_name = _member_id.strip() if _member_id and _member_id.strip() and _member_id.strip() != "Guest" else (_phone.strip() or "Guest")
            _created = r.get("created_at")
            _created_str = ""
            if _created:
                try:
                    if hasattr(_created, 'isoformat'):
                        if _created.tzinfo is None:
                            # MySQL TIMESTAMP is stored in UTC
                            _created_str = _created.replace(tzinfo=timezone.utc).isoformat()
                        else:
                            _created_str = _created.isoformat()
                    else:
                        _created_str = str(_created)
                except Exception:
                    _created_str = str(_created)
            normalized.append({
                "id": r.get("id",""),
                "customerName": _customer_name,
                "phone": _phone,
                "date": bd_str,
                "timeSlot": time_slot,
                "startTime": start_time_full,
                "endTime": end_time_str,
                "consoleType": _ctype,
                "durationMins": r.get("duration_mins",60),
                "gameName": r.get("game_name",""),
                "console_id": r.get("console_id",""),
                "consoleId": r.get("console_id",""),
                "member_id": r.get("member_id",""),
                "telegram_chat_id": r.get("telegram_chat_id",""),
                "telegramChatId": r.get("telegram_chat_id",""),
                "status": r.get("status",""),
                "created_at": _created_str,
                "end_time": str(r.get("end_time","") or ""),
                "cancelled_at": str(r.get("cancelled_at","") or ""),
                "cancelledAt": str(r.get("cancelled_at","") or ""),
                "deposit_status": r.get("deposit_status","") or "",
                "deposit_method": r.get("deposit_method","") or "",
                "deposit_amount": int(r.get("deposit_amount",0) or 0),
                "deposit_ref": (r.get("deposit_ref") or "") if _can_see_ref else "",
                "depositRef": (r.get("deposit_ref") or "") if _can_see_ref else "",
                "deposit_ref_type": (r.get("deposit_ref_type") or "") if _can_see_ref else "",
                "can_view_proof": _can_see_ref,
                "notes": r.get("notes","") or "",
                "source": r.get("source","") or ""
            })
        return ok({"bookings": normalized, "total": len(normalized)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/timeline", response_model=GenericResponse, tags=["Bookings"], summary="Staff App day timeline: consoles + bookings [MySQL]")
async def api_timeline(request: Request, date: str = Query("", description="YYYY-MM-DD (defaults to today)"),
                      branch_id: int = Query(0), auth=Depends(verify_api_key)):
    """Single-call grid for the Staff App timeline page.

    Query: ``?date=YYYY-MM-DD`` (optional; defaults to today).
    Returns ``{success, data:{date, consoles:[{id,name,type,status}],
    bookings:[{id,console_id,start,end,status,customer,guest,member_id,
    duration_mins,cancelled,deposit_status}], open_from, open_to}}``.

    Cancelled bookings are returned with ``cancelled: true`` so the UI can dim them
    instead of a second request.  One query for consoles + one for the day's bookings;
    a booking can never appear twice (latest session per booking is selected).
    """
    from datetime import datetime as _dt
    _clean = (date or "").strip()
    if _clean:
        for _fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y"):
            try:
                _clean = _dt.strptime(_clean, _fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
    else:
        _clean = _dt.now().strftime("%Y-%m-%d")
    bid = branch_id or 1
    try:
        consoles = _mysql_query(
            "SELECT console_id, console_type, status FROM console_status "
            "WHERE branch_id=%s ORDER BY console_id",
            (bid,),
        )
        rows = _mysql_query(_TIMELINE_SQL, (_clean,))
        bookings = []
        for r in rows:
            _bs = str(r.get("booking_status") or "").lower()
            _ss = str(r.get("session_status") or "").lower()
            _live = _ss in ("active", "checked_in")
            _start = r.get("start_dt")
            _end = r.get("end_dt")
            _name = (r.get("customer_name") or "").strip()
            _mid = (r.get("member_id") or "").strip()
            _guest = (not _mid) or _mid.lower() == "guest" or _name.lower() == "guest"
            bookings.append({
                "id": r.get("id"),
                "console_id": r.get("console_id"),
                "start": _hm(_start),
                "end": _hm(_end),
                "status": _ss if _live else _bs,
                "customer": _name or _mid or "Guest",
                "guest": bool(_guest),
                "member_id": _mid,
                "duration_mins": int(r.get("duration_mins") or 0),
                "cancelled": _bs == "cancelled",
                "deposit_status": r.get("deposit_status") or "",
            })
        branch = _mysql_query_one("SELECT open_time, close_time FROM branches WHERE id=%s", (bid,)) or {}
        return ok({
            "date": _clean,
            "consoles": [{
                "id": c.get("console_id"),
                "name": c.get("console_id"),
                "type": c.get("console_type"),
                "status": c.get("status"),
            } for c in consoles],
            "bookings": bookings,
            "open_from": _branch_hm(branch.get("open_time")) or "09:00",
            "open_to": _branch_hm(branch.get("close_time")) or "21:00",
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search-bookings", response_model=GenericResponse, tags=["Bookings"], summary="Search bookings by telegram_chat_id [MySQL]")
async def api_search_bookings(telegram_chat_id: str = Query("", description="Telegram chat ID of customer"), date: str = Query("", description="Filter by booking date (YYYY-MM-DD)"), auth=Depends(verify_api_key)):
    """Search bookings by telegram chat ID and/or date."""
    try:
        # Build dynamic WHERE clause
        sql = "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, " \
              "COALESCE(cs.actual_start, cb.planned_start) AS start_time, " \
              "COALESCE(cs.actual_end, cb.planned_end) AS end_time, " \
              "cb.status, cb.staff_name, cb.customer_name, cb.notes, cb.telegram_chat_id, " \
              "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, " \
              "cb.phone, cb.game_name, cb.deposit_status, cb.deposit_amount, cb.deposit_method " \
              "FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id = cb.id WHERE 1=1"
        params = []
        if telegram_chat_id:
            sql += " AND cb.telegram_chat_id=%s"
            params.append(telegram_chat_id)
        if date:
            # Normalize date formats to YYYY-MM-DD
            from datetime import datetime as _dt_parse
            _cleaned_date = date
            for _fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d"):
                try:
                    _cleaned_date = _dt_parse.strptime(date, _fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            sql += " AND cb.booking_date=%s"
            params.append(_cleaned_date)
        sql += " ORDER BY cb.booking_date DESC, COALESCE(cs.actual_start, cb.planned_start) DESC"
        rows = _mysql_query(sql, tuple(params))
        from datetime import datetime as _dt
        normalized = []
        for r in rows:
            # Derive consoleType from console_id
            _cid = r.get("console_id", "")
            _ctype = _cid
            if _cid and not any(t in _cid.lower() for t in ("ps5", "ps4", "ps3", "xbox", "switch", "pc")):
                try:
                    _crows = _mysql_query("SELECT console_type FROM console_status WHERE console_id=%s LIMIT 1", (_cid,))
                    if _crows and _crows[0].get("console_type"):
                        _ctype = _crows[0]["console_type"]
                except Exception:
                    pass
            start = r.get("start_time")
            time_slot = ""
            if start:
                try:
                    start_dt = _dt.fromisoformat(str(start)) if isinstance(start, str) else start
                    time_slot = start_dt.strftime("%H:%M")
                except (ValueError, TypeError):
                    time_slot = str(start)[:5] if start else ""
            duration = r.get("duration_mins") or ""
            game_name = r.get("game_name") or ""
            normalized.append({
                "id": r.get("id"),
                "console_id": r.get("console_id"),
                "consoleType": _ctype,
                "member_id": r.get("member_id"),
                "booking_date": str(r.get("booking_date", "")),
                "timeSlot": time_slot,
                "startTime": str(r.get("start_time", "")),
                "endTime": str(r.get("end_time", "")),
                "status": r.get("status", "pending"),
                "staff_name": r.get("staff_name"),
                "notes": r.get("notes"),
                "telegram_chat_id": r.get("telegram_chat_id"),
                "durationMins": duration,
                "duration_mins": duration,
                "gameName": game_name,
                "game_name": game_name,
                "phone": r.get("phone"),
                "deposit_status": r.get("deposit_status") or "none",
                "depositStatus": r.get("deposit_status") or "none",
                "deposit_amount": int(r.get("deposit_amount") or 0),
                "depositAmount": int(r.get("deposit_amount") or 0),
                "deposit_method": r.get("deposit_method") or "",
            })
        return JSONResponse(content={"success": True, "data": {"bookings": normalize_list(normalized), "total": len(normalized)}})
    except Exception as e:
        logger.error("api_search_bookings: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.get("/available-slots", response_model=GenericResponse, tags=["Bookings"], summary="Get available time slots for a date (with console count) [MySQL]")
async def api_available_slots(
    date: str = "",
    console_type: str = "",
    branch_id: str = "",
    auth=Depends(verify_api_key)
):
    """Return available time slots with free/total console counts.
    
    Query params:
    - date: YYYY-MM-DD (defaults to today MMT)
    - console_type: Filter by console type (PS5 / PS5 Pro)
    - branch_id: Filter by branch (optional)
    
    Returns: {slots: [{time: "10:00", free: 3, total: 5}, ...]}
    """
    try:
        if not date:
            date = now_mmt().strftime("%Y-%m-%d")
        
        # Get all consoles of matching type at matching branch
        where_clauses = []
        params = []
        if console_type:
            where_clauses.append("console_type=%s")
            params.append(console_type)
        if branch_id:
            where_clauses.append("branch_id=%s")
            params.append(branch_id)
        
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        cs_rows = _mysql_query(f"SELECT console_id, console_type FROM console_status{where_sql} ORDER BY console_id", tuple(params))
        all_consoles = [r["console_id"] for r in cs_rows]
        total_consoles = len(all_consoles)
        
        if total_consoles == 0:
            return ok({"date": date, "slots": [], "total_consoles": 0})
        
        # Get active bookings for this date
        bookings = _mysql_query(
            "SELECT cb.console_id, COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
            "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, cb.status "
            "FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id = cb.id "
            "WHERE cb.booking_date=%s AND cb.status NOT IN ('cancelled', 'done', 'rejected') "
            "ORDER BY COALESCE(cs.actual_start, cb.planned_start)",
            (date,)
        )
        
        # Build 30-min slots from 10:00 to 22:00
        OPEN_HOUR, CLOSE_HOUR = 10, 22
        SLOT_MINUTES = 30
        
        slots = []
        for h in range(OPEN_HOUR, CLOSE_HOUR):
            for m in (0, 30):
                if h == CLOSE_HOUR and m > 0:
                    break
                time_str = f"{h:02d}:{m:02d}"
                try:
                    slot_start = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
                    slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)
                except ValueError:
                    continue
                
                # Count busy consoles at this slot
                busy_consoles = set()
                for b in bookings:
                    b_console = b.get("console_id", "")
                    if b_console not in all_consoles:
                        continue
                    b_start = b.get("start_time")
                    b_end = b.get("end_time")
                    if not b_start:
                        continue
                    if isinstance(b_start, str):
                        try:
                            b_start = datetime.fromisoformat(b_start)
                        except ValueError:
                            continue
                    if not b_end:
                        b_dur = b.get("duration_mins")
                        if b_dur:
                            b_end = b_start + timedelta(minutes=int(b_dur))
                    if not b_end:
                        continue
                    if isinstance(b_end, str):
                        try:
                            b_end = datetime.fromisoformat(b_end)
                        except ValueError:
                            continue
                    
                    # Overlap check: slot overlaps with booking
                    if slot_start < b_end and slot_end > b_start:
                        busy_consoles.add(b_console)
                
                free = total_consoles - len(busy_consoles)
                slots.append({
                    "time": time_str,
                    "free": max(0, free),
                    "total": total_consoles,
                })
        
        return ok({
            "date": date,
            "slots": slots,
            "total_consoles": total_consoles,
        })
    except Exception as e:
        logger.error("api_available_slots: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

def _auto_settle_deposit(cur, booking_id: int, row: dict, now_dt) -> dict:
    """Session end → create the sale voucher automatically and redeem the deposit.

    Deposit = 30% of the scheduled fee, so the counter balance is recorded under the
    same payment channel as the deposit (Boss 2026-10-21: "session end တာနဲ့ တစ်ခါတည်း
    သူ့ဘာသာသူ sale voucher ထဲ settle လုပ်ရမှာ").
    """
    label_map = {"kpay": "KPay", "wavepay": "WavePay", "cash": "Cash",
                 "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB", "mmqr": "MMQR - CB"}
    dep = int(row.get("deposit_amount") or 0)
    method = (row.get("deposit_method") or "cash").lower()
    label = label_map.get(method, "Cash")

    # actual played minutes (fallback: planned duration)
    cur.execute(
        "SELECT actual_start FROM console_sessions "
        "WHERE booking_id=%s AND status IN ('active','checked_in') ORDER BY id DESC LIMIT 1",
        (booking_id,))
    sess = cur.fetchone() or {}
    dur = None
    start = sess.get("actual_start")
    if start:
        try:
            now_naive = now_dt.replace(tzinfo=None) if getattr(now_dt, "tzinfo", None) else now_dt
            dur = max(1, int((now_naive - start).total_seconds() / 60))
        except Exception:
            dur = None
    if not dur:
        dur = int(row.get("planned_duration_mins") or 60)

    base_rate = _mysql_get_setting("base_rate", 10000)
    if isinstance(base_rate, str):
        try:
            base_rate = int(base_rate)
        except ValueError:
            base_rate = 10000
    mults = _mysql_get_setting("console_multipliers", {})
    if isinstance(mults, str):
        try:
            mults = _json_notify.loads(mults)
        except Exception:
            mults = {}
    mult = mults.get(row.get("console_id", ""), 1.0) if isinstance(mults, dict) else 1.0
    if not isinstance(mult, (int, float)):
        mult = 1.0

    fee = int(round((dur / 60) * float(base_rate) * float(mult)))
    total_fee = max(fee, dep)          # never bill less than what was prepaid
    applied = min(dep, total_fee)
    remaining = total_fee - applied

    pm_str = f"{label}:{applied}(DepositRedeemed)"
    if remaining > 0:
        pm_str += f"|{label}:{remaining}"

    today_str = now_dt.strftime("%Y-%m-%d")
    staff_name = row.get("staff_name") or "system"
    member_ref = row.get("member_id") or row.get("phone") or "Guest"
    game_name, game_id, session_id = resolve_sale_game(
        row.get("console_id", ""), booking_id=booking_id)

    cur.execute(
        "INSERT INTO sales_daily (branch_id, sale_date, console_id, member_id, amount, gross, "
        "discount, net, staff_name, customer_name, payment_method, notes, created_at, "
        "session_id, game_name, game_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s)",
        (1, today_str, row.get("console_id", ""), member_ref, float(total_fee), float(total_fee),
         0.0, float(total_fee), staff_name, row.get("customer_name") or staff_name, pm_str,
         f"Auto-settle at session end - Booking #{booking_id} ({dur} min)",
         session_id, game_name, game_id))
    sales_id = cur.lastrowid

    if remaining > 0:
        cur.execute(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at) "
            "VALUES ('inject', %s, %s, %s, %s, NOW())",
            (label, remaining, f"Auto-settle balance - Booking #{booking_id}", staff_name))

    cur.execute(
        "UPDATE console_bookings SET deposit_status='redeemed', deposit_redeemed_at=NOW() WHERE id=%s",
        (booking_id,))
    return {"sales_daily_id": sales_id, "total_fee": total_fee, "deposit_applied": applied,
            "remaining": remaining, "duration_mins": dur, "method": label}


@router.put("/end_booking/{booking_id}", response_model=GenericResponse, tags=["Bookings"], summary="End booking [MySQL]")
async def api_end_booking(booking_id: str, force: bool = False, auth=Depends(verify_api_key)):
    """Mark a booking as Done in MySQL. Handles both Active and checked_in bookings.

    🔒 2026-10-20 checkout guard: a session whose deposit is still ``verified``
    (collected but never applied) cannot be closed silently — that is exactly how
    BK#4026/4073/4224/4236 ended up with orphaned deposits. Pass ``?force=true``
    only when the deposit was genuinely handled some other way."""
    try:
        # CI-3: Wrap end update + console_status update in transaction
        now = now_mmt()
        # Transient "MySQL server has gone away" (errno 2006/2013) must not reach
        # the staff app: retry once with a live-pinged connection.
        _end_conn = None
        _last_err = None
        for _attempt in range(2):
            try:
                _end_conn = _mc.connect(
                    host=os.environ.get('MYSQL_HOST', 'localhost'),
                    user=os.environ.get('MYSQL_USER', 'root'),
                    password=os.environ.get('MYSQL_PASSWORD', ''),
                    database=os.environ.get('MYSQL_DATABASE', 'psvibe_api'),
                    port=int(os.environ.get('MYSQL_PORT', '3306')),
                    charset='utf8mb4',
                    connection_timeout=10,
                )
                _end_conn.ping(reconnect=True, attempts=2, delay=1)
                break
            except Exception as _ce:
                _last_err = _ce
                _end_conn = None
                logger.warning("end_booking %s: DB connect attempt %s failed: %s",
                               booking_id, _attempt + 1, _ce)
                if _attempt == 0:
                    import time as _t
                    _t.sleep(0.5)
        if _end_conn is None:
            raise _last_err or RuntimeError("database unavailable")
        _end_conn.start_transaction()
        _end_cur = _end_conn.cursor(dictionary=True)
        try:
            # Check current status before deciding console cleanup
            _end_cur.execute(
                "SELECT console_id, status, deposit_status, deposit_amount, deposit_method, "
                "       deposit_redeemed_at, planned_duration_mins, member_id, phone, "
                "       customer_name, staff_name "
                "FROM console_bookings WHERE id=%s", (booking_id,))
            row = _end_cur.fetchone()
            if row and not force:
                _ds = (row.get("deposit_status") or "").lower()
                _damt = int(row.get("deposit_amount") or 0)
                if _ds in ("paid", "verified") and _damt > 0 and not row.get("deposit_redeemed_at"):
                    # Boss 2026-10-21: the sale voucher is settled automatically here.
                    _settled = _auto_settle_deposit(_end_cur, int(booking_id), row, now)
                    logger.info("end_booking %s auto-settled deposit: %s", booking_id, _settled)
            _ds_before = (row.get("deposit_status") or "") if row else ""
            if not row:
                _end_conn.rollback()
                raise HTTPException(status_code=404, detail="Booking not found")
            current_status = row.get("status", "")
            _console_id = row.get("console_id")  # save for _sync_console_status
            # Capture session console(s) BEFORE marking done — booking.console_id can drift
            # from console_sessions.console_id (move/swap), so free BOTH to avoid stale Active.
            _end_cur.execute(
                "SELECT DISTINCT console_id FROM console_sessions WHERE booking_id=%s AND status IN ('active','checked_in')",
                (booking_id,)
            )
            _session_consoles = [r["console_id"] for r in _end_cur.fetchall()]
            # End session: update console_sessions with actual_end and done status
            _end_cur.execute("UPDATE console_sessions SET actual_end=%s, status='done' WHERE booking_id=%s AND status IN ('active','checked_in')", (now, booking_id))
            # Also update console_bookings status to 'done' to reflect booking completed
            _end_cur.execute("UPDATE console_bookings SET status='done' WHERE id=%s", (booking_id,))
            # Free every console this booking's sessions ran on (session consoles + booking console)
            _consoles_to_free = set(c for c in _session_consoles if c)
            if current_status == "Active" and _console_id:
                _consoles_to_free.add(_console_id)
            for _cid in _consoles_to_free:
                _end_cur.execute(
                    "UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s",
                    (_cid,)
                )
            _end_conn.commit()
            # Safety: sync console_status in case of edge-case drift
            for _cid in _consoles_to_free:
                _sync_console_status(_cid)
            # Auto-cancel any remaining stock holds for this booking (food not sold/released)
            _mysql_exec("UPDATE stock_hold SET status = 'cancelled', updated_at = NOW() WHERE booking_id = %s AND status = 'held'", (booking_id,))

        except Exception:
            try:
                if not _end_conn.is_closed():
                    _end_conn.rollback()
            except Exception:
                pass  # connection already gone — never mask the real error with 2006
            raise
        finally:
            try:
                _end_conn.close()
            except Exception:
                pass  # non-critical: connection close
        return ok({"booking_id": booking_id, "status": "Done"})
    except HTTPException:
        raise  # 404 / 409 deposit-guard must keep their real status + message
    except Exception as e:
        logger.exception("end_booking %s failed", booking_id)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════





@router.get("/get-confirmed-booking", response_model=GenericResponse, tags=["Bookings"], summary="Get bind-candidate bookings for a console [MySQL]")
async def api_confirmed_bookings_for_console(console_id: str, auth=Depends(verify_api_key)):
    """Return today's bind candidates for a console (Staff App "Start Session" sheet).

    Backward compatible: `booking` still returns the single best candidate (same
    shape/keys as before) so existing callers keep working.

    ADDED (2026-09-19): `bookings` lists EVERY candidate for that console today with
    its REAL status verbatim (confirmed / checked_in / pending_check_in) so the app can
    render an honest per-row label instead of hardcoding "Checked-in". Rows are ordered
    checked-in-first, then newest created.

    NOTE: this endpoint is READ-ONLY. To bind a merely-'confirmed' booking the app calls
    the EXISTING `POST /api/bookings/checkin` first, then `POST /api/sessions/start`
    (which still REQUIRES the linked booking to have a checked_in session row — unchanged).
    """
    try:
        today = (datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)).strftime('%Y-%m-%d')
        rows = _mysql_query(
            "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, cb.customer_name, "
            "COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
            "COALESCE(cs.status, '') AS session_status, "
            "cb.status AS status, cb.staff_name, cb.notes, cb.telegram_chat_id, "
            "COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, "
            "cb.phone, cb.game_name "
            "FROM console_bookings cb" + _LATEST_SESSION_JOIN +
            " WHERE cb.console_id=%s "
            "AND (cb.status IN ('confirmed','checked_in','pending_check_in') OR cs.status='checked_in') "
            "AND DATE(cb.booking_date)=%s "
            "ORDER BY (cb.status='checked_in' OR cs.status='checked_in') DESC, cb.created_at DESC",
            (console_id, today)
        ) or []
        candidates = []
        for row in rows:
            for k in ("start_time", "end_time", "booking_date", "created_at"):
                if k in row and row[k] is not None:
                    row[k] = row[k].isoformat() if hasattr(row[k], "isoformat") else str(row[k])
            candidates.append(row)
        return ok({"booking": (candidates[0] if candidates else None), "bookings": candidates})
    except Exception as e:
        logger.exception(f"confirmed-by-console error: {e}")
        return ok({"booking": None, "bookings": []})

# ================================================================
#  WAITLIST AUTO-NOTIFY
# ================================================================
async def _auto_notify_waitlist(booking_date, start_time, console_id, cancelled_by="system"):
    """Auto-notify waitlisted customer when a slot frees up."""
    try:
        console = _mysql_query_one(
            "SELECT console_type FROM console_status WHERE console_id=%s", (console_id,))
        if not console or not console.get("console_type"):
            return False
        console_type = console["console_type"]
        entry = _mysql_query_one(
            """SELECT w.id, w.staff_name, w.member_id, w.telegram_chat_id, w.console_id
               FROM console_bookings w
               WHERE w.booking_date=%s AND w.status='waiting' AND w.console_id=%s
               ORDER BY w.created_at ASC LIMIT 1""",
            (booking_date, console_id))
        if not entry and console_type:
            entry = _mysql_query_one(
                """SELECT w.id, w.staff_name, w.member_id, w.telegram_chat_id, w.console_id
                   FROM console_bookings w
                   JOIN console_status cs ON cs.console_id = w.console_id
                   WHERE w.booking_date=%s AND w.status='waiting' AND cs.console_type=%s
                   ORDER BY w.created_at ASC LIMIT 1""",
                (booking_date, console_type))
        if not entry:
            return False
        _mysql_exec(
            "UPDATE console_bookings SET status='Notified', notes=CONCAT(IFNULL(notes,''), ' | Auto-notified: slot freed') WHERE id=%s",
            (entry["id"],))
        chat_id = entry.get("telegram_chat_id") or entry.get("member_id", "")
        customer_name = entry.get("customer_name") or entry.get("staff_name") or entry.get("member_id", "Customer")
        if chat_id:
            _CUSTOMER_BOT_TOKEN = os.environ.get("CUSTOMER_BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
            if _CUSTOMER_BOT_TOKEN:
                _bk_date = str(booking_date)[:10] if booking_date else ""
                _bk_time = ""
                if start_time:
                    try:
                        from datetime import datetime as __dt
                        s = str(start_time)
                        _start_dt = __dt.fromisoformat(s)
                        _bk_time = _start_dt.strftime("%H:%M")
                    except Exception:
                        _bk_time = str(start_time)[:5] if start_time else ""
                _msg = ("\U0001f3ae *Slot Available!*\n\n"
                        f"\u1019\u1004\u1039\u1039\u1002\u101c\u102c\u1015\u102b {customer_name} \u2014\n"
                        f"\U0001f4c5 {_bk_date}  \u23f0 {_bk_time}\n"
                        f"\U0001f3ae {console_type or entry.get("console_id", "Console")}\n\n"
                        "\u1005\u1031\u102c\u1004\u1037\u103a\u1006\u102d\u102f\u1004\u103a\u1038\u1011\u102c\u1038\u1010\u1032\u1037 slot \u101b\u1015\u103c\u1005\u103a!\n"
                        "Booking \u1015\u103c\u1014\u103a\u101c\u102f\u1015\u103a\u1016\u102d\u102f\u1037 @psvibeofficial \u1000\u102d\u102f\u1006\u1000\u103a\u101e\u101a\u103a\u1015\u102b\u104b")
                try:
                    _tel_payload = json.dumps({"chat_id": chat_id, "text": _msg, "parse_mode": "Markdown"}).encode()
                    _tel_url = f"https://api.telegram.org/bot{_CUSTOMER_BOT_TOKEN}/sendMessage"
                    _tel_req = urllib.request.Request(_tel_url, data=_tel_payload, headers={"Content-Type": "application/json"}, method="POST")
                    urllib.request.urlopen(_tel_req, timeout=5)
                    logger.info("[WAITLIST] Auto-notified entry #%s (chat=%s)", entry["id"], chat_id)
                except Exception as _e:
                    logger.warning("[WAITLIST] Telegram failed for entry #%s: %s", entry["id"], _e)
        return True
    except Exception as e:
        logger.warning("[WAITLIST] Auto-notify error: %s", e)
        return False

@router.post("/bookings/cancel", response_model=GenericResponse, tags=["Bookings"], summary="Cancel a booking by ID [MySQL]")
async def api_booking_cancel(req: dict, request: Request, auth=Depends(verify_api_key)):
    """Cancel a booking. Frees console if reserved.
    Staff App: accepts ``reason``/``cancel_reason`` (stored in notes) and records
    the acting staff (JWT name) on the booking for the audit trail."""
    try:
        booking_id = req.get("id") or req.get("booking_id")
        if not booking_id:
            return error_response(message="booking id required")
        
        booking = _mysql_query_one(
            "SELECT id, console_id, branch_id, member_id, booking_date, "
            "planned_start AS start_time, planned_end AS end_time, "
            "planned_duration_mins AS duration_mins, "
            "status, staff_name, notes, telegram_chat_id, phone, "
            "game_name, admin_notify_msg_id, created_at, cancelled_at, "
            "deposit_status, deposit_amount, deposit_method "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not booking:
            return error_response(message="Booking not found")
        # Staff App audit: cancel reason (Staff App sends reason/cancel_reason) + acting staff.
        _cancel_reason = (str(req.get("reason") or req.get("cancel_reason") or req.get("staffNote") or "")).strip()
        _cancel_staff = staff_audit.identity_from_request(request, req)
        console_id = booking.get("console_id", "")
        telegram_chat_id = booking.get("telegram_chat_id", "")
        
        # Capture pre-cancel deposit state for forfeit handling
        _pre_deposit_status = booking.get("deposit_status", "") or ""
        _pre_deposit_amount = int(booking.get("deposit_amount", 0) or 0)
        _pre_deposit_method = (booking.get("deposit_method", "") or "kpay").lower()

        # Persist the cancel reason for the owner WITHOUT wiping existing booking notes:
        # append "Cancel reason: <reason> (by <staff>)" so it stays visible on the dashboard.
        _prev_notes = (booking.get("notes") or "").strip()
        _cancel_note = ""
        if _cancel_reason:
            _cancel_note = f"Cancel reason: {_cancel_reason}"
        if _cancel_staff:
            _cancel_note = (_cancel_note + f" (by {_cancel_staff})") if _cancel_note else f"Cancelled by {_cancel_staff}"
        _new_notes = ((_prev_notes + " | " + _cancel_note) if _prev_notes else _cancel_note) if _cancel_note else None
        
        # CI-2 & CI-3: Only cancel non-Active/non-Done bookings, with transaction
        _cancel_conn = _mc.connect(
            host=os.environ.get('MYSQL_HOST', 'localhost'),
            user=os.environ.get('MYSQL_USER', 'root'),
            password=os.environ.get('MYSQL_PASSWORD', ''),
            database=os.environ.get('MYSQL_DATABASE', 'psvibe'),
            port=int(os.environ.get('MYSQL_PORT', '3306')),
            charset='utf8mb4'
        )
        _cancel_conn.start_transaction()
        _cancel_cur = _cancel_conn.cursor()
        try:
            # Auto-forfeit deposit ONLY if verified when cancelling (Boss policy 2026-07-31);
            # unverified ('paid') -> void to 'none' (NOT revenue, no cash movement)
            _cancel_cur.execute(
                "UPDATE console_bookings SET status='cancelled', cancelled_at=DATE_ADD(NOW(), INTERVAL 390 MINUTE), "
                "deposit_status=CASE WHEN deposit_status='verified' THEN 'forfeited' WHEN deposit_status='paid' THEN 'none' ELSE deposit_status END, "
                "notes=COALESCE(%s, notes), staff_name=COALESCE(NULLIF(%s, ''), staff_name) "
                "WHERE id=%s AND status NOT IN ('Active','Done')",
                (_new_notes, _cancel_staff, booking_id)
            )
            affected = _cancel_cur.rowcount
            if affected == 0:
                _cancel_conn.rollback()
                _cancel_conn.close()
                return JSONResponse(content={"success": False, "message": "Cannot cancel - booking is Active or Done"}, status_code=400)
            # Note: console_status will be synced after commit via _sync_console_status
            _cancel_conn.commit()
        except Exception:
            _cancel_conn.rollback()
            raise
        finally:
            try:
                _cancel_conn.close()
            except Exception:
                pass  # non-critical: connection close

        # HI-6: Sync console_status — derive from remaining bookings (not unconditional Free)
        if console_id:
            try:
                _sync_console_status(console_id)
            except Exception:
                pass
        
        # Auto-notify waitlisted customers (fire-and-forget, never fail the cancel)
        try:
            _booking_date = booking.get("booking_date", "")
            _start_time = booking.get("start_time", "")
            _console_id = booking.get("console_id", "")
            if _booking_date and _start_time and _console_id:
                import asyncio
                asyncio.create_task(_auto_notify_waitlist(_booking_date, _start_time, _console_id))
        except Exception as _wn_err:
            logger.warning("[WAITLIST] Auto-notify call failed: %s", _wn_err)

        # Notify customer via customer bot
        if telegram_chat_id:
            _CUSTOMER_BOT_TOKEN = os.environ.get("CUSTOMER_BOT_TOKEN", "")
            if _CUSTOMER_BOT_TOKEN:
                _bk_date = str(booking.get("booking_date", ""))[:10]
                _bk_time = ""
                _start = booking.get("start_time")
                if _start:
                    try:
                        from datetime import datetime as __dt
                        _start_dt = __dt.fromisoformat(str(_start)) if isinstance(_start, str) else _start
                        _bk_time = _start_dt.strftime("%H:%M")
                    except Exception:
                        _bk_time = str(_start)[:5] if _start else ""
                _console = booking.get("console_id") or "\u2014"
                _game = booking.get("game_name") or "\u2014"
                _duration = booking.get("duration_mins") or ""
                _cust_msg = "\u274c <b>Booking #" + str(booking_id) + " Cancelled</b>\n\n"
                _cust_msg += "\U0001F4C5 " + _bk_date + "  \U0001F550 " + _bk_time + "\n"
                _cust_msg += "\U0001F3AE " + _console + "  \u23F1\ufe0f " + str(_duration) + " mins\n"
                _cust_msg += "\U0001F579\ufe0f " + _game + "\n\n"
                _cust_msg += "\u1006\u1000\u103a\u101e\u1000\u1039\u101c\u102c\u101b\u103e\u1014\u1039: @psvibeofficial"
                try:
                    _tel_payload = json.dumps({
                        "chat_id": telegram_chat_id,
                        "text": _cust_msg,
                        "parse_mode": "HTML"
                    }).encode()
                    _tel_url = "https://api.telegram.org/bot" + _CUSTOMER_BOT_TOKEN + "/sendMessage"
                    _tel_req = urllib.request.Request(
                        _tel_url, data=_tel_payload,
                        headers={"Content-Type": "application/json"}, method="POST"
                    )
                    urllib.request.urlopen(_tel_req, timeout=5)
                except Exception as _e:
                    logger.warning("Cancel notification failed: %s", _e)

        # 🆕 Forfeit deposit handling: record as income in cash_movements
        # Boss policy 2026-07-31: ONLY verified deposits forfeit as revenue;
        # unverified ('paid') voids to 'none' (NOT revenue, no cash movement)
        if _pre_deposit_status == 'verified' and _pre_deposit_amount > 0:
            _method_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(_pre_deposit_method, "KPay")
            try:
                insert_cash_movement_if_absent(
                    "inject", _method_label, _pre_deposit_amount,
                    f"Forfeit BK#{booking_id}%",
                    f"Forfeit BK#{booking_id} via {_method_label}",
                    booking.get("staff_name", "") or _cancel_staff or "system", "deposit_forfeit")
                logger.info(f"Forfeit deposit recorded: BK#{booking_id} {_pre_deposit_amount}Ks {_method_label}")
            except Exception as _fe:
                logger.warning(f"Forfeit cash_movements BK#{booking_id} failed: {_fe}")
            # Notify admin group
            try:
                _notify_deposit_forfeited(booking_id, booking, _pre_deposit_amount)
            except Exception as _fe2:
                logger.warning(f"Forfeit notify BK#{booking_id} failed: {_fe2}")

        return ok({
            "message": "Booking cancelled",
            "booking_id": booking_id,
            "telegram_chat_id": telegram_chat_id,
        })
    except Exception as e:
        return error_response(message=str(e))

#  MUTATION — cancel_booking
# ═══════════════════════════════════════
# ═══════════════════════════════════════
# FIX 2.4: POST /api/sessions/start — Dedicated staff walk-in session start
# Replaces the legacy staff format that was in POST /api/bookings






async def api_bookings_by_phone_date(phone: str, date: str, auth=Depends(verify_api_key)):
    """Get bookings by phone + date for admin notification dedup."""
    try:
        rows = _mysql_query(
            "SELECT id, admin_notify_msg_id, status FROM console_bookings WHERE phone=%s AND booking_date=%s",
            (phone, date)
        )
        return rows if rows else []
    except Exception as e:
        logger.error(f"by-phone-date query failed: {e}")
        return []


# ═══════════════════════════════════════


def _notify_booking_received(name, date, time_slot, console_type, duration, game, phone, bk_id, console_id=""):
    """Notify via Telegram about new customer booking. Use Sale Bot to approve.
    Sends to DEPOSIT_NOTIFY_CHAT only (bookings & deposit group).
    """
    import json, urllib.request, os
    token = os.environ.get("CUSTOMER_BOT_TOKEN", os.environ.get("BOT_TOKEN", ""))
    chat_id = os.environ.get("DEPOSIT_NOTIFY_CHAT", os.environ.get("STAFF_NOTIFY_CHAT", ""))
    if not token or not chat_id:
        return
    console_label = f" {console_type}"
    if console_id:
        console_label = f" {console_id} ({console_type})"
    msg = ("\U0001f195 *New Booking Request #" + str(bk_id) + "*\n\n"
           + "\U0001f464 " + name + "\n\U0001f4de " + phone + "\n\U0001f4c5 " + date + " \u23f0 " + time_slot
           + "\n\U0001f3ae" + console_label + "\n\u23f1 " + str(duration) + " min\n\U0001f579 " + (game if game else "\u2014")
           + "\n━━━━━━━━━━━━━━━━━━"
           + "\n💸 *Deposit:* 30% required — pending payment\n"
           + "━━━━━━━━━━━━━━━━━━"
           + "\n*Customer မှ booking တင်ထားပါသည်။*\n"
           + "_(Sale Bot → Customer Booking မှ Approve/Reject ပြုလုပ်ပါ)_")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=5)
    except Exception as e:
        logger.warning("[NOTIFY] Failed to send booking notification for #%s: %s", bk_id, e)

    # ── Staff app push (FCM) — additive; must never break the booking flow ──
    try:
        import fcm as _fcm
        _fcm.send_to_staff(
            title="🎮 Booking အသစ် #" + str(bk_id),
            body=f"{name} · {date} {time_slot} · {console_label.strip()} · {duration} min",
            data={"type": "new_booking", "booking_id": str(bk_id)},
        )
    except Exception as _pne:
        logger.warning("[NOTIFY] staff push failed for #%s: %s", bk_id, _pne)


# ================================================================

def _parse_pm_and_inject(total_amount: float, payment_method: str, note: str, staff_name: str) -> None:
    """Parse payment method string (e.g. 'KPay:31000|Cash:20000' or 'KPay:90000/Cash:0')
    and inject cash_movements entries for each account."""
    if not payment_method or float(total_amount) <= 0:
        return
    import re
    # Handle both | and / separators
    parts = re.split(r'[|/]', payment_method)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            method, _, val_str = part.partition(":")
            val = float(val_str.strip() or 0) if val_str.strip() else 0
        else:
            method = part
            val = float(total_amount) / len(parts)
        method = method.strip()
        val = float(val)
        # Map internal method names to cash_movements account names
        acct_map = {
            "cash": "Cash",
            "kpay": "KPay",
            "wavepay": "WavePay",
            "aya pay": "AYA Pay",
            "aya_pay": "AYA Pay",
            "kbz bank": "KBZ Bank",
            "kbz_bank": "KBZ Bank",
            "acm acc": "Main (ACM)",
            "acm_acc": "Main (ACM)",
            "mmqr_cb": "MMQR - CB",
            "mmqr cb": "MMQR - CB",
            "mmqr - cb": "MMQR - CB",
            "mmqr": "MMQR - CB",
        }
        account = acct_map.get(method.strip().lower().replace("  ", " "), method)
        if val > 0:
            _mysql_exec(
                "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                ("inject", account, val, note, staff_name)
            )
            logger.info("Cash movement inject: %s +%.0f Ks (%s)", account, val, note)

#  MUTATION — topup/log
# ================================================================

@router.post("/receipt", response_model=GenericResponse, tags=["Receipts"], summary="Save receipt from bot push")
async def api_receipt_save(req: dict, auth=Depends(verify_api_key)):
    """Save receipt JSON payload from bot (stores full data in DB receipt_data column)."""
    try:
        voucher_id = req.get("voucher_id", "")
        receipt_no = req.get("receipt_no") or req.get("voucher_id", "")
        member_id = req.get("member_id", req.get("member", ""))
        if isinstance(member_id, dict):
            member_id = member_id.get("id", member_id.get("member_id", ""))
        amount = req.get("gross_total", req.get("amount", 0))
        if isinstance(amount, (dict, list)):
            amount = 0
        payment_method = req.get("payment_method", "")
        items = req.get("food_items", req.get("items", ""))
        if isinstance(items, (list, dict)):
            items = _sjson.dumps(items, ensure_ascii=False)
        receipt_date = req.get("date", req.get("receipt_date", ""))
        staff_name = req.get("staff_name", "")
        # Guard: never persist an empty receipt row (Staff App / bad callers).
        # A real receipt always carries at least a voucher/receipt no, a member or an amount.
        if (not any(str(x).strip() for x in (voucher_id, receipt_no, member_id))
                and not any(str(x).strip() for x in (items, receipt_date, payment_method))
                and not float(amount or 0)):
            return error_response(message="Empty receipt payload — voucher_id or receipt_no required")
        receipt_data = _sjson.dumps(req, ensure_ascii=False)

        logger.info("Saving receipt to DB: voucher_id=%s", voucher_id)

        _mysql_exec(
            "INSERT INTO receipts (receipt_no, voucher_id, member_id, amount, payment_method, items, receipt_data, receipt_date, staff_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE receipt_data=VALUES(receipt_data), amount=VALUES(amount), payment_method=VALUES(payment_method), items=VALUES(items), receipt_date=VALUES(receipt_date)",
            (receipt_no, voucher_id, member_id, amount, payment_method, items, receipt_data, receipt_date, staff_name)
        )
        logger.info("Receipt saved to DB: voucher_id=%s", voucher_id)
        return ok({"status": "ok", "voucher_id": voucher_id})
    except Exception as e:
        logger.error("Receipt save error: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
#  MUTATION — members/register
# ================================================================


#  MUTATION — save_receipt_json
# ═══════════════════════════════════════
@router.post("/save_receipt_json", response_model=GenericResponse, tags=["Receipts"], summary="Persist receipt data to MySQL")
async def api_save_receipt_json(req: dict, auth=Depends(verify_api_key)):
    """Persist receipt data to MySQL (stores full JSON in receipt_data column)."""
    try:
        inner = req.get("data")
        req_src = inner if (inner and isinstance(inner, dict)) else req
        receipt_no = req_src.get("receipt_no") or req.get("voucher_id", "")
        member_id = req_src.get("member_id", "")
        amount = req_src.get("amount", 0)
        payment_method = req_src.get("payment_method", "")
        items = req_src.get("items", "")
        receipt_date = req_src.get("receipt_date", "")
        staff_name = req_src.get("staff_name", "")

        # Store full receipt JSON in receipt_data column
        import json as _json_dump
        receipt_data = _json_dump.dumps(req_src, ensure_ascii=False)
        if isinstance(items, (list, dict)):
            items = _json_dump.dumps(items, ensure_ascii=False)

        logger.info("Saving receipt to MySQL: receipt_no=%s", receipt_no)

        _mysql_exec(
            "INSERT INTO receipts (receipt_no, member_id, amount, payment_method, items, receipt_data, receipt_date, staff_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (receipt_no, member_id, amount, payment_method, items, receipt_data, receipt_date, staff_name)
        )

        logger.info("Receipt saved: receipt_no=%s", receipt_no)
        return {"status": "ok", "receipt_no": receipt_no}
    except Exception as e:
        logger.error("Receipt save error: %s", str(e))
        return {"status": "error", "detail": str(e)}



# ═══════════════════════════════════════
#  QUERY — get_receipt_html
# ═══════════════════════════════════════
@router.get("/receipt/{voucher_id}", tags=["Receipts"])
async def api_get_receipt_html(voucher_id: str):
    """Render receipt HTML for a given voucher_id.
    Reads from DB receipt_data column first, falls back to filesystem for legacy.
    """
    import json as _json
    import os as _os
    
    try:
        # ── Try DB first (lookup by voucher_id, fallback to receipt_no) ──
        rows = _mysql_query("SELECT receipt_data FROM receipts WHERE voucher_id = %s LIMIT 1", (voucher_id,))
        if not rows or not rows[0].get("receipt_data"):
            rows = _mysql_query("SELECT receipt_data FROM receipts WHERE receipt_no = %s LIMIT 1", (voucher_id,))
        receipt_data = None
        if rows and rows[0].get("receipt_data"):
            try:
                receipt_data = _json.loads(rows[0]["receipt_data"])
                logger.info("Receipt from DB: voucher=%s", voucher_id)
            except Exception:
                receipt_data = None
        
        # ── Fallback to filesystem (legacy) ──
        if not receipt_data:
            safe_id = _os.path.basename(voucher_id)
            safe_id = safe_id.replace("/", "_").replace("\\", "_").replace("..", "_")
            receipt_dir = "/opt/psvibe/sales-bot/bot/receipts"
            receipt_path = _os.path.join(receipt_dir, f"{safe_id}.json")
            real_path = _os.path.realpath(receipt_path)
            if not real_path.startswith(_os.path.realpath(receipt_dir)):
                return HTMLResponse(
                    content=f"<html><body style=\"font-family:sans-serif;padding:40px;text-align:center\"><h2>403 - Forbidden</h2><p>Voucher: {voucher_id}</p></body></html>",
                    status_code=403
                )
            if _os.path.exists(receipt_path):
                with open(receipt_path, "r") as f:
                    receipt_data = _json.load(f)
                logger.info("Receipt from filesystem: voucher=%s", voucher_id)
        
        if not receipt_data:
            logger.warning("Receipt not found: voucher=%s", voucher_id)
            return HTMLResponse(
                content=f"<html><body style=\"font-family:sans-serif;padding:40px;text-align:center\"><h2>404 - Receipt Not Found</h2><p>Voucher: {voucher_id}</p></body></html>",
                status_code=404
            )
        
        # ── Render with template ──
        template_path = "/opt/psvibe/api/receipt_template.html"
        with open(template_path, "r") as f:
            template = f.read()
        json_str = _json.dumps(receipt_data, ensure_ascii=False)
        script_tag = f"<script>window.__RECEIPT_DATA__ = {json_str};</script>"
        injected = template.replace("</head>", script_tag + "\n</head>")
        
        logger.info("Receipt rendered: voucher=%s", voucher_id)
        return HTMLResponse(content=injected)
    except Exception as e:
        logger.error("Receipt render error: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — add_console_game / remove_console_game
# ═══════════════════════════════════════


# ── Customer Feedback ─────────────────────────────────────────────────────────
@router.post("/feedback/submit", response_model=GenericResponse, tags=["Feedback"], summary="Submit customer feedback (stars + optional comment)")
async def api_feedback_submit(req: dict, auth=Depends(verify_api_key)) -> JSONResponse:
    """Insert a customer feedback row into customer_feedback table.
    Payload: {tg_id, username, booking_id, rating, comment?, console_id?}
    """
    try:
        tg_id = (req.get("tg_id") or "").strip()
        username = (req.get("username") or "").strip()
        booking_id = req.get("booking_id", "")
        rating = req.get("rating", 0)
        comment = (req.get("comment") or "")[:500]
        console_id = (req.get("console_id") or "").strip()
        
        if not tg_id:
            return error_response(message="tg_id required")
        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                return error_response(message="rating must be 1-5")
        except (ValueError, TypeError):
            return error_response(message="rating must be a number 1-5")
        
        # Insert into MySQL
        _mysql_exec(
            "INSERT INTO customer_feedback (tg_id, username, booking_id, rating, comment, console_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 390 MINUTE))",
            (tg_id, username, str(booking_id), rating, comment, console_id)
        )
        logger.info("FEEDBACK: tg=%s rating=%d bk=%s comment=%.50s", tg_id, rating, booking_id, comment)
        
        return ok({"submitted": True, "rating": rating})
    except Exception as e:
        logger.exception("feedback/submit error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/suggestions", tags=["Suggestions"], summary="List all suggestions")
async def api_suggestions_list(
    status: str = Query("", description="Filter by status: pending, approved, rejected"),
    auth=Depends(verify_api_key)
):
    """Get all game suggestions with optional status filter."""
    try:
        suggestions = _load_suggestions()
        # Normalize: treat missing status as 'pending'
        for s in suggestions:
            if 'status' not in s or not s.get('status'):
                s['status'] = 'pending'
        if status and status in ('pending', 'approved', 'rejected'):
            suggestions = [s for s in suggestions if s.get('status') == status]
        return ok({"suggestions": suggestions, "total": len(suggestions)})
    except Exception as e:
        return error_response(message=str(e))

@router.get("/suggestions/{suggestion_id}", tags=["Suggestions"], summary="Get single suggestion")
async def api_suggestion_get(suggestion_id: int, auth=Depends(verify_api_key)):
    """Get a single suggestion by ID."""
    try:
        suggestions = _load_suggestions()
        suggestion = next((s for s in suggestions if s.get('id') == suggestion_id), None)
        if not suggestion:
            raise HTTPException(status_code=404, detail=f"Suggestion #{suggestion_id} not found")
        return ok(suggestion)
    except HTTPException:
        raise
    except Exception as e:
        return error_response(message=str(e))

@router.put("/suggestions/{suggestion_id}", tags=["Suggestions"], summary="Update suggestion status")
async def api_suggestion_update(suggestion_id: int, req: dict, auth=Depends(verify_api_key)):
    """Approve or reject a suggestion. Body: { status: 'approved'|'rejected', reviewedBy: 'username', reason: 'optional' }"""
    try:
        new_status = req.get('status', '')
        if new_status not in ('approved', 'rejected'):
            return error_response(message="Status must be 'approved' or 'rejected'")

        suggestions = _load_suggestions()
        suggestion = next((s for s in suggestions if s.get('id') == suggestion_id), None)
        if not suggestion:
            raise HTTPException(status_code=404, detail=f"Suggestion #{suggestion_id} not found")
        if suggestion.get('status') != 'pending':
            return error_response(message=f"Suggestion #{suggestion_id} is already {suggestion.get('status')}")

        suggestion['status'] = new_status
        suggestion['reviewedAt'] = datetime.now(timezone.utc).isoformat()
        suggestion['reviewedBy'] = req.get('reviewedBy', 'dashboard')

        if new_status == 'rejected':
            suggestion['rejectReason'] = req.get('reason', 'No reason provided')
            suggestion['rejectedBy'] = req.get('reviewedBy', 'dashboard')
        else:
            suggestion['approvedBy'] = req.get('reviewedBy', 'dashboard')

        if not _save_suggestions(suggestions):
            return error_response(message="Failed to save suggestions file")

        logger.info("Suggestion #%d %s by %s", suggestion_id, new_status, req.get('reviewedBy', 'unknown'))
        return ok({"suggestion": suggestion, "message": f"Suggestion #{suggestion_id} {new_status}"})
    except HTTPException:
        raise
    except Exception as e:
        return error_response(message=str(e))


# ════════════════════════════════════════════════════════════════════════════
#  POST /api/bookings/{booking_id}/reschedule — edit console/date/time (Staff App)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/bookings/{booking_id}/reschedule", response_model=GenericResponse, tags=["Bookings"],
             summary="Reschedule a confirmed booking (edit console/date/time/duration) [MySQL]")
async def api_booking_reschedule(booking_id: int, req: dict, request: Request, auth=Depends(verify_api_key)):
    """Edit the console, date, start time and duration of a CONFIRMED booking.

    Body: {console_id, date (YYYY-MM-DD), time_slot (HH:MM), duration_mins,
           staff_name?, reason?}

    Only ``confirmed`` bookings may be rescheduled; a booking that has already
    started a session (console_sessions active/checked_in) is blocked. The target
    console + slot is conflict-checked with the same overlap rules as
    /booking-conflicts, excluding this booking."""
    try:
        booking_id = int(booking_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid booking id")

    booking = _mysql_query_one(
        "SELECT id, console_id, branch_id, member_id, customer_name, booking_date, "
        "planned_start, planned_end, planned_duration_mins, status, staff_name, notes, "
        "telegram_chat_id, phone, game_name "
        "FROM console_bookings WHERE id=%s",
        (booking_id,)
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # ── Eligibility: confirmed only ──
    # 🆕 2026-10-20: an auto no-show cancellation may be rebooked (reactivated) instead of
    # forcing staff to create a duplicate booking — the deposit is restored + the forfeit
    # income entry is reversed below (BK#4073 phantom-forfeit fix).
    status = (booking.get("status") or "").strip()
    _auto_noshow = "Auto-cancelled: no-show" in (booking.get("notes") or "")
    _reactivate = (status == "cancelled" and _auto_noshow)
    if status != "confirmed" and not _reactivate:
        raise HTTPException(status_code=400, detail=f"Only confirmed bookings can be rescheduled (current: {status or '?'})")

    # ── Active-session guard ──
    _sess = _mysql_query_one(
        "SELECT id FROM console_sessions WHERE booking_id=%s AND status IN ('active','checked_in') LIMIT 1",
        (booking_id,)
    )
    if _sess:
        raise HTTPException(status_code=400, detail="Session \u1005\u1015\u103c\u102e\u1038\u101e\u102c\u1038 \u2014 edit \u1019\u101c\u102f\u1015\u103a\u1014\u102d\u102f\u1004\u103a\u1015\u102b")

    # ── Parse + validate new fields ──
    console_id = (req.get("console_id") or "").strip()
    date_str = (req.get("date") or req.get("booking_date") or "").strip()
    time_slot = (req.get("time_slot") or req.get("start_time") or "").strip()
    duration_mins = req.get("duration_mins")
    reason = str(req.get("reason") or "").strip()
    staff = staff_audit.identity_from_request(request, req)

    if not console_id:
        raise HTTPException(status_code=400, detail="Console is required")
    if not date_str:
        raise HTTPException(status_code=400, detail="Date is required")
    if not time_slot:
        raise HTTPException(status_code=400, detail="Start time is required")

    # console must exist
    _cons = _mysql_query_one("SELECT console_id, console_type FROM console_status WHERE console_id=%s", (console_id,))
    if not _cons:
        raise HTTPException(status_code=400, detail=f"Console {console_id} does not exist")

    # time_slot HH:MM (24h)
    try:
        h, m = map(int, time_slot.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="time_slot format must be HH:MM (24h)")

    # date YYYY-MM-DD (naive, matching how planned_start is stored)
    try:
        start_dt = datetime.strptime(f"{date_str} {h:02d}:{m:02d}:00", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # duration bounds (same as create path)
    try:
        duration_mins = int(duration_mins)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration_mins must be an integer")
    if duration_mins < 30 or duration_mins > 480:
        raise HTTPException(status_code=400, detail="Duration must be between 30 and 480 minutes")

    end_dt = start_dt + timedelta(minutes=duration_mins)

    # ── Conflict check (same overlap rules as /booking-conflicts, exclude self) ──
    _conf = _mysql_query(
        "SELECT cb.id FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
        "WHERE cb.console_id=%s "
        "AND (cb.status IN ('confirmed','pending_check_in','checked_in','Active') OR (cb.status = 'pending' AND (cb.deposit_status = 'paid' OR (cb.member_id IS NOT NULL AND cb.member_id != '') OR cb.created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)))) "
        "AND cb.id != %s "
        "AND NOT (COALESCE(cs.actual_end, cb.planned_end) <= %s OR COALESCE(cs.actual_start, cb.planned_start) >= %s) "
        "LIMIT 1",
        (console_id, booking_id, start_dt, end_dt)
    )
    if _conf:
        raise HTTPException(status_code=409, detail=f"\u23f0 Console {console_id} already has a booking (BK#{_conf[0]['id']}) at this time slot")

    # ── Compute before/after for the audit trail ──
    old_console = booking.get("console_id") or ""
    old_date = str(booking.get("booking_date"))[:10] if booking.get("booking_date") else ""
    old_start = booking.get("planned_start")
    old_time = ""
    if old_start:
        try:
            old_time = (datetime.fromisoformat(str(old_start)) if isinstance(old_start, str) else old_start).strftime("%H:%M")
        except Exception:
            old_time = str(old_start)[:5] if old_start else ""
    old_dur = booking.get("planned_duration_mins") or ""

    _before = f"{old_console} / {old_date} {old_time} / {old_dur}m"
    # 24h-normalised (raw staff-app time_slot sometimes arrives as 12h -> "06:30" for 18:30)
    _after = f"{console_id} / {date_str} {start_dt.strftime('%H:%M')} / {duration_mins}m"
    _note = f"Rescheduled: {_before} \u2192 {_after}"
    if staff:
        _note += f" (by {staff})"
    if reason:
        _note += f" \u2014 {reason}"
    _prev_notes = (booking.get("notes") or "").strip()
    _new_notes = ((_prev_notes + " | " + _note) if _prev_notes else _note)

    # ── Persist the reschedule ──
    if _reactivate:
        _mysql_exec(
            "UPDATE console_bookings SET console_id=%s, booking_date=%s, planned_start=%s, planned_end=%s, "
            "planned_duration_mins=%s, notes=%s, status='confirmed', cancelled_at=NULL WHERE id=%s",
            (console_id, date_str, start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S'),
             duration_mins, _new_notes, booking_id)
        )
    else:
        _mysql_exec(
            "UPDATE console_bookings SET console_id=%s, booking_date=%s, planned_start=%s, planned_end=%s, "
            "planned_duration_mins=%s, notes=%s WHERE id=%s",
            (console_id, date_str, start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S'),
             duration_mins, _new_notes, booking_id)
        )

    # ── Deposit safety net (🐛 2026-10-20) ──
    # Rebooking a booking that was auto-cancelled as a no-show must NOT leave a phantom
    # "Forfeit BK#N" income entry behind (that is what happened to BK#4073: forfeit kept
    # while the guest later played). Restore the deposit and reverse any outstanding
    # forfeit injects — _reverse_outstanding_forfeits() is idempotent (no-op if none).
    try:
        _dep_now = _mysql_query_one(
            "SELECT deposit_status, deposit_amount FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if _dep_now and (_dep_now.get("deposit_status") or "") == "forfeited":
            _mysql_exec(
                "UPDATE console_bookings SET deposit_status='verified', cancelled_at=NULL "
                "WHERE id=%s AND deposit_status='forfeited' AND deposit_amount > 0",
                (booking_id,)
            )
            logger.info("reschedule: restored forfeited deposit for BK#%s", booking_id)
        _rev_count, _rev_total = _reverse_outstanding_forfeits(booking_id)
        if _rev_count:
            logger.info(
                "reschedule: reversed %s phantom forfeit inject(s) for BK#%s (total %s Ks)",
                _rev_count, booking_id, _rev_total
            )
    except Exception as _dep_err:
        logger.warning("reschedule: deposit restore/reversal failed (non-critical) BK#%s: %s", booking_id, _dep_err)

    _log_booking_event(booking_id, "reschedule", "console/date/time", _before, _after,
                       staff, "api", f"reactivated={_reactivate}; raw_time_slot={time_slot}")

    # ── Re-point any pending 30-min advance reminder (cancel + re-add) ──
    try:
        cancel_booking_reminder(str(booking_id))
        # booking_reminder expects date as MM/DD/YYYY (matches its parse regex)
        _yy, _mo, _dd = date_str.split("-")
        add_booking_reminder({
            "bk_id": str(booking_id),
            "customer_name": booking.get("customer_name") or booking.get("member_id") or "Guest",
            "phone": booking.get("phone") or "",
            "console_id": console_id,
            "console_type": _cons.get("console_type") or "",
            "date": f"{_mo}/{_dd}/{_yy}",
            "time_slot": time_slot,
            "duration_mins": duration_mins,
            "telegram_chat_id": booking.get("telegram_chat_id") or "",
        })
        logger.info("reschedule: re-pointed booking reminder for BK#%s", booking_id)
    except Exception as _rm_err:
        logger.warning("reschedule: reminder re-point failed (non-critical): %s", _rm_err)

    # ── Sync console status (old + new console) ──
    for _cid in {old_console, console_id}:
        if _cid:
            try:
                _sync_console_status(_cid)
            except Exception:
                pass

    # ── Notifications (fire-and-forget, never fail the reschedule) ──
    try:
        _notify_booking_rescheduled(booking_id, booking, console_id, date_str, time_slot, duration_mins, staff, reason)
    except Exception as _nt_err:
        logger.warning("reschedule: notify failed (non-critical): %s", _nt_err)

    return ok({
        "booking_id": booking_id,
        "console_id": console_id,
        "date": date_str,
        "time_slot": time_slot,
        "duration_mins": duration_mins,
        "planned_start": start_dt.strftime('%Y-%m-%d %H:%M:%S'),
        "planned_end": end_dt.strftime('%Y-%m-%d %H:%M:%S'),
        "status": "confirmed",
        "reactivated": bool(_reactivate),
        "notes": _new_notes,
    })


# ════════════════════════════════════════════════════════════════════════════
#  POST /api/booking-conflicts — check for time/console conflicts
# ════════════════════════════════════════════════════════════════════════════
@router.post("/booking-conflicts", response_model=GenericResponse, tags=["Bookings"],
         summary="Check for booking conflicts before submission")
async def api_booking_conflicts(req: dict, auth=Depends(verify_api_key)):
    """Check if a proposed booking conflicts with existing active/confirmed bookings.
    
    Accepts:
        - date: str (YYYY-MM-DD)
        - time_slot: str (HH:MM)
        - duration_mins: int
        - console_id: str (optional — if provided, checks specific console only)
        - exclude_booking_id: int (optional — exclude this booking from conflict check)
    
    Returns:
        has_conflict: bool
        conflicts: list of conflicting bookings
    """
    try:
        date_str = req.get("date", "")
        time_slot = req.get("time_slot", "")
        duration_mins = int(req.get("duration_mins", 0))
        console_id = req.get("console_id", "")
        exclude_id = req.get("exclude_booking_id")  # for edits
        
        if not date_str or not time_slot or duration_mins <= 0:
            return error_response(message="date, time_slot, and duration_mins > 0 required")
        
        # Parse time
        try:
            h, m = map(int, time_slot.split(":"))
        except Exception:
            return error_response(message="time_slot format must be HH:MM")
        
        # Use MMT timezone (same as api_bookings_create) — bookings are stored in MMT
        _mmt_tz = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))
        start_dt = datetime.strptime(f"{date_str} {h:02d}:{m:02d}:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_mmt_tz)
        end_dt = start_dt + timedelta(minutes=duration_mins)
        
        # Query conflicting bookings (exclude cancelled/done)
        where_clause = """
            (cb.status IN ('confirmed','pending_check_in','checked_in','Active') OR (cb.status = 'pending' AND (cb.deposit_status = 'paid' OR (cb.member_id IS NOT NULL AND cb.member_id != '') OR cb.created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE))))
            AND NOT (COALESCE(cs.actual_end, cb.planned_end) <= %s OR COALESCE(cs.actual_start, cb.planned_start) >= %s)
        """
        params = [start_dt, end_dt]
        
        if console_id:
            where_clause += " AND cb.console_id = %s"
            params.append(console_id)
        
        if exclude_id:
            where_clause += " AND cb.id != %s"
            params.append(int(exclude_id))
        
        rows = _mysql_query(
            f"SELECT cb.id, cb.console_id, cb.member_id, COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins, cb.booking_date, COALESCE(cs.actual_start, cb.planned_start) AS start_time, COALESCE(cs.actual_end, cb.planned_end) AS end_time, cb.status FROM console_bookings cb LEFT JOIN console_sessions cs ON cs.booking_id=cb.id WHERE {where_clause} LIMIT 20",
            tuple(params)
        )
        
        return ok({
            "has_conflict": bool(rows),
            "conflicts": [{
                "id": r["id"],
                "console_id": r["console_id"],
                "member_id": r.get("member_id"),
                "time_slot": str(r.get("start_time", ""))[:16] if r.get("start_time") else "",
                "duration_mins": r.get("duration_mins"),
                "booking_date": str(r.get("booking_date", ""))[:10] if r.get("booking_date") else "",
                "status": r.get("status"),
            } for r in (rows or [])]
        })
    except Exception as e:
        return error_response(message=str(e))


# ════════════════════════════════════════════════════════════════════════════
#  POST /api/bookings/{booking_id}/refund — Refund deposit
# ════════════════════════════════════════════════════════════════════════════
@router.post("/bookings/{booking_id}/refund", response_model=GenericResponse, tags=["Bookings"],
          summary="Refund deposit for a cancelled booking")
async def api_booking_refund(booking_id: int, auth=Depends(verify_api_key), request: Request = None, body: dict = Body(default=None)):
    """Refund deposit for a cancelled booking.

    Rules:
    - Booking must be cancelled with forfeited deposit
    - Refund must be requested at least 1 hour before original booking start
    - Creates cash_movements eject entry
    - Notifies customer via Telegram
    """
    try:
        # Get booking
        booking = _mysql_query_one(
            "SELECT id, console_id, branch_id, member_id, booking_date, "
            "planned_start AS start_time, planned_end AS end_time, "
            "planned_duration_mins AS duration_mins, "
            "status, staff_name, notes, telegram_chat_id, phone, "
            "game_name, deposit_status, deposit_amount, deposit_method, "
            "deposit_verified_by, deposit_verified_at, "
            "created_at, cancelled_at "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not booking:
            return error_response(message="Booking not found")

        # Check if deposit can be refunded
        dep_status = booking.get("deposit_status", "") or ""
        dep_amount = int(booking.get("deposit_amount", 0) or 0)
        
        if dep_amount <= 0:
            return error_response(message="No deposit to refund")
        
        if dep_status not in ("forfeited", "paid", "verified", "refund_requested"):
            return error_response(message=f"Deposit status is '{dep_status}', cannot refund")
        
        # Check 1-hour rule: must be >= 1hr before original start time.
        # Skip when deposit_status == 'refund_requested': eligibility was already
        # locked in when the customer cancelled within the >=1h window, so staff
        # approval timing (near/after the cutoff) must not re-impose the rule.
        from datetime import datetime as _refund_dt, timedelta as _refund_td
        start_time = booking.get("start_time")
        now_mmt_dt = now_mmt()
        
        if dep_status != "refund_requested" and start_time:
            try:
                if isinstance(start_time, str):
                    start_dt = _refund_dt.fromisoformat(start_time)
                else:
                    start_dt = start_time
                # Check if at least 1 hour remains before start
                _1hr_before = start_dt - _refund_td(hours=1)
                if now_mmt_dt >= _1hr_before:
                    return error_response(
                        message=f"Too late to refund — booking at {start_dt.strftime('%H:%M')}, "
                                f"refunds allowed at least 1hr before (before {_1hr_before.strftime('%H:%M')})"
                    )
            except Exception as _parse_err:
                logger.warning(f"refund bk#{booking_id} start_time parse: {_parse_err}")
                pass  # If can't parse time, still allow refund

        # Auto-extract staff name from JWT
        refunded_by = ""
        auth_header = (request.headers.get("Authorization", "") if request else "")
        if auth_header.startswith("Bearer "):
            from auth import decode_token
            payload = decode_token(auth_header[7:])
            if payload:
                refunded_by = payload.get("name") or payload.get("sub") or ""

        # Determine refund payment method for cash_movements (default = deposit method)
        deposit_method = booking.get("deposit_method", "kpay") or "kpay"
        refund_method = ((body or {}).get("refund_method") or deposit_method).strip().lower()
        _method_map = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "cash": "Cash", "mmqr_cb": "MMQR - CB"}
        method_label = _method_map.get(refund_method, _method_map.get(deposit_method, deposit_method))

        # Update booking: deposit_status = 'refunded', add note
        import datetime as _refund_dt2
        now_str = _refund_dt2.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note_suffix = f" | Refunded {dep_amount}Ks by {refunded_by or 'admin'} at {now_str}"

        # Owner rule (2026-10-02): staff approval of a refund means the deposit
        # actually arrived -> AUTO-VERIFY it (stamp verified_by/at) before refunding.
        if dep_status in ("paid", "refund_requested") and not booking.get("deposit_verified_at"):
            try:
                _mysql_exec(
                    "UPDATE console_bookings SET deposit_verified_by=%s, deposit_verified_at=%s WHERE id=%s",
                    (refunded_by or "staff-approval", now_str, booking_id)
                )
                note_suffix += " [deposit auto-verified on approval]"
            except Exception as _av_e:
                logger.warning(f"refund bk#{booking_id} auto-verify: {_av_e}")
        
        _mysql_exec(
            "UPDATE console_bookings SET deposit_status='refunded', "
            "notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
            (note_suffix, booking_id)
        )

        # Record cash_movements eject
        note = f"[Refund] BK#{booking_id} via {method_label}"
        _mysql_exec(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, category, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
            ("eject", method_label, dep_amount, note, refunded_by or "admin", "deposit_refund")
        )

        # Notify customer via Telegram
        tg_chat_id = booking.get("telegram_chat_id", "")
        if tg_chat_id:
            bk_date = str(booking.get("booking_date", ""))[:10] if booking.get("booking_date") else ""
            bk_time = ""
            s = booking.get("start_time")
            if s:
                try:
                    st = _refund_dt.fromisoformat(str(s)) if isinstance(s, str) else s
                    bk_time = st.strftime("%H:%M")
                except Exception:
                    bk_time = str(s)[:5] if s else ""
            msg = (
                f"\U0001f504 <b>Deposit Refunded — Booking #{booking_id}</b>\n\n"
                f"\U0001f4b0 သင့် Deposit {dep_amount:,} Ks ကို "
                f"ပြန်အမ်းပေးလိုက်ပါပြီ။\n\n"
                f"\U0001f3ab Booking #{booking_id}\n"
                f"\U0001f4c5 {bk_date}  \u23f0 {bk_time}\n"
                f"\U0001f3ae {booking.get('console_id', '')}\n\n"
                f"\U0001f4b0 Refund: {dep_amount:,} Ks ({method_label})\n\n"
                f"\U0001f4ac ဆက်သွယ်ရန် @psvibeofficial"
            )
            import threading as _refund_thread
            _refund_thread.Thread(
                target=_send_telegram_customer,
                args=(tg_chat_id, msg),
                daemon=True
            ).start()

        # Notify admin group
        try:
            _notify_deposit_refunded(booking_id, booking, dep_amount, method_label)
        except Exception as _notify_e:
            logger.warning(f"refund bk#{booking_id} admin notify: {_notify_e}")

        logger.info(f"Deposit refunded: BK#{booking_id} {dep_amount}Ks {method_label} by {refunded_by or 'admin'}")

        return ok({
            "booking_id": booking_id,
            "deposit_status": "refunded",
            "deposit_amount": dep_amount,
            "cash_movement": f"eject {dep_amount} Ks from {method_label}"
        })
    except Exception as e:
        logger.error(f"refund bk#{booking_id} error: {e}")
        return error_response(message=str(e))


# ════════════════════════════════════════════════════════════════════════════
#  Customer Cancel — preview + cancel (free / refund_request / forfeit)
# ════════════════════════════════════════════════════════════════════════════
def _compute_cancel_mode(dep_amt: int, dep_status: str, planned_start):
    """Determine cancellation mode: free / refund_request / forfeit.

    planned_start is MMT wall-clock; compared against now_mmt() with NO tz shift.
    """
    dep_status = (dep_status or "none").strip().lower()
    dep_amt = int(dep_amt or 0)
    if dep_amt <= 0 or dep_status in ("none", "pending"):
        return "free"
    if dep_status in ("paid", "verified"):
        if planned_start:
            try:
                start_dt = planned_start
                if isinstance(start_dt, str):
                    start_dt = datetime.fromisoformat(start_dt)
                now_dt = now_mmt().replace(tzinfo=None)  # naive MMT wall-clock
                cutoff = start_dt - timedelta(hours=1)
                if now_dt < cutoff:
                    return "refund_request"
            except Exception as _e:
                logger.warning(f"_compute_cancel_mode planned_start parse: {_e}")
        return "forfeit"
    # forfeited / refunded / redeemed / refund_requested → already settled → free
    return "free"


def _notify_refund_request_pending(bk_id: int, row: dict, deposit_amount: int):
    """Notify STAFF group (+ deposit group) that a refund request is pending approval."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return
    # Owner rule 2026-10-02: staff must SEE refund requests -> post to STAFF group
    # (and the deposit group too). Previously only DEPOSIT_NOTIFY_CHAT was used.
    chat_ids = []
    for _k in ("STAFF_NOTIFY_CHAT", "DEPOSIT_NOTIFY_CHAT"):
        _v = (os.environ.get(_k, "") or "").strip()
        if _v and _v not in chat_ids:
            chat_ids.append(_v)
    if not chat_ids:
        chat_ids = ["-1003686032747"]
    date = str(row.get('booking_date', ''))[:10] if row.get('booking_date') else '\u2014'
    start = str(row.get('planned_start', ''))[:16] if row.get('planned_start') else '\u2014'
    console = str(row.get('console_id', 'PS5'))
    member = str(row.get('member_id', '\u2014'))
    phone = str(row.get('phone', '\u2014'))
    msg = (
        f"\U0001F4E9 <b>Refund Request Pending — BK#{bk_id}</b>\n\n"
        f"\U0001F464 {member}\n"
        f"\U0001F4DE {phone}\n"
        f"\U0001F4C5 {date}  \u23F0 {start}\n"
        f"\U0001F3AE {console}\n"
        f"\U0001F4B0 {deposit_amount:,} Ks — <b>Refund Requested</b>\n\n"
        f"<i>Customer cancelled and requested a refund. Awaiting approval.</i>"
    )
    for chat_id in chat_ids:
        try:
            payload = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"}
                ),
                timeout=5
            )
        except Exception as _e:
            logger.warning(f"[_notify_refund_request_pending] bk#{bk_id} chat {chat_id} notify failed: {_e}")


@router.get("/bookings/{booking_id}/cancel-preview", response_model=GenericResponse, tags=["Bookings"],
            summary="Preview cancellation mode for a booking")
async def api_booking_cancel_preview(booking_id: int, auth=Depends(verify_api_key)):
    """Return the cancel mode (free / refund_request / forfeit) for a booking."""
    try:
        row = _mysql_query_one(
            "SELECT id, status, deposit_status, deposit_amount, planned_start "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not row:
            return error_response(message=f"Booking #{booking_id} not found")

        status = (row.get("status") or "").strip().lower()
        if status not in ("pending", "confirmed", "scheduled", "active"):
            return error_response(message=f"Booking status '{row.get('status')}' cannot be cancelled")

        dep_status = row.get("deposit_status") or "none"
        dep_amt = int(row.get("deposit_amount") or 0)
        planned_start = row.get("planned_start")

        mode = _compute_cancel_mode(dep_amt, dep_status, planned_start)

        cutoff = ""
        minutes_to_start = None
        if planned_start:
            try:
                start_dt = planned_start
                if isinstance(start_dt, str):
                    start_dt = datetime.fromisoformat(start_dt)
                now_dt = now_mmt().replace(tzinfo=None)
                cutoff = (start_dt - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                minutes_to_start = int((start_dt - now_dt).total_seconds() // 60)
            except Exception as _e:
                logger.warning(f"cancel-preview bk#{booking_id} planned_start parse: {_e}")

        return ok({
            "booking_id": booking_id,
            "mode": mode,
            "deposit_status": dep_status,
            "deposit_amount": dep_amt,
            "planned_start": str(planned_start) if planned_start else "",
            "cutoff": cutoff,
            "minutes_to_start": minutes_to_start,
        })
    except Exception as e:
        logger.error(f"cancel-preview bk#{booking_id} error: {e}")
        return error_response(message=str(e))


@router.post("/bookings/{booking_id}/cancel", response_model=GenericResponse, tags=["Bookings"],
             summary="Cancel a booking (customer-side)")
async def api_booking_cancel_customer(booking_id: int, auth=Depends(verify_api_key), request: Request = None, body: dict = Body(default=None)):
    """Cancel a booking. Mode is recomputed server-side (free / refund_request / forfeit)."""
    try:
        row = _mysql_query_one(
            "SELECT id, status, deposit_status, deposit_amount, deposit_method, "
            "planned_start, planned_start AS start_time, booking_date, console_id, "
            "member_id, phone, telegram_chat_id, game_name "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not row:
            return error_response(message=f"Booking #{booking_id} not found")

        status = (row.get("status") or "").strip().lower()
        if status not in ("pending", "confirmed", "scheduled", "active"):
            return error_response(message=f"Booking status '{row.get('status')}' cannot be cancelled")

        dep_status = row.get("deposit_status") or "none"
        dep_amt = int(row.get("deposit_amount") or 0)
        planned_start = row.get("planned_start")

        # Recompute mode server-side (trust recomputed mode, NOT body.mode)
        mode = _compute_cancel_mode(dep_amt, dep_status, planned_start)

        body = body or {}
        confirmed = bool(body.get("confirmed", False))
        reason = str(body.get("reason") or "").strip()
        tg_chat_id = str(body.get("telegram_chat_id") or row.get("telegram_chat_id") or "")

        if mode == "forfeit" and not confirmed:
            return error_response(message="confirmation required")

        now_str = now_mmt().strftime("%Y-%m-%d %H:%M:%S")

        if mode == "free":
            note = f" | Customer cancelled (free) at {now_str}"
            if reason:
                note += f" — {reason}"
            _mysql_exec(
                "UPDATE console_bookings SET status='cancelled', cancelled_at=%s, "
                "notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
                (now_str, note, booking_id)
            )
            result_dep_status = dep_status

        elif mode == "refund_request":
            note = f" | Customer cancelled — refund requested at {now_str}"
            if reason:
                note += f" — {reason}"
            _mysql_exec(
                "UPDATE console_bookings SET status='cancelled', cancelled_at=%s, "
                "deposit_status='refund_requested', "
                "notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
                (now_str, note, booking_id)
            )
            result_dep_status = "refund_requested"
            # Notify staff group
            try:
                _notify_refund_request_pending(booking_id, row, dep_amt)
            except Exception as _n_e:
                logger.warning(f"cancel bk#{booking_id} refund-request staff notify: {_n_e}")
            # DM customer
            if tg_chat_id:
                _cust_msg = (
                    f"\u2705 <b>Booking #{booking_id} — Cancelled</b>\n\n"
                    f"သင့် Booking ကို cancel ပြုလုပ်ပြီးပါပြီ။\n"
                    f"Refund request တင်သွင်းပြီးပါပြီ။\n"
                    f"Approve ဖြစ်သည့်အခါ အကြောင်းကြားပေးပါမည်။\n\n"
                    f"\U0001F4F2 @psvibeofficial"
                )
                _threading_notify.Thread(target=_send_telegram_customer, args=(tg_chat_id, _cust_msg), daemon=True).start()

        elif mode == "forfeit":
            note = f" | Customer cancelled — deposit forfeited at {now_str}"
            if reason:
                note += f" — {reason}"
            _mysql_exec(
                "UPDATE console_bookings SET status='cancelled', cancelled_at=%s, "
                "deposit_status='forfeited', "
                "notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
                (now_str, note, booking_id)
            )
            result_dep_status = "forfeited"
            # Record forfeited deposit as shop income (mirror deposit_forfeit category)
            if dep_amt > 0:
                _dep_meth = (row.get("deposit_method") or "kpay").lower()
                _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "cash": "Cash", "mmqr_cb": "MMQR - CB"}.get(_dep_meth, "KPay")
                insert_cash_movement_if_absent(
                    "inject", _meth_label, dep_amt,
                    f"Forfeit BK#{booking_id}%",
                    f"Forfeit BK#{booking_id} via {_meth_label}", "system", "deposit_forfeit")
            # Notify staff group
            try:
                _notify_deposit_forfeited(booking_id, row, dep_amt)
            except Exception as _n_e:
                logger.warning(f"cancel bk#{booking_id} forfeit staff notify: {_n_e}")
            # DM customer
            if tg_chat_id:
                _cust_msg = (
                    f"\u26A0\uFE0F <b>Booking #{booking_id} — Cancelled</b>\n\n"
                    f"Deposit {dep_amt:,} Ks ကို forfeited အဖြစ် သတ်မှတ်လိုက်ပါသည်။\n\n"
                    f"\U0001F4F2 @psvibeofficial"
                )
                _threading_notify.Thread(target=_send_telegram_customer, args=(tg_chat_id, _cust_msg), daemon=True).start()

        logger.info(f"Booking #{booking_id} cancelled via customer-cancel mode={mode}")
        return ok({
            "booking_id": booking_id,
            "mode": mode,
            "deposit_status": result_dep_status,
        })
    except Exception as e:
        logger.error(f"cancel bk#{booking_id} error: {e}")
        return error_response(message=str(e))


# ════════════════════════════════════════════════════════════════════════════
#  POST /api/bookings/auto-cancel-no-show — cron-driven no-show cleanup
# ════════════════════════════════════════════════════════════════════════════
@router.post("/bookings/auto-cancel-no-show", tags=["Bookings"],
          summary="Auto-cancel no-show bookings (cron-driven)")
async def api_auto_cancel_no_show(auth=Depends(verify_api_key)):
    """Auto-cancel no-show bookings past their start_time.
    
    Cancels confirmed/pending bookings past grace period.
    Skips bookings with active/checked_in console_sessions.
    Forfeits deposit for no-shows with paid/verified deposit.
    
    Called by cron every 5 minutes.
    Grace period: 15 minutes after start_time.
    """
    try:
        _now = now_mmt()
        _grace_minutes = 15
        
        # Find bookings past start_time + grace with no check-in
        rows = _mysql_query("""
            SELECT cb.id, cb.console_id, cb.booking_date,
                   COALESCE(cs.actual_start, cb.planned_start) AS start_time,
                   COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins,
                   cb.phone, cb.telegram_chat_id,
                   cb.member_id, cb.game_name
            FROM console_bookings cb
            LEFT JOIN console_sessions cs ON cs.booking_id=cb.id
            WHERE cb.status IN ('confirmed', 'pending', 'pending_check_in')
              AND cb.planned_start < DATE_SUB(%s, INTERVAL %s MINUTE)
              AND cs.id IS NULL  -- skip bookings with active/checked_in sessions
            ORDER BY cb.id ASC
        """, (_now, _grace_minutes))
        
        cancelled = []
        for row in (rows or []):
            bk_id = row['id']
            try:
                # Check if deposit will be forfeited (Boss policy 2026-07-31: only verified forfeits;
                # unverified 'paid' -> void to 'none', not revenue)
                _pre = _mysql_query_one(
                    "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
                    (bk_id,)
                )
                _will_forfeit = _pre and _pre.get('deposit_status', '') == 'verified'
                
                _mysql_exec(
                    "UPDATE console_bookings SET status='cancelled', cancelled_at=DATE_ADD(NOW(), INTERVAL 390 MINUTE), "
                    "deposit_status=CASE WHEN deposit_status='verified' THEN 'forfeited' WHEN deposit_status='paid' THEN 'none' ELSE deposit_status END, "
                    "notes=CONCAT(COALESCE(notes,''), ' | Auto-cancelled: no-show') WHERE id=%s AND deposit_status <> 'forfeited'",
                    (bk_id,)
                )
                # Clear console_status
                cid = row.get('console_id', '')
                if cid:
                    _sync_console_status(cid)
                
                # Notify customer if telegram_chat_id exists
                tg_chat_id = row.get('telegram_chat_id', '')
                if tg_chat_id:
                    try:
                        _notify_no_show_cancel(bk_id, row)
                    except Exception as e:
                        logger.warning(f"auto-cancel notify bk#{bk_id} failed: {e}")
                
                # Notify admin group if deposit is forfeited
                if _will_forfeit:
                    _notify_deposit_forfeited(bk_id, row, _pre.get('deposit_amount', 0))
                    # 🆕 Record forfeited deposit as shop income in cash_movements
                    try:
                        _dep_amt = int(_pre.get('deposit_amount', 0) or 0)
                        _dep_meth = (_pre.get('deposit_method', '') or 'kpay').lower()
                        _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(_dep_meth, "KPay")
                        if _dep_amt > 0:
                            insert_cash_movement_if_absent(
                                "inject", _meth_label, _dep_amt,
                                f"Forfeit BK#{bk_id}%",
                                f"Forfeit BK#{bk_id} via {_meth_label}", "system", "deposit_forfeit")
                            logger.info(f"Auto-cancel forfeit: BK#{bk_id} {_dep_amt}Ks {_meth_label}")
                    except Exception as _fe:
                        logger.warning(f"Auto-cancel forfeit cash_movements BK#{bk_id}: {_fe}")
                
                cancelled.append(bk_id)
                logger.info(f"Auto-cancelled no-show booking #{bk_id}")
            except Exception as e:
                logger.error(f"auto-cancel bk#{bk_id} failed: {e}")
        
        # ═══════════════════════════════════════════════════════════════
        #  Pending Deposit Timeout — 15 min (Boss request 2026-07-24)
        #  Cancel pending bookings without deposit that are >15 min old
        # ═══════════════════════════════════════════════════════════════
        _pending_expired = _mysql_query("""
            SELECT id, console_id, member_id, phone, telegram_chat_id, game_name,
                   booking_date, planned_start AS start_time
            FROM console_bookings
            WHERE status='pending'
              AND (deposit_status IS NULL OR deposit_status='none' OR deposit_amount IS NULL OR deposit_amount=0)
              AND (member_id IS NULL OR member_id = '')
              AND created_at < DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)
            ORDER BY id ASC
        """)
        
        timeout_ids = []
        for row in (_pending_expired or []):
            bk_id = row['id']
            try:
                _mysql_exec(
                    "UPDATE console_bookings SET status='cancelled', cancelled_at=DATE_ADD(NOW(), INTERVAL 390 MINUTE), "
                    "notes=CONCAT(COALESCE(notes,''), ' | Auto-cancelled: deposit timeout (15min)') "
                    "WHERE id=%s AND status='pending'",
                    (bk_id,)
                )
                cid = row.get('console_id', '')
                if cid:
                    _sync_console_status(cid)
                
                # Notify customer
                tg_chat_id = row.get('telegram_chat_id', '')
                if tg_chat_id:
                    try:
                        _notify_no_show_cancel(bk_id, row)
                    except Exception as e:
                        logger.warning(f"pending timeout notify bk#{bk_id} failed: {e}")
                
                timeout_ids.append(bk_id)
                logger.info(f"Pending deposit timeout: BK#{bk_id}")
            except Exception as e:
                logger.error(f"pending timeout bk#{bk_id} failed: {e}")
        
        return ok({
            "cancelled": len(cancelled),
            "pending_timeout": len(timeout_ids),
            "ids": cancelled,
            "pending_timeout_ids": timeout_ids,
        })
    except Exception as e:
        logger.error(f"auto-cancel-no-show: {e}")
        return error_response(message=str(e))


# ════════════════════════════════════════════════════════════════════════════
#  Background task: Auto-cancel expired pending bookings
# ════════════════════════════════════════════════════════════════════════════
def _auto_cancel_expired_pending():
    """Cancel pending bookings with past start_time or older than 24h.
    Runs in a daemon thread; check every 30 minutes."""
    import time
    while True:
        try:
            if not _use_mysql():
                time.sleep(3600)
                continue
            now = datetime.now(timezone.utc)
            rows = mysql_query("""
                SELECT id, member_id, console_id, booking_date
                FROM console_bookings
                WHERE status = 'pending'
                AND (planned_start < %s OR created_at < %s)
            """, (now, now - timedelta(hours=24)))
            cancelled = 0
            for row in (rows or []):
                try:
                    _mysql_exec(
                        "UPDATE console_bookings SET status='cancelled', cancelled_at=DATE_ADD(NOW(), INTERVAL 390 MINUTE), notes=CONCAT(COALESCE(notes,''), ' | Auto-cancelled: expired pending') WHERE id=%s AND status='pending'",
                        (row['id'],)
                    )
                    cancelled += 1
                except Exception:
                    pass
            if cancelled:
                logging.info("Auto-cancelled %d expired pending bookings", cancelled)
        except Exception as e:
            logging.warning("auto_cancel_expired_pending error: %s", e)
        time.sleep(1800)  # Every 30 minutes


# Start daemon thread (no asyncio dependency, works at import time)
import threading

def now_mmt():
    """Return current datetime in MMT (UTC+6:30)"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=6, minutes=30)))
# DISABLED — auto-cancel now handled by cron (POST /api/bookings/auto-cancel-no-show)
# Was originally: auto-cancel disabled per Boss request (2026-06-22): manual-only policy.
# _bg_thread = threading.Thread(target=_auto_cancel_expired_pending, daemon=True, name="auto-cancel-pending")
# _bg_thread.start()


# Module-level resume moved to lifespan handler


# ════════════════════════════════════════════════════════════════════════════
#  DEPOSIT FLOW — Schedule / Submit / Verify / Settle / List
# ════════════════════════════════════════════════════════════════════════════

@router.get("/deposit/schedule", response_model=GenericResponse, tags=["Deposits"], summary="Calculate deposit amount for a session [MySQL]")
async def api_deposit_schedule(
    console: str = Query("", description="Console ID (e.g. C-01, C-09)"),
    duration_mins: int = Query(60, description="Session duration in minutes"),
    date: str = Query("", description="Booking date (YYYY-MM-DD)"),
    time: str = Query("", description="Booking start time (HH:MM)"),
    auth=Depends(verify_api_key)
):
    """Calculate deposit amount = session_fee × 30%.
    
    Session fee = duration_mins × base_rate × console_multiplier.
    """
    try:
        base_rate = _mysql_get_setting("base_rate", 10000)
        if isinstance(base_rate, str):
            base_rate = int(base_rate)
        # Get console multiplier
        multipliers_raw = _mysql_get_setting("console_multipliers", {})
        if isinstance(multipliers_raw, str):
            import json as _json
            try:
                multipliers_raw = _json.loads(multipliers_raw)
            except Exception:
                multipliers_raw = {}
        multiplier = multipliers_raw.get(console, 1.0)
        if not isinstance(multiplier, (int, float)):
            multiplier = 1.0
        # Calculate session fee and deposit
        # base_rate is per-hour (e.g., 10000 = 10,000 Ks/hr)
        session_fee = int((duration_mins / 60) * base_rate * multiplier)
        deposit_amount = int(session_fee * 0.3)
        # Round to nearest 500
        deposit_amount = round(deposit_amount / 500) * 500
        
        return ok({
            "deposit_amount": deposit_amount,
            "session_fee": session_fee,
            "duration_mins": duration_mins,
            "console": console,
            "base_rate": base_rate,
            "multiplier": multiplier
        })
    except Exception as e:
        logger.error(f"deposit/schedule error: {e}")
        return error_response(message=str(e))


# ════════════════════════════════════════════════════════════════════════════
#  Deposit auto-recovery (owner policy 2026-09-28)
#  A booking cancelled by the 15-min deposit timeout may still accept its
#  deposit within 30 minutes of cancellation and return to 'pending'.
# ════════════════════════════════════════════════════════════════════════════
_DEPOSIT_RECOVERY_WINDOW_MIN = 30


def _is_deposit_recoverable(status, notes, deposit_status, minutes_since_cancel):
    """Pure decision helper for deposit-timeout recovery.

    Args:
        status: booking status (e.g. 'cancelled')
        notes: booking notes text (may be None)
        deposit_status: deposit_status column value (may be None)
        minutes_since_cancel: minutes elapsed since cancelled_at (MMT wall-clock),
            or None if unknown.

    Returns True only when the cancellation was the deposit timeout, no deposit
    has been recorded, and the cancellation is within the recovery window.
    """
    if status != "cancelled":
        return False
    if "deposit timeout" not in str(notes or "").lower():
        return False
    if deposit_status not in (None, "", "none"):
        return False
    if minutes_since_cancel is None:
        return False
    try:
        mins = int(minutes_since_cancel)
    except (TypeError, ValueError):
        return False
    return 0 <= mins <= _DEPOSIT_RECOVERY_WINDOW_MIN


@router.post("/bookings/{booking_id}/deposit/submit", response_model=GenericResponse, tags=["Deposits"], summary="Submit deposit payment info [MySQL]")
async def api_deposit_submit(booking_id: int, req: dict, auth=Depends(verify_api_key)):
    """Customer submits deposit: method, ref (text or file_id), ref_type.
    Updates booking deposit_status to 'paid' and notifies staff."""
    try:
        deposit_method = req.get("deposit_method", "")
        if deposit_method == "mmqr":
            deposit_method = "mmqr_cb"
        deposit_ref = req.get("deposit_ref", "")
        deposit_ref_type = req.get("deposit_ref_type", "text")
        deposit_amount = req.get("deposit_amount", 0)
        
        # Validate
        if deposit_method not in ("kpay", "wavepay", "aya_pay", "mmqr_cb"):
            return error_response(message="Invalid deposit_method. Must be kpay, wavepay, aya_pay, or mmqr_cb")
        if not deposit_ref:
            return error_response(message="deposit_ref is required")
        if deposit_ref_type not in ("text", "image", "both"):
            return error_response(message="deposit_ref_type must be text, image, or both")
        
        # Check booking exists
        row = _mysql_query_one(
            "SELECT id, deposit_status, status, console_id, member_id, booking_date, "
            "       planned_start AS start_time, planned_end, created_at, deposit_amount, "
            "       cancelled_at, notes, "
            "       TIMESTAMPDIFF(MINUTE, cancelled_at, DATE_ADD(NOW(), INTERVAL 390 MINUTE)) AS minutes_since_cancel "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not row:
            return error_response(message=f"Booking #{booking_id} not found")
        
        # Owner policy (2026-09-28): a deposit-timeout cancellation may be
        # recovered within 30 minutes of cancellation.
        _recovering = row["status"] == "cancelled"
        if _recovering:
            if not _is_deposit_recoverable(
                    row.get("status"), row.get("notes"), row.get("deposit_status"),
                    row.get("minutes_since_cancel")):
                return error_response(message=f"Booking status is '{row['status']}', cannot submit deposit")
        elif row["status"] not in ("pending", "confirmed", "pending_check_in"):
            return error_response(message=f"Booking status is '{row['status']}', cannot submit deposit")
        
        if row["deposit_status"] not in ("none",):
            return error_response(message=f"Deposit already {row['deposit_status']} (must be 'none' to submit)")
        
        # Re-verify slot availability when recovering a cancelled booking — the
        # slot may have been taken by someone else during the recovery window.
        if _recovering:
            _rstart = row.get("start_time")
            _rend = row.get("planned_end")
            _rconsole = row.get("console_id")
            if _rstart and _rend and _rconsole:
                _taken = _mysql_query_one(
                    "SELECT id FROM console_bookings "
                    "WHERE console_id=%s AND id<>%s "
                    "AND (status IN ('confirmed','pending_check_in','checked_in','Active') "
                    "     OR (status='pending' AND (deposit_status='paid' "
                    "         OR (member_id IS NOT NULL AND member_id != '') "
                    "         OR created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)))) "
                    "AND planned_start < %s AND planned_end > %s LIMIT 1",
                    (_rconsole, booking_id, _rend, _rstart)
                )
                if _taken:
                    return error_response(message="အဲ့ slot ကို အခြားသူ ရယူသွားပါပြီ — slot အသစ် ရွေးပါ")
        
        # Update booking with deposit info
        if _recovering:
            _mysql_exec(
                "UPDATE console_bookings SET status='pending', cancelled_at=NULL, "
                "deposit_status='paid', deposit_method=%s, "
                "deposit_ref=%s, deposit_ref_type=%s, deposit_amount=%s, "
                "notes=CONCAT(COALESCE(notes,''), ' | Deposit recovered after timeout (owner policy 30min)') "
                "WHERE id=%s",
                (deposit_method, deposit_ref, deposit_ref_type, deposit_amount, booking_id)
            )
            _rcid = row.get("console_id")
            if _rcid:
                _sync_console_status(_rcid)
        else:
            _mysql_exec(
                "UPDATE console_bookings SET deposit_status='paid', deposit_method=%s, "
                "deposit_ref=%s, deposit_ref_type=%s, deposit_amount=%s WHERE id=%s",
                (deposit_method, deposit_ref, deposit_ref_type, deposit_amount, booking_id)
            )
        
        # Staff group notification (Telegram)
        import json as _sjson
        import urllib.request as _ureq
        token = os.environ.get("BOT_TOKEN", "8545665013:AAFgEuw4V_715Q9yzGOYloinIdbdYXYb8zU")
        chat_id = os.environ.get("DEPOSIT_NOTIFY_CHAT", os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747"))
        if token and chat_id:
            bk_date = str(row.get("booking_date", ""))[:10] if row.get("booking_date") else ""
            bk_time = ""
            s = row.get("start_time")
            if s:
                try:
                    from datetime import datetime as _pdt
                    st = _pdt.fromisoformat(str(s)) if isinstance(s, str) else s
                    bk_time = st.strftime("%H:%M")
                except Exception:
                    bk_time = str(s)[:5] if s else ""
            deposit_amt = int(deposit_amount or 0)
            member = row.get("member_id", "") or "Customer"
            method_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(deposit_method, deposit_method)
            msg = (
                f"\U0001f4b3 <b>Deposit Submitted — BK#{booking_id}</b>\n\n"
                f"\U0001f464 {member}\n"
                f"\U0001f4c5 {bk_date}  \u23f0 {bk_time}\n"
                f"\U0001f3ae {row.get('console_id', '')}\n"
                f"\U0001f4b0 {deposit_amt:,} Ks\n"
                f"\U0001f4b1 {method_label}\n"
                f"\U0001f4dd Ref: {deposit_ref[:50] if deposit_ref else '—'}\n\n"
                f"<i>Staff: Please verify in Deposit Pending list.</i>"
            )
            try:
                payload = _sjson.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
                _ureq.urlopen(
                    _ureq.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=payload,
                        headers={"Content-Type": "application/json"}
                    ),
                    timeout=5
                )
                # Forward deposit screenshot image if available
                # NOTE: file_id is from CUSTOMER_BOT_TOKEN (customer bot), must use same token to send
                if deposit_ref_type in ("image", "both") and deposit_ref and len(deposit_ref) > 1:
                    _photo_token = os.environ.get("CUSTOMER_BOT_TOKEN", os.environ.get("BOT_TOKEN", ""))
                    if _photo_token and _photo_token != token:
                        try:
                            _forward_url = f"https://api.telegram.org/bot{_photo_token}/sendPhoto"
                            _photo_payload = _sjson.dumps({
                                "chat_id": chat_id,
                                "photo": deposit_ref,
                                "caption": f"\U0001f4b3 Deposit SS — BK#{booking_id} ({method_label} {deposit_amt:,}Ks)",
                                "parse_mode": "HTML"
                            }).encode()
                            _ureq.urlopen(
                                _ureq.Request(
                                    _forward_url,
                                    data=_photo_payload,
                                    headers={"Content-Type": "application/json"}
                                ),
                                timeout=10
                            )
                        except Exception as _photo_e:
                            logger.warning(f"Deposit photo forward bk#{booking_id}: {_photo_e}")
            except Exception as _nte:
                logger.warning(f"Deposit staff notify failed: {_nte}")

            # ── Staff app push (FCM) — deposit needs staff verification ──
            try:
                import fcm as _fcm2
                _fcm2.send_to_staff(
                    title=f"💳 Deposit Submitted — BK#{booking_id}",
                    body=f"{member} · {bk_date} {bk_time} · {deposit_amt:,} Ks ({method_label})",
                    data={"type": "deposit_submitted", "booking_id": str(booking_id)},
                )
            except Exception as _pfe:
                logger.warning(f"Deposit push bk#{booking_id}: {_pfe}")
        
        return ok({"booking_id": booking_id, "deposit_status": "paid"})
    except Exception as e:
        logger.error(f"deposit/submit error: {e}")
        return error_response(message=str(e))


@router.patch("/bookings/{booking_id}/deposit/verify", response_model=GenericResponse, tags=["Deposits"], summary="Verify deposit & confirm booking [MySQL]")
async def api_deposit_verify(booking_id: int, req: dict, request: Request, auth=Depends(verify_api_key)):
    """Staff verifies deposit: marks as verified, updates booking to confirmed,
    creates cash_movements inject, and notifies customer."""
    try:
        # Auto-extract staff name from JWT if not provided in request
        verified_by = req.get("verified_by", "")
        deposit_amount_override = req.get("deposit_amount", None)
        
        if not verified_by:
            # Try to get from Authorization header (JWT Bearer token)
            auth_header = (request.headers.get("Authorization", "") if request else "")
            if auth_header.startswith("Bearer "):
                from auth import decode_token
                payload = decode_token(auth_header[7:])
                if payload:
                    verified_by = payload.get("name") or payload.get("sub") or ""
        
        if not verified_by:
            return error_response(message="verified_by is required (staff name)")
        
        # Check booking exists with paid deposit
        row = _mysql_query_one(
            "SELECT id, deposit_status, deposit_method, deposit_amount, "
            "       member_id, console_id, booking_date, planned_start AS start_time, "
            "       telegram_chat_id, phone, staff_name "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not row:
            return error_response(message=f"Booking #{booking_id} not found")
        
        if row["deposit_status"] != "paid":
            return error_response(message=f"Deposit status is '{row['deposit_status']}', expected 'paid'")
        
        deposit_method = row.get("deposit_method", "kpay") or "kpay"
        deposit_amount = int(row.get("deposit_amount") or 0)
        
        # Allow staff to correct deposit_amount (wrong amount cases)
        if deposit_amount_override is not None:
            try:
                deposit_amount = int(deposit_amount_override)
            except (ValueError, TypeError):
                return error_response(message="Invalid deposit_amount override")
        
        # Update booking: verified status, confirmed booking
        import datetime as _dep_dt
        now_str = _dep_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _mysql_exec(
            "UPDATE console_bookings SET deposit_status='verified', "
            "deposit_verified_by=%s, deposit_verified_at=%s, "
            "deposit_amount=%s, status='confirmed' "
            "WHERE id=%s",
            (verified_by, now_str, deposit_amount, booking_id)
        )
        
        # Create cash_movements inject entry
        method_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB"}.get(deposit_method, deposit_method)
        note = f"Deposit #{booking_id} via {method_label}"
        _mysql_exec(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, category, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
            ("inject", method_label, deposit_amount, note, verified_by, "deposit")
        )
        
        # Customer notification (Telegram)
        tg_chat_id = row.get("telegram_chat_id", "")
        if tg_chat_id:
            bk_date = str(row.get("booking_date", ""))[:10] if row.get("booking_date") else ""
            bk_time = ""
            s = row.get("start_time")
            if s:
                try:
                    from datetime import datetime as _pdt2
                    st = _pdt2.fromisoformat(str(s)) if isinstance(s, str) else s
                    bk_time = st.strftime("%H:%M")
                except Exception:
                    bk_time = str(s)[:5] if s else ""
            msg = (
                f"\u2705 <b>Deposit Verified & Booking Confirmed!</b>\n\n"
                f"\U0001f3ab Booking <b>#{booking_id}</b>\n"
                f"\U0001f4c5 {bk_date}  \u23f0 {bk_time}\n"
                f"\U0001f3ae {row.get('console_id', '')}\n"
                f"\U0001f4b0 Deposit: {deposit_amount:,} Ks ({method_label})"
            )
            _threading_notify.Thread(
                target=_send_telegram_customer,
                args=(tg_chat_id, msg),
                daemon=True
            ).start()
            # Owner 2026-10-02: send the Booking Cancel & Refund terms image on
            # confirmation — OFF by default (owner: "လောလောဆယ် မပို့သေးနဲ့"). Flip
            # on later with env PSVIBE_TERMS_IMAGE_ENABLED=1 (no code change).
            if _terms_image_enabled():
                try:
                    _threading_notify.Thread(
                        target=_send_telegram_customer_photo,
                        args=(tg_chat_id, PSVIBE_TERMS_IMAGE_URL,
                              "📋 Booking Cancel & Refund — စည်းကမ်းချက်များ"),
                        daemon=True
                    ).start()
                except Exception as _ti_e:
                    logger.warning(f"verify bk#{booking_id} terms image: {_ti_e}")

        return ok({
            "booking_id": booking_id,
            "deposit_status": "verified",
            "booking_status": "confirmed",
            "deposit_amount": deposit_amount,
            "cash_movement": f"inject {deposit_amount} Ks into {method_label}"
        })
    except Exception as e:
        logger.error(f"deposit/verify error: {e}")
        return error_response(message=str(e))


def _reverse_outstanding_forfeits(booking_id):
    """Safety net: reverse any outstanding forfeit injects for a booking before redeem.
    
    Restore paths already insert reversal ejects when deposit_status=='forfeited' at
    restore time, but if the booking was restored via a manual DB edit (bypassing
    code), the forfeit injects remain in cash_movements with no matching eject.
    This function catches that case at redeem time - idempotent: if already
    reversed, does nothing.
    
    Returns: (reversed_count, total_reversed_amount)
    """
    try:
        bk_str = str(booking_id)
        forfeit_pattern = f"Forfeit BK#{bk_str}%"

        # Get sum of forfeit injects
        inj_rows = _mysql_query(
            "SELECT COALESCE(SUM(amount),0) AS total FROM cash_movements "
            "WHERE movement_type='inject' AND note LIKE %s",
            (forfeit_pattern,)
        )
        inj_total = int(inj_rows[0]["total"] or 0) if inj_rows else 0
        if inj_total <= 0:
            return (0, 0)

        # Get sum of existing reversal ejects (both patterns)
        refund_pattern = f"Refund forfeit BK#{bk_str}%"
        reverse_pattern = f"%deposit reverse%BK#{bk_str}%"
        ej_rows = _mysql_query(
            "SELECT COALESCE(SUM(amount),0) AS total FROM cash_movements "
            "WHERE movement_type='eject' AND (note LIKE %s OR note LIKE %s)",
            (refund_pattern, reverse_pattern)
        )
        ej_total = int(ej_rows[0]["total"] or 0) if ej_rows else 0

        diff = inj_total - ej_total
        if diff <= 0:
            return (0, 0)

        # Get individual forfeit inject rows (may be multiple per-booking - e.g. partial)
        inj_details = _mysql_query(
            "SELECT id, account, amount FROM cash_movements "
            "WHERE movement_type='inject' AND note LIKE %s ORDER BY id",
            (forfeit_pattern,)
        )

        reversed_count = 0
        reversed_total = 0
        remaining_to_reverse = diff

        for row in inj_details:
            if remaining_to_reverse <= 0:
                break
            amt = int(row["amount"] or 0)
            if amt <= 0:
                continue
            account = row["account"] or "KPay"
            # Reverse at most the remaining diff (proportional handling for multi-inject)
            reverse_amt = min(amt, remaining_to_reverse)

            insert_cash_movement_if_absent(
                "eject",
                account,
                reverse_amt,
                f"Refund forfeit BK#{bk_str}%",
                f"Refund forfeit BK#{bk_str} (deposit reverse - redeemed, session played) via {account}",
                "system",
                "deposit_refund",
            )
            reversed_count += 1
            reversed_total += reverse_amt
            remaining_to_reverse -= reverse_amt

        logger.info(
            "_reverse_outstanding_forfeits BK#%s: reversed %s injects, total %s Ks (diff was %s Ks)",
            booking_id, reversed_count, reversed_total, diff
        )
        return (reversed_count, reversed_total)
    except Exception as e:
        logger.warning("_reverse_outstanding_forfeits BK#%s error (non-fatal): %s", booking_id, e)
        return (0, 0)


@router.patch("/bookings/{booking_id}/deposit/settle", response_model=GenericResponse, tags=["Deposits"], summary="Settle final sale with deposit redemption [MySQL]")
async def api_deposit_settle(booking_id: int, req: dict, auth=Depends(verify_api_key)):
    """Settle the booking after session done: record full sale + remaining payments.
    Deposit is redeemed (applied toward total_fee).
    """
    try:
        total_fee = req.get("total_fee", 0)
        remaining_payments = req.get("remaining_payments", [])
        
        # Validate
        if not total_fee or total_fee <= 0:
            return error_response(message="total_fee is required and must be > 0")
        if not isinstance(remaining_payments, list) or not remaining_payments:
            return error_response(message="remaining_payments list is required")
        
        valid_methods = {"kpay", "wavepay", "cash", "aya_pay", "mmqr_cb", "mmqr"}
        for pmt in remaining_payments:
            if pmt.get("method", "").lower() not in valid_methods:
                return error_response(message=f"Invalid payment method: {pmt.get('method', '')}")
        
        # Check booking
        row = _mysql_query_one(
            "SELECT id, deposit_status, deposit_method, deposit_amount, "
            "       member_id, console_id, booking_date, "
            "       planned_start AS start_time, staff_name, phone, "
            "       telegram_chat_id "
            "FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not row:
            return error_response(message=f"Booking #{booking_id} not found")
        
        if row["deposit_status"] != "verified":
            return error_response(message=f"Deposit status must be 'verified' to settle, got '{row['deposit_status']}'")
        
        deposit_amount = int(row.get("deposit_amount") or 0)
        deposit_method = row.get("deposit_method", "kpay") or "kpay"
        
        # Build payment_method string for sales_daily
        method_label_map = {"kpay": "KPay", "wavepay": "WavePay", "cash": "Cash", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB", "mmqr": "MMQR - CB"}
        deposit_label = method_label_map.get(deposit_method, "KPay")
        
        # Format: "KWPay:7500(DepositRedeemed)|Cash:2500"
        pm_parts = []
        total_remaining = 0
        for pmt in remaining_payments:
            method = pmt.get("method", "").lower()
            amt = int(pmt.get("amount", 0))
            if amt > 0:
                label = method_label_map.get(method, method.capitalize())
                pm_parts.append(f"{label}:{amt}")
                total_remaining += amt
        
        # Verify deposit + remaining = total_fee
        if deposit_amount + total_remaining != total_fee:
            return error_response(
                f"Total mismatch: deposit({deposit_amount}) + remaining({total_remaining}) = {deposit_amount + total_remaining}, expected {total_fee}"
            )
        
        # Payment method string with DepositRedeemed marker
        pm_str = f"{deposit_label}:{deposit_amount}(DepositRedeemed)"
        if pm_parts:
            pm_str += "|" + "|".join(pm_parts)
        
        # Record sale in sales_daily
        now_dt = datetime.now(timezone(timedelta(hours=6, minutes=30)))
        today_str = now_dt.strftime("%Y-%m-%d")
        staff_name = row.get("staff_name", "") or "system"
        member_id = row.get("member_id", "") or ""
        phone = row.get("phone", "") or ""
        member_ref = member_id if member_id else (phone if phone else "Guest")
        
        # ── Game attribution (2026-09-20): populate game_name + game_id at sale time ──
        _game_name, _game_id, _session_id = resolve_sale_game(
            row.get("console_id", ""), booking_id=booking_id)

        sales_id = _mysql_exec(
            "INSERT INTO sales_daily (branch_id, sale_date, console_id, member_id, "
            "amount, gross, discount, net, staff_name, customer_name, payment_method, notes, created_at, session_id, game_name, game_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)",
            (
                1,  # branch_id
                today_str,
                row.get("console_id", ""),
                member_ref,
                float(total_fee),
                float(total_fee),
                0.0,
                float(total_fee),
                staff_name,
                staff_name,
                pm_str,
                f"Deposit settle - Booking #{booking_id}",
                _session_id,
                _game_name,
                _game_id
            )
        )
        
        # Record remaining payments in cash_movements as injects
        for pmt in remaining_payments:
            method = pmt.get("method", "").lower()
            amt = int(pmt.get("amount", 0))
            if amt > 0:
                label = method_label_map.get(method, method.capitalize())
                _mysql_exec(
                    "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, NOW())",
                    (
                        "inject",
                        label,
                        amt,
                        f"Deposit remaining - Booking #{booking_id} via {label}",
                        staff_name
                    )
                )
        
        # Safety net: reverse any outstanding forfeit injects before redeeming
        _reverse_outstanding_forfeits(booking_id)

        # Update booking: deposit redeemed, booking done
        _mysql_exec(
            "UPDATE console_bookings SET deposit_status='redeemed', deposit_redeemed_at=NOW(), "
            "status='done' WHERE id=%s",
            (booking_id,)
        )
        _log_booking_event(booking_id, "deposit_change", "deposit_status", "verified", "redeemed",
                           staff_name, "api", f"settle total_fee={total_fee} sales_daily_id={sales_id}")
        
        return ok({
            "booking_id": booking_id,
            "deposit_status": "redeemed",
            "booking_status": "done",
            "total_fee": total_fee,
            "deposit_applied": deposit_amount,
            "remaining_paid": total_remaining,
            "sales_daily_id": sales_id
        })
    except Exception as e:
        logger.error(f"deposit/settle error: {e}")
        return error_response(message=str(e))


_DEP_LABELS = {"kpay": "KPay", "wavepay": "WavePay", "cash": "Cash", "aya_pay": "AYA Pay",
               "mmqr_cb": "MMQR - CB", "mmqr": "MMQR - CB"}


@router.patch("/bookings/{booking_id}/deposit/refund", response_model=GenericResponse, tags=["Deposits"],
              summary="Refund a collected deposit back to the guest [MySQL]")
async def api_deposit_refund(booking_id: int, req: dict, request: Request, auth=Depends(verify_api_key)):
    """Refund a collected (paid/verified) deposit — cash OUT, status → refunded (2026-10-20).

    Before this endpoint existed the only way to settle a deposit was to redeem it
    into a sale or hand-edit the DB (which is how BK#4073 ended up with a phantom
    forfeit). Any outstanding forfeit inject is reversed here as well.
    """
    try:
        row = _mysql_query_one(
            "SELECT id, deposit_status, deposit_amount, deposit_method, member_id, customer_name "
            "FROM console_bookings WHERE id=%s", (booking_id,))
        if not row:
            raise HTTPException(status_code=404, detail=f"Booking #{booking_id} not found")
        ds = (row.get("deposit_status") or "").lower()
        amt = int(row.get("deposit_amount") or 0)
        if ds not in ("paid", "verified"):
            raise HTTPException(status_code=400, detail=f"Deposit status must be paid/verified (current: {ds})")
        if amt <= 0:
            raise HTTPException(status_code=400, detail="Booking has no deposit amount")
        method = (row.get("deposit_method") or "kpay").lower()
        label = _DEP_LABELS.get(method, "KPay")
        staff = staff_audit.identity_from_request(request, req) or "system"
        reason = str(req.get("reason") or "").strip()

        insert_cash_movement_if_absent(
            "eject", label, amt, f"Refund BK#{booking_id}%",
            f"Refund BK#{booking_id} (deposit refund{(' - ' + reason) if reason else ''}) via {label}",
            staff, "deposit_refund")
        _reverse_outstanding_forfeits(booking_id)
        _mysql_exec(
            "UPDATE console_bookings SET deposit_status='refunded', "
            "notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
            (f" | Refunded {amt:,} Ks by {staff}{(' — ' + reason) if reason else ''}", booking_id))
        _log_booking_event(booking_id, "deposit_change", "deposit_status", ds, "refunded",
                           staff, "api", f"refund {amt} Ks via {label}; reason={reason}")
        return ok({"booking_id": booking_id, "deposit_status": "refunded",
                   "refunded_amount": amt, "method": label})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"deposit/refund error BK#{booking_id}: {e}")
        return error_response(message=str(e))


@router.patch("/bookings/{booking_id}/deposit/wallet-credit", response_model=GenericResponse, tags=["Deposits"],
              summary="Convert a collected deposit into member wallet minutes [MySQL]")
async def api_deposit_wallet_credit(booking_id: int, req: dict, request: Request, auth=Depends(verify_api_key)):
    """Credit the deposit value to the guest's member wallet instead of refunding cash.

    Uses the shop base rate (Ks per hour) exactly like /topup/log so the credited
    minutes match what the guest would have received at the counter.
    """
    try:
        row = _mysql_query_one(
            "SELECT id, deposit_status, deposit_amount, deposit_method, member_id, customer_name "
            "FROM console_bookings WHERE id=%s", (booking_id,))
        if not row:
            raise HTTPException(status_code=404, detail=f"Booking #{booking_id} not found")
        ds = (row.get("deposit_status") or "").lower()
        amt = int(row.get("deposit_amount") or 0)
        member_id = (row.get("member_id") or "").strip()
        if ds not in ("paid", "verified"):
            raise HTTPException(status_code=400, detail=f"Deposit status must be paid/verified (current: {ds})")
        if amt <= 0:
            raise HTTPException(status_code=400, detail="Booking has no deposit amount")
        if not member_id:
            raise HTTPException(status_code=400, detail="Booking has no member_id — wallet credit not possible")
        staff = staff_audit.identity_from_request(request, req) or "system"

        base_rate = int(float(_mysql_get_setting("base_rate", 10000) or 10000))
        mins = round((amt * 60) / base_rate) if base_rate > 0 else amt

        cur = _mysql_query_one(
            "SELECT balance_mins, total_bought_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        bal_before = int(cur.get("balance_mins") or 0) if cur else 0
        bal_after = bal_before + mins
        bought_before = int(cur.get("total_bought_mins") or 0) if cur else 0

        _mysql_exec(
            "INSERT INTO topup_log (member_id, amount, mins_added, topup_date, staff_name, payment_method, "
            "balance_before, balance_after, balance_mins_before, balance_mins_after, notes) "
            "VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s)",
            (member_id, amt, mins, staff, f"Deposit credit BK#{booking_id}",
             amt, amt, bal_before, bal_after,
             f"Deposit converted to wallet credit (booking #{booking_id})"))
        _mysql_exec(
            "INSERT INTO member_wallets (member_id, balance_mins, total_bought_mins) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE balance_mins=%s, total_bought_mins=%s, last_updated=NOW()",
            (member_id, bal_after, bought_before + mins, bal_after, bought_before + mins))
        _reverse_outstanding_forfeits(booking_id)
        _mysql_exec(
            "UPDATE console_bookings SET deposit_status='redeemed', deposit_redeemed_at=NOW(), "
            "notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
            (f" | Wallet credit {mins} mins ({amt:,} Ks) by {staff}", booking_id))
        _log_booking_event(booking_id, "deposit_change", "deposit_status", ds, "redeemed",
                           staff, "api", f"wallet credit {mins} mins for {amt} Ks (member {member_id})")
        return ok({"booking_id": booking_id, "deposit_status": "redeemed", "member_id": member_id,
                   "minutes_credited": mins, "balance_mins": bal_after, "amount": amt})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"deposit/wallet-credit error BK#{booking_id}: {e}")
        return error_response(message=str(e))


@router.get("/deposits/pending", response_model=GenericResponse, tags=["Deposits"], summary="List bookings with pending deposits (paid, unverified) [MySQL]")
async def api_pending_deposits(auth=Depends(verify_api_key)):
    """List all bookings where deposit_status='paid' (awaiting staff verification).
    Includes customer info."""
    try:
        rows = _mysql_query(
            "SELECT id, console_id, member_id, booking_date, "
            "       planned_start AS start_time, planned_duration_mins AS duration_mins, "
            "       status, staff_name, phone, telegram_chat_id, game_name, notes, "
            "       deposit_amount, deposit_status, deposit_method, deposit_ref, "
            "       deposit_ref_type, created_at "
            "FROM console_bookings "
            "WHERE deposit_status='paid' "
            "AND status NOT IN ('cancelled','rejected','no_show') "  # 2026-09-30 depfix: no uncollected leftovers
            "ORDER BY created_at DESC"
        )
        
        result = []
        for r in rows:
            bk_date = str(r.get("booking_date", ""))[:10] if r.get("booking_date") else ""
            start_time = r.get("start_time")
            time_str = ""
            if start_time:
                try:
                    from datetime import datetime as _pdt3
                    st = _pdt3.fromisoformat(str(start_time)) if isinstance(start_time, str) else start_time
                    time_str = st.strftime("%H:%M")
                except Exception:
                    time_str = str(start_time)[:5] if start_time else ""
            member = r.get("member_id", "") or ""
            phone = r.get("phone", "") or ""
            customer_name = member if member else (phone if phone else "Guest")
            
            result.append({
                "id": r["id"],
                "customer_name": customer_name,
                "phone": phone,
                "member_id": member,
                "console_id": r.get("console_id", ""),
                "date": bk_date,
                "time": time_str,
                "duration_mins": r.get("duration_mins", 60),
                "game_name": r.get("game_name", ""),
                "staff_name": r.get("staff_name", ""),
                "notes": r.get("notes", ""),
                "telegram_chat_id": r.get("telegram_chat_id", ""),
                "deposit_amount": int(r.get("deposit_amount") or 0),
                "deposit_status": r["deposit_status"],
                "deposit_method": r.get("deposit_method", ""),
                "deposit_ref": r.get("deposit_ref", ""),
                "deposit_ref_type": r.get("deposit_ref_type", "text"),
                "created_at": str(r.get("created_at", "")) if r.get("created_at") else ""
            })
        
        return ok({
            "bookings": result,
            "total": len(result)
        })
    except Exception as e:
        logger.error(f"pending-deposits error: {e}")
        return error_response(message=str(e))


@router.post("/bookings/{booking_id}/deposit/pre-redeem", response_model=GenericResponse, tags=["Deposits"], summary="Pre-redeem deposit before receipt (bot-side deduction) [MySQL]")
async def api_deposit_pre_redeem(booking_id: int, auth=Depends(verify_api_key)):
    """Return deposit amount for receipt display WITHOUT changing deposit_status.
    The /api/sales/record endpoint handles the actual deduction & status change.
    Called by the sales bot during step_sale_confirm so the receipt shows correct net_total.
    """
    try:
        row = _mysql_query_one(
            "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
            (booking_id,)
        )
        if not row:
            return error_response(message=f"Booking #{booking_id} not found")
        
        deposit_status = row.get("deposit_status", "none")
        deposit_amount = int(row["deposit_amount"] or 0)
        if deposit_status != "verified" or deposit_amount <= 0:
            return ok({"deposit_amount": 0, "deposit_status": deposit_status, "pre_redeemed": False, "message": "No verified deposit"})

        logger.info(
            "Pre-redeem check: BK#%s deposit=%s Ks (status stays verified for sales/record)",
            booking_id, deposit_amount
        )
        return ok({
            "deposit_amount": deposit_amount,
            "deposit_method": row.get("deposit_method", "kpay"),
            "deposit_status": "verified",
            "pre_redeemed": True
        })
    except Exception as e:
        logger.error(f"deposit/pre-redeem error: {e}")
        return error_response(message=str(e))

