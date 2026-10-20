"""PS VIBE API — Telegram Mini App initData validation.

Validates Telegram Mini App initData (HMAC-SHA256 with the staff bot token),
checks the caller is an allowed staff Telegram ID, and returns a short-lived JWT.

This lets a future Mini App frontend exchange initData for a JWT to call the API.
X-API-Key must NEVER go to the frontend.
"""
import hmac
import hashlib
import json
import time
import os
from datetime import timedelta
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse

from helpers import _mysql_get_setting
from auth import create_access_token

logger = __import__("logging").getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram-webapp"])

_INITDATA_MAX_AGE = 86400  # 24h freshness window (anti-replay)
_JWT_TTL_HOURS = 6


def _load_allowed_staff_ids() -> set[int]:
    val = _mysql_get_setting("allowed_staff_ids", "")
    if isinstance(val, str) and val.strip():
        return {int(x.strip()) for x in val.split(",") if x.strip().isdigit()}
    return set()


def _load_admin_user_ids() -> set[int]:
    """Telegram user IDs that map to the 'admin' role in the Mini App JWT.

    Staff get role 'staff' (4 shop accounts in transfer); admins (owner/partners)
    get role 'admin' (all 6 accounts, incl. Main (ACM) + KBZ Bank). Canonical source
    is the ADMIN_USER_IDS env var (comma-separated), same as the sales bot uses.
    """
    val = os.environ.get("ADMIN_USER_IDS", "")
    if val.strip():
        return {int(x.strip()) for x in val.split(",") if x.strip().isdigit()}
    return set()


def _validate_init_data(init_data: str, bot_token: str) -> dict:
    """Validate Telegram initData HMAC and freshness. Returns parsed dict or raises HTTPException."""
    if not init_data:
        raise HTTPException(status_code=403, detail="invalid init data")

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=403, detail="invalid init data")

    provided_hash = parsed.pop("hash", "")
    if not provided_hash:
        raise HTTPException(status_code=403, detail="invalid init data")

    # data_check_string = sorted key=value (all keys EXCEPT hash), joined by "\n"
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash.lower(), provided_hash.lower()):
        raise HTTPException(status_code=403, detail="invalid init data")

    # Freshness check (anti-replay)
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except (ValueError, TypeError):
        auth_date = 0
    if not auth_date or (time.time() - auth_date) > _INITDATA_MAX_AGE:
        raise HTTPException(status_code=403, detail="init data expired")

    return parsed


@router.post("/validate", summary="Validate Telegram Mini App initData and issue JWT [HMAC-SHA256]")
async def telegram_validate(body: dict = Body(...)):
    """Exchange Telegram Mini App initData for a short-lived staff JWT.

    Body: {"init_data": "<raw Telegram web_app_init_data query string>"}
    """
    bot_token = os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(status_code=500, detail="bot token not configured")

    init_data = body.get("init_data", "") if isinstance(body, dict) else ""
    parsed = _validate_init_data(init_data, bot_token)

    # Extract user info
    user_raw = parsed.get("user", "")
    try:
        user = json.loads(user_raw) if user_raw else {}
    except Exception:
        user = {}

    try:
        user_id = int(user.get("id"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=403, detail="not authorized staff")

    allowed = _load_allowed_staff_ids()
    if user_id not in allowed:
        raise HTTPException(status_code=403, detail="not authorized staff")

    admin_ids = _load_admin_user_ids()
    role = "admin" if user_id in admin_ids else "staff"
    username = user.get("username") or user.get("first_name") or ""
    token = create_access_token(
        {"sub": str(user_id), "scope": "staff-webapp", "role": role},
        expires_delta=timedelta(hours=_JWT_TTL_HOURS),
    )

    return JSONResponse(
        {
            "token": token,
            "user_id": user_id,
            "username": username,
            "role": role,
            "expires_in": _JWT_TTL_HOURS * 3600,
        }
    )
