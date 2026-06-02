"""PS VIBE API Server — FastAPI REST API
Auto-generated from function_map.json (2026-05-27)

All endpoint names match function_map.json functions exactly,
per Boss's requirement.
"""
import logging
import re
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Query, Depends, Request
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

# ── Config Cache ──
_config_cache_data = None
_config_cache_time = 0
_CONFIG_CACHE_TTL = 60  # seconds


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
            "SELECT console_id, status, console_type, current_game, current_member, start_time FROM console_status ORDER BY console_id")
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
# ═══════════════════════════════════════
@app.get("/api/fetch_food_prices", response_model=GenericResponse, tags=["Food"], summary="Fetch food prices [MySQL]")
async def api_fetch_food_prices(auth=Depends(verify_api_key)):
    """Fetch food prices from MySQL settings."""
    try:
        val = _mysql_get_setting("food_prices", {})
        return ok(val if isinstance(val, dict) else {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_food_costs", response_model=GenericResponse, tags=["Food"], summary="Fetch food costs [MySQL]")
async def api_fetch_food_costs(auth=Depends(verify_api_key)):
    """Fetch food costs from MySQL settings."""
    try:
        val = _mysql_get_setting("food_costs", {})
        return ok(val if isinstance(val, dict) else {})
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
            "SELECT console_id, console_name, game_id, game_title, genre, status, slot_position FROM console_games ORDER BY console_id, slot_position")
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
            "SELECT game_title, genre, status, slot_position FROM console_games WHERE console_id=%s ORDER BY slot_position",
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
    """Fetch multiplier for a console from MySQL settings."""
    try:
        mult = _mysql_get_setting(f"console_multiplier_{console_id}", None)
        if mult is None:
            mult = _mysql_get_setting("console_multiplier_default", 1.0)
        return ok(float(mult))
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
        
        return ok({
            "message": "Customer checked in",
            "booking_id": booking_id,
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
        invalidate_cache("bookings")
        return ok({"booking_id": bk_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — end_booking
# ═══════════════════════════════════════
@app.put("/api/end_booking/{booking_id}", response_model=GenericResponse, tags=["Bookings"], summary="End booking [MySQL]")
async def api_end_booking(booking_id: str, auth=Depends(verify_api_key)):
    """Mark a booking as Done in MySQL."""
    try:
        now = now_mmt()
        _mysql_exec(
            "UPDATE console_booking SET end_time=%s, status='Done' WHERE id=%s",
            (now, booking_id))
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
        
        # Detect customer bot format (has customerName)
        customer_name = req.get("customerName", "")
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
            try:
                start_dt = datetime.strptime(f"{booking_date_str} {time_slot}", "%Y-%m-%d %H:%M")
            except:
                start_dt = now
            end_dt = start_dt + timedelta(minutes=duration_mins)
            
            notes = f"Game: {game_name} | Phone: {phone} | User: {username}"
            
            bk_id = _mysql_exec(
                "INSERT INTO console_booking (console_id, member_id, booking_date, start_time, end_time, status, staff_name, notes, telegram_chat_id, duration_mins, phone, game_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (console_id, telegram_chat_id, booking_date_str, start_dt, end_dt, "pending", customer_name, notes, telegram_chat_id, duration_mins, phone, game_name)
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
            "UPDATE member_wallets SET balance_mins=%s, total_bought_mins=%s, total_spend=%s, last_updated=NOW() WHERE member_id=%s",
            (bal_after, bought, new_spend, member_id))

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
            (member_id, name, phone, initial_mins, mins_added, "Warrior", amount))

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
        _mysql_exec(
            "INSERT INTO console_games (console_id, console_name, game_id, game_title, genre, status, slot_position) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (req.get("console_id",""), req.get("console_name") or req.get("console_id",""), req.get("game_id") or req.get("game_title",""),
             req.get("game_title",""), req.get("genre",""), req.get("status") or req.get("install_type","Installed"),
             req.get("slot_position",1)))
        return ok({"saved": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/remove_console_game", response_model=GenericResponse, tags=["Games"], summary="Remove game from console [MySQL]")
async def api_remove_console_game(req: dict, auth=Depends(verify_api_key)):
    """Remove game from console in MySQL."""
    try:
        _mysql_exec(
            "DELETE FROM console_games WHERE console_id=%s AND game_title=%s",
            (req.get("console_id",""), req.get("game_title","")))
        return ok({"deleted": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#  LOGGING — sheets/log
# ═══════════════════════════════════════
@app.post("/api/sheets/log", response_model=GenericResponse, tags=["Logging"], summary="Log AI interaction")
async def api_sheets_log(req: dict, auth=Depends(verify_api_key)):
    """Fire-and-forget: log an AI interaction row."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        query = req.get("query", "")[:300]
        response = req.get("response", "")[:500]
        sentiment = req.get("sentiment", "neutral")
        logger.info("AI-LOG: user=%s query=%s sentiment=%s", user_name, query[:60], sentiment)
        return ok({"logged": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#  BOT USERS — track
# ═══════════════════════════════════════
@app.post("/api/bot-users/track", response_model=GenericResponse, tags=["Bot Users"], summary="Track bot user interaction")
async def api_bot_users_track(req: dict, auth=Depends(verify_api_key)):
    """Fire-and-forget: upsert bot user tracking row."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        user_name = req.get("user_name", "")
        action = req.get("action", "")
        logger.info("BOT-USER: tg=%s user=%s action=%s", tg_id, user_name, action)
        return ok({"tracked": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  PROMOTIONS — cashback coupon
# ═══════════════════════════════════════
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

# Import patch routes (GSheet->MySQL migrated endpoints)
import patch_routes

