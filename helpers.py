"""PS VIBE API — Shared helpers (no circular import from app).

Import these from route modules instead of importing from app.py.
"""
import json as _json
import os as _os
import time
import logging as _logging
from collections import defaultdict

from fastapi import HTTPException, Request
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete_rows

_logger = _logging.getLogger("psvibe_api")

# ── Rate Limiter (in-memory) ──
_rate_limit_cache = defaultdict(list)

class RateLimiter:
    """Simple in-memory rate limiter for FastAPI endpoints."""
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def __call__(self, request: Request):
        key = request.client.host if request.client else "unknown"
        now = time.time()
        _rate_limit_cache[key] = [t for t in _rate_limit_cache[key] if t > now - self.window_seconds]
        if len(_rate_limit_cache[key]) >= self.max_requests:
            raise HTTPException(status_code=429, detail="Too many requests")
        _rate_limit_cache[key].append(now)
        return True

rate_limit_10pm = RateLimiter(max_requests=10, window_seconds=60)
rate_limit_5pm = RateLimiter(max_requests=5, window_seconds=60)
rate_limit_30pm = RateLimiter(max_requests=30, window_seconds=60)
# Staff App Wave 4: reads must never trip the limiter during normal app usage.
# NOTE: behind the Cloudflare tunnel every request arrives from 127.0.0.1, so this
# in-memory key is effectively GLOBAL.  Keep read limits generous; keep writes strict.
rate_limit_120pm = RateLimiter(max_requests=120, window_seconds=60)  # generous reads: 120/min

# ── Response helpers ──
def ok(data=None, message=""):
    d = {"success": True, "data": data}
    if message:
        d["message"] = message
    return d

def error_response(message="Internal error", code=500, detail=None):
    resp = {"success": False, "error": message, "code": code}
    if detail:
        resp["detail"] = detail
    return resp

def success_response(data=None, message=None):
    return ok(data=data, message=message or "")

def api_error(status_code: int, message: str, detail=None):
    content = error_response(message=message, code=status_code, detail=detail)
    raise HTTPException(status_code=status_code, detail=content)

# ── MySQL settings helpers ──
def _mysql_get_setting(key, default=None):
    try:
        rows = _mysql_query("SELECT config_value, config_type FROM settings_config WHERE config_key=%s", (key,))
    except Exception:
        return default
    if not rows:
        return default
    val, typ = rows[0]["config_value"], rows[0].get("config_type", "string")
    if typ == "int": return int(val) if val else 0
    if typ == "float": return float(val) if val else 0.0
    if typ == "bool": return str(val).lower() in ("true", "1", "yes")
    if typ == "json": return _json.loads(val) if val else {}
    return str(val) if val else default

def _mysql_get_settings_dict(prefix=None):
    try:
        if prefix:
            rows = _mysql_query("SELECT config_key, config_value, config_type FROM settings_config WHERE config_key LIKE %s", (f"{prefix}%",))
        else:
            rows = _mysql_query("SELECT config_key, config_value, config_type FROM settings_config")
    except Exception:
        return {}
    result = {}
    for r in rows:
        k, val, typ = r["config_key"], r["config_value"], r.get("config_type", "string")
        if typ == "int": result[k] = int(val) if val else 0
        elif typ == "float": result[k] = float(val) if val else 0.0
        elif typ == "json": result[k] = _json.loads(val) if val else {}
        elif typ == "bool": result[k] = str(val).lower() in ("true", "1", "yes")
        else: result[k] = str(val) if val else ""
    return result


# ── Payment method parser (used by topup, register, etc.) ──
def insert_cash_movement_if_absent(movement_type: str, account: str, amount, note_pattern: str, note: str, staff_name: str = "system", category: str = None) -> bool:
    """Insert a cash_movement atomically only if no matching row exists.

    Uses INSERT ... SELECT ... WHERE NOT EXISTS so concurrent duplicate calls
    cannot double-insert (idempotent guard for forfeit inject / refund eject).

    Returns True if a row was inserted, False if an equivalent row already existed.
    """
    try:
        # note_pattern is matched with LIKE (already includes trailing % by convention).
        affected = _mysql_delete_rows(
            "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, category, created_at) "
            "SELECT %s, %s, %s, %s, %s, %s, NOW() "
            "WHERE NOT EXISTS (SELECT 1 FROM cash_movements WHERE movement_type=%s AND note LIKE %s)",
            (movement_type, account, amount, note, staff_name, category, movement_type, note_pattern),
        )
        inserted = affected is not None and int(affected) > 0
        if not inserted:
            _logger.info(
                "insert_cash_movement_if_absent: skipped duplicate %s %r (%s)",
                movement_type, account, note,
            )
        return inserted
    except Exception as _e:
        _logger.warning("insert_cash_movement_if_absent failed for %s %r (%s): %s", movement_type, account, note, _e)
        return False


