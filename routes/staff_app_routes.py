"""PS VIBE API — Staff App (standalone Android/WebView) routes.

Adds:
  POST   /api/staff/devices          — register/refresh a staff device (Bearer JWT; staff or dashboard user)
  GET    /api/staff/devices          — list registered devices (admin only)
  DELETE /api/staff/devices/{id}     — remove a registered device (admin only)
  GET    /api/staff/app/version      — PUBLIC — APK version manifest (served from staff-app-dist/version.json)
  POST   /api/staff/push/test        — send a test FCM push to all registered devices (admin only)
  POST   /api/staff/push/internal    — service-to-service push for the sales bot (X-Internal-Key)
  GET    /api/staff/notifications             — in-app history for the calling staff user (+ broadcast)
  POST   /api/staff/notifications/{id}/read   — mark one notification read
  POST   /api/staff/notifications/read-all    — mark all of the user's notifications read
  GET    /api/staff/notifications/unread-count — unread badge counter

The staff_notifications table is created idempotently by fcm.ensure_notifications_table().

The staff_devices table is created idempotently on first use.
Static APK + download page are mounted separately in app.py at /staff-app/.
"""
import json
import logging
import os
import secrets
import hashlib

from fastapi import APIRouter, Depends, Body, Header, HTTPException, Request, Response

from auth import get_staff_or_dashboard_user, require_admin
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_rowcount

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/staff", tags=["staff-app"])

# Directory that hosts the APK + download page + version.json (mounted at /staff-app/).
_STAFF_APP_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "staff-app-dist")
_VERSION_FILE = os.path.join(_STAFF_APP_DIST, "version.json")

_DEFAULT_VERSION = {
    "version": "1.0.0",
    "version_code": 1,
    "apk_url": "https://ps-vibe.com/staff-app/psvibe-staff.apk",
    "notes": "PS VIBE Staff App",
}

_table_ready = False


def _ensure_table() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for staff_devices."""
    global _table_ready
    if _table_ready:
        return
    try:
        _mysql_execute(
            """
            CREATE TABLE IF NOT EXISTS staff_devices (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) NOT NULL,
                fcm_token VARCHAR(255) NOT NULL,
                platform VARCHAR(32) DEFAULT NULL,
                app_version VARCHAR(32) DEFAULT NULL,
                last_seen TIMESTAMP NULL DEFAULT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_fcm_token (fcm_token),
                KEY idx_username (username)
            )
            """
        )
        _table_ready = True
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"staff_devices table init failed: {e}")


@router.post("/devices")
async def register_device(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Upsert a staff device by fcm_token (Bearer JWT required — staff or dashboard user)."""
    _ensure_table()
    fcm_token = str(body.get("fcm_token") or "").strip()
    # Owner 2026-11-08: the native SwiftUI shell cannot run the Capacitor JS bridge,
    # so iOS registers its raw APNs token here and the API pushes via APNs directly.
    apns_token = str(body.get("apns_token") or "").strip().lower()
    # 2026-10-25 diagnostics: iOS push registration debugging — log every attempt
    # (platform + token length + user) so an empty token is visible in the log.
    try:
        logger.info(
            "POST /staff/devices: user=%s platform=%s app_version=%s fcm_len=%d apns_len=%d keys=%s",
            (user.get("username") if isinstance(user, dict) else "?"),
            body.get("platform"), body.get("app_version"), len(fcm_token), len(apns_token),
            sorted(body.keys()),
        )
    except Exception:
        pass
    if not fcm_token and not apns_token:
        return {"success": False, "error": "fcm_token or apns_token is required"}
    # Keep the fcm_token unique index happy for APNs-only (native iOS) devices.
    if not fcm_token:
        fcm_token = "apns:" + apns_token[:60]
    platform = (str(body.get("platform") or "").strip() or None)
    app_version = (str(body.get("app_version") or "").strip() or None)
    username = str(user.get("username") or "").strip() or "unknown"
    try:
        _mysql_execute(
            """
            INSERT INTO staff_devices (username, fcm_token, apns_token, platform, app_version, last_seen, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                username = VALUES(username),
                apns_token = COALESCE(NULLIF(VALUES(apns_token), ''), apns_token),
                platform = VALUES(platform),
                app_version = VALUES(app_version),
                last_seen = NOW()
            """,
            (username, fcm_token, apns_token or None, platform, app_version),
        )
        row = _mysql_query_one("SELECT id FROM staff_devices WHERE fcm_token = %s", (fcm_token,))
        # Owner 2026-11-08: prove the token works immediately — send a welcome push
        # through APNs right here and log the result (200 = Apple accepted it).
        if apns_token:
            try:
                import apns_push as _apns
                _r = _apns.send("PS VIBE", "Push connected ✓ — notifications are live",
                                {"type": "registration"}, [apns_token])
                logger.info("APNs registration test: sent=%s failed=%s results=%s",
                            _r.get("sent"), _r.get("failed"), _r.get("results"))
            except Exception as _ape:
                logger.warning("APNs registration test failed: %s", _ape)
        return {"success": True, "data": {"id": row["id"] if row else None, "username": username}}
    except Exception as e:
        logger.error(f"POST /staff/devices error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/devices")
