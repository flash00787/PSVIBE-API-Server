"""PS VIBE API — Firebase Cloud Messaging (FCM) for the Staff App.

Lazy-initialises ``firebase_admin`` exactly once from
``firebase-service-account.json`` (same directory as this file, chmod 600).
If the file is missing or init fails, every send becomes a no-op that logs a
warning — **the API must never crash because push is unavailable**.

Public API
----------
send_to_staff(title, body, data=None, usernames=None) -> dict
    Send a notification to all registered ``staff_devices`` rows (or only the
    given usernames). Prunes invalid / unregistered tokens. Never raises.

push_session_reminder(console_id, minutes_left=None, member_id=None, ...) -> dict
push_booking_reminder(booking_id, when=None, ...) -> dict
    Documented builders for the two reminder payloads. Data payload shape:
    {"type": "session_reminder"|"booking_reminder", "booking_id": ..., "title": ..., "body": ..., ...}

WIRING TODO (sales-bot is OUT OF SCOPE for the API repo — hook these later):
  1. Session reminders  → /opt/psvibe/sales-bot/bot/handlers/session_reminder.py
       * ``_remind_loop``  — where it builds "⏰ <b>Session Reminder!</b>" (~L332)
         and sends via ``bot.send_message`` (~L359); call
         ``fcm.push_session_reminder(cid, minutes_left=..., member_id=...)``.
       * ``_send_session_reminder`` (~L417) — same, for the one-shot path.
  2. Booking reminders  → /opt/psvibe/sales-bot/bot/handlers/admin_bookings.py
       * after a booking is approved (~L626-641) next to
         ``_post_n8n_booking_reminder`` / ``_sbk_advance_reminder``; call
         ``fcm.push_booking_reminder(bk_id, when=...)``.
  3. Advance reminder task → /opt/psvibe/sales-bot/bot/handlers/booking.py
       * ``_sbk_advance_reminder`` (the 30-min-before fallback).
  4. API-side DB timer sync → /opt/psvibe/api/routes/booking_routes.py and
       the ``bookings/mark-bot-reminder`` / ``webhook/booking-reminder`` paths.
"""
import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_SERVICE_ACCOUNT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase-service-account.json")

_lock = threading.Lock()
_app = None
_init_attempted = False

# FCM error substrings that mean "this token is dead" → prune it.
_INVALID_TOKEN_HINTS = (
    "not a valid fcm registration token",
    "requested entity was not found",
    "registration token is not valid",
    "unregistered",
)


def _get_app():
    """Initialise firebase_admin once. Returns the App, or None if disabled."""
    global _app, _init_attempted
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        if _init_attempted:
            return None
        _init_attempted = True
        try:
            import firebase_admin
            from firebase_admin import credentials
            if not os.path.isfile(_SERVICE_ACCOUNT):
                logger.warning("FCM disabled: service-account file not found at %s", _SERVICE_ACCOUNT)
                return None
            cred = credentials.Certificate(_SERVICE_ACCOUNT)
            _app = firebase_admin.initialize_app(cred)
            logger.info("FCM initialised (project=%s)", getattr(_app, "project_id", "?"))
            return _app
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("FCM init failed (push disabled): %s", e)
            return None