def _parse_pm_and_inject(total_amount: float, payment_method: str, note: str, staff_name: str, category=None) -> None:
    """Parse payment method string (e.g. 'KPay:31000|Cash:20000' or 'KPay:90000/Cash:0')
    and inject cash_movements entries for each account.

    Optional ``category`` (added 2026-09-20, flag migration): when given, the
    cash_movements row is tagged with it (e.g. ``category='topup'`` for member
    top-ups / new-member card sales). Default ``None`` preserves the previous
    behaviour exactly — every other caller inserts a NULL category."""
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
            # strip '(dep)' / '(DepositRedeemed)' markers before float()
            _v = re.sub(r"\([^)]*\)", "", val_str).strip()
            val = float(_v or 0) if _v else 0
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
        }
        account = acct_map.get(method.strip().lower().replace("  ", " "), method)
        if val > 0:
            _mysql_execute(
                "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, category, created_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())",
                ("inject", account, val, note, staff_name, category)
            )
            _logger.info("Cash movement inject: %s +%.0f Ks (%s, category=%s)", account, val, note, category)



# ── Sale-time game resolution (sales_daily.game_name / game_id) ──────────────
# 2026-09-20: sales_daily.game_name + game_id are populated AT SALE TIME. A sale
# row links to a game via (in priority order): explicit hint → console_sessions
# by session_id → the booking's latest session → the console's active session →
# console_status.current_game. game_id is the matching console_games.id.
# NOTE the collation trap: console_games.* is utf8mb4_unicode_ci while
# console_sessions/sales_daily are utf8mb4_0900_ai_ci, so the join needs an
# explicit COLLATE on BOTH sides. Never raises — returns (None, None, None).
def resolve_sale_game(console_id, session_id=None, booking_id=None, game_name_hint=None):
    """Return (game_name, game_id, session_id) for a sale row; NULLs when unknown."""
    sid = None
    gname = (game_name_hint or "").strip()
    try:
        # Normalise a caller-supplied session id.
        if session_id not in (None, "", "0", 0):
            try:
                sid = int(session_id)
            except (TypeError, ValueError):
                sid = None

        row = None
        if not gname:
            if sid:
                row = _mysql_query_one(
                    "SELECT id, game_name FROM console_sessions WHERE id=%s", (sid,))
            elif booking_id not in (None, "", 0, "0"):
                row = _mysql_query_one(
                    "SELECT id, game_name FROM console_sessions WHERE booking_id=%s "
                    "ORDER BY id DESC LIMIT 1", (booking_id,))
            if row:
                sid = row.get("id") or sid
                gname = (row.get("game_name") or "").strip()

        # Fallback: the console's current (active) session.
        if not gname and console_id:
            row = _mysql_query_one(
                "SELECT id, game_name FROM console_sessions WHERE console_id=%s "
                "AND status='active' ORDER BY id DESC LIMIT 1", (console_id,))
            if row:
                sid = sid or row.get("id")
                gname = (row.get("game_name") or "").strip()

        # Last resort: console_status.current_game (no session id attached).
        if not gname and console_id:
            cs = _mysql_query_one(
                "SELECT current_game FROM console_status WHERE console_id=%s", (console_id,))
            if cs:
                gname = (cs.get("current_game") or "").strip()

        if not gname or gname == "-":
            return (None, None, sid)

        # Map (console_id, title) → console_games.id with explicit COLLATE both sides.
        gid = None
        if console_id:
            try:
                cg = _mysql_query_one(
                    "SELECT cg.id FROM console_games cg "
                    "WHERE cg.console_id = %s COLLATE utf8mb4_unicode_ci "
                    "AND cg.game_title = %s COLLATE utf8mb4_unicode_ci "
                    "ORDER BY cg.id DESC LIMIT 1",
                    (console_id, gname))
                if cg:
                    gid = int(cg["id"])
            except Exception as _e:
                _logger.warning("resolve_sale_game: game_id lookup failed: %s", _e)
        return (gname, gid, sid)
    except Exception as _e:
        _logger.warning("resolve_sale_game failed console=%s session=%s booking=%s: %s",
                        console_id, session_id, booking_id, _e)
        return (None, None, None)
