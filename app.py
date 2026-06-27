"""PS VIBE API Server — FastAPI REST API
Auto-generated from function_map.json (2026-05-27)

All endpoint names match function_map.json functions exactly,
per Boss's requirement.
"""
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
import os
import asyncio
import urllib.request
import urllib.error

# Telegram notifications (direct, no n8n needed)
# Session timer module for 5-min-before-end reminder
from session_timer import resume_active_timers, schedule_session_timer, _active_timers
from booking_reminder import add_booking_reminder, restore_reminders, cancel_booking_reminder

# Field name normalization (snake_case ↔ camelCase backward compat)
from field_utils import normalize_list, normalize_response

from fastapi import FastAPI, HTTPException, Query, Depends, Request, Body
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from config import (
    API_TITLE, API_VERSION, API_DESCRIPTION,
    HOST, PORT, DEBUG, API_KEY,
    SHEET_SALES_DAILY, SHEET_SETTING, SHEET_CARD_WALLET,
    SHEET_TOPUP_LOG, SHEET_ATTENDANCE_LOG, SHEET_CONSOLE_BOOKING,
    SHEET_SALARY_ADVANCE, SHEET_GAME_LIBRARY, SHEET_CONSOLE_GAMES,
    MMT_HOURS, MMT_MINUTES,
)
from sheets_client import (
    get_workbook, get_worksheet, get_member_rows, get_booking_rows,
    get_game_rows, get_console_game_rows,
    invalidate_cache, int_safe, float_safe,
)
from mysql_db import query as mysql_query, query_one as mysql_query_one, execute as mysql_execute
from auth import register_auth_routes, get_current_user
from dashboard_routes import router as dashboard_router
from kora_cmds import router as kora_cmds_router
from digest_routes import router as digest_router
from models import (
    GameResponse, ConfigResponse, MemberResponse,
    BookingResponse, HealthResponse, GenericResponse
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("psvibe_api")

# ── MySQL query helpers ──
# NOTE (MI-1): This module uses both mysql.connector (direct helper functions)
# and pymysql (via mysql_db.py). For future consolidation, migrate all helpers
# to use mysql_db.py's pymysql-based functions (query, query_one, execute).
import mysql.connector as _mc
import json, json as _json, os, os as _os
import random, string

_MYSQL_CFG = {
    "host": _os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "user": _os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": _os.environ.get("MYSQL_PASSWORD", ""),
    "database": _os.environ.get("MYSQL_DATABASE", "psvibe_api"),
}

def _mysql_query(sql, params=None):
    conn = _mc.connect(**_MYSQL_CFG)
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    conn.close()
    return rows


def _mysql_query_one(sql, params=None):
    """Execute query and return first row, or None."""
    rows = _mysql_query(sql, params)
    return rows[0] if rows else None

def _mysql_exec(sql, params=None):
    conn = _mc.connect(**_MYSQL_CFG)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid

def _sync_console_status(console_id):
    """Derive console_status from current booking table.
    Called after each booking mutation to prevent status drift."""
    try:
        # 🐛 Fix Jun 25: Use MMT time for comparisons (DB stores times in MMT, not UTC)
        _now_mmt = now_mmt()
        _now_mmt_str = _now_mmt.strftime('%Y-%m-%d %H:%M:%S')
        _now_plus_60 = (_now_mmt + timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S')
        active = _mysql_query_one(
            "SELECT id, member_id, game_name, start_time FROM console_booking "
            "WHERE console_id=%s AND status='Active' ORDER BY id DESC LIMIT 1",
            (console_id,)
        )
        if active:
            # Preserve existing start_time for normal extends (Active→Active),
            # but fix stale start_time from a previous session.
            current_status = _mysql_query_one("SELECT status, start_time FROM console_status WHERE console_id=%s", (console_id,))
            booking_start = active.get('start_time')
            if current_status and current_status.get('status') == 'Active':
                cs_start = current_status.get('start_time')
                # If console_status start_time is stale (>5 min diff from booking), fix it
                if cs_start and booking_start:
                    diff = abs((booking_start - cs_start).total_seconds())
                    if diff > 300:  # >5 min mismatch → update to booking time
                        _mysql_exec(
                            "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                            (active.get('member_id', ''), active.get('game_name', ''), booking_start, console_id)
                        )
                        return
                _mysql_exec(
                    "UPDATE console_status SET status='Active', current_member=%s, current_game=%s WHERE console_id=%s",
                    (active.get('member_id', ''), active.get('game_name', ''), console_id)
                )
            else:
                # New activation — use booking's start_time, fallback to NOW()
                st = booking_start if booking_start else None
                if st:
                    _mysql_exec(
                        "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                        (active.get('member_id', ''), active.get('game_name', ''), st, console_id)
                    )
                else:
                    _mysql_exec(
                        "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                        (active.get('member_id', ''), active.get('game_name', ''), _now_mmt_str, console_id)
                    )
            return
        # Check for confirmed or checked_in bookings within 60-min window
        confirmed = _mysql_query_one(
            "SELECT id, member_id, game_name FROM console_booking "
            "WHERE console_id=%s AND status IN ('confirmed','checked_in') AND start_time <= %s AND end_time >= %s ORDER BY id DESC LIMIT 1",
            (console_id, _now_plus_60, _now_mmt_str)
        )
        if confirmed:
            _mysql_exec(
                "UPDATE console_status SET status='Reserved', current_member=%s, current_game=%s, start_time=NULL WHERE console_id=%s",
                (confirmed.get('member_id', ''), confirmed.get('game_name', ''), console_id)
            )
            return
        _mysql_exec(
            "UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s",
            (console_id,)
        )
    except Exception as e:
        logger.warning("_sync_console_status failed for %s: %s", console_id, e)

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
        else: result[k] = str(val) if val else ""
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: resume active session timers + booking reminders inside the event loop."""
    try:
        resume_active_timers()
        restore_reminders()
        active_consoles = _mysql_query(
            "SELECT DISTINCT console_id FROM console_booking WHERE status='Active'"
        )
        for _row in (active_consoles or []):
            _cid = _row.get('console_id', '')
            if _cid:
                try:
                    _sync_console_status(_cid)
                    logging.info("Synced console_status for %s on startup", _cid)
                except Exception as _sync_err:
                    logging.warning("sync_console_status failed for %s: %s", _cid, _sync_err)
    except Exception as e:
        logging.error("resume_active_timers failed on startup: %s", e, exc_info=True)
    yield
    # Shutdown: nothing to clean up

app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Branch Context Middleware ─────────────────────────────────────────
# Backward-compatible: defaults to branch_id=1 (Sanchaung)
# To use a different branch, pass X-Branch-ID header
@app.middleware("http")
async def branch_context_middleware(request: Request, call_next):
    try:
        branch_id = int(request.headers.get("X-Branch-ID", "1"))
    except (ValueError, TypeError):
        branch_id = 1
    request.state.branch_id = branch_id
    response = await call_next(request)
    return response

def get_branch_id(request: Request) -> int:
    """Helper: get current branch_id from request context. Defaults to 1."""
    return getattr(request.state, "branch_id", 1)


# ── Dashboard Auth & Routes ──
register_auth_routes(app)
app.include_router(dashboard_router)
app.include_router(kora_cmds_router)
app.include_router(digest_router)

# ── Config Cache ──
_config_cache_data = None
_config_cache_time = 0
_CONFIG_CACHE_TTL = 60  # seconds



@app.get("/api/debug_auth")
async def debug_api_key(
    api_key_query: str = Query(None, alias="api_key"),
    request: Request = None,
):
    import os
    env_key = os.environ.get("API_KEY", "")
    header_key = request.headers.get("X-API-Key", "") if request else ""
    return {
        "query_param": api_key_query or "(none)",
        "header_key": header_key[:8] + "..." if header_key and len(header_key) > 8 else header_key or "(none)",
        "env_key": env_key[:8] + "..." if env_key and len(env_key) > 8 else env_key or "(not set)",
        "header_matches_env": (header_key == env_key) if header_key and env_key else "one_or_both_empty",
        "query_matches_env": (api_key_query == env_key) if api_key_query and env_key else "one_or_both_empty",
        "env_key_len": len(env_key),
        "header_key_len": len(header_key),
        "env_key_repr": repr(env_key[:50]),
        "header_key_repr": repr(header_key[:50]) if header_key else "",
    }

async def verify_api_key(
    api_key: str = Query(None, alias="api_key"),
    request: Request = None,
):
    """Verify API key OR JWT Bearer token."""
    if API_KEY:
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


def now_mmt():
    mmt = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))
    return datetime.now(mmt)


def today_str():
    return now_mmt().strftime("%-m/%-d/%Y")


def _norm_cid(cid: str) -> str:
    return cid.replace(" ", "").upper()


def ok(data=None, message=""):
    d = {"success": True, "data": data}
    if message:
        d["message"] = message
    return d


def error_response(message="Internal error", code=500):
    """Return a standardized error dict (use in route handlers directly)."""
    return {"success": False, "error": message, "code": code}


def success_response(data=None, message=None):
    """Alias for ok() — canonical success wrapper."""
    return ok(data=data, message=message or "")


# ── Global exception handler: wrap all HTTPException as {success:False,error:...} ──
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail, "code": exc.status_code},
    )
    allowed = {
        "health": "curl -s http://127.0.0.1:8000/api/health",
        "mysql_health": "curl -s http://127.0.0.1:8000/api/mysql/health",
        "alerts": "python3 /root/coordination/check_alerts.py 2>/dev/null || echo \"No alerts system\"",
        "backup": "date && echo \"Backup triggered\"",
        "uptime": "uptime && free -h | head -2",
        "disk": "df -h /",
        "docker": "docker ps --format \"table {{.Names}}\t{{.Status}}\" 2>/dev/null",
        "services": "systemctl list-units --type=service --state=running 2>/dev/null | head -10 || docker ps --format \"{{.Names}}: {{.Status}}\"",
        "mem": "free -h",
        "process": "ps aux --sort=-%mem | head -10",
    }
    if command not in allowed:
        return {"success": False, "error": f"Unknown command: {command}. Allowed: {list(allowed.keys())}"}
    try:
        r = sp.run(shlex.split(allowed[command]), capture_output=True, text=True, timeout=15)
        return {"success": True, "command": command, "output": r.stdout.strip() or "(no output)", "error": r.stderr.strip() or None}
    except sp.TimeoutExpired:
        return {"success": False, "command": command, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "command": command, "error": str(e)}


@app.get("/kora")
@app.get("/kora/{path:path}")
async def serve_kora_dashboard(request: Request, path: str = ""):
    """Serve Kora Dashboard"""
    import os
    kora_file = os.path.join(os.path.dirname(__file__), "..", ".openclaw", "workspace", "kora_dashboard", "index.html")
    if os.path.exists(kora_file):
        with open(kora_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return JSONResponse({"success": False, "error": "Kora Dashboard not found"}, status_code=404)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={"success": False, "error": "Not Found", "code": 404},
    )


# ═══════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════
@app.get("/api/health", response_model=HealthResponse, tags=["System"], summary="Health check")
async def health_check():
    # Sheets health is checked on startup via startup event; skip per-request
    # to avoid 300ms+ gspread round-trip on every health probe.
    return {"status": "ok", "timestamp": __import__("time").time(), "services": {"api": "running"}}


# ═══════════════════════════════════════
#  MYSQL HEALTH
# ═══════════════════════════════════════
@app.get("/api/mysql/health", response_model=GenericResponse, tags=["System"], summary="MySQL health check")
def mysql_health():
    try:
        mysql_query_one("SELECT 1 as ok")
        return {"success": True, "mysql_connected": True, "data_source": "mysql"}
    except Exception as e:
        return {"success": True, "mysql_connected": False, "data_source": "gspread", "error": str(e)}


# ═══════════════════════════════════════
#  MYSQL HELPER
# ═══════════════════════════════════════
_MYSQL_ENABLED = True

def _use_mysql() -> bool:
    global _MYSQL_ENABLED
    if not _MYSQL_ENABLED:
        return False
    try:
        mysql_query_one("SELECT 1 as ok")
        return True
    except Exception:
        _MYSQL_ENABLED = False
        return False


# ═══════════════════════════════════════
#  MYSQL STATUS
# ═══════════════════════════════════════
@app.get("/api/mysql/status", response_model=GenericResponse, tags=["System"], summary="MySQL table status")
def mysql_status(auth=Depends(verify_api_key)):
    """Show all MySQL tables with row counts."""
    if not _use_mysql():
        return {"success": True, "mysql_connected": False, "data_source": "gpsread"}
    try:
        tables = mysql_query("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'psvibe_api'")
        result = {}
        for t in tables:
            tn = t["TABLE_NAME"]
            cnt = mysql_query_one(f"SELECT COUNT(*) as cnt FROM `{tn}`")
            result[tn] = cnt["cnt"] if cnt else 0
        return ok({"mysql_connected": True, "total_tables": len(result), "tables": result})
    except Exception as e:
        return {"success": False, "error": str(e), "mysql_connected": False}


# ═══════════════════════════════════════
#  MYSQL WRAPPERS
# ═══════════════════════════════════════

def _fetch_games_from_mysql():
    """Try MySQL first for games list, return data or None."""
    try:
        if _use_mysql():
            rows = mysql_query("SELECT game_title as title, final_status, disc_count as discs, solo_multi, genre FROM games_library")
            return rows if rows else None
    except Exception:
        pass
    return None

def _fetch_members_from_mysql():
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            return mysql_query("SELECT * FROM member_wallets")
    except Exception:
        pass
    return None


def _fetch_member_data_from_mysql(member_id):
    """Try MySQL first, return data or None.
    Returns None if balance data is > 15 min stale (forces GSheet fallback)."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT * FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                age = (datetime.now(timezone.utc) - row.get("last_updated", datetime.min.replace(tzinfo=timezone.utc))).total_seconds()
                if age < 900:  # 15 min max staleness for full member data
                    return row
                logging.warning("MySQL member_data for %s is stale (%.0fs old), falling back to GSheet", member_id, age)
    except Exception:
        pass
    return None


def _fetch_console_status_from_mysql():
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            return mysql_query("SELECT * FROM console_status")
    except Exception:
        pass
    return None


def _fetch_allowed_staff_ids_from_mysql():
    """Read allowed Telegram user IDs from MySQL settings_config."""
    try:
        val = _mysql_get_setting("allowed_staff_ids", "")
        if val:
            return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]
    except Exception:
        pass
    return None


def _fetch_console_games_from_mysql():
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            return mysql_query("SELECT * FROM console_games")
    except Exception:
        pass
    return None


def _fetch_wallet_mins_from_mysql(member_id):
    """Try MySQL first, return data or None.
    Returns None if data is > 10 min stale (forces GSheet fallback)."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT balance_mins, last_updated FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                age = (datetime.now(timezone.utc) - row.get("last_updated", datetime.min.replace(tzinfo=timezone.utc))).total_seconds()
                if age < 120:  # 2 min max staleness (MySQL primary)
                    return row.get("balance_mins", 0)
                logging.warning("MySQL wallet_mins for %s is stale (%.0fs old), falling back to GSheet", member_id, age)
    except Exception:
        pass
    return None


def _fetch_balance_mins_from_mysql(member_id):
    """Try MySQL first, return data or None (same as wallet_mins live read).
    Returns None if data is > 10 min stale (forces GSheet fallback)."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT balance_mins, last_updated FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                age = (datetime.now(timezone.utc) - row.get("last_updated", datetime.min.replace(tzinfo=timezone.utc))).total_seconds()
                if age < 120:  # 2 min max staleness (MySQL primary)
                    return row.get("balance_mins", 0)
                logging.warning("MySQL balance_mins for %s is stale (%.0fs old), falling back to GSheet", member_id, age)
    except Exception:
        pass
    return None


def _fetch_daily_sales_from_mysql(date_str=None):
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            if date_str:
                return mysql_query("SELECT * FROM sales_daily WHERE date = %s", (date_str,))
            return mysql_query("SELECT * FROM sales_daily")
    except Exception:
        pass
    return None


def _fetch_topups_from_mysql(days=30):
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            return mysql_query("SELECT * FROM topup_log ORDER BY created_at DESC LIMIT 1000")
    except Exception:
        pass
    return None


# ═══════════════════════════════════════
#  fetch_console_status
# ═══════════════════════════════════════
@app.get("/api/fetch_console_status", response_model=GenericResponse, tags=["Consoles"], summary="Fetch console statuses [MySQL]")
async def api_fetch_console_status(auth=Depends(verify_api_key)) -> JSONResponse:
    """Fetch live console statuses from MySQL console_status table."""
    try:
        rows = _mysql_query(
            "SELECT cs.console_id, cs.status, cs.console_type, cs.current_game, cs.current_member, DATE_FORMAT(cs.start_time, '%H:%i') as start_time, DATE_FORMAT(cs.start_time, '%Y-%m-%d %H:%i:%s') as start_time_dt, cb.id as booking_id, cb.staff_name, cb.duration_mins FROM console_status cs LEFT JOIN (    SELECT cb1.id, cb1.console_id, cb1.staff_name, cb1.duration_mins     FROM console_booking cb1     INNER JOIN (        SELECT console_id, MAX(id) as max_id         FROM console_booking WHERE status = 'Active' GROUP BY console_id    ) cb2 ON cb1.id = cb2.max_id) cb ON REPLACE(cb.console_id, ' ', '') LIKE CONCAT(REPLACE(cs.console_id, ' ', ''), '%') ORDER BY cs.console_id")
        # Add id alias for backward compat — bot code uses c["id"] everywhere
        for r in rows:
            if "id" not in r:
                r["id"] = r.get("console_id", "")
        # Display-only Reserved: show Free consoles as "Reserved" if a confirmed
        # booking starts within 120 minutes. Does NOT change console_status table —
        # consoles remain Free for walk-ins. This is display-only indication.
        # Also attach upcoming booking info to Active consoles for dual-status display.
        try:
            _now_ref = now_mmt()
            _window_end = _now_ref + timedelta(minutes=120)
            upcoming = _mysql_query(
                "SELECT cb.console_id, cb.start_time, cb.staff_name, cb.duration_mins FROM console_booking cb "
                "WHERE cb.status IN ('confirmed', 'pending_check_in') "
                "AND cb.start_time <= %s AND cb.end_time > %s "
                "AND cb.id = (SELECT MAX(cb2.id) FROM console_booking cb2 WHERE cb2.console_id = cb.console_id AND cb2.status IN ('confirmed', 'pending_check_in') AND cb2.start_time <= %s AND cb2.end_time > %s)",
                (_window_end, _now_ref, _window_end, _now_ref)
            )
            reserved_ids = {r["console_id"].strip() for r in upcoming}
            reserved_info = {}
            for u in upcoming:
                cid = u["console_id"].strip()
                st = u.get("start_time")
                reserved_info[cid] = {
                    "reserved_for": u.get("staff_name") or "",
                    "reserved_at": st.strftime("%I:%M %p") if st else "",
                    "reserved_duration": u.get("duration_mins") if u.get("duration_mins") else 0,
                }
        except Exception:
            reserved_ids = set()
            reserved_info = {}
        for r in rows:
            cid = r["console_id"].strip()
            if r.get("status") == "Free" and cid in reserved_ids:
                r["status"] = "Reserved"
            elif r.get("status") == "Reserved" and cid not in reserved_ids:
                r["status"] = "Free"
            # For Active consoles: attach upcoming reservation info for dual display
            if r.get("status") == "Active" and cid in reserved_info:
                r.update(reserved_info[cid])
        return ok({"consoles": normalize_list(rows)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  extend_booking_duration
# ═══════════════════════════════════════
@app.post("/api/bookings/extend-duration", response_model=GenericResponse, tags=["Bookings"], summary="Extend active booking duration [MySQL]")
async def api_extend_booking_duration(req: dict, auth=Depends(verify_api_key)):
    """Extend active booking duration by extra_mins; updates console_booking.duration_mins."""
    try:
        console_id = req.get("console_id")
        extra_mins = int(req.get("extra_mins", 0))
        if not console_id or extra_mins <= 0:
            return error_response(message="console_id and extra_mins required")
        row = _mysql_query_one(
            "SELECT id, duration_mins FROM console_booking WHERE console_id=%s AND status='Active' ORDER BY id DESC LIMIT 1",
            (console_id,)
        )
        if not row:
            return error_response(message="No active booking found")
        new_duration = (row["duration_mins"] or 0) + extra_mins
        _dur_end = 360 if new_duration == 0 else new_duration
        _mysql_exec("UPDATE console_booking SET duration_mins=%s, end_time=DATE_ADD(start_time, INTERVAL %s MINUTE) WHERE id=%s", (new_duration, _dur_end, row["id"]))
        invalidate_cache("bookings")
        return ok({"booking_id": row["id"], "new_duration_mins": new_duration})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/bookings/mark-bot-reminder", response_model=GenericResponse, tags=["Bookings"], summary="Mark booking for bot-managed reminders (prevents API timer duplication)")
async def api_mark_bot_reminder(req: dict, auth=Depends(verify_api_key)):
    """Called by the Sales Bot when _remind_loop starts handling a session.
    Updates telegram_chat_id so the API-side session_timer will skip this booking."""
    try:
        console_id = req.get("console_id", "")
        telegram_chat_id = req.get("telegram_chat_id", "")
        if not console_id or not telegram_chat_id:
            return error_response(message="console_id and telegram_chat_id required")
        row = _mysql_query_one(
            "SELECT id, telegram_chat_id FROM console_booking WHERE console_id=%s AND status='Active' ORDER BY id DESC LIMIT 1",
            (console_id,)
        )
        if not row:
            return error_response(message="No active booking found for this console")
        # Only update if telegram_chat_id is currently empty (avoid overwriting real customer chat IDs)
        existing_tg = (row.get("telegram_chat_id") or "").strip()
        if not existing_tg:
            _mysql_exec("UPDATE console_booking SET telegram_chat_id=%s WHERE id=%s",
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
@app.post("/webhook/booking-reminder", response_model=GenericResponse, tags=["Webhooks"], summary="Receive booking data for 30-min advance reminder")
async def webhook_booking_reminder(req: dict):
    """Webhook called by Sales Bot when a booking is confirmed.
    Schedules a 30-min-before reminder to STAFF_NOTIFY_CHAT via Telegram."""
    try:
        result = add_booking_reminder(req)
        if result == "ok":
            return ok({"message": "reminder scheduled"})
        return ok({"message": result})
    except Exception as e:
        logger.error(f"webhook/booking-reminder: {e}")
        return error_response(message=str(e))

@app.post("/webhook/booking-reminder/cancel", response_model=GenericResponse, tags=["Webhooks"], summary="Cancel a pending booking reminder")
async def webhook_booking_reminder_cancel(req: dict):
    """Called when a booking is cancelled/rejected — removes any pending reminder."""
    try:
        bk_id = str(req.get("bk_id") or req.get("booking_id", ""))
        if not bk_id:
            return ok({"message": "missing bk_id"})
        cancelled = cancel_booking_reminder(bk_id)
        return ok({"message": "cancelled" if cancelled else "none pending"})
    except Exception as e:
        logger.error(f"webhook/booking-reminder/cancel: {e}")
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  fetch_members
# ═══════════════════════════════════════
def _calc_member_liability_kyat(member_id):
    """Calculate FIFO-based liability in kyat for a member."""
    try:
        from fifo_wallet import fifo_calc
        tu = _mysql_query(
            "SELECT amount, mins_added FROM topup_log WHERE member_id=%s ORDER BY topup_date ASC",
            (member_id,)
        ) or []
        topups = [{"amount": float(t["amount"] or 0), "mins_added": float(t["mins_added"] or 0)} for t in tu]
        bw = _mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        bal = float(bw["balance_mins"]) if bw and bw.get("balance_mins") else 0
        fifo = fifo_calc(topups, bal)
        return int(fifo["liability"])
    except:
        return 0

@app.get("/api/fetch_members", response_model=GenericResponse, tags=["Members"], summary="Fetch all members [MySQL]")
async def api_fetch_members(auth=Depends(verify_api_key)):
    """Fetch all members with name, phone, tier, and wallet balance (liability)."""
    try:
        rows = _mysql_query("SELECT member_id, member_name, phone, balance_mins, tier, total_spend, lifetime_spend FROM member_wallets WHERE member_id IS NOT NULL AND member_id != '' ORDER BY member_id")
        result = []
        for r in rows:
            result.append({
                "id": r["member_id"],
                "name": r.get("member_name") or r["member_id"],
                "phone": r.get("phone") or "",
                "tier": r.get("tier") or "",
                "liability_mins": int(r.get("balance_mins") or 0),
                "liability_kyat": _calc_member_liability_kyat(r["member_id"]),
                "wallet_balance": int(r.get("balance_mins") or 0)
            })
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_data
# ═══════════════════════════════════════
@app.get("/api/fetch_member_data/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch member data [MySQL]")
async def api_fetch_member_data(member_id: str, auth=Depends(verify_api_key)):
    """Fetch consolidated member data (name, phone, rank, wallet) from MySQL."""
    try:
        row = _mysql_query_one(
            "SELECT member_id, member_name, phone, balance_mins, tier, total_spend, referral_code FROM member_wallets WHERE member_id=%s",
            (member_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Member not found")
        net_spend = int(row.get("total_spend", 0) or 0)
        # Compute rank from spend using thresholds
        try:
            mt = int(_mysql_get_setting("master_threshold", "300000"))
            it = int(_mysql_get_setting("immortal_threshold", "1000000"))
        except Exception:
            mt, it = 300000, 1000000
        if net_spend >= it:
            rank_raw = "Immortal"
        elif net_spend >= mt:
            rank_raw = "Master"
        else:
            rank_raw = "Warrior"
        return ok({
            "name": row.get("member_name", "-") or "-",
            "phone": row.get("phone", "-") or "-",
            "email": "",
            "net_spend": net_spend,
            "rank_raw": rank_raw,
            "wallet_mins": row.get("balance_mins"),
            "referral_code": row.get("referral_code", "") or "",
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_wallet_mins
# ═══════════════════════════════════════
@app.get("/api/fetch_wallet_mins/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch wallet minutes [MySQL]")
async def api_fetch_wallet_mins(member_id: str, auth=Depends(verify_api_key)):
    """Fetch wallet balance from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT balance_mins FROM member_wallets WHERE member_id=%s",
            (member_id,))
        if not rows:
            return ok(0)
        return ok(rows[0].get("balance_mins", 0) or 0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_balance_mins (alias live read)
# ═══════════════════════════════════════
@app.get("/api/fetch_balance_mins/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch wallet balance live [MySQL]")
async def api_fetch_balance_mins(member_id: str, auth=Depends(verify_api_key)):
    """Fetch wallet balance in mins (live from MySQL)."""
    try:
        rows = _mysql_query("SELECT balance_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        if rows:
            return ok(rows[0]["balance_mins"] or 0)
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_tier
# ═══════════════════════════════════════

#  wallet_deduct (after sale)
@app.post("/api/wallet/deduct", response_model=GenericResponse, tags=["Members"], summary="Deduct wallet balance after sale [MySQL]")
async def api_wallet_deduct(req: dict, auth=Depends(verify_api_key)):
    """Deduct wallet balance_mins after a gaming session sale."""
    try:
        member_id = req.get("member_id", "")
        deduct_mins = int(req.get("deduct_mins", 0))
        total_mins = int(req.get("total_mins", 0))

        if not member_id or deduct_mins <= 0:
            return error_response(message="member_id and deduct_mins > 0 required")

        rows = _mysql_query(
            "SELECT balance_mins, total_spend, total_bought_mins FROM member_wallets WHERE member_id=%s",
            (member_id,))
        if not rows:
            return error_response(message=f"Member {member_id} not found in wallet")

        cur = rows[0]
        old_bal = cur.get("balance_mins", 0) or 0
        new_bal = max(0, old_bal - deduct_mins)
        new_spend = (cur.get("total_spend", 0) or 0) + int(total_mins)

        _mysql_exec(
            "UPDATE member_wallets SET balance_mins=%s, total_spend=%s, last_updated=NOW() WHERE member_id=%s",
            (new_bal, new_spend, member_id))

        return ok({
            "success": True,
            "member_id": member_id,
            "balance_before": old_bal,
            "balance_after": new_bal,
            "deducted": deduct_mins,
            "total_spend": new_spend
        })
    except Exception as e:
        return error_response(message=str(e))

@app.get("/api/fetch_member_tier/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch member tier [MySQL]")
async def api_fetch_member_tier(member_id: str, auth=Depends(verify_api_key)):
    """Fetch member tier from MySQL member_wallets."""
    try:
        rows = _mysql_query("SELECT tier, total_spend FROM member_wallets WHERE member_id=%s", (member_id,))
        r = rows[0] if rows else {"tier": "Warrior", "total_spend": 0}
        tier = (r.get("tier") or "Warrior") if r else "Warrior"
        return ok(tier)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_staff / fetch_staff_names
# ═══════════════════════════════════════
@app.get("/api/fetch_staff", response_model=GenericResponse, tags=["Staff"], summary="Fetch staff list [MySQL]")
async def api_fetch_staff(auth=Depends(verify_api_key)):
    """Fetch staff list from MySQL staff_records."""
    try:
        rows = _mysql_query("SELECT staff_name FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        return ok([r["staff_name"] for r in rows])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_staff_names", response_model=GenericResponse, tags=["Staff"], summary="Fetch staff names [MySQL]")
async def api_fetch_staff_names(auth=Depends(verify_api_key)):
    """Fetch active staff names from MySQL."""
    try:
        rows = _mysql_query("SELECT staff_name FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        return ok([r["staff_name"] for r in rows])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_food_prices / fetch_food_costs
# ==============================================
@app.get("/api/fetch_food_prices", response_model=GenericResponse, tags=["Food"], summary="Fetch food prices [MySQL]")
async def api_fetch_food_prices(auth=Depends(verify_api_key)):
    """Fetch food prices from inventory table WHERE category=Food AND quantity > 0."""
    try:
        rows = _mysql_query("SELECT item_name, unit_price FROM inventory WHERE category IN (%s,%s,%s,%s,%s,%s,%s,%s) AND quantity > 0", ("Food","Drinks","Instant Noodles","Snacks","Candy","Other","Soft Drinks","Coffee"))
        result = {}
        for r in rows:
            name = r["item_name"]
            price = int(float(r["unit_price"])) if r["unit_price"] else 0
            result[name] = price
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/fetch_food_menu", response_model=GenericResponse, tags=["Food"], summary="Fetch food menu grouped by category [MySQL]")
async def api_fetch_food_menu(auth=Depends(verify_api_key)):
    """Fetch food items grouped by category from inventory."""
    try:
        rows = _mysql_query("SELECT item_name, unit_price, category FROM inventory WHERE category IN (%s,%s,%s,%s,%s,%s,%s,%s) AND quantity > 0 ORDER BY category, item_name", ("Food","Drinks","Instant Noodles","Snacks","Candy","Other","Soft Drinks","Coffee"))
        result = {}
        for r in rows:
            cat = r["category"]
            name = r["item_name"]
            price = int(float(r["unit_price"])) if r["unit_price"] else 0
            if cat not in result:
                result[cat] = {}
            result[cat][name] = price
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_food_costs", response_model=GenericResponse, tags=["Food"], summary="Fetch food costs [MySQL]")
async def api_fetch_food_costs(auth=Depends(verify_api_key)):
    """Fetch food costs from inventory table WHERE category='Food' AND quantity > 0."""
    try:
        rows = _mysql_query("SELECT item_name, unit_price FROM inventory WHERE category IN (%s,%s,%s,%s,%s,%s,%s,%s) AND quantity > 0", ("Food","Drinks","Instant Noodles","Snacks","Candy","Other","Soft Drinks","Coffee"))
        result = {}
        for r in rows:
            name = r["item_name"]
            price = int(float(r["unit_price"])) if r["unit_price"] else 0
            result[name] = price
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/inventory/stock-out", response_model=GenericResponse, tags=["Inventory"], summary="Record stock-out (sale) and update inventory [MySQL]")
async def api_inventory_stock_out(req: dict, auth=Depends(verify_api_key)):
    """Record a stock-out from a food sale and decrement inventory quantity."""
    try:
        item_name = req.get("item_name", "")
        qty = int(req.get("qty", 0))
        unit_price = float(req.get("unit_price", 0))
        subtotal = float(req.get("subtotal", 0))
        sale_date = req.get("date", "")
        voucher_no = req.get("voucher_no", "")

        if not item_name or qty <= 0:
            return error_response(message="item_name and qty > 0 required")

        # Insert into stock_out table
        _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (item_name, qty, unit_price, subtotal, sale_date, f"Voucher: {voucher_no}")
        )

        # Decrement inventory.quantity (legacy summary column — NOTE: FIFO stock_in-stock_out is authoritative; use reconcile_inventory_quantities() to sync)
        _mysql_exec(
            "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
            (qty, item_name)
        )

        return ok({"success": True, "item_name": item_name, "qty_deducted": qty})
    except Exception as e:
        return error_response(message=str(e))



# ═══════════════════════════════════════
#  reconcile_inventory_quantities — sync inventory.quantity from FIFO
# ═══════════════════════════════════════
def reconcile_inventory_quantities():
    """Sync inventory.quantity to match authoritative FIFO calculation.
    
    inventory.quantity is a legacy summary column that can drift from stock_in/stock_out.
    This function recalculates it from: SUM(stock_in.quantity) - SUM(stock_out.quantity).
    Call this periodically or after batch stock operations.
    
    Returns dict with {item_name: old_qty, new_qty, drift} for changed items.
    """
    conn = _mc.connect(**_MYSQL_CFG)
    try:
        cur = conn.cursor(dictionary=True)
        # Get all inventory items with their current quantities
        cur.execute("SELECT item_name, quantity FROM inventory")
        items = cur.fetchall()
        changes = []
        for item in items:
            item_name = item["item_name"]
            old_qty = int(item["quantity"] or 0)
            # FIFO calculation: stock_in - stock_out (same as inventory_fifo.py get_fifo_stock)
            cur.execute(
                "SELECT COALESCE((SELECT SUM(quantity) FROM stock_in WHERE item_name=%s), 0) "
                "- COALESCE((SELECT SUM(quantity) FROM stock_out WHERE item_name=%s), 0) AS fifo_qty",
                (item_name, item_name)
            )
            row = cur.fetchone()
            fifo_qty = int(row["fifo_qty"] or 0)
            if fifo_qty != old_qty:
                cur.execute(
                    "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE item_name = %s",
                    (fifo_qty, item_name)
                )
                changes.append({"item_name": item_name, "old_quantity": old_qty, "new_quantity": fifo_qty, "drift": fifo_qty - old_qty})
        conn.commit()
        return changes
    finally:
        conn.close()


@app.get("/api/inventory/reconcile", response_model=GenericResponse, tags=["Inventory"], summary="Reconcile inventory.quantity from FIFO tracking")
async def api_reconcile_inventory(auth=Depends(verify_api_key)):
    """Recalculate inventory.quantity from authoritative FIFO stock_in/stock_out.
    
    inventory.quantity is a legacy summary column. The true stock is derived from
    stock_in - stock_out (FIFO). This endpoint syncs the summary column.
    """
    try:
        changes = reconcile_inventory_quantities()
        return ok({
            "message": f"Reconciled {len(changes)} items",
            "changes": changes
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  fetch_games / fetch_game_library
# ═══════════════════════════════════════
@app.get("/api/fetch_games", response_model=GenericResponse, tags=["Games"], summary="Fetch game list [MySQL]")
async def api_fetch_games(auth=Depends(verify_api_key)):
    """Fetch game list from MySQL games_library."""
    try:
        rows = _mysql_query("SELECT game_title, genre, solo_multi, final_status, disc_count FROM games_library ORDER BY game_title")
        return ok({"games": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_game_library", response_model=GenericResponse, tags=["Games"], summary="Fetch game library [MySQL]")
async def api_fetch_game_library(auth=Depends(verify_api_key)):
    """Fetch full game library from MySQL."""
    try:
        rows = _mysql_query("SELECT game_title, genre, solo_multi, final_status, disc_count FROM games_library ORDER BY game_title")
        return ok({"games": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_console_games
# ═══════════════════════════════════════
@app.get("/api/fetch_console_games", response_model=GenericResponse, tags=["Games"], summary="Fetch console games [MySQL]")
async def api_fetch_console_games(auth=Depends(verify_api_key)):
    """Fetch games installed on consoles from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT console_id, console_name, game_id, game_title, genre, status, install_type, slot_position, created_at, created_at AS `date` FROM console_games ORDER BY console_id, slot_position")
        return ok({"console_games": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  get_games_on_console
# ═══════════════════════════════════════
@app.get("/api/get_games_on_console/{console_id}", response_model=GenericResponse, tags=["Games"], summary="Get games on console [MySQL]")
async def api_get_games_on_console(console_id: str, auth=Depends(verify_api_key)):
    """Get games installed on a specific console from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT game_title, genre, status, install_type, slot_position, created_at, created_at AS `date` FROM console_games WHERE console_id=%s ORDER BY slot_position",
            (console_id,))
        return ok({"console_id": console_id, "games": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  get_consoles_with_game
# ═══════════════════════════════════════
@app.get("/api/get_consoles_with_game", response_model=GenericResponse, tags=["Games"], summary="Get consoles with a game [MySQL]")
async def api_get_consoles_with_game(game_title: str = Query("", description="Game title to search"), auth=Depends(verify_api_key)):
    """Get consoles that have a specific game from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT DISTINCT console_id, console_name FROM console_games WHERE game_title=%s",
            (game_title,))
        return ok({"game_title": game_title, "consoles": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_base_rate
# ═══════════════════════════════════════
@app.get("/api/fetch_base_rate", response_model=GenericResponse, tags=["Settings"], summary="Fetch hourly base rate [MySQL]")
async def api_fetch_base_rate(auth=Depends(verify_api_key)):
    """Fetch hourly base rate from MySQL settings_config."""
    try:
        return ok(_mysql_get_setting("base_rate", 0))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_console_multiplier
# ═══════════════════════════════════════
@app.get("/api/fetch_console_multiplier/{console_id}", response_model=GenericResponse, tags=["Settings"], summary="Fetch console multiplier [MySQL]")
async def api_fetch_console_multiplier(console_id: str, auth=Depends(verify_api_key)):
    """Fetch multiplier for a console from MySQL settings JSON blob."""
    try:
        mults = _mysql_get_setting("console_multipliers", None)
        if isinstance(mults, dict):
            cid = console_id.replace(" ", "").upper()
            norm = {k.replace(" ", "").upper(): v for k, v in mults.items()}
            if cid in norm:
                return ok(float(norm[cid]))
        return ok(1.0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_new_member_defaults
# ═══════════════════════════════════════
@app.get("/api/fetch_new_member_defaults", response_model=GenericResponse, tags=["Settings"], summary="Fetch new member defaults [MySQL]")
async def api_fetch_new_member_defaults(auth=Depends(verify_api_key)):
    """Fetch default card price and base mins from MySQL settings."""
    try:
        return ok({
            "card_price": int(_mysql_get_setting("new_member_card_price", "90000")),
            "base_mins": int(_mysql_get_setting("new_member_base_mins", "600")),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_rank_thresholds
# ═══════════════════════════════════════
@app.get("/api/fetch_rank_thresholds", response_model=GenericResponse, tags=["Settings"], summary="Fetch rank thresholds [MySQL]")
async def api_fetch_rank_thresholds(auth=Depends(verify_api_key)):
    """Fetch Master and Immortal thresholds from MySQL settings."""
    try:
        return ok({
            "master_threshold": int(_mysql_get_setting("master_threshold", "300000")),
            "immortal_threshold": int(_mysql_get_setting("immortal_threshold", "1000000")),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_bonus_table
# ═══════════════════════════════════════
@app.get("/api/fetch_bonus_table", response_model=GenericResponse, tags=["Settings"], summary="Fetch bonus table [MySQL]")
async def api_fetch_bonus_table(auth=Depends(verify_api_key)):
    """Fetch bonus table from MySQL settings (list-of-dicts format)."""
    try:
        val = _mysql_get_setting("bonus_table", [])
        if not isinstance(val, list):
            return ok([])
        result = []
        for row in val:
            if isinstance(row, list) and len(row) >= 4:
                result.append({
                    "threshold": int(row[0] or 0),
                    "warrior_bonus": int(row[1] or 0),
                    "master_bonus": int(row[2] or 0),
                    "immortal_bonus": int(row[3] or 0),
                })
            elif isinstance(row, dict):
                result.append(row)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_rank_table_display
# ═══════════════════════════════════════
@app.get("/api/fetch_rank_table_display", response_model=GenericResponse, tags=["Settings"], summary="Fetch rank display table [MySQL]")
async def api_fetch_rank_table_display(auth=Depends(verify_api_key)):
    """Fetch formatted rank bonus table string from MySQL bonus_table setting."""
    try:
        val = _mysql_get_setting("bonus_table", [])
        if not isinstance(val, list) or not val:
            return ok("_(data not available)_")
        lines = [
            "%-14s %9s %9s %10s" % ("Amount (Ks)", "Warrior", "Master", "Immortal"),
            "-" * 48,
        ]
        for row in val:
            if isinstance(row, list) and len(row) >= 4:
                amt = int(row[0] or 0)
                if amt == 0:
                    continue
                lines.append("%14s  %8s  %8s  %9s" % (f"{amt:,}", f"{int(row[1] or 0):,}", f"{int(row[2] or 0):,}", f"{int(row[3] or 0):,}"))
            elif isinstance(row, dict):
                amt = int(row.get("threshold", 0))
                if amt == 0:
                    continue
                lines.append("%14s  %8s  %8s  %9s" % (f"{amt:,}", f"{row.get('warrior_bonus',0):,}", f"{row.get('master_bonus',0):,}", f"{row.get('immortal_bonus',0):,}"))
        return ok("\n".join(lines))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_alltime_effective_rate
# ═══════════════════════════════════════
@app.get("/api/fetch_alltime_effective_rate", response_model=GenericResponse, tags=["Analytics"], summary="All-time average effective rate [MySQL]")
async def api_fetch_alltime_effective_rate(auth=Depends(verify_api_key)):
    """All-time average effective rate (Ks/min) from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT SUM(total_spend) as total_spend, SUM(balance_mins) as total_mins FROM member_wallets WHERE total_spend > 0")
        r = rows[0] if rows else {"total_spend": 0, "total_mins": 0}
        total_spend = float(r["total_spend"] or 0)
        total_mins = float(r["total_mins"] or 0)
        rate = round(total_spend / total_mins, 2) if total_mins > 0 else 0.0
        return ok({"alltime_effective_rate": rate, "total_spend": total_spend, "total_mins": total_mins})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_effective_rate
# ═══════════════════════════════════════
@app.get("/api/fetch_member_effective_rate/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch member effective rate [MySQL]")
async def api_fetch_member_effective_rate(member_id: str, auth=Depends(verify_api_key)):
    """Fetch member effective rate from MySQL member_wallets."""
    try:
        rows = _mysql_query("SELECT effective_rate FROM member_wallets WHERE member_id=%s", (member_id,))
        rate = float(rows[0]["effective_rate"]) if rows and rows[0]["effective_rate"] else 1.0
        return ok({"member_id": member_id, "effective_rate": rate})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/update_member_effective_rate", response_model=GenericResponse, tags=["Members"], summary="Update member effective rate [MySQL]")
async def api_update_member_effective_rate(req: dict, auth=Depends(verify_api_key)):
    """Update member effective rate in MySQL."""
    try:
        mid = req.get("member_id", "")
        rate = req.get("effective_rate", 1.0)
        _mysql_exec("UPDATE member_wallets SET effective_rate=%s WHERE member_id=%s", (rate, mid))
        return ok({"member_id": mid, "effective_rate": rate, "updated": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  build_member_rate_dict
# ═══════════════════════════════════════
@app.get("/api/build_member_rate_dict", response_model=GenericResponse, tags=["Members"], summary="Build member rate dict [MySQL]")
async def api_build_member_rate_dict(auth=Depends(verify_api_key)):
    """Build dict of member_id to effective rate from MySQL."""
    try:
        rows = _mysql_query("SELECT member_id, effective_rate FROM member_wallets WHERE effective_rate IS NOT NULL")
        rate_dict = {r["member_id"]: float(r["effective_rate"] or 1.0) for r in rows}
        return ok({"rate_dict": rate_dict, "count": len(rate_dict)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_base_salaries
# ═══════════════════════════════════════
@app.get("/api/fetch_base_salaries", response_model=GenericResponse, tags=["Staff"], summary="Fetch base salaries [MySQL]")
async def api_fetch_base_salaries(auth=Depends(verify_api_key)):
    """Fetch base salaries from MySQL staff_records."""
    try:
        rows = _mysql_query("SELECT staff_name, base_salary, role FROM staff_records WHERE is_active=1 ORDER BY staff_name")
        return ok({"salaries": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_attendance
# ═══════════════════════════════════════
@app.get("/api/fetch_attendance/{month_str}", response_model=GenericResponse, tags=["Staff"], summary="Fetch attendance [MySQL]")
async def api_fetch_attendance(month_str: str, auth=Depends(verify_api_key)):
    """Fetch attendance for a month from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT staff_name, login_time, logout_time, date, hours_worked, status FROM attendance_log WHERE DATE_FORMAT(date, %s)=%s ORDER BY date DESC",
            ("%Y-%m", month_str))
        return ok({"attendance": rows, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/save_attendance", response_model=GenericResponse, tags=["Staff"], summary="Save attendance [MySQL]")
async def api_save_attendance(req: dict, auth=Depends(verify_api_key)):
    """Save attendance record to MySQL."""
    try:
        _mysql_exec(
            "INSERT INTO attendance_log (staff_name, login_time, date, status) VALUES (%s, NOW(), CURDATE(), %s)",
            (req.get("staff_name") or req.get("staff", ""), req.get("status", "Present")))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_salary_advances
# ═══════════════════════════════════════
@app.get("/api/fetch_salary_advances/{month_str}", response_model=GenericResponse, tags=["Staff"], summary="Fetch salary advances [MySQL]")
async def api_fetch_salary_advances(month_str: str, auth=Depends(verify_api_key)):
    """Fetch salary advances for a month from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT staff_name, amount, advance_date, repayment_status, notes FROM salary_advance WHERE DATE_FORMAT(advance_date, %s)=%s ORDER BY advance_date DESC",
            ("%Y-%m", month_str))
        return ok({"advances": rows, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_promotions_cached
# ═══════════════════════════════════════
@app.get("/api/fetch_promotions_cached", response_model=GenericResponse, tags=["Promotions"], summary="Fetch promotions [MySQL]")
async def api_fetch_promotions_cached(auth=Depends(verify_api_key)):
    """Fetch active promotions from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT id, promo_name, discount_type, discount_value, start_date, end_date, status FROM promotions WHERE status='active'")
        return ok({"promotions": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_allowed_staff_ids
# ═══════════════════════════════════════
@app.get("/api/fetch_allowed_staff_ids", response_model=GenericResponse, tags=["Staff"], summary="Fetch allowed staff IDs [MySQL]")
async def api_fetch_allowed_staff_ids(auth=Depends(verify_api_key)):
    """Fetch allowed staff Telegram user IDs from MySQL settings."""
    try:
        val = _mysql_get_setting("allowed_staff_ids", "")
        if isinstance(val, str) and val.strip():
            ids = [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]
            return ok(ids)
        return ok([])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  next_voucher / next_member_id / next_member_row_no
# ═══════════════════════════════════════
@app.get("/api/next_voucher", response_model=GenericResponse, tags=["Utility"], summary="Get next voucher number [MySQL]")
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


@app.get("/api/next_member_id", response_model=GenericResponse, tags=["Utility"], summary="Get next member ID [MySQL]")
async def api_next_member_id(auth=Depends(verify_api_key)):
    """Get next available member ID from MySQL."""
    try:
        rows = _mysql_query("SELECT MAX(CAST(REPLACE(member_id, 'PSV_A_', '') AS UNSIGNED)) as max_id FROM member_wallets")
        last = rows[0]["max_id"] if rows and rows[0]["max_id"] else 0
        return ok(f"PSV_A_{last + 1:03d}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/next_member_row_no", response_model=GenericResponse, tags=["Utility"], summary="Get next member row [MySQL]")
async def api_next_member_row_no(auth=Depends(verify_api_key)):
    """Get next member row number from MySQL."""
    try:
        rows = _mysql_query("SELECT COUNT(*) as cnt FROM member_wallets")
        count = rows[0]["cnt"] if rows else 0
        return ok(count + 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_referral_code
# ═══════════════════════════════════════
@app.get("/api/fetch_referral_code/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch referral code [MySQL]")
async def api_fetch_referral_code(member_id: str, auth=Depends(verify_api_key)):
    """Fetch referral code from MySQL."""
    try:
        rows = _mysql_query("SELECT referral_code FROM member_wallets WHERE member_id=%s", (member_id,))
        code = rows[0]["referral_code"] if rows and rows[0]["referral_code"] else ""
        return ok({"member_id": member_id, "referral_code": code})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/save_referral_code", response_model=GenericResponse, tags=["Members"], summary="Save referral code [MySQL]")
async def api_save_referral_code(req: dict, auth=Depends(verify_api_key)):
    """Save referral code to MySQL."""
    try:
        mid = req.get("member_id", "")
        code = req.get("referral_code", "")
        _mysql_exec("UPDATE member_wallets SET referral_code=%s WHERE member_id=%s", (code, mid))
        return ok({"member_id": mid, "referral_code": code})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/api/bookings/checkin", response_model=GenericResponse, tags=["Bookings"], summary="Check in a customer [MySQL]")
async def api_booking_checkin(req: dict, auth=Depends(verify_api_key)):
    """Mark a confirmed booking as checked in (Active). Updates booking status and console.
    Accepts optional consoleId to assign/override the console at checkin time."""
    try:
        booking_id = req.get("id") or req.get("booking_id")
        if not booking_id:
            return error_response(message="booking id required")
        
        rows = _mysql_query("SELECT * FROM console_booking WHERE id=%s", (booking_id,))
        if not rows:
            return error_response(message="Booking not found")
        
        booking = rows[0]
        
        # Optional console override from checkin payload
        override_console = req.get("consoleId") or req.get("console_id") or ""
        console_id = override_console if override_console else (booking.get("console_id", "") or "")
        # If console was overridden at checkin, update the booking's console_id
        if override_console and override_console != booking.get("console_id", ""):
            _mysql_exec("UPDATE console_booking SET console_id=%s WHERE id=%s", (override_console, booking_id))
        telegram_chat_id = booking.get("telegram_chat_id", "")
        
        # CI-3: Wrap status update in transaction (checked_in only marks arrival, NO timer start)
        _now_ref = now_mmt()
        _chk_conn = _mc.connect(**_MYSQL_CFG)
        _chk_conn.start_transaction()
        _chk_cur = _chk_conn.cursor()
        try:
            # Mark as checked_in (customer arrived, but session NOT started yet)
            _chk_cur.execute("UPDATE console_booking SET status='checked_in' WHERE id=%s", (booking_id,))
            # Don't start timer — session start is a separate action
            # Don't set console_status to Active — console remains Free/Reserved until session starts
            _chk_conn.commit()
        except Exception:
            _chk_conn.rollback()
            raise
        finally:
            try:
                _chk_conn.close()
            except:
                pass
        
        # ⏰ Timer is now handled by Sale Bot's _remind_loop (admin_bookings.py schedules it after checkin)
        
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
@app.post("/api/create_booking", response_model=GenericResponse, tags=["Bookings"], summary="[DEPRECATED] Create new console booking — use POST /api/bookings instead [MySQL]", deprecated=True)
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
            "INSERT INTO console_booking (console_id, member_id, booking_date, start_time, status, staff_name, notes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (console_id, member_id, now.date(), now, "Active", staff, notes))
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
        
        invalidate_cache("bookings")
        return ok({"booking_id": bk_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════


# ================================================================


# ── PATCH /api/bookings/{booking_id}/status ──
@app.patch("/api/bookings/{booking_id}/status", response_model=GenericResponse, tags=["Bookings"], summary="Update booking status (approve/reject) [MySQL]")
async def api_update_booking_status(booking_id: int, req: dict, auth=Depends(verify_api_key)):
    """Update a booking's status and optionally assign a console."""
    try:
        status = req.get("status", "")
        if status not in ("pending", "confirmed", "rejected", "pending_check_in", "checked_in", "Active", "cancelled", "Done"):
            return error_response(message=f"Invalid status: {status}")
        # HI-1: Optimistic lock — only approve pending bookings
        _app_conn = _mc.connect(**_MYSQL_CFG)
        _app_conn.start_transaction()
        _app_cur = _app_conn.cursor()
        try:
            # Allow cancel from pending/confirmed/pending_check_in/checked_in
            # Allow Active from checked_in (session start for checked-in customer)
            # All other transitions from pending only
            if status == "cancelled":
                _allowed_from = "('pending','confirmed','pending_check_in','checked_in')"
            elif status == "Active":
                _allowed_from = "('pending','checked_in')"
            else:
                _allowed_from = "('pending')"
            # For cancelled status, also stamp cancelled_at
            if status == "cancelled":
                _app_cur.execute(
                    f"UPDATE console_booking SET status=%s, cancelled_at=NOW(), staff_name=COALESCE(NULLIF(%s, ''), staff_name), notes=COALESCE(NULLIF(%s, ''), notes) WHERE id=%s AND status IN {_allowed_from}",
                    (status, req.get("staffName", ""), req.get("staffNote", ""), booking_id))
            else:
                _app_cur.execute(
                    f"UPDATE console_booking SET status=%s, staff_name=COALESCE(NULLIF(%s, ''), staff_name), notes=COALESCE(NULLIF(%s, ''), notes) WHERE id=%s AND status IN {_allowed_from}",
                    (status, req.get("staffName", ""), req.get("staffNote", ""), booking_id))
            affected = _app_cur.rowcount
            if affected == 0:
                _app_conn.rollback()
                _app_conn.close()
                return JSONResponse(content={"success": False, "message": "Booking already processed or cannot be cancelled"}, status_code=409)
            # ✅ Overlap check BEFORE confirming — prevent double-booking (2026-06-19)
            # 🐛 Fix Jun 25: Use requested console_id for overlap check, not existing one
            if status == "confirmed":
                _bk = _mysql_query_one(
                    "SELECT console_id, start_time, end_time FROM console_booking WHERE id=%s",
                    (booking_id,)
                )
                if _bk and _bk.get("start_time") and _bk.get("end_time"):
                    _check_cid = req.get("consoleId") or _bk.get("console_id", "")
                    if _check_cid:
                        _app_cur.execute(
                            "SELECT id FROM console_booking WHERE console_id=%s"
                            " AND status IN ('confirmed','pending','pending_check_in','checked_in','Active')"
                            " AND id != %s"
                            " AND NOT (end_time <= %s OR start_time >= %s)"
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
                _bk_old = _mysql_query_one("SELECT console_id FROM console_booking WHERE id=%s", (booking_id,))
                if _bk_old:
                    _old_console_id = _bk_old.get("console_id", "")
                _app_cur.execute("UPDATE console_booking SET console_id=%s WHERE id=%s", (req["consoleId"], booking_id))
            # Sync console_status in same transaction — prevent drift on partial failure
            _cid = req.get("consoleId") or _mysql_query_one("SELECT console_id FROM console_booking WHERE id=%s", (booking_id,))
            if _cid:
                _console_id = _cid if isinstance(_cid, str) else _cid.get("console_id", "")
                if status == "confirmed":
                    # Booking confirmed — do NOT hold the console (keep Free for walk-ins)
                    # The booking itself shows as "Reserved" in displays, but the physical
                    # console stays Free so walk-in customers can still use it until booked time.
                    pass
                elif status == "Active":
                    # Checkin bind / session start — mark console as Active
                    _bk_info = _mysql_query_one(
                        "SELECT staff_name, member_id, game_name FROM console_booking WHERE id=%s",
                        (booking_id,)
                    ) or {}
                    _member_name = _bk_info.get("staff_name", "") or _bk_info.get("member_id", "") or ""
                    _now_mmt_str = now_mmt().strftime('%Y-%m-%d %H:%M:%S')
                    _app_cur.execute(
                        "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                        (_member_name, _bk_info.get("game_name", "") or "", _now_mmt_str, _console_id)
                    )
                elif status in ("rejected", "cancelled"):
                    _app_cur.execute("UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s", (_console_id,))
            _app_conn.commit()
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
            except:
                pass
        invalidate_cache("bookings")
        return ok({"booking_id": booking_id, "status": status})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/bookings/broadcast-targets ── (must be BEFORE {booking_id} catch-all)
@app.get("/api/bookings/broadcast-targets", response_model=GenericResponse, tags=["Bookings"], summary="Get broadcast targets [MySQL]")
async def api_bookings_broadcast_targets(auth=Depends(verify_api_key)):
    """Get broadcast targets (distinct Telegram chat IDs from customer bot bookings)."""
    try:
        rows = _mysql_query(
            "SELECT DISTINCT telegram_chat_id FROM console_booking "
            "WHERE telegram_chat_id IS NOT NULL AND telegram_chat_id != ''"
        )
        telegram_ids = [r["telegram_chat_id"] for r in rows]
        return ok({"telegram_ids": telegram_ids, "count": len(telegram_ids)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/bookings/{booking_id} ──
@app.get("/api/bookings/{booking_id}", response_model=GenericResponse, tags=["Bookings"], summary="Get single booking by ID [MySQL]")
async def api_get_booking(booking_id: int, auth=Depends(verify_api_key)):
    """Fetch a single booking's full details."""
    try:
        row = _mysql_query_one(
            "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking WHERE id=%s",
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
            except:
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
        _staff_name = row.get("staff_name", "") or ""
        _phone = row.get("phone", "") or ""
        if not _staff_name.strip():
            _staff_name = _phone
        return ok({
            "booking": {
                "id": row.get("id"),
                "customerName": _staff_name,
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
            }
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#  GET /api/bookings - list bookings by status
# ================================================================
@app.get("/api/bookings", response_model=GenericResponse, tags=["Bookings"], summary="List bookings by status [MySQL]")
async def api_get_bookings(status: str = "", member_id: str = Query(""), date: str = Query(""), auth=Depends(verify_api_key)):
    """List bookings with optional status, member_id, and date filters."""
    try:
        sql = "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name, created_at, cancelled_at FROM console_booking"
        where_clauses = []
        params = []
        if status:
            where_clauses.append("status=%s")
            params.append(status)
        if member_id:
            where_clauses.append("member_id=%s")
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
            where_clauses.append("booking_date=%s")
            params.append(_clean_date)
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        # Server-side filter: cancelled bookings — only show within last 2 hours
        if status == "cancelled":
            sql += " AND " if where_clauses else " WHERE "
            sql += "cancelled_at >= NOW() - INTERVAL 2 HOUR"
        sql += " ORDER BY booking_date DESC, start_time DESC"
        rows = _mysql_query(sql, tuple(params)) if params else _mysql_query(sql)
        if not rows:
            return ok({"bookings": []})
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
            _phone = r.get("phone", "") or r.get("telegram_chat_id", "") or ""
            _staff_name = r.get("staff_name","") or ""
            if not _staff_name.strip():
                _staff_name = _phone
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
                "customerName": _staff_name,
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
                "created_at": _created_str
            })
        return ok({"bookings": normalized})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/search-bookings", response_model=GenericResponse, tags=["Bookings"], summary="Search bookings by telegram_chat_id [MySQL]")
async def api_search_bookings(telegram_chat_id: str = Query("", description="Telegram chat ID of customer"), date: str = Query("", description="Filter by booking date (YYYY-MM-DD)"), auth=Depends(verify_api_key)):
    """Search bookings by telegram chat ID and/or date."""
    try:
        # Build dynamic WHERE clause
        sql = "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking WHERE 1=1"
        params = []
        if telegram_chat_id:
            sql += " AND telegram_chat_id=%s"
            params.append(telegram_chat_id)
        if date:
            # Normalize date formats to YYYY-MM-DD
            from datetime import datetime as _dt_parse
            _cleaned_date = date
            for _fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d"):
                try:
                    _cleaned_date = _dt_parse.strptime(date, _fmt).strftime("%Y-%m-%d")
                    break
                except:
                    continue
            sql += " AND booking_date=%s"
            params.append(_cleaned_date)
        sql += " ORDER BY booking_date DESC, start_time DESC"
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
            })
        return JSONResponse(content={"success": True, "data": {"bookings": normalize_list(normalized), "total": len(normalized)}})
    except Exception as e:
        logger.error("api_search_bookings: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.get("/api/available-slots", response_model=GenericResponse, tags=["Bookings"], summary="Get available time slots for a date (with console count) [MySQL]")
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
            "SELECT console_id, start_time, end_time, duration_mins, status FROM console_booking "
            "WHERE booking_date=%s AND status NOT IN ('cancelled', 'done', 'rejected') "
            "ORDER BY start_time",
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

@app.put("/api/end_booking/{booking_id}", response_model=GenericResponse, tags=["Bookings"], summary="End booking [MySQL]")
async def api_end_booking(booking_id: str, auth=Depends(verify_api_key)):
    """Mark a booking as Done in MySQL. Handles both Active and checked_in bookings."""
    try:
        # CI-3: Wrap end update + console_status update in transaction
        now = now_mmt()
        _end_conn = _mc.connect(**_MYSQL_CFG)
        _end_conn.start_transaction()
        _end_cur = _end_conn.cursor(dictionary=True)
        try:
            # Check current status before deciding console cleanup
            _end_cur.execute("SELECT console_id, status FROM console_booking WHERE id=%s", (booking_id,))
            row = _end_cur.fetchone()
            if not row:
                _end_conn.rollback()
                _end_conn.close()
                raise HTTPException(status_code=404, detail="Booking not found")
            current_status = row.get("status", "")
            _console_id = row.get("console_id")  # save for _sync_console_status
            _end_cur.execute("UPDATE console_booking SET end_time=%s, status='Done' WHERE id=%s", (now, booking_id))
            # Only free console if booking was Active (session was actually running)
            # checked_in bookings never started the session — console was never marked Active
            if current_status == "Active" and _console_id:
                _end_cur.execute(
                    "UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s",
                    (_console_id,)
                )
            _end_conn.commit()
            # Safety: sync console_status in case of edge-case drift
            if _console_id:
                _sync_console_status(_console_id)
            # Auto-cancel any remaining stock holds for this booking (food not sold/released)
            _mysql_exec("UPDATE stock_hold SET status = 'cancelled', updated_at = NOW() WHERE booking_id = %s AND status = 'held'", (booking_id,))
        except Exception:
            _end_conn.rollback()
            raise
        finally:
            try:
                _end_conn.close()
            except:
                pass
        invalidate_cache("bookings")
        return ok({"booking_id": booking_id, "status": "Done"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════



@app.post("/api/consoles/start-session", response_model=GenericResponse, tags=["Consoles"], summary="Start session, auto-checkin confirmed booking [MySQL]")
async def api_start_console_session(req: dict, auth=Depends(verify_api_key)):
    """Start a session on a console. If there is a Confirmed booking, auto-checkin and link it."""
    try:
        console_id = (req.get('console_id') or '').strip()
        member_id = req.get('member_id') or req.get('memberId') or 'Guest'
        game_name = req.get('game_name') or req.get('gameName') or ''
        duration_mins = req.get('duration_mins') if req.get('duration_mins') is not None else (req.get('durationMins') if req.get('durationMins') is not None else 60)
        if duration_mins < 1 or duration_mins > 1440:
            return error_response(message="durationMins must be between 1 and 1440")
        if not console_id:
            return error_response(message='console_id required')

        # HI-2: Only auto-checkin if booking time is within ±30 minutes
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        _now_mmt = now_mmt()
        bk = _mysql_query_one(
            "SELECT id, member_id, game_name, duration_mins, telegram_chat_id, staff_name, phone FROM console_booking "
            "WHERE console_id=%s AND status IN ('confirmed','checked_in') AND DATE(booking_date)=%s "
            "AND start_time <= DATE_ADD(%s, INTERVAL 30 MINUTE) "
            "AND end_time >= %s "
            "ORDER BY created_at DESC LIMIT 1",
            (console_id, today, _now_mmt, _now_mmt)
        )

        # CI-3: Wrap conflict check + console_status update in transaction with row locking
        _now_ref = now_mmt()
        # No Timer (duration=0): hold console with 6hr window to prevent overbooking
        _hold_window = 360 if duration_mins == 0 else duration_mins
        booking_id = None
        _start_conn = _mc.connect(**_MYSQL_CFG)
        _start_conn.start_transaction()
        _start_cur = _start_conn.cursor(dictionary=True)
        try:
            # C1: Lock console_status row — prevent double-start race
            _start_cur.execute(
                "SELECT status FROM console_status WHERE console_id=%s FOR UPDATE",
                (console_id,)
            )
            _cs_locked = _start_cur.fetchone()
            if _cs_locked and _cs_locked['status'] == 'Active':
                _start_conn.rollback()
                _start_conn.close()
                return error_response(message=f"Console {console_id} is already Active")

            # C2: Overlap check — only block if overlap > 30 min (grace period)
            if bk:
                _start_cur.execute(
                    "SELECT id, status FROM console_booking WHERE console_id=%s"
                    " AND id != %s"
                    " AND status IN ('pending','confirmed','pending_check_in','checked_in','Active')"
                    " AND start_time < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND end_time > DATE_ADD(%s, INTERVAL 30 MINUTE)"
                    " LIMIT 1 FOR UPDATE",
                    (console_id, bk["id"], _now_ref, _hold_window, _now_ref)
                )
            else:
                _start_cur.execute(
                    "SELECT id, status FROM console_booking WHERE console_id=%s"
                    " AND status IN ('pending','confirmed','pending_check_in','checked_in','Active')"
                    " AND start_time < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND end_time > DATE_ADD(%s, INTERVAL 30 MINUTE)"
                    " LIMIT 1 FOR UPDATE",
                    (console_id, _now_ref, _hold_window, _now_ref)
                )
            _overlap = _start_cur.fetchone()
            if _overlap:
                _start_conn.rollback()
                _start_conn.close()
                return JSONResponse(content={
                    "success": False,
                    "error": f"\u23f0 Console {console_id} has a {_overlap['status']} booking (BK#{_overlap['id']}) at this time slot",
                    "conflict_id": _overlap["id"]
                }, status_code=409)
            if bk:
                # Use booking data (autofill)
                if bk['member_id']:
                    member_id = bk['member_id']
                else:
                    # Try to resolve member_id from booking's phone
                    bk_phone = bk.get("phone", "")
                    if bk_phone:
                        _start_cur.execute("SELECT member_id FROM member_wallets WHERE phone=%s AND member_id IS NOT NULL AND member_id != ''", (bk_phone,))
                        _pm = _start_cur.fetchone()
                        if _pm:
                            member_id = _pm["member_id"]
                game_name = bk['game_name'] or game_name
                # 🐛 Fix: Respect staff's chosen duration. If staff picked a different
                # duration than the booking (e.g., booking=60min but launch=180min),
                # use the staff's choice and update the booking.
                _bk_dur = bk['duration_mins'] or 0
                if duration_mins and abs(duration_mins - _bk_dur) > 5:
                    # Staff chose a different duration — use theirs
                    pass  # keep duration_mins from request
                else:
                    duration_mins = _bk_dur if _bk_dur > 0 else duration_mins
                booking_id = bk['id']
                _dur_end2 = 360 if duration_mins == 0 else duration_mins
                _start_cur.execute("UPDATE console_booking SET status='Active', duration_mins=%s, start_time=%s, end_time=DATE_ADD(%s, INTERVAL %s MINUTE) WHERE id=%s", (duration_mins, _now_ref, _now_ref, _dur_end2, booking_id))
                logger.info(f"Auto-checkin booking #{booking_id} for console {console_id}")
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
            else:
                _dur_end5 = 360 if duration_mins == 0 else duration_mins
                _start_cur.execute(
                    "INSERT INTO console_booking (console_id, member_id, status, booking_date, start_time, duration_mins, end_time, game_name) VALUES (%s,%s,'Active',%s,%s,%s,DATE_ADD(%s, INTERVAL %s MINUTE),%s)",
                    (console_id, member_id, today, _now_ref, duration_mins, _now_ref, _dur_end5, game_name)
                )
                booking_id = _start_cur.lastrowid
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass

            _start_cur.execute(
                "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                (member_id, game_name, _now_ref, console_id)
            )
            _start_conn.commit()
            # Safety: sync console_status to ensure no drift
            try:
                _sync_console_status(console_id)
            except Exception:
                pass
        except Exception:
            _start_conn.rollback()
            raise
        finally:
            try:
                _start_conn.close()
            except:
                pass

        return ok({
            "booking_id": booking_id,
            "console_id": console_id,
            "member_id": member_id,
            "status": "Active",
            "linked_booking": True if bk else False,
        })
    except Exception as e:
        logger.exception(f"start-session error: {e}")
        return error_response(message=str(e))

@app.get("/api/get-confirmed-booking", response_model=GenericResponse, tags=["Bookings"], summary="Get confirmed bookings for a console [MySQL]")
async def api_confirmed_bookings_for_console(console_id: str, auth=Depends(verify_api_key)):
    """Get today's Confirmed booking for a console (for auto-checkin)."""
    try:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        row = _mysql_query_one(
            "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking "
            "WHERE console_id=%s AND status IN ('confirmed','checked_in') AND DATE(booking_date)=%s ORDER BY created_at DESC LIMIT 1",
            (console_id, today)
        )
        if not row:
            return ok({"booking": None})
        for k in ("start_time", "end_time", "created_at"):
            if k in row and row[k] is not None:
                row[k] = row[k].isoformat() if hasattr(row[k], "isoformat") else str(row[k])
        return ok({"booking": row})
    except Exception as e:
        logger.exception(f"confirmed-by-console error: {e}")
        return ok({"booking": None})

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
               FROM console_booking w
               WHERE w.booking_date=%s AND w.status='Waiting' AND w.console_id=%s
               ORDER BY w.created_at ASC LIMIT 1""",
            (booking_date, console_id))
        if not entry and console_type:
            entry = _mysql_query_one(
                """SELECT w.id, w.staff_name, w.member_id, w.telegram_chat_id, w.console_id
                   FROM console_booking w
                   JOIN console_status cs ON cs.console_id = w.console_id
                   WHERE w.booking_date=%s AND w.status='Waiting' AND cs.console_type=%s
                   ORDER BY w.created_at ASC LIMIT 1""",
                (booking_date, console_type))
        if not entry:
            return False
        _mysql_exec(
            "UPDATE console_booking SET status='Notified', notes=CONCAT(IFNULL(notes,''), ' | Auto-notified: slot freed') WHERE id=%s",
            (entry["id"],))
        chat_id = entry.get("telegram_chat_id") or entry.get("member_id", "")
        customer_name = entry.get("staff_name") or entry.get("member_id", "Customer")
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
                    except:
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

@app.post("/api/bookings/cancel", response_model=GenericResponse, tags=["Bookings"], summary="Cancel a booking by ID [MySQL]")
async def api_booking_cancel(req: dict, auth=Depends(verify_api_key)):
    """Cancel a booking. Frees console if reserved."""
    try:
        booking_id = req.get("id") or req.get("booking_id")
        if not booking_id:
            return error_response(message="booking id required")
        
        rows = _mysql_query("SELECT * FROM console_booking WHERE id=%s", (booking_id,))
        if not rows:
            return error_response(message="Booking not found")
        
        booking = rows[0]
        console_id = booking.get("console_id", "")
        telegram_chat_id = booking.get("telegram_chat_id", "")
        
        # CI-2 & CI-3: Only cancel non-Active/non-Done bookings, with transaction
        _cancel_conn = _mc.connect(**_MYSQL_CFG)
        _cancel_conn.start_transaction()
        _cancel_cur = _cancel_conn.cursor()
        try:
            _cancel_cur.execute(
                "UPDATE console_booking SET status='cancelled', cancelled_at=NOW() WHERE id=%s AND status NOT IN ('Active','Done')",
                (booking_id,)
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
            except:
                pass

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
                    except:
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
@app.post("/api/sessions/start", response_model=GenericResponse, tags=["Sessions"], summary="Start a walk-in session (staff only) [MySQL]")
async def api_sessions_start(req: dict, auth=Depends(verify_api_key)):
    """Start a walk-in session on a console. Staff-only endpoint.
    
    Accepts: {console_id, member_id, member_name, duration_mins, booking_date?, linked_booking_id?}
    - If linked_booking_id provided: find checked_in booking, transition to Active with timer
    - console_status check (already Active?)
    - Conflict check (any overlapping Active booking on same console)
    - Create booking row + sync console_status
    - ALL inside a transaction with FOR UPDATE
    """
    try:
        console_id = (req.get("console_id") or "").strip()
        member_id = req.get("member_id") or req.get("memberId") or "Guest"
        member_name = req.get("member_name") or req.get("memberName") or member_id
        game_name = req.get("game_name") or req.get("gameName") or ""
        duration_mins = req.get("duration_mins")
        if duration_mins is None:
            duration_mins = req.get("durationMins", 60)
        try:
            duration_mins = int(duration_mins)
        except (ValueError, TypeError):
            return JSONResponse(content={"success": False, "error": "duration_mins must be a number"}, status_code=400)
        
        MAX_SESSION_MINS = 1440
        if duration_mins < 0 or duration_mins > MAX_SESSION_MINS:
            return JSONResponse(content={"success": False, "error": f"duration_mins must be 0-{MAX_SESSION_MINS} (0=no timer)"}, status_code=400)
        
        if not console_id:
            return JSONResponse(content={"success": False, "error": "console_id required"}, status_code=400)
        
        # Optional: link a checked-in booking to this session
        linked_booking_id = req.get("linked_booking_id")
        linked_booking = None
        if linked_booking_id:
            try:
                linked_booking_id = int(linked_booking_id)
            except (ValueError, TypeError):
                return JSONResponse(content={"success": False, "error": "linked_booking_id must be an integer"}, status_code=400)
            linked_booking = _mysql_query_one(
                "SELECT * FROM console_booking WHERE id=%s AND status='checked_in'",
                (linked_booking_id,)
            )
            if not linked_booking:
                return JSONResponse(content={"success": False, "error": f"Booking #{linked_booking_id} not found or not in checked_in status"}, status_code=400)
            # Autofill from linked booking
            if not member_id or member_id == "Guest":
                member_id = linked_booking.get("member_id", "") or member_id
            if not game_name:
                game_name = linked_booking.get("game_name", "") or game_name
            if duration_mins <= 0:
                _lk_dur = linked_booking.get("duration_mins") or 0
                duration_mins = _lk_dur if _lk_dur > 0 else 0  # keep 0=No Timer if linked booking also has no duration
        
        booking_date = req.get("booking_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        # Check if console already Active
        cs = _mysql_query_one("SELECT status FROM console_status WHERE console_id=%s", (console_id,))
        if cs and cs["status"] == "Active":
            return JSONResponse(content={"success": False, "error": f"Console {console_id} is already Active"}, status_code=409)
        
        _now_ref = now_mmt()
        
        # CI-3: ALL inside transaction with FOR UPDATE
        conn = _mc.connect(**_MYSQL_CFG)
        conn.start_transaction()
        cur = conn.cursor(dictionary=True)
        try:
            # FOR UPDATE: lock the console_status row to prevent race conditions
            cur.execute("SELECT status FROM console_status WHERE console_id=%s FOR UPDATE", (console_id,))
            cs_locked = cur.fetchone()
            if cs_locked and cs_locked["status"] == "Active":
                conn.rollback()
                conn.close()
                return JSONResponse(content={"success": False, "error": f"Console {console_id} is already Active"}, status_code=409)
            
            # Conflict check: only block if another Active booking exists AND hasn't ended yet
            cur.execute(
                "SELECT id FROM console_booking WHERE console_id=%s AND status='Active' AND (end_time IS NULL OR end_time > %s) LIMIT 1 FOR UPDATE",
                (console_id, _now_ref)
            )
            active_conflict = cur.fetchone()
            if active_conflict:
                conn.rollback()
                conn.close()
                return JSONResponse(content={"success": False, "error": f"Console {console_id} already has active session (BK#{active_conflict['id']})"}, status_code=409)

            # H2: Check for pending/confirmed/checked_in bookings — block only if overlap > 30 min (grace period)
            # No Timer (duration=0): use 480 min (8 hr max session) as conflict window
            # Exclude linked_booking itself (we're transitioning IT to Active)
            _conflict_dur = duration_mins if duration_mins > 0 else 480
            if linked_booking:
                cur.execute(
                    "SELECT id, status FROM console_booking WHERE console_id=%s"
                    " AND id != %s"
                    " AND status IN ('pending','confirmed','pending_check_in','checked_in')"
                    " AND start_time < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND end_time > DATE_ADD(%s, INTERVAL 30 MINUTE)"
                    " LIMIT 1 FOR UPDATE",
                    (console_id, linked_booking_id, _now_ref, _conflict_dur, _now_ref)
                )
            else:
                cur.execute(
                    "SELECT id, status FROM console_booking WHERE console_id=%s"
                    " AND status IN ('pending','confirmed','pending_check_in','checked_in')"
                    " AND start_time < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND end_time > DATE_ADD(%s, INTERVAL 30 MINUTE)"
                    " LIMIT 1 FOR UPDATE",
                    (console_id, _now_ref, _conflict_dur, _now_ref)
                )
            _warn_bk = cur.fetchone()
            if _warn_bk:
                conn.rollback()
                conn.close()
                return JSONResponse(content={
                    "success": False,
                    "error": f"Cannot start session on {console_id} — {_warn_bk['status']} booking BK#{_warn_bk['id']} exists at this time"
                }, status_code=409)
            
            telegram_chat_id = ""
            if linked_booking:
                # Transition linked booking from checked_in → Active with timer
                telegram_chat_id = linked_booking.get("telegram_chat_id", "") or ""
                _dur_end3 = 360 if duration_mins == 0 else duration_mins
                cur.execute(
                    "UPDATE console_booking SET status='Active', start_time=%s, end_time=DATE_ADD(%s, INTERVAL %s MINUTE), duration_mins=%s, game_name=COALESCE(NULLIF(%s, ''), game_name), console_id=%s, telegram_chat_id=COALESCE(NULLIF(%s, ''), telegram_chat_id) WHERE id=%s",
                    (_now_ref, _now_ref, _dur_end3, duration_mins, game_name, console_id, telegram_chat_id, linked_booking_id)
                )
                booking_id = linked_booking_id
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
            else:
                # Create booking with end_time
                _dur_end4 = 360 if duration_mins == 0 else duration_mins
                cur.execute(
                    "INSERT INTO console_booking (console_id, member_id, status, booking_date, start_time, end_time, duration_mins, game_name) VALUES (%s, %s, 'Active', %s, %s, DATE_ADD(%s, INTERVAL %s MINUTE), %s, %s)",
                    (console_id, member_id, booking_date, _now_ref, _now_ref, _dur_end4, duration_mins, game_name)
                )
                booking_id = cur.lastrowid
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
            
            # Sync console_status
            cur.execute(
                "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                (member_id, game_name, _now_ref, console_id)
            )
            
            conn.commit()
            # Safety: sync console_status to ensure no drift
            try:
                _sync_console_status(console_id)
            except Exception:
                pass
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                conn.close()
            except:
                pass
        
        return ok({
            "booking_id": booking_id,
            "console_id": console_id,
            "member_id": member_id,
            "member_name": member_name,
            "status": "Active",
            "duration_mins": duration_mins,
            "linked_booking": bool(linked_booking),
            "telegram_chat_id": telegram_chat_id if linked_booking else "",
            "warning": None,  # H2: conflict now blocks instead of warning
        })
    except Exception as e:
        logger.exception(f"sessions/start error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  MUTATION — move active session to another console
# ═══════════════════════════════════════
@app.post("/api/sessions/move", response_model=GenericResponse, tags=["Sessions"], summary="Move a booking (Active/confirmed/checked_in) to another console [MySQL]")
async def api_sessions_move(req: dict, auth=Depends(verify_api_key)):
    """Move a booking to another console.
    
    Request: { "booking_id": int, "to_console_id": str }
    
    - Supports Active, confirmed, and checked_in bookings
    - Active: full swap (console_status + timers)
    - Confirmed/checked_in: just updates console_id (no console_status)
    """
    try:
        booking_id = req.get("booking_id")
        to_console_id = (req.get("to_console_id") or "").strip()
        
        if not booking_id or not to_console_id:
            return error_response(message="booking_id and to_console_id are required")
        
        _now = now_mmt()
        _conn = _mc.connect(**_MYSQL_CFG)
        _conn.start_transaction()
        _cur = _conn.cursor(dictionary=True)
        try:
            # C1: Lock source booking row (Active, confirmed, or checked_in)
            _cur.execute(
                "SELECT id, console_id, member_id, start_time, duration_mins, game_name, status FROM console_booking WHERE id=%s AND status IN ('Active','confirmed','checked_in') FOR UPDATE",
                (booking_id,)
            )
            _bk = _cur.fetchone()
            if not _bk:
                _conn.rollback()
                return error_response(message=f"Booking {booking_id} not found or cannot be moved (must be Active, confirmed, or checked_in)")
            
            _is_active = _bk["status"] == "Active"
            _from_cid = _bk["console_id"]
            if _from_cid == to_console_id:
                _conn.rollback()
                return error_response(message=f"Booking is already on {to_console_id}")
            
            if _is_active:
                # C2-Active: Lock target console_status — must exist and be Free
                _cur.execute(
                    "SELECT status FROM console_status WHERE console_id=%s FOR UPDATE",
                    (to_console_id,)
                )
                _tgt = _cur.fetchone()
                if not _tgt:
                    _conn.rollback()
                    return error_response(message=f"Console {to_console_id} does not exist")
                if _tgt["status"] != "Free":
                    _conn.rollback()
                    return error_response(message=f"Console {to_console_id} is not Free (currently: {_tgt['status']})")
                
                # ── Execute swap ──
                # 1. Update booking: change console_id
                _cur.execute(
                    "UPDATE console_booking SET console_id=%s WHERE id=%s",
                    (to_console_id, booking_id)
                )
                # 2. Free up old console
                _cur.execute(
                    "UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s",
                    (_from_cid,)
                )
                # 3. Activate new console
                _cur.execute(
                    "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                    (_bk["member_id"], _bk["game_name"], _bk["start_time"], to_console_id)
                )
                
                # Cancel old API timer and schedule new one
                if _bk["duration_mins"] and _bk["duration_mins"] > 0:
                    try:
                        _old_timer = _active_timers.pop(booking_id, None)
                        if _old_timer:
                            _old_timer["task"].cancel()
                        # Normalize start_time to MMT-aware (DB returns naive)
                        _bk_st = _bk["start_time"]
                        if hasattr(_bk_st, 'tzinfo') and _bk_st.tzinfo is None:
                            from datetime import timezone as _tz, timedelta as _td
                            _bk_st = _bk_st.replace(tzinfo=_tz(_td(hours=6, minutes=30)))
                        schedule_session_timer(booking_id, to_console_id, _bk["member_id"], _bk_st, _bk["duration_mins"])
                    except Exception:
                        pass  # timer failure is non-critical
            else:
                # Non-active (confirmed/checked_in): just update console_id — no console_status swap
                _cur.execute(
                    "UPDATE console_booking SET console_id=%s WHERE id=%s",
                    (to_console_id, booking_id)
                )
            
            _conn.commit()
            
            invalidate_cache("bookings")
            
            logger.info(f"Session moved: BK#{booking_id} {_from_cid} -> {to_console_id}")
            return ok({
                "booking_id": booking_id,
                "from_console": _from_cid,
                "to_console": to_console_id,
                "member_id": _bk["member_id"],
                "moved": True
            })
        except Exception:
            _conn.rollback()
            raise
        finally:
            try: _conn.close()
            except: pass
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"sessions/move error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/bookings", response_model=GenericResponse, tags=["Bookings"], summary="Create booking from customer bot payload [MySQL]")
async def api_bookings_create(req: dict, auth=Depends(verify_api_key)) -> JSONResponse:
    """Create a booking — customer bot format ONLY (staff use POST /api/sessions/start)."""
    try:
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
        logging.warning("api_bookings_create: req keys=%s", list(req.keys()))
        
        # Detect customer bot format (has customerName)
        customer_name = req.get("customerName", "")
        logging.warning("api_bookings_create: customer_name=%s date=%s time=%s console=%s", 
                        customer_name, req.get("date","?"), req.get("timeSlot","?"), req.get("console_id","?"))
        if customer_name:
            # Customer bot format
            telegram_chat_id = req.get("telegramChatId", "")
            phone = req.get("phone", "")
            # Fallback: look up telegram_chat_id from phone if not provided in request
            if not telegram_chat_id and phone:
                _tg = _mysql_query_one(
                    "SELECT telegram_chat_id FROM console_booking WHERE phone=%s AND telegram_chat_id IS NOT NULL AND telegram_chat_id != '' ORDER BY id DESC LIMIT 1",
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
                except:
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
            conn = _mc.connect(**_MYSQL_CFG)
            conn.start_transaction()
            cur = conn.cursor(dictionary=True)
            try:
                # ── Slot conflict check with FOR UPDATE locking ──
                # CI-5: Conflict check — find ALL busy consoles (not just first)
                _conflict_ids = set()
                if console_id:
                    # Specific console: check this one directly
                    cur.execute(
                        "SELECT id FROM console_booking WHERE console_id=%s AND status IN ('pending','confirmed','pending_check_in','checked_in','Active') AND start_time < %s AND end_time > %s LIMIT 1 FOR UPDATE",
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
                            f"SELECT DISTINCT console_id FROM console_booking WHERE console_id IN ({_placeholders}) AND status IN ('pending','confirmed','pending_check_in','checked_in','Active') AND start_time < %s AND end_time > %s FOR UPDATE",
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

                # Resolve actual member_id from phone or telegram_chat_id
                bk_member_id = ""
                if phone:
                    cur.execute("SELECT member_id FROM member_wallets WHERE phone=%s AND member_id IS NOT NULL AND member_id != ''", (phone,))
                    _pm_rows = cur.fetchall()
                    if _pm_rows:
                        bk_member_id = _pm_rows[0]["member_id"]
                if not bk_member_id:
                    cur.execute("SELECT member_id FROM member_wallets WHERE member_id=%s", (telegram_chat_id,))
                    _pm_rows = cur.fetchall()
                    if _pm_rows:
                        bk_member_id = _pm_rows[0]["member_id"]

                cur.execute(
                    "INSERT INTO console_booking (console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (console_id, bk_member_id, _parsed_date, start_dt, end_dt, _bk_status, customer_name, notes, telegram_chat_id, duration_mins, phone, game_name)
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
                except:
                    pass

            # Sync console_status after commit (derives correct status: Active > Reserved > Free)
            if console_id:
                try:
                    _sync_console_status(console_id)
                except Exception:
                    pass

            # Send admin notification (fire-and-forget — don't block response)
            import threading
            threading.Thread(target=_notify_booking_received, args=(customer_name, booking_date_str, time_slot, console_type, duration_mins, game_name, phone, bk_id, console_id), daemon=True).start()

            return ok({"id": bk_id, "message": "Booking created"})
        else:
            # No customerName — staff should use POST /api/sessions/start
            return JSONResponse(content={"success": False, "error": "Staff bookings must use POST /api/sessions/start"}, status_code=400)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))





# ═══════════════════════════════════════


def _notify_booking_received(name, date, time_slot, console_type, duration, game, phone, bk_id, console_id=""):
    """Notify admin via Telegram about new customer booking. Use Sale Bot to approve."""
    import json, urllib.request, os
    token = os.environ.get("CUSTOMER_BOT_TOKEN", os.environ.get("BOT_TOKEN", ""))
    chat_id = "-1003686032747"
    if not token or not chat_id:
        return
    console_label = f" {console_type}"
    if console_id:
        console_label = f" {console_id} ({console_type})"
    msg = ("\U0001f195 *New Booking Request #" + str(bk_id) + "*\n\n"
           + "\U0001f464 " + name + "\n\U0001f4de " + phone + "\n\U0001f4c5 " + date + " \u23f0 " + time_slot
           + "\n\U0001f3ae" + console_label + "\n\u23f1 " + str(duration) + " min\n\U0001f579 " + (game if game else "\u2014")
           + "\n\n_Customer မှ booking တင်ထားပါသည်။_\n"
           + "_(Sale Bot → Customer Booking မှ Approve/Reject ပြုလုပ်ပါ)_")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=5)
    except Exception as e:
        logger.warning("[NOTIFY] Failed to send booking notification for #%s: %s", bk_id, e)


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
            "acm acc": "ACM's Acc",
            "acm_acc": "ACM's Acc",
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
@app.post("/api/topup/log", response_model=GenericResponse, tags=["Topup"], summary="Log a top-up transaction and update wallet")
async def api_topup_log(req: dict, auth=Depends(verify_api_key)) -> JSONResponse:
    """Log a top-up transaction into topup_log table + update member wallet."""
    try:
        member_id = req.get("member_id", "")
        amount = req.get("amount", 0)
        mins_added = req.get("mins_added", 0)
        pm = req.get("payment_method") or f"Kpay:{req.get('kpay',0)}/Cash:{req.get('cash',0)}"
        staff = req.get("staff_name") or req.get("staff", "")

        logger.info("Topup: member=%s amount=%s mins=%s", member_id, amount, mins_added)

        current = _mysql_query_one(
            "SELECT balance_mins, total_bought_mins, total_spend FROM member_wallets WHERE member_id=%s",
            (member_id,))
        bal_before = current["balance_mins"] if current else 0
        bal_after = bal_before + mins_added
        bought = (current["total_bought_mins"] if current else 0) + mins_added
        new_spend = (current["total_spend"] if current else 0) + amount

        _mysql_exec(
            "INSERT INTO topup_log (member_id, amount, mins_added, topup_date, staff_name, payment_method, balance_before, balance_after, balance_mins_before, balance_mins_after) VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s)",
            (member_id, amount, mins_added, staff, pm, amount, amount, bal_before, bal_after))

        _mysql_exec(
            "INSERT INTO member_wallets (member_id, balance_mins, total_bought_mins, total_spend) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE balance_mins=%s, total_bought_mins=%s, total_spend=%s, last_updated=NOW()",
            (member_id, bal_after, bought, new_spend, bal_after, bought, new_spend))

        # 🆕 Record cash_movements for top-up income
        if float(amount) > 0:
            try:
                _parse_pm_and_inject(amount, pm, f"Topup {member_id} + {mins_added} mins", staff)
            except Exception as _e:
                logger.warning("Topup cash_movements failed: %s", _e)
        # ✅ Also create sales_daily record for topup
        if float(amount) > 0:
            try:
                _mysql_exec(
                    "INSERT INTO sales_daily (sale_date, member_id, amount, gross, net, payment_method, notes, staff_name) "
                    "VALUES (CURDATE(), %s, %s, %s, %s, %s, %s, %s)",
                    (member_id, amount, amount, amount, pm, f"Topup {mins_added} mins", staff))
            except Exception as _e:
                logger.warning("Topup sales_daily failed: %s", _e)

        return ok({"success": True, "balance_mins": bal_after, "total_spend": new_spend})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
#  MUTATION — receipt (bot push)
# ================================================================
@app.post("/api/receipt", response_model=GenericResponse, tags=["Receipts"], summary="Save receipt from bot push")
async def api_receipt_save(req: dict, auth=Depends(verify_api_key)):
    """Save receipt JSON payload from bot (best-effort fire-and-forget)."""
    try:
        logger.info("Receipt saved: %s", req.get("voucher_id", req.get("type", "?")))
        return ok({"status": "ok"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
#  MUTATION — members/register
# ================================================================
@app.post("/api/members/register", response_model=GenericResponse, tags=["Members"], summary="Register a new member into MySQL")
async def api_member_register(req: dict, auth=Depends(verify_api_key)):
    """Register a new member. Writes to member_wallets and topup_log."""
    try:
        member_id = req.get("member_id", "")
        name = req.get("name", "")
        phone = req.get("phone", "")
        staff = req.get("staff", "")
        email = req.get("email", "")
        initial_mins = req.get("initial_mins", 0)
        amount = req.get("amount", 0)
        kpay = req.get("kpay", 0)
        cash = req.get("cash", 0)
        mins_added = req.get("mins_added", initial_mins)
        is_gift = req.get("is_gift", False)
        referral_code = req.get("referral_code", "")

        logger.info("Registering member: %s (%s) mins=%s", member_id, name, initial_mins)

        # Upsert into member_wallets
        _mysql_exec(
            "INSERT INTO member_wallets (member_id, member_name, phone, balance_mins, total_bought_mins, tier, total_spend) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE member_name=VALUES(member_name), phone=VALUES(phone)",
            (member_id, name, phone, initial_mins, mins_added, "Warrior", 0))

        # 🆕 Record cash_movements for member registration income
        if float(amount) > 0:
            try:
                _parse_pm_and_inject(amount, f"KPay:{kpay}/Cash:{cash}", f"New member {member_id} + {mins_added} mins", staff)
            except Exception as _e:
                logger.warning("Reg cash_movements failed: %s", _e)
        # ✅ Also create sales_daily record for registration
        if float(amount) > 0:
            try:
                _mysql_exec(
                    "INSERT INTO sales_daily (sale_date, member_id, amount, gross, net, payment_method, notes, staff_name) "
                    "VALUES (CURDATE(), %s, %s, %s, %s, %s, %s, %s)",
                    (member_id, amount, amount, amount, f"KPay:{kpay}/Cash:{cash}", f"New member {member_id} + {mins_added} mins", staff))
            except Exception as _e:
                logger.warning("Reg sales_daily failed: %s", _e)

        # Log into topup_log
        if mins_added > 0:
            _mysql_exec(
                "INSERT INTO topup_log (member_id, amount, mins_added, topup_date, staff_name, payment_method, balance_mins_before, balance_mins_after) "
                "VALUES (%s,%s,%s,NOW(),%s,%s,0,%s)",
                (member_id, amount, mins_added, staff, f"KPay:{kpay}/Cash:{cash}", initial_mins))

        logger.info("Member registered: %s", member_id)
        return ok({"status": "ok", "member_id": member_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#  MUTATION — save_receipt_json
# ═══════════════════════════════════════
@app.post("/api/save_receipt_json", response_model=GenericResponse, tags=["Receipts"], summary="Persist receipt data to MySQL")
async def api_save_receipt_json(req: dict, auth=Depends(verify_api_key)):
    """Persist receipt data to MySQL."""
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

        logger.info("Saving receipt to MySQL: receipt_no=%s", receipt_no)

        mysql_execute(
            "INSERT INTO receipts (receipt_no, member_id, amount, payment_method, items, receipt_date, staff_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (receipt_no, member_id, amount, payment_method, items, receipt_date, staff_name)
        )

        logger.info("Receipt saved: receipt_no=%s", receipt_no)
        return {"status": "ok", "receipt_no": receipt_no}
    except Exception as e:
        logger.error("Receipt save error: %s", str(e))
        return {"status": "error", "detail": str(e)}



# ═══════════════════════════════════════
#  QUERY — get_receipt_html
# ═══════════════════════════════════════
@app.get("/api/receipt/{voucher_id}", tags=["Receipts"])
async def api_get_receipt_html(voucher_id: str):
    """Render receipt HTML for a given voucher_id."""
    import json
    import os as _os
    
    try:
        # Normalise voucher_id (replace / and \ with -)
        safe_id = voucher_id.replace("/", "-").replace("\\", "-")
        receipt_dir = "/root/psvibe-sales-bot/bot/receipts"
        receipt_path = _os.path.join(receipt_dir, f"{safe_id}.json")
        template_path = "/root/psvibe_api_server/receipt_template.html"
        
        if not _os.path.exists(receipt_path):
            logger.warning("Receipt not found: voucher=%s path=%s", voucher_id, receipt_path)
            return HTMLResponse(
                content=f"<html><body style=\"font-family:sans-serif;padding:40px;text-align:center\"><h2>404 - Receipt Not Found</h2><p>Voucher: {voucher_id}</p></body></html>",
                status_code=404
            )
        
        with open(receipt_path, "r") as f:
            receipt_data = json.load(f)
        
        with open(template_path, "r") as f:
            template = f.read()
        
        # Inject receipt data before </head>
        json_str = json.dumps(receipt_data, ensure_ascii=False)
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
@app.post("/api/add_console_game", response_model=GenericResponse, tags=["Games"], summary="Add game to console [MySQL]")
async def api_add_console_game(req: dict, auth=Depends(verify_api_key)):
    """Add game installation to console in MySQL."""
    try:
        _cid = req.get("console_id","").replace(" ", "")
        _cname = (req.get("console_name") or _cid).replace(" ", "")
        _game = req.get("game_title","")
        # ── Duplicate check ──
        dup = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_games WHERE console_id=%s AND game_title=%s AND install_type != 'Session'",
            (_cid, _game))
        if dup and dup["cnt"] > 0:
            return ok({"saved": True, "duplicate": True, "message": f"{_game} already on {_cid}"})
        _mysql_exec(
            "INSERT INTO console_games (console_id, console_name, game_id, game_title, genre, status, install_type, slot_position) VALUES (%s, %s, %s, %s, %s, 'Installed', %s, %s)",
            (_cid, _cname, req.get("game_id") or _game,
             _game, req.get("genre",""), req.get("install_type",""),
             req.get("slot_position",1)))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/remove_console_game", response_model=GenericResponse, tags=["Games"], summary="Remove game from console [MySQL]")
async def api_remove_console_game(req: dict, auth=Depends(verify_api_key)):
    """Remove game from console in MySQL."""
    try:
        _cid = req.get("console_id","").replace(" ", "")
        _mysql_exec(
            "DELETE FROM console_games WHERE REPLACE(console_id, ' ', '')=%s AND game_title=%s",
            (_cid, req.get("game_title","")))
        return ok({"deleted": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#  LOGGING — sheets/log
# ═══════════════════════════════════════
@app.post("/api/sheets/log", response_model=GenericResponse, tags=["Logging"], summary="Log AI interaction")
async def api_sheets_log(req: dict, auth=Depends(verify_api_key)):
    """Fire-and-forget: log an AI interaction row to Bot_Users sheet."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        query = req.get("query", "")[:300]
        response = req.get("response", "")[:500]
        sentiment = req.get("sentiment", "neutral")
        logger.info("AI-LOG: user=%s query=%s sentiment=%s", user_name, query[:60], sentiment)
        try:
            from sheets_client import get_worksheet
            ws = get_worksheet("Input_Log")
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            ws.append_row([ts, str(tg_id), username or '', user_name or '', 'ai_chat', '', '', query, response, sentiment])
        except Exception as se:
            logger.warning("Input_Log sheet write failed: %s", se)
        return ok({"logged": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/bot-users/track", response_model=GenericResponse, tags=["Bot Users"], summary="Track bot user interaction")
async def api_bot_users_track(req: dict, auth=Depends(verify_api_key)):
    """Track bot user interaction - MySQL upsert + GSheets fallback."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        action = req.get("action", "")
        member_id = req.get("member_id", "")
        phone = req.get("phone", "")
        detail = req.get("detail", "")
        member_name = req.get("member_name", "")
        balance_mins = req.get("balance_mins", 0)
        logger.info("BOT-USER: tg=%s user=%s action=%s", tg_id, user_name, action)

        # MySQL: upsert user + log activity
        try:
            # Build dynamic UPSERT for optional fields
            upsert_sets = [
                "username = VALUES(username)",
                "first_name = VALUES(first_name)",
                "total_interactions = total_interactions + 1",
                "last_seen = NOW()",
                "last_command = VALUES(last_command)",
                "last_activity_type = VALUES(last_activity_type)",
            ]
            insert_cols = ["tg_id", "username", "first_name", "total_interactions", "last_command", "last_activity_type"]
            insert_vals = [tg_id, username, user_name, 1, action, action]

            # Optionally include member_id, phone, member_name, balance_mins
            if member_id:
                insert_cols.append("member_id")
                insert_vals.append(member_id)
                upsert_sets.append("member_id = VALUES(member_id)")
            if phone:
                insert_cols.append("phone")
                insert_vals.append(phone)
                upsert_sets.append("phone = VALUES(phone)")
            if member_name:
                insert_cols.append("member_name")
                insert_vals.append(member_name)
                upsert_sets.append("member_name = VALUES(member_name)")
            if balance_mins:
                insert_cols.append("balance_mins")
                insert_vals.append(balance_mins)
                upsert_sets.append("balance_mins = VALUES(balance_mins)")

            # Track session/book events
            if action == "session_start":
                upsert_sets.append("session_count = session_count + 1")
                upsert_sets.append("last_session_date = NOW()")
            if action.startswith("book_step_"):
                upsert_sets.append("last_funnel_step = VALUES(last_funnel_step)")
                insert_cols.append("last_funnel_step")
                insert_vals.append(action)
            if action == "booking_created":
                upsert_sets.append("booking_count = booking_count + 1")
                upsert_sets.append("last_booking_date = NOW()")

            placeholders = ", ".join(["%s"] * len(insert_vals))
            sql = (
                f"INSERT INTO customer_bot_users ({', '.join(insert_cols)}) "
                f"VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {', '.join(upsert_sets)}"
            )
            _mysql_exec(sql, insert_vals)

            # Log activity
            if action:
                _mysql_exec(
                    "INSERT INTO customer_bot_activity_log (tg_id, activity_type, detail) VALUES (%s, %s, %s)",
                    (tg_id, action, detail[:500] if detail else "")
                )

            # ── Auto-segmentation (Task 6) ──
            user = _mysql_query_one(
                "SELECT booking_count, total_interactions, last_seen, total_spend_ks, frustration_count, tags "
                "FROM customer_bot_users WHERE tg_id=%s", (tg_id,)
            )
            if user:
                tags = set(t.strip() for t in (user.get("tags", "") or "").split(",") if t.strip())
                bc = user.get("booking_count", 0) or 0
                ti = user.get("total_interactions", 0) or 0
                sp = user.get("total_spend_ks", 0) or 0
                fc = user.get("frustration_count", 0) or 0
                last_seen = user.get("last_seen")

                if bc >= 3:
                    tags.add("active_booker")
                if ti > 50 and bc == 0:
                    tags.add("chat_only")
                if last_seen:
                    days_ago = (datetime.now() - last_seen).days if isinstance(last_seen, datetime) else 999
                    if days_ago > 30:
                        tags.add("lapsed_30d")
                if sp >= 100000:
                    tags.add("high_value")
                if fc >= 3:
                    tags.add("needs_attention")

                new_tags = ",".join(sorted(tags))
                _mysql_exec("UPDATE customer_bot_users SET tags=%s WHERE tg_id=%s", (new_tags, tg_id))

        except Exception as dbe:
            logger.warning("bot-users MySQL write failed: %s", dbe)

        # GSheets fallback (keep existing)
        try:
            from sheets_client import get_worksheet
            ws = get_worksheet("Bot_Users")
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            ws.append_row([ts, str(tg_id), username or "", user_name or "", action or "", member_id or "", phone or ""])
        except Exception as se:
            logger.warning("Bot_Users sheet write failed: %s", se)

        return ok({"tracked": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Customer Feedback ─────────────────────────────────────────────────────────
@app.post("/api/feedback/submit", response_model=GenericResponse, tags=["Feedback"], summary="Submit customer feedback (stars + optional comment)")
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
            "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
            (tg_id, username, str(booking_id), rating, comment, console_id)
        )
        logger.info("FEEDBACK: tg=%s rating=%d bk=%s comment=%.50s", tg_id, rating, booking_id, comment)
        
        return ok({"submitted": True, "rating": rating})
    except Exception as e:
        logger.exception("feedback/submit error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bot-users/stats", response_model=GenericResponse, tags=["Bot Users"], summary="Bot user stats")
async def api_bot_users_stats(auth=Depends(verify_api_key)):
    """Return aggregated bot user statistics with conversion rate, tag distribution, and peak hours."""
    try:
        total = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users")
        active_today = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE DATE(last_seen) = CURDATE()")
        active_week = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE last_seen >= DATE_SUB(NOW(), INTERVAL 7 DAY)")
        ai_chats = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_activity_log WHERE activity_type IN ('ai_chat','ai_chat_cached')")
        top_cmds = _mysql_query(
            "SELECT activity_type, COUNT(*) AS cnt FROM customer_bot_activity_log "
            "WHERE activity_type NOT IN ('ai_chat','ai_chat_cached','message') "
            "GROUP BY activity_type ORDER BY cnt DESC LIMIT 10"
        )
        by_day = _mysql_query(
            "SELECT DATE(first_seen) AS d, COUNT(*) AS cnt FROM customer_bot_users "
            "WHERE first_seen >= DATE_SUB(NOW(), INTERVAL 7 DAY) "
            "GROUP BY DATE(first_seen) ORDER BY d"
        )

        # Booking conversion rate
        users_with_booking = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE booking_count > 0")
        total_users = total["c"] if total else 0
        booking_cr = round((users_with_booking["c"] / total_users * 100), 1) if total_users > 0 else 0

        # Tag distribution
        tag_rows = _mysql_query("SELECT tags FROM customer_bot_users WHERE tags != '' AND tags IS NOT NULL")
        tag_dist: dict = {}
        for r in (tag_rows or []):
            for t in r["tags"].split(","):
                t = t.strip()
                if t:
                    tag_dist[t] = tag_dist.get(t, 0) + 1

        # Peak hours (hours with most activity from activity_log)
        peak_hours = _mysql_query(
            "SELECT HOUR(created_at) AS h, COUNT(*) AS cnt FROM customer_bot_activity_log "
            "WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            "GROUP BY HOUR(created_at) ORDER BY cnt DESC LIMIT 12"
        )

        return ok({
            "total_users": total_users,
            "active_today": active_today["c"] if active_today else 0,
            "active_this_week": active_week["c"] if active_week else 0,
            "total_ai_chats": ai_chats["c"] if ai_chats else 0,
            "top_commands": [{"action": r["activity_type"], "count": r["cnt"]} for r in (top_cmds or [])],
            "users_by_day": [{"date": str(r["d"]), "count": r["cnt"]} for r in (by_day or [])],
            "booking_conversion_rate_pct": booking_cr,
            "users_with_bookings": users_with_booking["c"] if users_with_booking else 0,
            "tag_distribution": tag_dist,
            "peak_hours": [{"hour": r["h"], "count": r["cnt"]} for r in (peak_hours or [])],
        })
    except Exception as e:
        return error_response(message=str(e))


@app.get("/api/bot-users/list", response_model=GenericResponse, tags=["Bot Users"], summary="List bot users")
async def api_bot_users_list(limit: int = 50, offset: int = 0, search: str = "", auth=Depends(verify_api_key)):
    """Return paginated bot user list with all tracking columns."""
    base_cols = "tg_id, username, first_name, first_seen, last_seen, total_interactions, ai_chat_count, last_command, last_activity_type, member_id, member_name, phone, balance_mins, is_active, session_count, last_session_date, booking_count, total_spend_ks, last_booking_date, tags, last_funnel_step, frustration_count"
    try:
        if search:
            like = f"%{search}%"
            total = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users WHERE username LIKE %s OR first_name LIKE %s OR tg_id LIKE %s", (like, like, like))
            rows = _mysql_query(
                f"SELECT {base_cols} FROM customer_bot_users WHERE username LIKE %s OR first_name LIKE %s OR tg_id LIKE %s "
                "ORDER BY last_seen DESC LIMIT %s OFFSET %s",
                (like, like, like, limit, offset)
            )
        else:
            total = _mysql_query_one("SELECT COUNT(*) AS c FROM customer_bot_users")
            rows = _mysql_query(
                f"SELECT {base_cols} FROM customer_bot_users ORDER BY last_seen DESC LIMIT %s OFFSET %s",
                (limit, offset)
            )
        users = []
        for r in (rows or []):
            users.append({
                "tg_id": r["tg_id"],
                "username": r["username"],
                "first_name": r["first_name"],
                "first_seen": str(r["first_seen"]) if r["first_seen"] else "",
                "last_seen": str(r["last_seen"]) if r["last_seen"] else "",
                "total_interactions": r["total_interactions"],
                "ai_chat_count": r["ai_chat_count"],
                "last_command": r["last_command"] or "",
                "last_activity": r["last_activity_type"] or "",
                "member_id": r["member_id"] or "",
                "member_name": r["member_name"] or "",
                "phone": r["phone"] or "",
                "balance_mins": r["balance_mins"],
                "is_active": bool(r["is_active"]),
                "session_count": r["session_count"],
                "last_session_date": str(r["last_session_date"]) if r["last_session_date"] else "",
                "booking_count": r["booking_count"],
                "total_spend_ks": str(r["total_spend_ks"]) if r["total_spend_ks"] is not None else "0",
                "last_booking_date": str(r["last_booking_date"]) if r["last_booking_date"] else "",
                "tags": r["tags"] or "",
                "last_funnel_step": r["last_funnel_step"] or "",
                "frustration_count": r["frustration_count"],
            })
        return ok({"users": users, "total": total["c"] if total else 0})
    except Exception as e:
        return error_response(message=str(e))


@app.get("/api/bot-users/activity", response_model=GenericResponse, tags=["Bot Users"], summary="Bot user activity log")
async def api_bot_users_activity(tg_id: str, limit: int = 50, auth=Depends(verify_api_key)):
    """Return activity log for a specific user."""
    try:
        rows = _mysql_query(
            "SELECT id, activity_type, detail, created_at FROM customer_bot_activity_log "
            "WHERE tg_id=%s ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        activities = []
        for r in (rows or []):
            activities.append({
                "id": r["id"],
                "activity_type": r["activity_type"],
                "detail": r["detail"] or "",
                "created_at": str(r["created_at"]) if r["created_at"] else "",
            })
        return ok({"activities": activities})
    except Exception as e:
        return error_response(message=str(e))


@app.get("/api/bot-users/timeline/{tg_id}", response_model=GenericResponse, tags=["Bot Users"], summary="Bot user timeline")
async def api_bot_users_timeline(tg_id: str, limit: int = 100, auth=Depends(verify_api_key)):
    """Return a user's complete timeline: bookings, activity log, and feedback merged chronologically."""
    try:
        events = []

        # User info
        user = _mysql_query_one(
            "SELECT * FROM customer_bot_users WHERE tg_id=%s", (tg_id,)
        )

        # Activity log entries
        act_rows = _mysql_query(
            "SELECT id, activity_type, detail, created_at FROM customer_bot_activity_log "
            "WHERE tg_id=%s ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        for r in (act_rows or []):
            events.append({
                "type": "activity",
                "id": r["id"],
                "subtype": r["activity_type"],
                "detail": r["detail"] or "",
                "timestamp": str(r["created_at"]) if r["created_at"] else "",
            })

        # Bookings
        bk_rows = _mysql_query(
            "SELECT id, member_id, booking_date, console_id, staff_name, phone, "
            "duration_mins, game_name, status, created_at "
            "FROM console_booking WHERE telegram_chat_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        for r in (bk_rows or []):
            events.append({
                "type": "booking",
                "id": r["id"],
                "subtype": r.get("status", ""),
                "detail": f"{r.get('console_id','')} | {r.get('member_id','')} | {r.get('staff_name','')} | {r.get('duration_mins',0)}min | {r.get('game_name','')}",
                "timestamp": str(r["created_at"]) if r["created_at"] else "",
                "booking_date": str(r.get("booking_date", "")) if r.get("booking_date") else "",
                "console_id": r.get("console_id", ""),
                "status": r.get("status", ""),
            })

        # Feedback entries
        fb_rows = _mysql_query(
            "SELECT id, tg_id, username, booking_id, rating, comment, console_id, created_at "
            "FROM customer_feedback WHERE tg_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (tg_id, limit)
        )
        for r in (fb_rows or []):
            events.append({
                "type": "feedback",
                "id": r["id"],
                "subtype": str(r.get("rating", 0)),
                "detail": r.get("comment", "") or "",
                "timestamp": str(r["created_at"]) if r["created_at"] else "",
                "booking_id": r.get("booking_id", ""),
            })

        # Sort merged timeline by timestamp descending
        events.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        result = {
            "tg_id": tg_id,
            "events": events[:limit],
            "total_events": len(events),
        }
        if user:
            result["user"] = {
                "username": user.get("username", ""),
                "first_name": user.get("first_name", ""),
                "total_interactions": user.get("total_interactions", 0),
                "booking_count": user.get("booking_count", 0),
                "tags": user.get("tags", ""),
            }
        return ok(result)
    except Exception as e:
        return error_response(message=str(e))


@app.get("/api/promotions/active", response_model=GenericResponse, tags=["Promotions"])
async def api_promotions_active(auth=Depends(verify_api_key)):
    try:
        rows = _mysql_query("SELECT * FROM promotions WHERE promo_type='cashback_coupon' AND start_date <= CURDATE() AND end_date >= CURDATE() ORDER BY id DESC LIMIT 1")
        if rows:
            row = rows[0]
            return ok({"promotion": {
                "id": row["id"],
                "name": row["name"],
                "promo_type": row["promo_type"],
                "start_date": str(row["start_date"]),
                "end_date": str(row["end_date"]),
                "coupon_expiry_date": str(row["coupon_expiry_date"])
            }})
        return ok({"promotion": None})
    except Exception as e:
        return error_response(message=str(e))


@app.post("/api/coupons/generate", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_generate(req: dict, auth=Depends(verify_api_key)):
    try:
        member_id = req.get("member_id", "")
        session_minutes = int(req.get("session_minutes", 0))
        session_id = req.get("session_id", 0)
        
        if not member_id or session_minutes <= 0:
            return error_response(message="member_id and session_minutes required")
        
        # Check active promotion
        rows = _mysql_query("SELECT id, coupon_expiry_date FROM promotions WHERE promo_type='cashback_coupon' AND start_date <= CURDATE() AND end_date >= CURDATE() LIMIT 1")
        if not rows:
            return ok({"coupon": None, "message": "No active promotion"})
        
        promo = rows[0]
        promo_id = promo["id"]
        expiry_date = str(promo["coupon_expiry_date"]) + " 23:59:59"
        
        # Generate unique code: CB + 6 random alphanumeric chars
        code = "CB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        # Check uniqueness
        existing = _mysql_query("SELECT id FROM member_coupons WHERE coupon_code=%s", (code,))
        retries = 0
        while existing and retries < 5:
            code = "CB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            existing = _mysql_query("SELECT id FROM member_coupons WHERE coupon_code=%s", (code,))
            retries += 1
        
        bk_id = _mysql_exec(
            "INSERT INTO member_coupons (coupon_code, member_id, original_minutes, balance_minutes, issued_date, expiry_date, status, promo_id, source_session_id) VALUES (%s, %s, %s, %s, NOW(), %s, 'active', %s, %s)",
            (code, member_id, session_minutes, session_minutes, expiry_date, promo_id, session_id)
        )
        
        return ok({"coupon": {
            "id": bk_id,
            "code": code,
            "member_id": member_id,
            "minutes": session_minutes,
            "expiry": expiry_date
        }})
    except Exception as e:
        return error_response(message=str(e))


@app.get("/api/coupons/list", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_list(member_id: str, auth=Depends(verify_api_key)):
    try:
        rows = _mysql_query(
            "SELECT id, coupon_code, original_minutes, balance_minutes, issued_date, expiry_date, status FROM member_coupons WHERE member_id=%s ORDER BY issued_date DESC",
            (member_id,))
        coupons = []
        for r in rows:
            coupons.append({
                "id": r["id"],
                "code": r["coupon_code"],
                "original_minutes": r["original_minutes"],
                "balance_minutes": r["balance_minutes"],
                "issued_date": str(r["issued_date"]),
                "expiry_date": str(r["expiry_date"]),
                "status": r["status"]
            })
        return ok({"coupons": coupons})
    except Exception as e:
        return error_response(message=str(e))


@app.post("/api/coupons/validate", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_validate(req: dict, auth=Depends(verify_api_key)):
    try:
        code = req.get("code", "").strip().upper()
        if not code:
            return error_response(message="coupon code required")
        
        rows = _mysql_query(
            "SELECT id, coupon_code, member_id, original_minutes, balance_minutes, expiry_date, status FROM member_coupons WHERE coupon_code=%s",
            (code,))
        if not rows:
            return error_response(message="Coupon not found")
        
        coupon = rows[0]
        if coupon["status"] != "active":
            return error_response(message=f"Coupon status: {coupon['status']}")
        if coupon["expiry_date"] and coupon["expiry_date"] < datetime.now():
            return error_response(message="Coupon has expired")
        if coupon["balance_minutes"] <= 0:
            return error_response(message="Coupon has no remaining balance")
        
        return ok({"coupon": {
            "id": coupon["id"],
            "code": coupon["coupon_code"],
            "member_id": coupon["member_id"],
            "balance_minutes": coupon["balance_minutes"],
            "expiry_date": str(coupon["expiry_date"])
        }})
    except Exception as e:
        return error_response(message=str(e))


@app.post("/api/coupons/redeem", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_redeem(req: dict, auth=Depends(verify_api_key)):
    try:
        code = req.get("code", "").strip().upper()
        minutes_to_deduct = int(req.get("minutes", 0))
        
        if not code or minutes_to_deduct <= 0:
            return error_response(message="code and minutes required")
        
        rows = _mysql_query("SELECT id, balance_minutes, status, expiry_date FROM member_coupons WHERE coupon_code=%s", (code,))
        if not rows:
            return error_response(message="Coupon not found")
        
        coupon = rows[0]
        if coupon["status"] != "active":
            return error_response(message=f"Coupon status: {coupon['status']}")
        if coupon["balance_minutes"] < minutes_to_deduct:
            return error_response(message=f"Insufficient balance. Available: {coupon['balance_minutes']} mins")
        if coupon["expiry_date"] and coupon["expiry_date"] < datetime.now():
            return error_response(message="Coupon has expired")
        
        new_balance = coupon["balance_minutes"] - minutes_to_deduct
        new_status = "used" if new_balance <= 0 else "active"
        
        _mysql_exec(
            "UPDATE member_coupons SET balance_minutes=%s, status=%s, redeemed_at=NOW() WHERE id=%s",
            (new_balance, new_status, coupon["id"]))
        
        return ok({
            "remaining_minutes": new_balance,
            "deducted_minutes": minutes_to_deduct,
            "status": new_status
        })
    except Exception as e:
        return error_response(message=str(e))

# ── Serve Dashboard SPA ──
try:
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import FileResponse
    import os
    dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard-dist")
    if os.path.isdir(dashboard_dir):
        # Serve static assets directly (with hash-based routing, no base path needed)
        @app.get("/assets/FoodMenuRegister-{rest:path}", include_in_schema=False)
        async def serve_food_menu_js(rest: str):
            """Serve FoodMenuRegister JS with no-cache headers to bypass CDN/browser cache."""
            from starlette.responses import FileResponse
            import os
            fpath = os.path.join(dashboard_dir, "assets", f"FoodMenuRegister-{rest}")
            if os.path.isfile(fpath):
                resp = FileResponse(fpath, media_type="application/javascript")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
                return resp
            return Response(status_code=404)

        
        
        class _NoCacheStaticFiles(StaticFiles):
            """StaticFiles subclass that adds no-cache headers."""
            async def get_response(self, path, scope):
                resp = await super().get_response(path, scope)
                if "Cache-Control" not in resp.headers:
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    resp.headers["Pragma"] = "no-cache"
                    resp.headers["Expires"] = "0"
                return resp
        
        app.mount("/assets", _NoCacheStaticFiles(directory=os.path.join(dashboard_dir, "assets")), name="dashboard_assets")
        
        # Serve favicon
        @app.get("/favicon.svg")
        async def dashboard_favicon():
            return FileResponse(os.path.join(dashboard_dir, "favicon.svg"))
        
        # Serve Vue SPA at root - hash routing handles the rest client-side
        @app.get("/")
        async def serve_dashboard_root():
            idx = os.path.join(dashboard_dir, "index.html")
            if os.path.isfile(idx):
                resp = FileResponse(idx, media_type="text/html")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                return resp
            return HTMLResponse("<h1>Dashboard not built</h1>", status_code=404)

        # ═══ SEL Currency Exchange Dashboard ═══
        _sel_dash_dir = "/opt/kora-projects/sel_exchange/dashboard"
        if os.path.isdir(_sel_dash_dir):
            # API proxy: forward /currency-exchange/api-proxy/* → SEL API on port 8001
            @app.api_route("/currency-exchange/api-proxy/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE"])
            async def proxy_sel_api(path: str, request: Request):
                from starlette.responses import Response as _SResp
                import urllib.request as _ureq, urllib.parse as _uparse
                sel_base = "http://127.0.0.1:8001"
                # Build query string with API key
                qs_parts = dict(request.query_params)
                qs_parts["api_key"] = "sel-exchange-2026"
                qs_str = _uparse.urlencode(qs_parts)
                target = f"{sel_base}/api/{path}?{qs_str}"
                body = await request.body()
                headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
                auth_hdr = request.headers.get("Authorization")
                if auth_hdr:
                    headers["Authorization"] = auth_hdr
                req = _ureq.Request(target, data=body if body else None, method=request.method, headers=headers)
                try:
                    resp = _ureq.urlopen(req, timeout=15)
                    content = resp.read()
                    return _SResp(content=content, status_code=resp.status, media_type=resp.headers.get("Content-Type", "application/json"))
                except _ureq.HTTPError as e:
                    err_body = e.read()
                    return _SResp(content=err_body, status_code=e.code, media_type="application/json")

            @app.get("/currency-exchange")
            async def serve_sel_dashboard():
                idx = os.path.join(_sel_dash_dir, "index.html")
                if os.path.isfile(idx):
                    resp = FileResponse(idx, media_type="text/html")
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    resp.headers["Pragma"] = "no-cache"
                    return resp
                return HTMLResponse("<h1>SEL Dashboard not found</h1>", status_code=404)

            @app.get("/currency-exchange/login")
            async def serve_sel_login():
                lp = os.path.join(_sel_dash_dir, "login.html")
                if os.path.isfile(lp):
                    resp = FileResponse(lp, media_type="text/html")
                    resp.headers["Cache-Control"] = "no-cache"
                    return resp
                return HTMLResponse("<h1>Login page not found</h1>", status_code=404)

            logger.info("SEL Currency Exchange dashboard mounted at /currency-exchange")

        import patch_routes

        # Catch-all: Serve SPA for all non-API paths (e.g., /login, /dashboard)
        @app.get("/{full_path:path}")
        async def serve_spa_fallback(full_path: str):
            # Skip API and static paths
            if full_path.startswith("api/") or full_path.startswith("static/") or full_path.startswith("uploads/"):
                return HTMLResponse(
                    content=json.dumps({"success": False, "error": "Not Found", "code": 404}),
                    status_code=404,
                    media_type="application/json"
                )
            idx = os.path.join(dashboard_dir, "index.html")
            if os.path.isfile(idx):
                resp = FileResponse(idx, media_type="text/html")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                return resp
            return HTMLResponse("<h1>Dashboard not built</h1>", status_code=404)

        logger.info("Dashboard SPA mounted at /")
except Exception as e:
    logger.warning(f"Could not mount dashboard: {e}")

# Import patch routes (GSheet->MySQL migrated endpoints)
"""PS VIBE API — New Game/Console Management Endpoints (MySQL)"""


# ═══════════════════════════════════════
#  CONSOLE SETTINGS — add/remove/update console multipliers
# ═══════════════════════════════════════
@app.post("/api/add_console_to_setting")
async def api_add_console_to_setting(req: dict):
    try:
        console_id = req.get("console_id", "").strip()
        multiplier = float(req.get("multiplier", 1.0))
        if not console_id:
            return error_response(message="console_id required")
        multipliers = _mysql_get_setting("console_multipliers", {})
        if not isinstance(multipliers, dict):
            multipliers = {}
        multipliers[console_id] = multiplier
        _mysql_exec(
            "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
            "VALUES ('console_multipliers', %s, 'json', 'console', 'Console multiplier mapping') "
            "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
            (_json.dumps(multipliers),))
        return ok({"console_id": console_id, "multiplier": multiplier, "all_multipliers": multipliers})
    except Exception as e:
        return error_response(message=str(e))


@app.delete("/api/remove_console_from_setting/{console_id}")
async def api_remove_console_from_setting(console_id: str):
    try:
        multipliers = _mysql_get_setting("console_multipliers", {})
        if not isinstance(multipliers, dict):
            multipliers = {}
        removed = multipliers.pop(console_id, None)
        _mysql_exec(
            "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
            "VALUES ('console_multipliers', %s, 'json', 'console', 'Console multiplier mapping') "
            "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
            (_json.dumps(multipliers),))
        return ok({"removed": removed is not None, "console_id": console_id})
    except Exception as e:
        return error_response(message=str(e))


@app.put("/api/update_console_multiplier/{console_id}")
async def api_update_console_multiplier(console_id: str, req: dict):
    try:
        multiplier = float(req.get("multiplier", 1.0))
        multipliers = _mysql_get_setting("console_multipliers", {})
        if not isinstance(multipliers, dict):
            multipliers = {}
        multipliers[console_id] = multiplier
        _mysql_exec(
            "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
            "VALUES ('console_multipliers', %s, 'json', 'console', 'Console multiplier mapping') "
            "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
            (_json.dumps(multipliers),))
        return ok({"console_id": console_id, "multiplier": multiplier})
    except Exception as e:
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  GAME LIBRARY CRUD
# ═══════════════════════════════════════
@app.put("/api/set_game_disc_count")
async def api_set_game_disc_count(req: dict):
    try:
        game_title = req.get("game_title", "").strip()
        discs = int(req.get("discs", 0))
        if not game_title:
            return error_response(message="game_title required")
        _mysql_exec("UPDATE games_library SET disc_count=%s WHERE game_title=%s", (discs, game_title))
        return ok({"game_title": game_title, "discs": discs, "updated": True})
    except Exception as e:
        return error_response(message=str(e))


@app.put("/api/update_game_library_install")
async def api_update_game_library_install(req: dict):
    try:
        game_title = req.get("game_title", "").strip()
        console_id = req.get("console_id", "").replace(" ", "")
        installed = req.get("installed", True)
        status_val = "Installed" if installed else "Not Installed"
        if not game_title or not console_id:
            return error_response(message="game_title and console_id required")
        existing = _mysql_query_one(
            "SELECT id FROM console_games WHERE console_id=%s AND game_title=%s",
            (console_id, game_title))
        if existing:
            _mysql_exec(
                "UPDATE console_games SET status=%s, updated_at=NOW() WHERE console_id=%s AND game_title=%s",
                (status_val, console_id, game_title))
        elif installed:
            # Only INSERT for new installs — never for uninstalls (avoids ghost records)
            _mysql_exec(
                "INSERT INTO console_games (console_id, console_name, game_id, game_title, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (console_id, console_id, game_title, game_title, status_val))
        # Update games_library.final_status based on console_games
        has_installed = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_games WHERE game_title=%s AND status='Installed'",
            (game_title,))
        new_final = "Installed" if (has_installed and has_installed.get("cnt", 0) > 0) else "Not Installed"
        _mysql_exec("UPDATE games_library SET final_status=%s WHERE game_title=%s", (new_final, game_title))
        
        return ok({"game_title": game_title, "console_id": console_id, "installed": installed})
    except Exception as e:
        return error_response(message=str(e))


@app.post("/api/add_game")
async def api_add_game(req: dict):
    try:
        title = req.get("title", "").strip()
        solo_multi = req.get("solo_multi", "").strip()
        genre = req.get("genre", "").strip()
        copies = int(req.get("copies", 1))
        if not title:
            return error_response(message="title required")
        _mysql_exec(
            "INSERT INTO games_library (game_title, genre, solo_multi, disc_count, final_status) "
            "VALUES (%s, %s, %s, %s, 'Not Installed') "
            "ON DUPLICATE KEY UPDATE genre=VALUES(genre), solo_multi=VALUES(solo_multi), disc_count=VALUES(disc_count)",
            (title, genre, solo_multi, copies))
        return ok({"title": title, "genre": genre, "solo_multi": solo_multi, "copies": copies, "saved": True})
    except Exception as e:
        return error_response(message=str(e))


@app.put("/api/edit_game")
async def api_edit_game(req: dict):
    try:
        title = req.get("title", "").strip()
        field = req.get("field", "").strip()
        value = req.get("value", "").strip()
        if not title or not field:
            return error_response(message="title and field required")
        tag = field
        if tag == "solo_multi":
            _mysql_exec("UPDATE games_library SET solo_multi=%s WHERE game_title=%s", (value, title))
        elif tag == "genre":
            _mysql_exec("UPDATE games_library SET genre=%s WHERE game_title=%s", (value, title))
        elif tag == "disc_count":
            _mysql_exec("UPDATE games_library SET disc_count=%s WHERE game_title=%s", (int(value), title))
        else:
            return error_response(message="Invalid field: " + field)
        return ok({"title": title, "field": field, "value": value, "updated": True})
    except Exception as e:
        return error_response(message=str(e))


@app.delete("/api/delete_game/{title}")
async def api_delete_game(title: str):
    try:
        _mysql_exec("DELETE FROM games_library WHERE game_title=%s", (title,))
        return ok({"title": title, "deleted": True})
    except Exception as e:
        return error_response(message=str(e))


@app.delete("/api/delete_session_game/{console_id}")
async def api_delete_session_game(console_id: str):
    try:
        _cid = console_id.replace(" ", "")
        _mysql_exec(
            "DELETE FROM console_games WHERE REPLACE(console_id, ' ', '')=%s AND status='Session'",
            (_cid,))
        return ok({"console_id": console_id, "deleted": True})
    except Exception as e:
        return error_response(message=str(e))
@app.post("/api/move_console_game", response_model=GenericResponse, tags=["Games"], summary="Move game between console/SSD [MySQL]")
async def api_move_console_game(req: dict, auth=Depends(verify_api_key)):
    try:
        game_title = req.get("game_title", "").strip()
        from_console = req.get("from_console", "").replace(" ", "")
        to_console = req.get("to_console", "").replace(" ", "")
        if not game_title or not from_console or not to_console:
            return error_response(message="game_title, from_console, to_console required")
        if from_console == to_console:
            return error_response(message="source and destination must be different")
        # Normalize console_id by removing spaces for matching
        norm = lambda cid: cid.replace(" ", "")
        _mysql_exec("DELETE FROM console_games WHERE REPLACE(console_id, ' ', '')=%s AND game_title=%s", (norm(from_console), game_title))
        _mysql_exec("DELETE FROM console_games WHERE REPLACE(console_id, ' ', '')=%s AND game_title=%s AND install_type NOT IN ('Session')", (norm(to_console), game_title))
        _mysql_exec(
            "INSERT INTO console_games (console_id, console_name, game_id, game_title, status, install_type, slot_position) VALUES (%s, %s, %s, %s, 'Installed', 'Moved', 0)",
            (norm(to_console), norm(to_console), game_title, game_title))
        has_installed = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM console_games WHERE game_title=%s AND status='Installed'",
            (game_title,))
        new_final = "Installed" if (has_installed and has_installed.get("cnt", 0) > 0) else "Not Installed"
        _mysql_exec("UPDATE games_library SET final_status=%s WHERE game_title=%s", (new_final, game_title))
        return ok({"game_title": game_title, "from": from_console, "to": to_console, "moved": True})
    except Exception as e:
        return error_response(message=str(e))




# ═══════════════════════════════════════
#  NEW API ENDPOINTS — Direct GSheets → MySQL migration
# ═══════════════════════════════════════

@app.post("/api/sales/record", response_model=GenericResponse, tags=["Sales"], summary="Save sales record [MySQL]")
async def api_sales_record(req: dict, auth=Depends(verify_api_key)):
    """Save a sales record to MySQL sales_daily table (replaces direct GSheets write)."""
    try:
        voucher_no = req.get("voucher_no", "")
        member_id = req.get("member_id", "")
        console_id = req.get("console_id", "")
        mins = req.get("mins", 0)
        base_rate = float(req.get("base_rate", 0))
        multiplier = float(req.get("multiplier", 1.0))
        game_amt = float(req.get("game_amt", 0))
        food_items = req.get("food_items", "")
        food_total = float(req.get("food_total", 0))
        discount = float(req.get("discount", 0))
        net_total = float(req.get("net_total", 0))
        payment_method = req.get("payment_method", "")
        staff = req.get("staff", "")
        created_at = req.get("created_at", "")
        notes = req.get("notes", "")
        promotion_name = req.get("promotion_name", "")
        coupon_code = req.get("coupon_code", "")

        if not voucher_no:
            return error_response(message="voucher_no required")

        # Build enriched notes with extended fields
        extra_notes = []
        if promotion_name:
            extra_notes.append(f"Promotion: {promotion_name}")
        if coupon_code:
            extra_notes.append(f"Coupon: {coupon_code}")
        if mins:
            extra_notes.append(f"Mins: {mins}")
        if base_rate:
            extra_notes.append(f"BaseRate: {base_rate}")
        if multiplier and multiplier != 1.0:
            extra_notes.append(f"Multiplier: {multiplier}")
        if food_items:
            extra_notes.append(f"Food: {food_items}")
        combined_notes = notes
        if extra_notes:
            combined_notes = (notes + " | " if notes else "") + " | ".join(extra_notes)

        sale_date = created_at if created_at else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        gross = game_amt + food_total

        _mysql_exec(
            "INSERT INTO sales_daily (voucher_no, sale_date, console_id, member_id, amount, gross, discount, net, staff_name, payment_method, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (voucher_no, sale_date, console_id, member_id, game_amt, gross, discount, net_total, staff, payment_method, combined_notes)
        )

        # Deduct wallet balance for member sales
        _wallet_deduct = int(req.get("wallet_deduct", 0))
        if _wallet_deduct > 0 and member_id and member_id.strip() not in ("", "0 (Guest)", "-"):
            _mysql_exec(
                "UPDATE member_wallets SET balance_mins = GREATEST(0, COALESCE(balance_mins, 0) - %s), total_used_mins = COALESCE(total_used_mins, 0) + %s WHERE member_id=%s",
                (_wallet_deduct, _wallet_deduct, member_id)
            )
            logger.info("Wallet deducted: %s mins for member %s (total_used +%s)", _wallet_deduct, member_id, _wallet_deduct)

        logger.info("Sales record saved: voucher=%s member=%s console=%s net=%s", voucher_no, member_id, console_id, net_total)
        return ok({"voucher_no": voucher_no, "success": True})
    except Exception as e:
        logger.error(f"api_sales_record: {e}")
        return error_response(str(e))


@app.post("/api/stock-out/log", response_model=GenericResponse, tags=["Stock"], summary="Log stock-out record [MySQL]")
async def api_stock_out_log(req: dict, auth=Depends(verify_api_key)):
    """Insert a stock-out record into stock_out table (replaces direct GSheets append)."""
    try:
        item_name = req.get("item_name", "")
        qty = int(req.get("qty", 0))
        unit_cost = float(req.get("unit_cost", 0))
        total_cost = float(req.get("total_cost", 0))
        staff = req.get("staff", "")
        notes = req.get("notes", "")
        created_at = req.get("created_at", "")

        if not item_name or qty <= 0:
            return error_response(message="item_name and qty > 0 required")

        sale_date = created_at if created_at else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        _mysql_exec(
            "INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, staff_name, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (item_name, qty, unit_cost, total_cost, sale_date, staff, notes)
        )

        # Decrement inventory quantity
        _mysql_exec(
            "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
            (qty, item_name)
        )

        logger.info("Stock-out logged: item=%s qty=%s staff=%s", item_name, qty, staff)
        return ok({"item_name": item_name, "qty": qty, "success": True})
    except Exception as e:
        logger.error(f"api_stock_out_log: {e}")
        return error_response(str(e))


@app.post("/api/stock-in/log", response_model=GenericResponse, tags=["Stock"], summary="Log stock-in record [MySQL]")
async def api_stock_in_log(req: dict, auth=Depends(verify_api_key)):
    """Insert a stock-in record into stock_in table (replaces direct GSheets append)."""
    try:
        item_name = req.get("item_name", "")
        qty = int(req.get("qty", 0))
        unit_cost = float(req.get("unit_cost", 0))
        staff = req.get("staff", "")
        supplier = req.get("supplier", "")
        notes = req.get("notes", "")
        created_at = req.get("created_at", "")

        if not item_name or qty <= 0:
            return error_response(message="item_name and qty > 0 required")

        batch_id = f"IN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{item_name[:20].replace(' ', '_')}"

        # Note: total_cost is STORED GENERATED column (= quantity * unit_cost), do not insert
        _mysql_exec(
            "INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, staff_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (batch_id, item_name, qty, unit_cost, supplier or "Manual", notes or "", staff)
        )

        # Update inventory quantity
        _mysql_exec(
            "INSERT INTO inventory (item_name, category, quantity, unit_price, reorder_level, last_updated) "
            "VALUES (%s, %s, %s, %s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE quantity = quantity + %s, unit_price = %s, last_updated = NOW()",
            (item_name, "", qty, unit_cost, 0, qty, unit_cost)
        )

        logger.info("Stock-in logged: batch=%s item=%s qty=%s", batch_id, item_name, qty)
        return ok({"batch_id": batch_id, "item_name": item_name, "qty": qty, "success": True})
    except Exception as e:
        logger.error(f"api_stock_in_log: {e}")
        return error_response(str(e))


@app.post("/api/stock/in", response_model=GenericResponse, tags=["Stock"], summary="Stock-in purchase with payment deduction [MySQL]")
async def api_stock_in(req: dict, auth=Depends(verify_api_key)):
    """Record stock-in purchase AND deduct from payment account balance.

    Creates a stock_in record and corresponding cash_movements eject entry.
    Supports both single-payment (Cash/KPay) and split-payment (Cash/KPay combo).
    """
    try:
        item_name = req.get("item_name", "")
        quantity = int(req.get("quantity", 0))
        unit_cost = float(req.get("unit_cost", 0))
        payment_method = req.get("payment_method", "")  # display string, may be composite
        paid_by = req.get("paid_by", "")
        staff_name = req.get("staff_name", "")
        source = req.get("source", "")
        receipt_no = req.get("receipt_no", "")
        # Split-payment amounts (optional — used when payment_method is composite like "Cash X / KPay Y")
        cash_amount = req.get("cash_amount")
        kpay_amount = req.get("kpay_amount")

        if not item_name or quantity <= 0:
            return error_response(message="item_name and quantity > 0 required")

        import uuid
        batch_id = "SI-" + uuid.uuid4().hex[:12].upper()
        total_amount = quantity * unit_cost

        # Insert into stock_in
        _mysql_exec(
            """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (batch_id, item_name, quantity, unit_cost, source or "", receipt_no or "", payment_method, paid_by, staff_name)
        )

        # Update inventory quantity
        _mysql_exec(
            """INSERT INTO inventory (item_name, category, quantity, unit_price, reorder_level, last_updated)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON DUPLICATE KEY UPDATE quantity = quantity + %s, unit_price = %s, last_updated = NOW()""",
            (item_name, "", quantity, unit_cost, 0, quantity, unit_cost)
        )

        # Create cash_movements entries to deduct from account balance
        note = f"Stock In: {item_name} x{quantity}"
        if cash_amount is not None and kpay_amount is not None:
            # Split payment: create separate eject entries for Cash and KPay
            if float(cash_amount) > 0:
                _mysql_exec(
                    """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("eject", "Cash", float(cash_amount), note, staff_name)
                )
            if float(kpay_amount) > 0:
                _mysql_exec(
                    """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("eject", "KPay", float(kpay_amount), note, staff_name)
                )
        elif payment_method:
            # Single payment: one eject entry for the payment method
            _mysql_exec(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                   VALUES (%s, %s, %s, %s, %s)""",
                ("eject", payment_method, total_amount, note, staff_name)
            )

        logger.info(
            "Stock-in via /api/stock/in: batch=%s item=%s qty=%s cost=%s payment=%s amount=%s split=Cash%s/KPay%s",
            batch_id, item_name, quantity, unit_cost, payment_method, total_amount,
            cash_amount or 0, kpay_amount or 0
        )
        return ok({
            "batch_id": batch_id,
            "item_name": item_name,
            "qty": quantity,
            "unit_cost": unit_cost,
            "total": total_amount,
            "payment_method": payment_method,
            "success": True
        })
    except Exception as e:
        logger.error(f"api_stock_in: {e}")
        return error_response(str(e))


@app.post("/api/settings/console", response_model=GenericResponse, tags=["Settings"], summary="Add/update console in settings [MySQL]")
async def api_settings_console(req: dict, auth=Depends(verify_api_key)):
    """Add or update console settings (type and multiplier) in settings_config."""
    try:
        console_id = req.get("console_id", "").strip()
        console_type = req.get("console_type", "")
        multiplier = float(req.get("multiplier", 1.0))

        if not console_id:
            return error_response(message="console_id required")

        # Update multiplier mapping
        multipliers = _mysql_get_setting("console_multipliers", {})
        if not isinstance(multipliers, dict):
            multipliers = {}
        multipliers[console_id] = multiplier
        _mysql_exec(
            "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
            "VALUES ('console_multipliers', %s, 'json', 'console', 'Console multiplier mapping') "
            "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
            (_json.dumps(multipliers),))

        # Update console type mapping if provided
        if console_type:
            types = _mysql_get_setting("console_types", {})
            if not isinstance(types, dict):
                types = {}
            types[console_id] = console_type
            _mysql_exec(
                "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
                "VALUES ('console_types', %s, 'json', 'console', 'Console type mapping') "
                "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
                (_json.dumps(types),))

        logger.info("Console setting updated: id=%s type=%s multiplier=%s", console_id, console_type, multiplier)
        return ok({"console_id": console_id, "multiplier": multiplier, "console_type": console_type, "success": True})
    except Exception as e:
        logger.error(f"api_settings_console: {e}")
        return error_response(str(e))


@app.post("/api/member/wallet/update", response_model=GenericResponse, tags=["Members"], summary="Update member wallet balance [MySQL]")
async def api_member_wallet_update(req: dict, auth=Depends(verify_api_key)):
    """Update member wallet balance_mins, total_spend, and optional bonus_mins."""
    try:
        member_id = req.get("member_id", "")
        balance_mins = req.get("balance_mins")
        total_spend = req.get("total_spend")
        bonus_mins = req.get("bonus_mins")
        deduct_mins = req.get("deduct_mins")

        if not member_id:
            return error_response(message="member_id required")

        # Build dynamic UPDATE
        updates = []
        params = []

        if balance_mins is not None:
            updates.append("balance_mins=%s")
            params.append(int(balance_mins))
        if deduct_mins is not None:
            updates.append("balance_mins = balance_mins - %s")
            params.append(int(deduct_mins))
        if total_spend is not None:
            updates.append("total_spend=%s")
            params.append(float(total_spend))
        if bonus_mins is not None:
            # Bonus mins: add to total_bought_mins and balance_mins
            updates.append("total_bought_mins = total_bought_mins + %s")
            updates.append("balance_mins = balance_mins + %s")
            params.append(int(bonus_mins))
            params.append(int(bonus_mins))

        if updates:
            updates.append("last_updated=NOW()")
            params.append(member_id)
            _mysql_exec(
                f"UPDATE member_wallets SET {', '.join(updates)} WHERE member_id=%s",
                tuple(params))

        # Read back current state
        row = _mysql_query_one(
            "SELECT balance_mins, total_spend, total_bought_mins FROM member_wallets WHERE member_id=%s",
            (member_id,))

        return ok({
            "member_id": member_id,
            "balance_mins": row["balance_mins"] if row else 0,
            "total_spend": row["total_spend"] if row else 0,
            "total_bought_mins": row["total_bought_mins"] if row else 0,
            "success": True
        })
    except Exception as e:
        logger.error(f"api_member_wallet_update: {e}")
        return error_response(str(e))


# ── OPEX / Operating Expenses ─────────────────────────────────────────────────
@app.get("/api/opex/list", tags=["OPEX"], summary="List expenses")
async def api_opex_list(
    date_from: str = "", date_to: str = "",
    category: str = "", limit: int = 100,
    user: dict = Depends(get_current_user),
):
    sql = "SELECT * FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    if category:
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY expense_date DESC, created_at DESC LIMIT %s"
    params.append(limit)
    rows = mysql_query(sql, tuple(params))
    return GenericResponse(success=True, data=list(rows))


@app.post("/api/opex/add", tags=["OPEX"], summary="Add expense")
async def api_opex_add(body: dict = Body(...), user: dict = Depends(get_current_user)):
    cat = body.get("category", "").strip()
    desc = body.get("description", "").strip()
    amt = int(body.get("amount", 0))
    pmt = body.get("payment_method", "Cash").strip()
    by = body.get("recorded_by", "").strip()
    dt = body.get("expense_date", now_mmt().strftime("%Y-%m-%d")).strip()
    if not cat or amt <= 0:
        return GenericResponse(success=False, error="Category and amount required")
    mysql_execute("INSERT INTO opex (category,description,amount,payment_method,recorded_by,expense_date) VALUES (%s,%s,%s,%s,%s,%s)", (cat, desc, amt, pmt, by, dt))
    # Deduct from account balance if payment method matches an account name
    try:
        acc = mysql_query_one("SELECT id, balance FROM accounts WHERE account_name = %s", (pmt,))
        if acc:
            new_bal = float(acc["balance"]) - amt
            mysql_execute("UPDATE accounts SET balance = %s WHERE id = %s", (new_bal, acc["id"]))
            logger.info("OPEX: deducted %d from account '%s' (new balance: %.2f)", amt, pmt, new_bal)
    except Exception as ae:
        logger.warning("OPEX account deduction failed: %s", ae)
    return GenericResponse(success=True, data={"msg": f"{cat}: {amt:,} Ks recorded"})
@app.get("/api/opex/summary", tags=["OPEX"], summary="Expense summary by category + date")
async def api_opex_summary(
    date_from: str = "", date_to: str = "",
    user: dict = Depends(get_current_user),
):
    sql = "SELECT category, SUM(amount) as total, COUNT(*) as count FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    sql += " GROUP BY category ORDER BY total DESC"
    rows = mysql_query(sql, tuple(params))
    grand_total = sum(r["total"] for r in rows) if rows else 0
    return GenericResponse(success=True, data={"categories": list(rows), "grand_total": grand_total})





# ================================================================
#  WALLET — deduct/bonus (Sale Bot calls this when member plays)
# ================================================================
@app.post("/api/member/wallet/update", response_model=GenericResponse, tags=["Members"], summary="Update member wallet (deduct mins / add bonus)")
async def api_wallet_update(req: dict, auth=Depends(verify_api_key)):
    """Deduct or add bonus minutes to a member's wallet."""
    try:
        member_id = req.get("member_id", "")
        deduct_mins = int(req.get("deduct_mins", 0))
        bonus_mins = int(req.get("bonus_mins", 0))
        total_mins = int(req.get("total_mins", 0))

        if not member_id:
            return ok({"success": False, "error": "member_id required"})

        # Get current wallet
        cur = _mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        if not cur:
            return ok({"success": False, "error": f"Member {member_id} not found"})

        current_bal = int(cur["balance_mins"] or 0)
        new_bal = current_bal

        if deduct_mins > 0:
            new_bal = max(0, current_bal - deduct_mins)
            _mysql_exec("UPDATE member_wallets SET balance_mins=%s, last_updated=NOW() WHERE member_id=%s", (new_bal, member_id))
            logger.info("Wallet deduct: %s -%d mins (bal: %d→%d)", member_id, deduct_mins, current_bal, new_bal)
        elif bonus_mins > 0:
            new_bal = current_bal + bonus_mins
            _mysql_exec("UPDATE member_wallets SET balance_mins=%s, last_updated=NOW() WHERE member_id=%s", (new_bal, member_id))
            logger.info("Wallet bonus: %s +%d mins (bal: %d→%d)", member_id, bonus_mins, current_bal, new_bal)

        return ok({
            "success": True,
            "member_id": member_id,
            "balance_before": current_bal,
            "deduct_mins": deduct_mins,
            "bonus_mins": bonus_mins,
            "balance_after": new_bal,
        })
    except Exception as e:
        logger.exception("wallet/update error")
        raise HTTPException(status_code=500, detail=str(e))



# ── n8n Reverse Proxy (HTTP middleware, path preserved) ──
@app.middleware("http")
async def n8n_middleware(request, call_next):
    if request.url.path.startswith("/n8n") or request.url.path == "/favicon.ico":
        import httpx as _httpx
        from starlette.responses import StreamingResponse as _StreamingResponse

        # Rewrite n8n HTML: prefix asset paths with /n8n/ so they get
        # proxied back to n8n through the /n8n/ middleware handler
        # (without this, assets at /assets/* conflict with dashboard)
        if request.url.path == "/n8n/" or request.url.path == "/n8n":
            target = f"http://localhost:5678{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            body = await request.body()
            try:
                async with _httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.request(
                        method=request.method,
                        url=target,
                        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                        content=body or None,
                    )
                    # Read full HTML body
                    html = b""
                    async for chunk in resp.aiter_bytes():
                        html += chunk
                    # Rewrite asset paths: /assets/ -> /n8n/assets/ etc.
                    text = html.decode("utf-8", errors="replace")
                    text = text.replace('src="/assets/', 'src="/n8n/assets/')
                    text = text.replace('href="/assets/', 'href="/n8n/assets/')
                    text = text.replace('src="/static/', 'src="/n8n/static/')
                    text = text.replace('href="/static/', 'href="/n8n/static/')
                    text = text.replace('href="/favicon.ico"', 'href="/n8n/favicon.ico"')
                    text = text.replace("window.BASE_PATH = '/';", "window.BASE_PATH = '/n8n/',")
                    # Strip headers that cause garbled output (gzip already decoded)
                    clean_headers = {k: v for k, v in resp.headers.items()
                                     if k.lower() not in ("content-encoding", "content-length",
                                                          "transfer-encoding", "content-security-policy",
                                                          "strict-transport-security")}
                    # Prevent Cloudflare from auto-minifying/transforming n8n HTML
                    clean_headers["Cache-Control"] = "no-cache, no-store, must-revalidate, proxy-revalidate, no-transform"
                    clean_headers["Cloudflare-No-Transform"] = "1" 
                    return _StreamingResponse(
                        [text.encode("utf-8")],
                        status_code=resp.status_code,
                        headers=clean_headers,
                    )
            except Exception as e:
                from starlette.responses import JSONResponse
                return JSONResponse({"success": False, "error": f"n8n proxy: {e}"}, status_code=502)

        target = f"http://localhost:5678{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        body = await request.body()
        try:
            async with _httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                    content=body or None,
                )
                return _StreamingResponse(
                    resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("content-encoding", "content-length",
                                                  "transfer-encoding", "content-security-policy",
                                                  "strict-transport-security")},
                )
        except Exception as e:
            from starlette.responses import JSONResponse
            return JSONResponse({"success": False, "error": f"n8n proxy: {e}"}, status_code=502)

    return await call_next(request)


# n8n assets proxy — handles /n8n/assets/, /n8n/static/, /n8n/favicon.ico
# (called when n8n's rewritten HTML references these paths)
@app.middleware("http")
async def n8n_assets_middleware(request, call_next):
    if request.url.path.startswith("/n8n/assets/") or request.url.path.startswith("/n8n/static/") or request.url.path == "/n8n/favicon.ico":
        import httpx as _httpx
        from starlette.responses import StreamingResponse as _StreamingResponse

        # Override base-path.js to use /n8n/ prefix for API calls
        if request.url.path == "/n8n/static/base-path.js":
            from starlette.responses import Response as _N8nResp
            return _N8nResp(content="window.BASE_PATH = '/n8n/';", media_type="text/javascript")

        # Strip /n8n prefix, forward remainder to n8n
        internal_path = request.url.path[4:]  # /n8n/assets/... -> /assets/...
        target = f"http://localhost:5678{internal_path}"
        if request.url.query:
            target += f"?{request.url.query}"
        body = await request.body()
        try:
            async with _httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                    content=body or None,
                )
                return _StreamingResponse(
                    resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("content-encoding", "content-length",
                                                  "transfer-encoding", "content-security-policy",
                                                  "strict-transport-security")},
                )
        except Exception as e:
            from starlette.responses import JSONResponse
            return JSONResponse({"success": False, "error": f"n8n assets proxy: {e}"}, status_code=502)

    return await call_next(request)



@app.get('/shell/{path:path}', include_in_schema=False)
async def proxy_shell_get(path: str, request: Request):
    import httpx
    from starlette.responses import Response
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(f'http://localhost:8888/{path}', headers=dict(request.headers))
        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))

@app.get('/shell', include_in_schema=False)
async def proxy_shell_root(request: Request):
    import httpx
    from starlette.responses import Response
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get('http://localhost:8888/', headers=dict(request.headers))
        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))

@app.post('/shell/{path:path}', include_in_schema=False)
async def proxy_shell_post(path: str, request: Request):
    import httpx
    from starlette.responses import Response
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.post(f'http://localhost:8888/{path}', content=body, headers=dict(request.headers))
        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
# ═══════════════════════════════════════
#  SUGGESTIONS API — Discord Game Suggestions
# ═══════════════════════════════════════
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
    except Exception as e:
        logger.error("Failed to save suggestions: %s", e)
        return False

@app.get("/api/suggestions", tags=["Suggestions"], summary="List all suggestions")
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

@app.get("/api/suggestions/{suggestion_id}", tags=["Suggestions"], summary="Get single suggestion")
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

@app.put("/api/suggestions/{suggestion_id}", tags=["Suggestions"], summary="Update suggestion status")
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
#  POST /api/booking-conflicts — check for time/console conflicts
# ════════════════════════════════════════════════════════════════════════════
@app.post("/api/booking-conflicts", response_model=GenericResponse, tags=["Bookings"],
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
        if not _use_mysql():
            return error_response(message="MySQL not available")
        
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
            status IN ('pending','confirmed','pending_check_in','checked_in','Active')
            AND NOT (end_time <= %s OR start_time >= %s)
        """
        params = [start_dt, end_dt]
        
        if console_id:
            where_clause += " AND console_id = %s"
            params.append(console_id)
        
        if exclude_id:
            where_clause += " AND id != %s"
            params.append(int(exclude_id))
        
        rows = mysql_query(
            f"SELECT id, console_id, member_id, duration_mins, booking_date, start_time, end_time, status FROM console_booking WHERE {where_clause} LIMIT 20",
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
                FROM console_booking
                WHERE status = 'pending'
                AND (start_time < %s OR created_at < %s)
            """, (now, now - timedelta(hours=24)))
            cancelled = 0
            for row in (rows or []):
                try:
                    mysql_execute(
                        "UPDATE console_booking SET status='cancelled', cancelled_at=NOW(), notes=CONCAT(COALESCE(notes,''), ' | Auto-cancelled: expired pending') WHERE id=%s AND status='pending'",
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
# Auto-cancel disabled per Boss request (2026-06-22): manual-only policy.
# _bg_thread = threading.Thread(target=_auto_cancel_expired_pending, daemon=True, name="auto-cancel-pending")
# _bg_thread.start()


# Module-level resume moved to lifespan handler