def _load_tokens(usernames: Optional[Iterable[Any]] = None) -> List[Dict[str, Any]]:
    """Return [{id, username, fcm_token}] from staff_devices (active, non-empty)."""
    try:
        from mysql_db import query as _q
        if usernames:
            names = [str(u) for u in usernames]
            ph = ",".join(["%s"] * len(names))
            rows = _q(
                "SELECT id, username, fcm_token FROM staff_devices "
                f"WHERE fcm_token IS NOT NULL AND fcm_token <> '' AND username IN ({ph})",
                tuple(names),
            )
        else:
            rows = _q(
                "SELECT id, username, fcm_token FROM staff_devices "
                "WHERE fcm_token IS NOT NULL AND fcm_token <> ''"
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("FCM token load failed: %s", e)
        return []


def _prune_tokens(tokens: List[str]) -> int:
    if not tokens:
        return 0
    try:
        from mysql_db import execute as _e
        removed = 0
        for t in tokens:
            _e("DELETE FROM staff_devices WHERE fcm_token = %s", (t,))
            removed += 1
        logger.info("FCM pruned %d invalid token(s)", removed)
        return removed
    except Exception as e:
        logger.warning("FCM prune failed: %s", e)
        return 0


def send_to_staff(title: str, body: str, data: Optional[Dict[str, Any]] = None,
                  usernames: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Send a push to all registered staff devices (or the given usernames).

    Returns ``{"enabled": bool, "sent": n, "failed": n, "pruned": n,
    "results": [{id, username, ok, error}]}``. Never raises.
    """
    result: Dict[str, Any] = {"enabled": False, "sent": 0, "failed": 0, "pruned": 0, "results": []}
    app = _get_app()
    if app is None:
        result["error"] = "fcm_disabled"
        return result

    rows = _load_tokens(usernames)
    result["enabled"] = True
    if not rows:
        return result

    try:
        from firebase_admin import messaging
        tokens = [r["fcm_token"] for r in rows]
        norm_data = {k: ("" if v is None else str(v)) for k, v in (data or {}).items()}
        msg = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title or "", body=body or ""),
            data=norm_data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="psvibe_staff", default_sound=True),
            ),
        )
        resp = messaging.send_each_for_multicast(msg, app=app)
        invalid: List[str] = []
        for row, sr in zip(rows, resp.responses):
            ok = bool(getattr(sr, "success", False))
            err = None if ok else str(getattr(sr, "exception", "unknown error"))
            if ok:
                result["sent"] += 1
            else:
                result["failed"] += 1
                low = err.lower()
                if isinstance(getattr(sr, "exception", None), messaging.UnregisteredError) or \
                        any(h in low for h in _INVALID_TOKEN_HINTS):
                    if row.get("fcm_token"):
                        invalid.append(row["fcm_token"])
            result["results"].append({
                "id": row.get("id"),
                "username": row.get("username"),
                "ok": ok,
                "error": err,
            })
        if invalid:
            result["pruned"] = _prune_tokens(invalid)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("FCM send failed: %s", e)
        result["error"] = str(e)
    return result


# ── Reminder payload builders (see WIRING TODO above) ───────────────────────
def push_session_reminder(console_id: Any = None, minutes_left: Any = None,
                          member_id: Any = None, title: Optional[str] = None,
                          body: Optional[str] = None, usernames: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Staff push for an ending game session.

    data = {type:"session_reminder", booking_id:"", console_id, minutes_left, member_id, title, body}
    Hook in sales-bot/bot/handlers/session_reminder.py (see module docstring).
    """
    t = title or "Session ending soon"
    b = body or (f"Console {console_id}: session ending" + (f" in {minutes_left} min" if minutes_left is not None else ""))
    data = {
        "type": "session_reminder",
        "booking_id": "",
        "console_id": "" if console_id is None else str(console_id),
        "title": t,
        "body": b,
    }
    if minutes_left is not None:
        data["minutes_left"] = str(minutes_left)
    if member_id is not None:
        data["member_id"] = str(member_id)
    return send_to_staff(t, b, data=data, usernames=usernames)


def push_booking_reminder(booking_id: Any = None, when: Any = None,
                          title: Optional[str] = None, body: Optional[str] = None,
                          usernames: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Staff push for an upcoming booking.

    data = {type:"booking_reminder", booking_id, console_id:"", when, title, body}
    Hook in sales-bot/bot/handlers/admin_bookings.py / booking.py (see module docstring).
    """
    t = title or "Upcoming booking"
    b = body or (f"Booking {booking_id} coming up" + (f" at {when}" if when else ""))
    data = {
        "type": "booking_reminder",
        "booking_id": "" if booking_id is None else str(booking_id),
        "console_id": "",
        "title": t,
        "body": b,
    }
    if when:
        data["when"] = str(when)
    return send_to_staff(t, b, data=data, usernames=usernames)
