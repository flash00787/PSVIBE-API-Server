"""PS VIBE API — direct APNs push (native iOS).

Why this exists (2026-11-08): the Staff App is a SwiftUI shell that never loads the
Capacitor webview, so the JS push-register path can never obtain an FCM token. The
app now sends its raw APNs device token to /api/staff/devices, and this module
delivers alerts straight to Apple (HTTP/2 + JWT ES256), so FCM / the Capacitor
plugins are not involved at all.

Key:  /root/secrets/apns/AuthKey_<KEYID>.p8   (APNs Auth Key, .p8)
Env:  APNS_KEY_ID, APNS_TEAM_ID, APNS_TOPIC (defaults below)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)

_KEY_PATH = os.environ.get("APNS_KEY_PATH", "/root/secrets/apns/AuthKey_A9GXTLF3LK.p8")
_KEY_ID = os.environ.get("APNS_KEY_ID", "A9GXTLF3LK")
_TEAM_ID = os.environ.get("APNS_TEAM_ID", "98HVDSZD88")
_TOPIC = os.environ.get("APNS_TOPIC", "com.psvibe.staff")
_PROD = "https://api.push.apple.com"
_DEV = "https://api.sandbox.push.apple.com"

_lock = threading.Lock()
_token_cache: Dict[str, Any] = {"jwt": None, "iat": 0}


def _read_key() -> Optional[str]:
    try:
        with open(_KEY_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("APNs key not readable (%s): %s", _KEY_PATH, exc)
        return None


def _jwt() -> Optional[str]:
    """Signed provider token, refreshed every ~40 minutes (Apple allows 60)."""
    with _lock:
        now = int(time.time())
        if _token_cache["jwt"] and now - _token_cache["iat"] < 40 * 60:
            return _token_cache["jwt"]
        pem = _read_key()
        if not pem:
            return None
        try:
            import jwt as _pyjwt
            token = _pyjwt.encode(
                {"iss": _TEAM_ID, "iat": now},
                pem,
                algorithm="ES256",
                headers={"alg": "ES256", "kid": _KEY_ID},
            )
            if isinstance(token, bytes):
                token = token.decode()
            _token_cache["jwt"] = token
            _token_cache["iat"] = now
            return token
        except Exception as exc:
            logger.warning("APNs JWT signing failed: %s", exc)
            return None


def _send_one(client: httpx.Client, jwt: str, token: str, payload: dict, host: str) -> Dict[str, Any]:
    url = f"{host}/3/device/{token}"
    headers = {
        "authorization": f"bearer {jwt}",
        "apns-topic": _TOPIC,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    try:
        resp = client.post(url, headers=headers, content=json.dumps(payload))
        ok = resp.status_code == 200
        reason = None if ok else (resp.text or "")[:200]
        return {"ok": ok, "status": resp.status_code, "reason": reason, "host": host}
    except Exception as exc:
        return {"ok": False, "status": 0, "reason": str(exc)[:200], "host": host}


def send(title: str, body: str, data: Optional[Dict[str, Any]] = None,
         tokens: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Deliver an alert to every APNs token. Never raises."""
    result: Dict[str, Any] = {"enabled": False, "sent": 0, "failed": 0, "results": []}
    toks: List[str] = [t for t in (tokens or []) if t]
    if not toks:
        return result
    jwt = _jwt()
    if not jwt:
        result["error"] = "apns_disabled"
        return result
    result["enabled"] = True
    payload = {
        "aps": {
            "alert": {"title": title or "", "body": body or ""},
            "sound": "default",
            "badge": 1,
        }
    }
    for k, v in (data or {}).items():
        payload[str(k)] = "" if v is None else str(v)
    try:
        with httpx.Client(http2=True, timeout=20.0) as client:
            for tok in toks:
                out = _send_one(client, jwt, tok, payload, _PROD)
                if not out["ok"] and out.get("reason") and (
                        "BadDeviceToken" in out["reason"] or "DeviceTokenNotForTopic" in out["reason"]):
                    # A development/TestFlight token only works on the sandbox host.
                    alt = _send_one(client, jwt, tok, payload, _DEV)
                    if alt["ok"]:
                        out = alt
                result["results"].append({"token": tok[:10] + "…", **out})
                if out["ok"]:
                    result["sent"] += 1
                else:
                    result["failed"] += 1
                    logger.warning("APNs push failed (%s): %s", out["host"], out["reason"])
    except Exception as exc:
        logger.warning("APNs send crashed: %s", exc)
        result["error"] = str(exc)
    return result
