"""PS VIBE API Server — FastAPI REST API
Auto-generated from function_map.json (2026-05-27)

All endpoint names match function_map.json functions exactly,
per Boss's requirement.
"""
import logging
import re
import time
from datetime import datetime, timezone, timedelta
import os
import asyncio
import urllib.request
import urllib.error

# Telegram notifications (direct, no n8n needed)
# Session timer module for 5-min-before-end reminder
from session_timer import schedule_session_timer, resume_active_timers


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
import mysql.connector as _mc
import json as _json, os as _os
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


app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Dashboard Auth & Routes ──
register_auth_routes(app)
app.include_router(dashboard_router)

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
    """Verify API key from query param or X-API-Key header."""
    if API_KEY:
        key = api_key
        if not key and request:
            key = request.headers.get("X-API-Key")
        if not key or key != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
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
                if age < 600:  # 10 min max staleness
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
                if age < 600:  # 10 min max staleness
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
async def api_fetch_console_status(auth=Depends(verify_api_key)):
    """Fetch live console statuses from MySQL console_status table."""
    try:
        rows = _mysql_query(
            "SELECT cs.console_id, cs.status, cs.console_type, cs.current_game, cs.current_member, DATE_FORMAT(cs.start_time, '%H:%i') as start_time, cb.id as booking_id, cb.staff_name FROM console_status cs LEFT JOIN (    SELECT cb1.id, cb1.console_id, cb1.staff_name     FROM console_booking cb1     INNER JOIN (        SELECT console_id, MAX(id) as max_id         FROM console_booking WHERE status = 'Active' GROUP BY console_id    ) cb2 ON cb1.id = cb2.max_id) cb ON REPLACE(cb.console_id, ' ', '') LIKE CONCAT(REPLACE(cs.console_id, ' ', ''), '%') ORDER BY cs.console_id")
        # Add id alias for backward compat — bot code uses c["id"] everywhere
        for r in rows:
            if "id" not in r:
                r["id"] = r.get("console_id", "")
        # Mark Free consoles as Reserved if booking time slot includes NOW
        try:
            upcoming = _mysql_query(
                "SELECT console_id FROM console_booking "
                "WHERE status IN ('confirmed', 'pending_check_in') "
                "AND start_time <= NOW() AND end_time > NOW()"
            )
            reserved_ids = {r["console_id"].strip() for r in upcoming}
        except Exception:
            reserved_ids = set()
        for r in rows:
            if r.get("status") == "Free" and r["console_id"].strip() in reserved_ids:
                r["status"] = "Reserved"
        return ok({"consoles": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_members
# ═══════════════════════════════════════
@app.get("/api/fetch_members", response_model=GenericResponse, tags=["Members"], summary="Fetch all members [MySQL]")
async def api_fetch_members(auth=Depends(verify_api_key)):
    """Fetch sorted list of all member IDs from MySQL."""
    try:
        rows = _mysql_query("SELECT member_id FROM member_wallets WHERE member_id IS NOT NULL AND member_id != '' ORDER BY member_id")
        return ok(sorted([r["member_id"] for r in rows]))
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
        rows = _mysql_query("SELECT item_name, unit_price FROM inventory WHERE category IN (%s,%s,%s,%s,%s,%s) AND quantity > 0", ("Food","Drinks","Instant Noodles","Snacks","Candy","Other"))
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
        rows = _mysql_query("SELECT item_name, unit_price, category FROM inventory WHERE category IN (%s,%s,%s,%s,%s,%s) AND quantity > 0 ORDER BY category, item_name", ("Food","Drinks","Instant Noodles","Snacks","Candy","Other"))
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
        rows = _mysql_query("SELECT item_name, unit_price FROM inventory WHERE category IN (%s,%s,%s,%s,%s,%s) AND quantity > 0", ("Food","Drinks","Instant Noodles","Snacks","Candy","Other"))
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

        # Decrement inventory quantity (best-effort)
        _mysql_exec(
            "UPDATE inventory SET quantity = GREATEST(0, quantity - %s), last_updated = NOW() WHERE item_name = %s",
            (qty, item_name)
        )

        return ok({"success": True, "item_name": item_name, "qty_deducted": qty})
    except Exception as e:
        return error_response(message=str(e))


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
    """Mark a confirmed booking as checked in (Active). Updates booking status and console."""
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
        
        # Update booking status to Active
        _mysql_exec("UPDATE console_booking SET status='Active', start_time=NOW() WHERE id=%s", (booking_id,))
        
        # Set console to Active
        if console_id:
            member_name = booking.get("staff_name") or booking.get("member_id", "")
            _mysql_exec(
                "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=NOW() WHERE console_id=%s",
                (booking.get("member_id", ""), booking.get("game_name", ""), console_id)
            )
        
        # ⏰ Schedule session timer for 5-min-before-end reminder
        try:
            duration_mins = booking.get("duration_mins") or 60
            member_id = booking.get("member_id") or booking.get("staff_name") or "Unknown"
            schedule_session_timer(
                booking_id=booking_id,
                console_id=console_id,
                member_id=member_id,
                start_time=datetime.now(timezone.utc),
                duration_mins=duration_mins
            )
            logger.info("Session timer scheduled for booking %s (console=%s, %d min)", booking_id, console_id, duration_mins)
        except Exception as e:
            logger.error("Failed to schedule session timer for booking %s: %s", booking_id, e)
        
        # 📢 Send Telegram notification to admin group
        STAFF_NOTIFY_CHAT = os.environ.get("STAFF_NOTIFY_CHAT", "-1003686032747")
        BOT_TOKEN = os.environ.get("BOT_TOKEN", "8545665013:AAFgEuw4V_715Q9yzGOYloinIdbdYXYb8zU")
        try:
            _session_msg = (
                "🎮 *Session Started!*\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 Member: `{booking.get('member_id', '')}`\n"
                f"🎮 Console: `{console_id}`\n"
                f"⏱ Time: `{datetime.now(timezone.utc).strftime('%H:%M')} MMT`\n"
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
            logger.info("Session notification sent for booking %s", booking_id)
        except Exception as e:
            logger.warning("Session notification error: %s", e)
        
        return ok({
            "console_id": console_id,
            "telegram_chat_id": telegram_chat_id,
        })
    except Exception as e:
        return error_response(message=str(e))

# ═══════════════════════════════════════
#  MUTATION — create_booking
# ═══════════════════════════════════════
@app.post("/api/create_booking", response_model=GenericResponse, tags=["Bookings"], summary="Create new console booking [MySQL]")
async def api_create_booking(req: dict, auth=Depends(verify_api_key)):
    """Create a booking in MySQL console_booking."""
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
        if status not in ("pending", "confirmed", "rejected", "pending_check_in", "Active", "cancelled", "Done"):
            return error_response(message=f"Invalid status: {status}")
        _mysql_exec(
            "UPDATE console_booking SET status=%s, staff_name=COALESCE(%s, staff_name), notes=COALESCE(%s, notes) WHERE id=%s",
            (status, req.get("staffName", ""), req.get("staffNote", ""), booking_id))
        if req.get("consoleId"):
            _mysql_exec("UPDATE console_booking SET console_id=%s WHERE id=%s", (req["consoleId"], booking_id))
        invalidate_cache("bookings")
        return ok({"booking_id": booking_id, "status": status})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/bookings/{booking_id} ──

@app.get("/api/bookings/search", response_model=GenericResponse, tags=["Bookings"], summary="Search bookings by telegram_chat_id [MySQL]")
async def api_search_bookings(telegram_chat_id: str = Query("", description="Telegram chat ID of customer"), auth=Depends(verify_api_key)):
    """Search bookings by telegram chat ID."""
    try:
        if telegram_chat_id:
            rows = _mysql_query(
                "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking WHERE telegram_chat_id=%s ORDER BY booking_date DESC, start_time DESC",
                (telegram_chat_id,)
            )
        else:
            rows = _mysql_query(
                "SELECT id, console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name FROM console_booking ORDER BY booking_date DESC, start_time DESC"
            )
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
        return JSONResponse(content={"success": True, "data": {"bookings": normalized, "total": len(normalized)}})
    except Exception as e:
        logger.error("api_search_bookings: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})



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
        return ok({
            "booking": {
                "id": row.get("id"),
                "customerName": row.get("staff_name", ""),
                "phone": row.get("phone", ""),
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

@app.put("/api/end_booking/{booking_id}", response_model=GenericResponse, tags=["Bookings"], summary="End booking [MySQL]")
async def api_end_booking(booking_id: str, auth=Depends(verify_api_key)):
    """Mark a booking as Done in MySQL."""
    try:
        now = now_mmt()
        _mysql_exec(
            "UPDATE console_booking SET end_time=%s, status='Done' WHERE id=%s",
            (now, booking_id))
        # Sync console_status: free up the console
        row = _mysql_query_one("SELECT console_id FROM console_booking WHERE id=%s", (booking_id,))
        if row:
            _mysql_exec("UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s", (row["console_id"],))
        invalidate_cache("bookings")

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
                _console = booking.get("console_id") or "—"
                _game = booking.get("game_name") or "—"
                _duration = booking.get("duration_mins") or ""
                _cust_msg = "❌ <b>Booking #" + str(booking_id) + " Cancelled</b>\n\n"
                _cust_msg += "📅 " + _bk_date + "  🕐 " + _bk_time + "\n"
                _cust_msg += "🎮 " + _console + "  ⏱️ " + str(_duration) + " mins\n"
                _cust_msg += "🕹️ " + _game + "\n\n"
                _cust_msg += "ဆက်သက္လာရှန္: @psvibeofficial"
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

        return ok({"booking_id": booking_id, "status": "Done"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════

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
        
        _mysql_exec("UPDATE console_booking SET status='cancelled' WHERE id=%s", (booking_id,))
        
        if console_id:
            _mysql_exec("UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s", (console_id,))
        
        return ok({
            "message": "Booking cancelled",
            "booking_id": booking_id,
            "telegram_chat_id": telegram_chat_id,
        })
    except Exception as e:
        return error_response(message=str(e))

#  MUTATION — cancel_booking
# ═══════════════════════════════════════
@app.post("/api/bookings", response_model=GenericResponse, tags=["Bookings"], summary="Create booking from customer bot payload [MySQL]")
async def api_bookings_create(req: dict, auth=Depends(verify_api_key)):
    """Create a booking - supports both customer bot and staff formats."""
    try:
        now = now_mmt()
        logging.warning("api_bookings_create: req keys=%s", list(req.keys()))
        
        # Detect customer bot format (has customerName)
        customer_name = req.get("customerName", "")
        logging.warning("api_bookings_create: customer_name=%s date=%s time=%s console=%s", 
                        customer_name, req.get("date","?"), req.get("timeSlot","?"), req.get("console_id","?"))
        if customer_name:
            # Customer bot format
            telegram_chat_id = req.get("telegramChatId", "")
            phone = req.get("phone", "")
            booking_date_str = req.get("date", "")
            time_slot = req.get("timeSlot", "")
            console_type = req.get("consoleType", "PS5")
            console_id = req.get("console_id", "")
            duration_mins = int(req.get("durationMins", 60))
            game_name = req.get("gameName", "")
            username = req.get("username", "")
            
            from datetime import datetime, timedelta
            # Handle multiple date formats (bot sends M/D/YYYY, API expects YYYY-MM-DD)
            _parsed_date = booking_date_str
            for _fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d"):
                try:
                    _parsed_date = datetime.strptime(booking_date_str, _fmt).strftime("%Y-%m-%d")
                    break
                except:
                    continue
            try:
                start_dt = datetime.strptime(f"{_parsed_date} {time_slot}", "%Y-%m-%d %H:%M")
            except:
                start_dt = now
            end_dt = start_dt + timedelta(minutes=duration_mins)
            
            # ── Slot conflict check ──
            if console_id:
                _conflicts = _mysql_query(
                    "SELECT id, start_time, end_time FROM console_booking WHERE console_id=%s AND status IN ('pending','confirmed','pending_check_in','Active') AND ((start_time <= %s AND end_time > %s) OR (start_time < %s AND end_time >= %s) OR (start_time >= %s AND end_time <= %s)) LIMIT 1",
                    (console_id, start_dt, start_dt, end_dt, end_dt, start_dt, end_dt)
                )
                if _conflicts:
                    _existing = _conflicts[0]
                    return error_response(message=f"⏰ Console {console_id} သည် {_parsed_date} {time_slot} တွင် ကြိုတင်စာရင်းရှိပြီးဖြစ်ပါသည် (Booking #{_existing['id']})")
            
            notes = f"Game: {game_name} | Phone: {phone} | User: {username}"
            
            # Staff bookings auto-confirm; customer bookings need approval
            _bk_status = "confirmed" if req.get("source") == "staff" else "pending"
            
            bk_id = _mysql_exec(
                "INSERT INTO console_booking (console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (console_id, telegram_chat_id, _parsed_date, start_dt, end_dt, _bk_status, customer_name, notes, telegram_chat_id, duration_mins, phone, game_name)
            )
            
            
            # Send admin notification
            _notify_booking_received(customer_name, booking_date_str, time_slot, console_type, duration_mins, game_name, phone, bk_id)
            
            return ok({"id": bk_id, "message": "Booking created"})
        else:
            # Staff format (existing)
            console_id = req.get("console_id", "")
            member_id = req.get("member_id", "")
            staff = req.get("staff_name", req.get("staff", ""))
            notes = req.get("notes", "")
            _mysql_exec("INSERT INTO console_booking (console_id, member_id, booking_date, start_time, status, staff_name, notes) VALUES (%s, %s, %s, %s, %s, %s, %s)", (console_id, member_id, now.date(), now, "Active", staff, notes))
            # Sync console_status: mark console as Active (prevent duplicate sessions)
            _mysql_exec("UPDATE console_status SET status='Active', current_member=%s, current_game='', start_time=NOW() WHERE console_id=%s", (member_id, console_id))
            return ok({"booking_id": f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ','').replace('-','')}-{now.strftime('%H%M')}"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))





# ═══════════════════════════════════════


def _notify_booking_received(name, date, time_slot, console_type, duration, game, phone, bk_id):
    """Notify admin via Telegram about new customer booking. Use Sale Bot to approve."""
    import json, urllib.request, os
    token = os.environ.get("CUSTOMER_BOT_TOKEN", os.environ.get("BOT_TOKEN", ""))
    chat_id = "-1003686032747"
    if not token or not chat_id:
        return
    msg = ("\U0001f195 *New Booking Request #" + str(bk_id) + "*\n\n"
           + "\U0001f464 " + name + "\n\U0001f4de " + phone + "\n\U0001f4c5 " + date + " \u23f0 " + time_slot
           + "\n\U0001f3ae " + console_type + "\n\u23f1 " + str(duration) + " min\n\U0001f579 " + (game if game else "\u2014")
           + "\n\n_Customer မှ booking တင်ထားပါသည်။_\n"
           + "_(Sale Bot → Customer Booking မှ Approve/Reject ပြုလုပ်ပါ)_")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=5)
    except:
        pass


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
async def api_topup_log(req: dict, auth=Depends(verify_api_key)):
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
    """Fire-and-forget: upsert bot user tracking row to Bot_Users sheet."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        action = req.get("action", "")
        member_id = req.get("member_id", "")
        phone = req.get("phone", "")
        logger.info("BOT-USER: tg=%s user=%s action=%s", tg_id, user_name, action)
        try:
            from sheets_client import get_worksheet
            ws = get_worksheet("Bot_Users")
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            ws.append_row([ts, str(tg_id), username or '', user_name or '', action or '', member_id or '', phone or ''])
        except Exception as se:
            logger.warning("Bot_Users sheet write failed: %s", se)
        return ok({"tracked": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

        app.mount("/assets", StaticFiles(directory=os.path.join(dashboard_dir, "assets")), name="dashboard_assets")
        
        # Serve favicon
        @app.get("/favicon.svg")
        async def dashboard_favicon():
            return FileResponse(os.path.join(dashboard_dir, "favicon.svg"))
        
        # Serve index.html for root - hash routing handles the rest client-side
        @app.get("/")
        async def serve_dashboard_root():
            idx = os.path.join(dashboard_dir, "index.html")
            if os.path.isfile(idx):
                return FileResponse(idx, media_type="text/html")
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
        else:
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


import patch_routes

# Resume active session timers on startup
try:
    resume_active_timers()
except Exception:
    pass

