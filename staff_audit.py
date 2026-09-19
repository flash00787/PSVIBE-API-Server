"""PS VIBE API — Staff identity / audit helpers (Staff App Wave 1).

Many legacy endpoints authenticate with ``verify_api_key`` (which accepts a JWT
Bearer token but does NOT surface the user) or ``get_current_user`` (dashboard
user dict).  For the Staff App we need a *reliable* audit trail on every
state-changing / money-facing action, so this module normalises "who did it"
from either:

  * an explicit request field (staff_name / staffName / staff / verified_by / ...)
  * the ``Authorization: Bearer <JWT>`` header (``name`` claim, else ``sub``)

It never raises — worst case it returns "" so the caller can fall back.
"""
import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("psvibe_api.staff_audit")

_DEFAULT_KEYS = ("staff_name", "staffName", "staff", "verified_by", "recorded_by")


def _decode_bearer(request) -> Dict[str, Any]:
    try:
        auth = (request.headers.get("Authorization", "") if request is not None else "") or ""
        if auth.startswith("Bearer "):
            from auth import decode_token
            return decode_token(auth[7:]) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("staff_audit decode failed: %s", e)
    return {}


def identity_from_request(
    request=None,
    payload: Optional[Dict[str, Any]] = None,
    keys: Iterable[str] = _DEFAULT_KEYS,
) -> str:
    """Return the acting staff name (explicit field > JWT name > JWT sub)."""
    payload = payload or {}
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    data = _decode_bearer(request)
    if data:
        return str(data.get("name") or data.get("sub") or "").strip()
    return ""


def role_from_request(request=None) -> str:
    """Return the JWT role claim ('' when unknown / API-key auth)."""
    return str(_decode_bearer(request).get("role") or "").strip()


_STAFF_ROLES = {"admin", "staff", "manager", "supervisor", "owner"}
_BLOCKED_SCOPES = {"member", "customer", "public"}


def can_view_payment_ref(request=None) -> bool:
    """True when the caller is trusted staff/service and may see payment refs.

    Allowed: service-to-service API key (bots), staff-webapp JWT
    (``scope=staff-webapp``), or a dashboard JWT carrying a known staff role.
    Denied: unknown/ordinary tokens and any customer/member scope — so a
    deposit slip / payment reference never leaks to a non-staff caller.
    """
    if request is None:
        return False
    try:
        from config import API_KEY
        if API_KEY:
            key = request.headers.get("X-API-Key") or ""
            if key and key == API_KEY:
                return True
    except Exception:  # pragma: no cover - defensive
        pass
    data = _decode_bearer(request)
    if not data:
        return False
    scope = str(data.get("scope") or "").strip().lower()
    if scope == "staff-webapp":
        return True
    if scope in _BLOCKED_SCOPES:
        return False
    role = str(data.get("role") or "").strip().lower()
    if role in _STAFF_ROLES:
        return True
    # Legacy dashboard JWT with no scope claim and no role -> treat as dashboard staff.
    if data.get("sub") and not scope and not role:
        return True
    return False