async def list_devices(user: dict = Depends(require_admin)):
    """List registered staff devices (admin only)."""
    _ensure_table()
    try:
        rows = _mysql_query(
            "SELECT id, username, fcm_token, platform, app_version, last_seen, created_at "
            "FROM staff_devices ORDER BY last_seen DESC, id DESC LIMIT 500"
        )
        return {"success": True, "data": list(rows)}
    except Exception as e:
        logger.error(f"GET /staff/devices error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/devices/{device_id}")
async def delete_device(device_id: int, user: dict = Depends(require_admin)):
    """Delete a registered staff device (admin only)."""
    _ensure_table()
    try:
        _mysql_execute("DELETE FROM staff_devices WHERE id = %s", (device_id,))
        return {"success": True, "data": {"id": device_id, "deleted": True}}
    except Exception as e:
        logger.error(f"DELETE /staff/devices/{device_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/app/version")
async def app_version(request: Request, response: Response):
    """PUBLIC — current APK version manifest. Reads staff-app-dist/version.json.

    Rarely-changing and polled by every app launch, so it is safely cacheable at the
    edge/browser: ``Cache-Control: public, max-age=60`` + a strong ``ETag`` (304 on
    match).  This also cuts the request volume Cloudflare sees from the app.
    """
    payload = _DEFAULT_VERSION
    try:
        if os.path.isfile(_VERSION_FILE):
            with open(_VERSION_FILE, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
    except Exception as e:
        logger.warning(f"version.json read failed: {e}")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["ETag"] = etag
    return payload


@router.post("/push/internal")
async def push_internal(body: dict = Body(...), x_internal_key: str = Header(None, alias="X-Internal-Key")):
    """Service-to-service push (sales bot → API).

    Auth: shared secret in the ``X-Internal-Key`` header, read from
    ``PSVIBE_INTERNAL_PUSH_KEY`` in /etc/psvibe/secrets.env.
    Body: {type, title, body, booking_id?, usernames?, console_id?, minutes_left?, member_id?, when?}
    """
    expected = (os.environ.get("PSVIBE_INTERNAL_PUSH_KEY") or "").strip()
    provided = (x_internal_key or "").strip()
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    ptype = str(body.get("type") or "generic")
    title = body.get("title")
    text = body.get("body")
    usernames = body.get("usernames")
    try:
        import fcm
        if ptype == "session_reminder":
            result = fcm.push_session_reminder(
                console_id=body.get("console_id"),
                minutes_left=body.get("minutes_left"),
                member_id=body.get("member_id"),
                title=title, body=text, usernames=usernames,
            )
        elif ptype == "booking_reminder":
            result = fcm.push_booking_reminder(
                booking_id=body.get("booking_id"), when=body.get("when"),
                title=title, body=text, usernames=usernames,
            )
        else:
            data = {"type": ptype, "title": str(title or ""), "body": str(text or "")}
            if body.get("booking_id") is not None:
                data["booking_id"] = str(body.get("booking_id"))
            result = fcm.send_to_staff(title or "PS VIBE", text or "", data=data, usernames=usernames)
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"POST /staff/push/internal error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/push/test")
async def push_test(body: dict = Body(...), user: dict = Depends(require_admin)):
    """Send a test FCM push to ALL registered staff devices (admin only).

    Body: {"title": "...", "body": "..."}. Returns per-token send results.
    This is the end-to-end verification path for the Staff App push pipeline.
    """
    title = str(body.get("title") or "PS VIBE test push")
    text = str(body.get("body") or "FCM test from the PS VIBE staff API")
    try:
        import fcm
        result = fcm.send_to_staff(title, text, data={"type": "test", "title": title, "body": text})
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"POST /staff/push/test error: {e}")
        return {"success": False, "error": str(e)}


