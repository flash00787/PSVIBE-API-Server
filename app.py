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
from console_status import sync_console_status as _sync_console_status

# Field name normalization (snake_case ↔ camelCase backward compat)

# ═══════════════════════════════════════
#  Telegram Customer Notification Helper
# ═══════════════════════════════════════
import json as _json_notify
import threading as _threading_notify

from field_utils import normalize_list, normalize_response

from fastapi import FastAPI, HTTPException, Query, Depends, Request, Body
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from config import (
    API_TITLE, API_VERSION, API_DESCRIPTION,
    HOST, PORT, DEBUG, API_KEY,
    MMT_HOURS, MMT_MINUTES,
)
# sheets_client import removed (GSheet dependency eliminated 2026-07-01)
from mysql_db import query as mysql_query, query_one as mysql_query_one, execute as mysql_execute
from auth import register_auth_routes, get_current_user
from routes.member_routes import router as member_router
from routes.coupon_routes import router as coupon_router
from routes.botuser_routes import router as botuser_router
from routes.food_game_routes import router as food_game_router
from routes.webhook_routes import router as webhook_router
from routes.booking_routes import router as booking_router
from routes.console_routes import router as console_router
from routes.admin_sales_routes import router as admin_sales_router
from routes.config import router as config_router
from routes.stock_routes import router as stock_router
from routes.staff_routes import router as staff_routes_router
from routes.finance_routes import router as finance_router
from routes.reports import router as reports_router
from routes.promotions import router as promotions_router
from routes.tournament_routes import router as tournament_router
from routes.telegram_webapp import router as telegram_webapp_router
from routes.staff_app_routes import router as staff_app_router
from routes.account_routes import router as account_router
from routes.reconciliation_routes import router as reconciliation_router
from routes.menu_routes import router as menu_router
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
from collections import defaultdict

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

# Pre-configured limiters
rate_limit_10pm = RateLimiter(max_requests=10, window_seconds=60)   # standard: 10/min
rate_limit_5pm = RateLimiter(max_requests=5, window_seconds=60)     # stricter: 5/min
rate_limit_30pm = RateLimiter(max_requests=30, window_seconds=60)   # relaxed: 30/min

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

