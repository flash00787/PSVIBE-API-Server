"""PS VIBE API — Staff App (standalone Android/WebView) routes.

Adds:
  POST   /api/staff/devices          — register/refresh a staff device (Bearer JWT; staff or dashboard user)
  GET    /api/staff/devices          — list registered devices (admin only)
  DELETE /api/staff/devices/{id}     — remove a registered device (admin only)
  GET    /api/staff/app/version      — PUBLIC — APK version manifest (served from staff-app-dist/version.json)
  POST   /api/staff/push/test        — send a test FCM push to all registered devices (admin only)
  POST   /api/staff/push/internal    — service-to-service push for the sales bot (X-Internal-Key)

The staff_devices table is created idempotently on first use.
Static APK + download page are mounted separately in app.py at /staff-app/.
"""
import json
import logging
import os
import secrets

from fastapi import APIRouter, Depends, Body, Header, HTTPException

from auth import get_staff_or_dashboard_user, require_admin
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute

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
    if not fcm_token:
        return {"success": False, "error": "fcm_token is required"}
    platform = (str(body.get("platform") or "").strip() or None)
    app_version = (str(body.get("app_version") or "").strip() or None)
    username = str(user.get("username") or "").strip() or "unknown"
    try:
        _mysql_execute(
            """
            INSERT INTO staff_devices (username, fcm_token, platform, app_version, last_seen, created_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                username = VALUES(username),
                platform = VALUES(platform),
                app_version = VALUES(app_version),
                last_seen = NOW()
            """,
            (username, fcm_token, platform, app_version),
        )
        row = _mysql_query_one("SELECT id FROM staff_devices WHERE fcm_token = %s", (fcm_token,))
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
async def app_version():
    """PUBLIC — current APK version manifest. Reads staff-app-dist/version.json."""
    try:
        if os.path.isfile(_VERSION_FILE):
            with open(_VERSION_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception as e:
        logger.warning(f"version.json read failed: {e}")
    return _DEFAULT_VERSION


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