# ── In-app notification center (history) ────────────────────────────────────
# staff_notifications: username NULL = broadcast (all staff), else targeted.
# A staff user only ever sees their own rows + broadcast rows.

def _notif_username(user: dict) -> str:
    return str((user or {}).get("username") or "").strip()


def _notif_init() -> None:
    try:
        import fcm
        fcm.ensure_notifications_table()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"notifications table init skipped: {e}")


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


@router.get("/notifications")
async def list_notifications(response: Response, limit: int = 50, unread_only: int = 0,
                             user: dict = Depends(get_staff_or_dashboard_user)):
    """In-app notification history for the calling staff user (+ broadcast rows).

    Query: ?limit=50&unread_only=0
    Returns: {items:[{id,type,title,body,booking_id,created_at,read_at}], unread:<int>}
    """
    _notif_init()
    response.headers["Cache-Control"] = "no-store"
    username = _notif_username(user)
    try:
        lim = int(limit or 50)
    except Exception:
        lim = 50
    lim = max(1, min(lim, 200))
    try:
        unread_only = int(unread_only or 0)
    except Exception:
        unread_only = 0
    try:
        where = "(username = %s OR username IS NULL)"
        args = [username]
        if unread_only:
            where += " AND read_at IS NULL"
        rows = _mysql_query(
            "SELECT id, type, title, body, booking_id, created_at, read_at FROM staff_notifications "
            f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %s",
            tuple(args + [lim]),
        )
        cnt = _mysql_query_one(
            "SELECT COUNT(*) AS c FROM staff_notifications "
            "WHERE (username = %s OR username IS NULL) AND read_at IS NULL",
            (username,),
        )
        items = [{
            "id": r.get("id"),
            "type": r.get("type"),
            "title": r.get("title"),
            "body": r.get("body"),
            "booking_id": r.get("booking_id"),
            "created_at": _iso(r.get("created_at")),
            "read_at": _iso(r.get("read_at")),
        } for r in rows]
        return {"items": items, "unread": int((cnt or {}).get("c") or 0)}
    except Exception as e:
        logger.error(f"GET /staff/notifications error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/notifications/read-all")
async def read_all_notifications(user: dict = Depends(get_staff_or_dashboard_user)):
    """Mark every notification visible to the caller as read."""
    _notif_init()
    username = _notif_username(user)
    try:
        updated = _mysql_rowcount(
            "UPDATE staff_notifications SET read_at = DATE_ADD(NOW(), INTERVAL 390 MINUTE) "
            "WHERE (username = %s OR username IS NULL) AND read_at IS NULL",
            (username,),
        )
        return {"success": True, "data": {"updated": int(updated or 0)}}
    except Exception as e:
        logger.error(f"POST /staff/notifications/read-all error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/notifications/unread-count")
async def notifications_unread_count(response: Response, user: dict = Depends(get_staff_or_dashboard_user)):
    """Unread badge counter for the calling staff user. Returns {unread:<int>}."""
    _notif_init()
    response.headers["Cache-Control"] = "no-store"
    username = _notif_username(user)
    try:
        cnt = _mysql_query_one(
            "SELECT COUNT(*) AS c FROM staff_notifications "
            "WHERE (username = %s OR username IS NULL) AND read_at IS NULL",
            (username,),
        )
        return {"unread": int((cnt or {}).get("c") or 0)}
    except Exception as e:
        logger.error(f"GET /staff/notifications/unread-count error: {e}")
        return {"unread": 0}


@router.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: int, user: dict = Depends(get_staff_or_dashboard_user)):
    """Mark one notification read (must belong to the caller or be a broadcast row)."""
    _notif_init()
    username = _notif_username(user)
    try:
        _mysql_rowcount(
            "UPDATE staff_notifications SET read_at = DATE_ADD(NOW(), INTERVAL 390 MINUTE) "
            "WHERE id = %s AND (username = %s OR username IS NULL) AND read_at IS NULL",
            (notification_id, username),
        )
        return {"success": True, "data": {"id": notification_id, "read": True}}
    except Exception as e:
        logger.error(f"POST /staff/notifications/{notification_id}/read error: {e}")
        return {"success": False, "error": str(e)}