# _sync_console_status → moved to console_status.py (imported above)

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
        # Dish → ingredient FIFO consumption ledger (additive; idempotent).
        try:
            import dish_consumption as _dc
            _dc.ensure_tables()
            _b = _dc.backfill_stock_layers()
            logging.info(
                "dish_consumption ledger ready: backfill created=%s skipped=%s",
                _b.get("created"), _b.get("skipped"),
            )
        except Exception as _dc_err:
            logging.warning("dish_consumption init failed on startup: %s", _dc_err)
        active_consoles = _mysql_query(
            "SELECT DISTINCT console_id FROM console_sessions WHERE status='Active'"
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

# CORS: restrict to known dashboard and admin origins
_trusted_origins = [
    "http://localhost:8888",
    "http://127.0.0.1:8888",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
# Allow additional origins from env (comma-separated)
_extra_origins = os.environ.get("CORS_EXTRA_ORIGINS", "")
if _extra_origins:
    _trusted_origins.extend(o.strip() for o in _extra_origins.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=_trusted_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Proxy: /agent/* routes to VPN Agent Server (port 9357) ─────────────
# Cloudflare tunnel bypasses Caddy, so we proxy agent paths here
import httpx as _httpx_proxy
_agent_proxy_client = _httpx_proxy.AsyncClient(base_url="http://localhost:9357", timeout=30.0, follow_redirects=False)

# Also handle /agent and /agent/ without path
@app.api_route("/agent", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/agent/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/agent/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_agent_requests(request: Request, path: str = ""):
    # Preserve query string from original request
    raw_scope = getattr(request, "scope", {})
    query = str(request.url.query) if request.url.query else ""
    target_path = f"/agent/{path}?{query}" if path and query else f"/agent/{path}" if path else "/agent/login"
    body = await request.body()
    headers = dict(request.headers)
    # Remove host header to let proxy client set it
    headers.pop("host", None)
    headers.pop("content-length", None)
    try:
        resp = await _agent_proxy_client.request(
            method=request.method,
            url=target_path,
            content=body,
            headers=headers,
        )
        # Build proxy response — pass ALL headers including Set-Cookie
        _proxy_headers = {}
        for k, v in resp.headers.items():
            if k.lower() not in ("transfer-encoding", "content-encoding"):
                _proxy_headers[k] = v
        _proxy_headers["Content-Length"] = str(len(resp.content))
        _proxy_headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_proxy_headers,
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": f"Agent proxy failed: {str(e)}"},
        )

# ── Content-Type Validation Middleware ────────────────────────────────
# Require application/json for POST/PUT/PATCH endpoints in /api/
_CONTENT_JSON_METHODS = {"POST", "PUT", "PATCH"}

@app.middleware("http")
async def content_type_middleware(request: Request, call_next):
    if request.method in _CONTENT_JSON_METHODS and request.url.path.startswith("/api/"):
        ct = request.headers.get("content-type", "")
        # Skip multipart/form-data (file uploads) and webhooks from n8n
        if ct and "multipart/form-data" not in ct and "application/json" not in ct:
            return JSONResponse(
                status_code=415,
                content={"success": False, "error": "Content-Type must be application/json", "code": 415},
            )
    response = await call_next(request)
    return response

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
app.include_router(member_router)
app.include_router(coupon_router)
app.include_router(botuser_router)
app.include_router(food_game_router)
app.include_router(webhook_router)
app.include_router(booking_router)
app.include_router(console_router)
app.include_router(admin_sales_router)
app.include_router(config_router)
app.include_router(stock_router)
app.include_router(staff_routes_router)
app.include_router(finance_router)
app.include_router(reports_router)
app.include_router(promotions_router)
app.include_router(tournament_router)
app.include_router(telegram_webapp_router)
app.include_router(staff_app_router)
app.include_router(account_router)
app.include_router(reconciliation_router)
app.include_router(menu_router)


@app.get("/firebase-service-account.json", include_in_schema=False)
async def _block_firebase_service_account():
    """Defense-in-depth: never serve the Firebase service account.
    It lives in the API dir (outside every static mount); this hard-404s any request anyway."""
    from starlette.responses import Response as _Resp
    return _Resp(status_code=404)

# ── Config Cache ──
_config_cache_data = None
_config_cache_time = 0
_CONFIG_CACHE_TTL = 60  # seconds



# SECURITY: /api/debug_auth endpoint removed — it leaked API key info
# async def debug_api_key(...) was here — removed 2026-07-01

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
    """Verify webhook requests using a shared secret.
    
    n8n sends x-webhook-secret header — check it against WEBHOOK_SECRET env var.
    If WEBHOOK_SECRET is not configured, allow all (backward-compatible).
    """
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not expected:
        return True  # no secret configured = allow all
    provided = request.headers.get("x-webhook-secret", "")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    return True


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


def error_response(message="Internal error", code=500, detail=None):
    """Return a standardized error dict for consistent API error responses.
    
    Args:
        message: Human-readable error message
        code: HTTP status code
        detail: Optional machine-readable detail (e.g., field name, validation error)
    
    Usage:
        return error_response(message="Member not found", code=404)
        return error_response(message="Validation failed", code=422, detail={"field": "email"})
    """
    resp = {"success": False, "error": message, "code": code}
    if detail:
        resp["detail"] = detail
    return resp


def success_response(data=None, message=None):
    """Canonical success wrapper — returns {success: True, data: ..., message: ...}."""
    return ok(data=data, message=message or "")


def api_error(status_code: int, message: str, detail=None):
    """Raise an HTTPException with standardized error format.
    
    Use this instead of raw HTTPException for consistent error responses.
    
    Usage:
        raise api_error(404, "Member not found")
        raise api_error(400, "Invalid input", detail={"missing": "phone"})
    """
    content = error_response(message=message, code=status_code, detail=detail)
    raise HTTPException(status_code=status_code, detail=content)


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


@app.get("/api/dual-write/check", tags=["System"], summary="Phase 3: Dual-write discrepancy monitor")
def dual_write_check():
    """Compare console_booking vs console_bookings + console_sessions row counts.
    Returns discrepancies if any. Used for Phase 3 monitoring (24hr)."""
    try:
        from dual_write import dual_discrepancy_check
        result = dual_discrepancy_check()
        return {
            "success": True,
            "phase": "Phase 3 — Dual-Write",
            "ok": result["ok"],
            "totals": result["totals"],
            "discrepancies": result["discrepancies"]
        }
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


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
    """Fetch member data from MySQL (primary source).
    Previously had a 15-min staleness check that forced GSheet fallback.
    GSheet fallback removed — MySQL is the sole source."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT * FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                return row
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
    """Fetch wallet mins from MySQL (primary source).
    GSheet fallback removed — MySQL is the sole source."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                return row.get("balance_mins", 0)
    except Exception:
        pass
    return None


def _fetch_balance_mins_from_mysql(member_id):
    """Fetch balance mins from MySQL (primary source, same as wallet_mins).
    GSheet fallback removed — MySQL is the sole source."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                return row.get("balance_mins", 0)
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
#  SERVE DASHBOARD SPA
# ═══════════════════════════════════════
try:
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import FileResponse, Response
    import os
    dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard-dist")
    if os.path.isdir(dashboard_dir):
        # Serve FoodMenuRegister JS with no-cache headers
        @app.get("/psvibe-assets/FoodMenuRegister-{rest:path}", include_in_schema=False)
        async def serve_food_menu_js(rest: str):
            fpath = os.path.join(dashboard_dir, "assets", f"FoodMenuRegister-{rest}")
            if os.path.isfile(fpath):
                resp = FileResponse(fpath, media_type="application/javascript")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
                return resp
            return Response(status_code=404)

        class _NoCacheStaticFiles(StaticFiles):
            async def get_response(self, path, scope):
                resp = await super().get_response(path, scope)
                if "Cache-Control" not in resp.headers:
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    resp.headers["Pragma"] = "no-cache"
                    resp.headers["Expires"] = "0"
                return resp

        app.mount("/psvibe-assets", _NoCacheStaticFiles(directory=os.path.join(dashboard_dir, "assets")), name="dashboard_assets")

        # Also mount /assets for JS hardcoded paths (logo images etc.)
        app.mount("/assets", _NoCacheStaticFiles(directory=os.path.join(dashboard_dir, "assets")), name="dashboard_assets_legacy")

        @app.get("/favicon.ico")
        @app.get("/favicon.png")
        async def dashboard_favicon():
            icon_path = os.path.join(dashboard_dir, "logo-icon.png")
            if os.path.isfile(icon_path):
                return FileResponse(icon_path, media_type="image/png")
            return FileResponse(os.path.join(dashboard_dir, "favicon.svg"), media_type="image/svg+xml")

        @app.get("/")
        async def serve_dashboard_root():
            idx = os.path.join(dashboard_dir, "index.html")
            if os.path.isfile(idx):
                resp = FileResponse(idx, media_type="text/html")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                return resp
            return HTMLResponse("<h1>Dashboard not built</h1>", status_code=404)

        # Alias kept for older docs/bookmarks (`PROJECT_STRUCTURE.md` still lists
        # https://ps-vibe.com/dashboard/) — same SPA as `/`, assets included.
        @app.get("/dashboard")
        @app.get("/dashboard/")
        @app.get("/dashboard/{rest:path}")
        async def serve_dashboard_alias(rest: str = ""):
            if rest.startswith("assets/"):
                fpath = os.path.join(dashboard_dir, rest)
                if os.path.isfile(fpath):
                    return FileResponse(fpath)
                return Response(status_code=404)
            idx = os.path.join(dashboard_dir, "index.html")
            if os.path.isfile(idx):
                resp = FileResponse(idx, media_type="text/html")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                return resp
            return HTMLResponse("<h1>Dashboard not built</h1>", status_code=404)

        # ═══════════════════════════════════════
        #  TELEGRAM MINI APP (Phase 1 — read-only)
        # ═══════════════════════════════════════
        webapp_dir = os.path.join(os.path.dirname(__file__), "webapp-dist")
        if os.path.isdir(webapp_dir):
            app.mount("/webapp", _NoCacheStaticFiles(directory=webapp_dir, html=True), name="webapp_static")
            logger.info("Telegram Mini App mounted at /webapp")

        # ═══════════════════════════════════════
        #  STAFF APP (standalone Android/WebView)
        #  Hosts the APK + download page + version.json at /staff-app/
        # ═══════════════════════════════════════
        staff_app_dir = os.path.join(os.path.dirname(__file__), "staff-app-dist")
        if os.path.isdir(staff_app_dir):
            app.mount("/staff-app", _NoCacheStaticFiles(directory=staff_app_dir, html=True), name="staff_app_static")
            logger.info("PS VIBE Staff App mounted at /staff-app")

        # ═══ SEL Currency Exchange Dashboard ═══
        _sel_dash_dir = "/opt/kora-projects/sel_exchange/dashboard"
        if os.path.isdir(_sel_dash_dir):
            @app.api_route("/currency-exchange/api-proxy/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE"])
            async def proxy_sel_api(path: str, request: Request):
                import urllib.request as _ureq, urllib.parse as _uparse
                sel_base = "http://127.0.0.1:8001"
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
                    return Response(content=resp.read(), status_code=resp.status, media_type=resp.headers.get("Content-Type", "application/json"))
                except _ureq.HTTPError as e:
                    return Response(content=e.read(), status_code=e.code, media_type="application/json")

            @app.get("/currency-exchange")
            async def serve_sel_dashboard():
                idx = os.path.join(_sel_dash_dir, "index.html")
                if os.path.isfile(idx):
                    resp = FileResponse(idx, media_type="text/html")
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
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

        import patch_routes  # noqa: E402,F401


        # === AKT Clothing Shop ERP Proxy ===
        @app.api_route("/akt-clothing-shop/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
        async def proxy_akt_clothing(request: Request, path: str):
            import httpx
            target = f"http://127.0.0.1:8010/{path}"
            qs = str(request.url.query)
            if qs:
                target += f"?{qs}"
            body = await request.body() if request.method in ("POST","PUT","PATCH") else None
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "transfer-encoding")}
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    method=request.method, url=target,
                    headers=headers, content=body or None
                )
            from starlette.responses import Response
            # Build response headers + no-cache for Cloudflare
            resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
            resp_headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp_headers["Pragma"] = "no-cache"
            resp_headers["Expires"] = "0"
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=resp_headers
            )

        # ═══ Uploads Static File Serving ═══
        # Serve uploaded files (tournament payment screenshots, etc.)
        # Must be registered BEFORE the SPA catch-all
        _uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")

        @app.get("/uploads/{path:path}")
        async def serve_uploads(path: str):
            file_path = os.path.join(_uploads_dir, path)
            # Security: prevent directory traversal
            real_path = os.path.realpath(file_path)
            if not real_path.startswith(os.path.realpath(_uploads_dir)):
                return Response(status_code=404)
            if not os.path.isfile(real_path):
                return Response(status_code=404)
            # Determine content type
            ext = os.path.splitext(path)[1].lower()
            content_type_map = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".mp4": "video/mp4", ".pdf": "application/pdf",
            }
            media_type = content_type_map.get(ext, "application/octet-stream")
            return FileResponse(real_path, media_type=media_type)

        # Catch-all: Serve SPA for all non-API paths
        @app.get("/{full_path:path}")
        @app.post("/{full_path:path}")
        @app.put("/{full_path:path}")
        @app.delete("/{full_path:path}")
        @app.patch("/{full_path:path}")
        async def serve_spa_fallback(full_path: str, request: Request = None):
            # Proxy n8n/wf to n8n via httpx
            if full_path.startswith("wf/") or full_path.startswith("n8n/") or full_path.startswith("n8n-app/"):
                try:
                    prefix = full_path[:full_path.find("/")]
                    rest = full_path[len(prefix)+1:] if len(full_path) > len(prefix)+1 else ""
                    if prefix == "wf":
                        target_path = f"/{prefix}/{rest}"
                    else:
                        target_path = f"/{prefix}/{rest}"
                    target_url = f"http://127.0.0.1:5678{target_path}"
                    import httpx
                    async with httpx.AsyncClient() as client:
                        body = await request.body() if request else b""
                        headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "content-encoding"]} if request else {}
                        resp = await client.request(
                            method=request.method,
                            url=target_url,
                            headers=headers,
                            content=body,
                        )
                        from starlette.responses import Response as StarletteResponse
                        return StarletteResponse(
                            content=resp.content,
                            status_code=resp.status_code,
                            headers=dict(resp.headers),
                        )
                except Exception as e:
                    return JSONResponse(content={"error": str(e)}, status_code=502)
            # Proxy n8n static/assets/API paths to n8n
            if full_path.startswith("static/") or full_path.startswith("assets/") or full_path.startswith("rest/"):
                try:
                    target_url = f"http://127.0.0.1:5678/{full_path}"
                    import httpx
                    async with httpx.AsyncClient() as client:
                        body = await request.body() if request else b""
                        headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "content-encoding"]} if request else {}
                        resp = await client.request(
                            method=request.method,
                            url=target_url,
                            headers=headers,
                            content=body,
                        )
                        from starlette.responses import Response as StarletteResponse
                        return StarletteResponse(
                            content=resp.content,
                            status_code=resp.status_code,
                            headers=dict(resp.headers),
                        )
                except Exception as e:
                    return JSONResponse(content={"error": str(e)}, status_code=502)
            # Normal SPA routing
            if full_path.startswith("api/") or full_path.startswith("uploads/"):
                return JSONResponse(
                    content={"success": False, "error": "Not Found", "code": 404},
                    status_code=404
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

