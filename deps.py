"""PS VIBE API — Shared FastAPI dependencies (no circular import from app)."""
import logging
import os as _os

from fastapi import HTTPException, Query, Request
from config import API_KEY

logger = logging.getLogger("psvibe_api.deps")


async def verify_api_key(
    api_key: str = Query(None, alias="api_key"),
    request: Request = None,
):
    """Verify API key OR JWT Bearer token.
    
    ⚠️ Query-string API key is DEPRECATED. Use X-API-Key header or JWT Bearer token.
    """
    if API_KEY:
        # ⚠️ DEPRECATED: query-string API key — log warning and prefer header
        if api_key and request and request.headers.get("X-API-Key"):
            logger.warning("DEPRECATED: API key passed via query string from %s — use X-API-Key header instead",
                           request.client.host if request.client else "unknown")
        
        # Try API key first
        key = api_key
        if not key and request:
            key = request.headers.get("X-API-Key")
        if key and key == API_KEY:
            return api_key
        # Try JWT Bearer token
        if request:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                from auth import decode_token
                payload = decode_token(auth[7:])
                if payload and payload.get("sub"):
                    return api_key
        raise HTTPException(status_code=401, detail="Invalid or missing authentication")
    return api_key


async def verify_webhook_secret(request: Request):
    """Verify webhook requests using a shared secret."""
    expected = _os.environ.get("WEBHOOK_SECRET", "")
    if not expected:
        return True
    provided = request.headers.get("x-webhook-secret", "")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    return True
