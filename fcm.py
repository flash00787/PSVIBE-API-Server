"""PS VIBE API — Firebase Cloud Messaging (FCM) for the Staff App.

Lazy-initialises ``firebase_admin`` exactly once from
``firebase-service-account.json`` (same directory as this file, chmod 600).
If the file is missing or init fails, every send becomes a no-op that logs a
warning — **the API must never crash because push is unavailable**.

Public API
----------
send_to_staff(title, body, data=None, usernames=None) -> dict
    Persist a ``staff_notifications`` history row (broadcast -> username NULL,
    targeted -> one row per username) BEFORE sending, then push to all
    registered ``staff_devices`` rows (or only the given usernames). Each push
    carries the history row id in ``data['notification_id']`` so the Staff App
    can deep-link a tapped push into its notification center (the native shell
    calls ``window.PSVOpenNotification(id)`` — see webapp app.js). Prunes invalid
    / unregistered tokens. Never raises.

record_notification(type, title, body, booking_id=None, usernames=None) -> dict
    Standalone history writer for non-push events. Returns {username|None: id}.

push_session_reminder(console_id, minutes_left=None, member_id=None, ...) -> dict
push_booking_reminder(booking_id, when=None, ...) -> dict
    Documented builders for the two reminder payloads. Data payload shape:
    {"type": "session_reminder"|"booking_reminder", "booking_id": ..., "title": ..., "body": ..., ...}

WIRED (2026-09-19, Staff App Stage 4) — push is ADDITIVE next to each Telegram send,
and every call site is wrapped in try/except so a push failure can never break a
reminder or a booking flow:
  1. Session reminders (sales bot) → /opt/psvibe/sales-bot/bot/handlers/session_reminder.py
       * ``_remind_loop`` and ``_send_session_reminder`` call the module-level helper
         ``_push_staff_app("session_reminder", ...)``, which POSTs to the API at
         ``/api/staff/push/internal`` (X-Internal-Key) because the bot has no
         firebase-admin. The API then calls ``push_session_reminder`` below.
  2. Session reminders (API-side timer fallback) → /opt/psvibe/api/session_timer.py
       * ``_session_timer_task`` calls ``fcm.push_session_reminder(...)`` right after
         its ``_send_telegram`` timer alert.
  3. Booking reminders (API-side, restart-proof) → /opt/psvibe/api/booking_reminder.py
       * ``_send_reminder_notification`` calls ``fcm.push_booking_reminder(...)`` next
         to the 30-min-before admin Telegram send.
  NOTE: the bot's ``_sbk_advance_reminder`` fallback (booking.py) is DISABLED (returns
  immediately — n8n/API own booking reminders), so no push hook is needed there.
  NOTE: Android channel id MUST stay ``psvibe_sessions`` — that is the channel the
  APK (assets/psv-native-bridge.js, CHANNEL_ID) creates at high importance. A wrong
  channel id means Android 8+ silently drops the notification.
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

# ── Notification history (Staff App in-app notification center) ─────────────
# staff_notifications mirrors every push (and any other recorded event) so the
# Staff App can show a scrollable history with unread state.
#   username  NULL  -> broadcast (visible to every staff user)
#   username  'x'   -> targeted (visible only to that staff user)
# read_at NULL means unread. See routes/staff_app_routes.py for the read APIs.
_notif_table_ready = False


def ensure_notifications_table() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for staff_notifications."""
    global _notif_table_ready
    if _notif_table_ready:
        return
    try:
        from mysql_db import execute as _e
        _e(
            """
            CREATE TABLE IF NOT EXISTS staff_notifications (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) DEFAULT NULL,
                type VARCHAR(32) NOT NULL DEFAULT 'generic',
                title VARCHAR(255) DEFAULT NULL,
                body TEXT,
                booking_id VARCHAR(64) DEFAULT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TIMESTAMP NULL DEFAULT NULL,
                KEY idx_notif_username (username),
                KEY idx_notif_created (created_at),
                KEY idx_notif_read (read_at)
            )
            """
        )
        _notif_table_ready = True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("staff_notifications table init failed: %s", e)


def record_notification(ntype: str = "generic", title: Optional[str] = None,
                       body: Optional[str] = None, booking_id: Any = None,
                       usernames: Optional[Iterable[Any]] = None) -> Dict[Any, Any]:
    """Write notification history row(s) BEFORE any push is attempted.

    Broadcast (``usernames`` empty/None) -> ONE row with username NULL.
    Targeted -> ONE row per username.
    Returns ``{username_or_None: id}`` (None key = broadcast row). Never raises.
    """
    ensure_notifications_table()
    ids: Dict[Any, Any] = {}
    try:
        from mysql_db import execute as _e
        _type = (str(ntype or "generic").strip() or "generic")[:32]
        _title = None if title is None else str(title)[:255]
        _body = None if body is None else str(body)
        _bid = None if booking_id in (None, "", "None", "null") else str(booking_id)[:64]
        if usernames:
            names: List[str] = []
            for u in usernames:
                u = str(u).strip()
                if u and u not in names:
                    names.append(u)
            for u in names:
                ids[u] = _e(
                    "INSERT INTO staff_notifications (username, type, title, body, booking_id, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 390 MINUTE))",
                    (u, _type, _title, _body, _bid),
                )
        else:
            ids[None] = _e(
                "INSERT INTO staff_notifications (username, type, title, body, booking_id, created_at) "
                "VALUES (NULL, %s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 390 MINUTE))",
                (_type, _title, _body, _bid),
            )
    except Exception as e:
        logger.warning("record_notification failed: %s", e)
    return ids


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
    data = data or {}
    result: Dict[str, Any] = {"enabled": False, "sent": 0, "failed": 0, "pruned": 0, "results": []}

    # ── Persist history FIRST (even if push is disabled/unavailable) ──
    nid_map = record_notification(
        ntype=str(data.get("type") or "generic"),
        title=title, body=body, booking_id=data.get("booking_id"), usernames=usernames,
    )
    result["notification_ids"] = {str(k): v for k, v in nid_map.items()}

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
        norm_data = {k: ("" if v is None else str(v)) for k, v in data.items()}
        # Group tokens by username so each push carries that user's own
        # notification_id for deep-linking (broadcast rows use the None key).
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r.get("username"), []).append(r)
        invalid: List[str] = []
        for uname, grows in groups.items():
            nid = nid_map.get(uname, nid_map.get(None))
            gdata = dict(norm_data)
            if nid is not None:
                gdata["notification_id"] = str(nid)
            msg = messaging.MulticastMessage(
                tokens=[r["fcm_token"] for r in grows],
                notification=messaging.Notification(title=title or "", body=body or ""),
                data=gdata,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(channel_id="psvibe_sessions", default_sound=True),
                ),
            )
            resp = messaging.send_each_for_multicast(msg, app=app)
            for row, sr in zip(grows, resp.responses):
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
    # NOTE: send_to_staff() adds "notification_id" (the staff_notifications row id)
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
    # NOTE: send_to_staff() adds "notification_id" (the staff_notifications row id)
    if when:
        data["when"] = str(when)
    return send_to_staff(t, b, data=data, usernames=usernames)
