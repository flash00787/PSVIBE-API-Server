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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("psvibe_api")

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
@app.get("/api/health", tags=["System"])
async def health_check():
    # Sheets health is checked on startup via startup event; skip per-request
    # to avoid 300ms+ gspread round-trip on every health probe.
    return {"success": True, "sheets_ok": True, "data_source": "mysql"}


# ═══════════════════════════════════════
#  MYSQL HEALTH
# ═══════════════════════════════════════
@app.get("/api/mysql/health", tags=["System"])
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
@app.get("/api/mysql/status", tags=["System"])
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
def _fetch_members_from_mysql():
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            return mysql_query("SELECT * FROM member_wallets")
    except Exception:
        pass
    return None


def _fetch_member_data_from_mysql(member_id):
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            return mysql_query_one("SELECT * FROM member_wallets WHERE member_id = %s", (member_id,))
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
    """Try MySQL first, return data or None.

    NOTE: staff_records.staff_id is an internal auto-increment PK (1,2,3...),
    NOT a Telegram user ID.  The real Telegram whitelist lives in Google Sheets
    Setting!B30.  Return None here so the endpoint falls back to Sheets.
    """
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
    """Try MySQL first, return data or None."""
    try:
        if _use_mysql():
            row = mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id = %s", (member_id,))
            if row:
                return row.get("balance_mins", 0)
    except Exception:
        pass
    return None


def _fetch_balance_mins_from_mysql(member_id):
    """Try MySQL first, return data or None (same as wallet_mins live read)."""
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
#  fetch_console_status
# ═══════════════════════════════════════
@app.get("/api/fetch_console_status", tags=["Console"])
async def api_fetch_console_status(auth=Depends(verify_api_key)):
    """Return list of console dicts with live status from Console_Booking sheet."""
    data = _fetch_console_status_from_mysql()
    if data is not None:
        return ok([{
            "id": d.get("console_id", ""),
            "type": d.get("console_type", d.get("type", "")),
            "status": d.get("status", "Free"),
            "member": d.get("member_id", d.get("member")),
            "start": d.get("start_time", d.get("start")),
            "staff": d.get("staff"),
            "booking_id": d.get("booking_id"),
            "mult": d.get("multiplier", d.get("mult", 1.0)),
        } for d in data])
    try:
        today = today_str()
        setting_sh = get_worksheet(SHEET_SETTING)
        names = setting_sh.col_values(8)[1:]
        types = setting_sh.col_values(9)[1:]
        mults = setting_sh.col_values(10)[1:]

        consoles = []
        for i, name in enumerate(names):
            if not name.strip():
                continue
            try:
                mult = float_safe(mults[i]) if i < len(mults) else 1.0
                mult = mult if mult > 0 else 1.0
            except Exception:
                mult = 1.0
            ctype = (types[i] if i < len(types) else "").strip()
            consoles.append({
                "id": name.strip(), "type": ctype, "mult": mult,
                "status": "Free", "member": None, "start": None,
                "staff": None, "booking_id": None,
            })

        try:
            bk_rows = get_booking_rows()
            for row in bk_rows[1:]:
                if len(row) < 7:
                    continue
                bk_date = row[1].strip()
                bk_cid = row[2].strip()
                bk_status = row[6].strip()
                if bk_date == today and bk_status in ("Active", "Scheduled"):
                    for c in consoles:
                        if c["id"] == bk_cid:
                            c["status"] = bk_status
                            c["member"] = row[3].strip() or "Guest"
                            c["start"] = row[4].strip()
                            c["staff"] = row[7].strip() if len(row) > 7 else ""
                            c["booking_id"] = row[0].strip()
                            break
        except Exception as e:
            logger.warning("Booking overlay error: %s", e)

        return ok(consoles)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_members
# ═══════════════════════════════════════
@app.get("/api/fetch_members", tags=["Members"])
async def api_fetch_members(auth=Depends(verify_api_key)):
    """Return sorted list of all member IDs from Card_Wallet."""
    data = _fetch_members_from_mysql()
    if data is not None:
        return ok(sorted([m["member_id"] for m in data if m.get("member_id")]))
    try:
        ws = get_worksheet(SHEET_CARD_WALLET)
        raw = ws.col_values(2)[1:]
        members = [m.strip() for m in raw if m.strip()]
        return ok(sorted(members))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_data
# ═══════════════════════════════════════
@app.get("/api/fetch_member_data/{member_id}", tags=["Members"])
async def api_fetch_member_data(member_id: str, auth=Depends(verify_api_key)):
    """Return consolidated member data (name, phone, email, rank, wallet, spend) from Card_Wallet."""
    data = _fetch_member_data_from_mysql(member_id)
    if data is not None:
        return ok({
            "name": data.get("member_name", data.get("name", "-")) or "-",
            "phone": data.get("phone", "-") or "-",
            "email": data.get("email", ""),
            "net_spend": data.get("net_spend", 0),
            "rank_raw": data.get("tier", data.get("rank_raw", "Warrior")),
            "wallet_mins": data.get("balance_mins", data.get("wallet_mins")),
        })
    try:
        rows = get_member_rows()
        for row in rows[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                name = row[2].strip() if len(row) > 2 else "-"
                phone = row[3].strip() if len(row) > 3 else "-"
                net_spend = int_safe(row[5]) if len(row) > 5 else 0
                wallet_mins = int_safe(row[7]) if len(row) > 7 and row[7].strip() else None
                email = row[12].strip() if len(row) > 12 else ""

                setting_sh = get_worksheet(SHEET_SETTING)
                master_thresh = int_safe(setting_sh.cell(3, 13).value)
                immortal_thresh = int_safe(setting_sh.cell(4, 13).value)

                rank_raw = "Warrior"
                if immortal_thresh > 0 and net_spend >= immortal_thresh:
                    rank_raw = "Immortal"
                elif master_thresh > 0 and net_spend >= master_thresh:
                    rank_raw = "Master"

                return ok({
                    "name": name or "-", "phone": phone or "-",
                    "email": email, "net_spend": net_spend,
                    "rank_raw": rank_raw, "wallet_mins": wallet_mins,
                })
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_wallet_mins
# ═══════════════════════════════════════
@app.get("/api/fetch_wallet_mins/{member_id}", tags=["Members"])
async def api_fetch_wallet_mins(member_id: str, auth=Depends(verify_api_key)):
    """Fetch wallet balance in mins for a member (col I, formula)."""
    data = _fetch_wallet_mins_from_mysql(member_id)
    if data is not None:
        return ok(data)
    try:
        for row in get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                mins = int_safe(row[7]) if len(row) > 7 and row[7].strip() else 0
                return ok(mins)
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_balance_mins (alias live read)
# ═══════════════════════════════════════
@app.get("/api/fetch_balance_mins/{member_id}", tags=["Members"])
async def api_fetch_balance_mins(member_id: str, auth=Depends(verify_api_key)):
    """Fetch wallet balance in mins (live read, bypasses cache)."""
    data = _fetch_balance_mins_from_mysql(member_id)
    if data is not None:
        return ok(data)
    try:
        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for row in rows[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                mins = int_safe(row[7]) if len(row) > 7 and row[7].strip() else 0
                return ok(mins)
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_tier
# ═══════════════════════════════════════
@app.get("/api/fetch_member_tier/{member_id}", tags=["Members"])
async def api_fetch_member_tier(member_id: str, auth=Depends(verify_api_key)):
    """Fetch member's current tier from Card_Wallet col G."""
    try:
        for row in get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                tier = row[6].strip() if len(row) > 6 else ""
                return ok(tier if tier else "Warrior")
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_staff / fetch_staff_names
# ═══════════════════════════════════════
@app.get("/api/fetch_staff", tags=["Staff"])
async def api_fetch_staff(auth=Depends(verify_api_key)):
    """Return list of staff names from Setting col S."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        vals = ws.col_values(19)[1:]
        return ok([v.strip() for v in vals if v.strip()])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_staff_names", tags=["Staff"])
async def api_fetch_staff_names(auth=Depends(verify_api_key)):
    """Alias for fetch_staff."""
    return await api_fetch_staff(auth)


# ═══════════════════════════════════════
#  fetch_food_prices / fetch_food_costs
# ═══════════════════════════════════════
@app.get("/api/fetch_food_prices", tags=["Food"])
async def api_fetch_food_prices(auth=Depends(verify_api_key)):
    """Return dict of food item name -> price from Setting (D-E)."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(4)[1:]
        prices = ws.col_values(5)[1:]
        return ok({n.strip(): int_safe(p) for n, p in zip(names, prices) if n.strip()})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_food_costs", tags=["Food"])
async def api_fetch_food_costs(auth=Depends(verify_api_key)):
    """Return dict of food item -> cost price from Setting (D, F)."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(4)[1:]
        costs = ws.col_values(6)[1:]
        return ok({n.strip(): (int_safe(c) if str(c).strip() else 0)
                   for n, c in zip(names, costs) if n.strip()})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_games / fetch_game_library
# ═══════════════════════════════════════
@app.get("/api/fetch_games", tags=["Games"])
async def api_fetch_games(auth=Depends(verify_api_key)):
    """Return all games from Game_Library sheet (cached 10 min)."""
    try:
        rows = get_game_rows()
        if len(rows) < 2:
            return ok([])
        games = []
        for i, row in enumerate(rows[1:], start=2):
            if not row or not row[1].strip():
                continue
            games.append({
                "row": i,
                "title": row[1].strip() if len(row) > 1 else "",
                "platform": row[2].strip() if len(row) > 2 else "",
                "genre": row[3].strip() if len(row) > 3 else "",
                "status": row[4].strip() if len(row) > 4 else "",
                "discs": row[5].strip() if len(row) > 5 else "",
            })
        return ok(games)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fetch_game_library", tags=["Games"])
async def api_fetch_game_library(auth=Depends(verify_api_key)):
    """Alias for fetch_games."""
    return await api_fetch_games(auth)


# ═══════════════════════════════════════
#  fetch_console_games
# ═══════════════════════════════════════
@app.get("/api/fetch_console_games", tags=["Games"])
async def api_fetch_console_games(auth=Depends(verify_api_key)):
    """Return all console-game installation records (cached 5 min)."""
    data = _fetch_console_games_from_mysql()
    if data is not None:
        return ok([{
            "row": d.get("id", i),
            "console_id": d.get("console_id", ""),
            "game_title": d.get("game_title", ""),
            "install_type": d.get("install_type", ""),
            "date": d.get("date", ""),
            "notes": d.get("notes", ""),
        } for i, d in enumerate(data, start=2)])
    try:
        rows = get_console_game_rows()
        if len(rows) < 2:
            return ok([])
        result = []
        for i, row in enumerate(rows[1:], start=2):
            if not row or not row[0].strip():
                continue
            result.append({
                "row": i,
                "console_id": row[0].strip() if len(row) > 0 else "",
                "game_title": row[1].strip() if len(row) > 1 else "",
                "install_type": row[2].strip() if len(row) > 2 else "",
                "date": row[3].strip() if len(row) > 3 else "",
                "notes": row[4].strip() if len(row) > 4 else "",
            })
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  get_games_on_console
# ═══════════════════════════════════════
@app.get("/api/get_games_on_console/{console_id}", tags=["Games"])
async def api_get_games_on_console(console_id: str, auth=Depends(verify_api_key)):
    """Return list of game titles installed on a specific console."""
    try:
        rows = get_console_game_rows()
        games = []
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip().upper() == console_id.strip().upper() and row[1].strip():
                games.append(row[1].strip())
        return ok(games)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  get_consoles_with_game
# ═══════════════════════════════════════
@app.get("/api/get_consoles_with_game", tags=["Games"])
async def api_get_consoles_with_game(game_title: str = Query(...), auth=Depends(verify_api_key)):
    """Return list of console IDs that have a specific game installed."""
    try:
        rows = get_console_game_rows()
        gl = game_title.strip().lower()
        consoles = []
        for row in rows[1:]:
            if len(row) >= 2 and row[1].strip().lower() == gl and row[0].strip():
                consoles.append(row[0].strip())
        return ok(list(dict.fromkeys(consoles)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_base_rate
# ═══════════════════════════════════════
@app.get("/api/fetch_base_rate", tags=["Settings"])
async def api_fetch_base_rate(auth=Depends(verify_api_key)):
    """Fetch hourly base rate from Setting!B2 (Ks/hr)."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        return ok(int_safe(ws.cell(2, 2).value))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_console_multiplier
# ═══════════════════════════════════════
@app.get("/api/fetch_console_multiplier/{console_id}", tags=["Settings"])
async def api_fetch_console_multiplier(console_id: str, auth=Depends(verify_api_key)):
    """Fetch multiplier for a console from Setting!J."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)[1:]
        mults = ws.col_values(10)[1:]
        for name, mult in zip(names, mults):
            if name.strip() == console_id.strip():
                val = float_safe(mult)
                return ok(val if val > 0 else 1.0)
        return ok(1.0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_new_member_defaults
# ═══════════════════════════════════════
@app.get("/api/fetch_new_member_defaults", tags=["Settings"])
async def api_fetch_new_member_defaults(auth=Depends(verify_api_key)):
    """Fetch default card price (B20) and base mins (B21) from Setting."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        return ok({"card_price": int_safe(ws.cell(20, 2).value),
                   "base_mins": int_safe(ws.cell(21, 2).value)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_rank_thresholds
# ═══════════════════════════════════════
@app.get("/api/fetch_rank_thresholds", tags=["Settings"])
async def api_fetch_rank_thresholds(auth=Depends(verify_api_key)):
    """Fetch Master and Immortal threshold from Setting!M3:M4."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        return ok({"master_threshold": int_safe(ws.cell(3, 13).value),
                   "immortal_threshold": int_safe(ws.cell(4, 13).value)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_bonus_table
# ═══════════════════════════════════════
@app.get("/api/fetch_bonus_table", tags=["Settings"])
async def api_fetch_bonus_table(auth=Depends(verify_api_key)):
    """Fetch bonus table from Setting!O2:R5."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        rows = ws.get("O2:R5")
        result = []
        for row in rows:
            if len(row) < 4:
                continue
            try:
                thresh = int_safe(row[0])
                w_b = int_safe(row[1])
                m_b = int_safe(row[2])
                i_b = int_safe(row[3])
                if thresh > 0 or any([w_b, m_b, i_b]):
                    result.append({
                        "threshold": thresh, "warrior_bonus": w_b,
                        "master_bonus": m_b, "immortal_bonus": i_b,
                    })
            except Exception:
                continue
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_rank_table_display
# ═══════════════════════════════════════
@app.get("/api/fetch_rank_table_display", tags=["Settings"])
async def api_fetch_rank_table_display(auth=Depends(verify_api_key)):
    """Fetch Setting!O1:R5 and return formatted string table for display."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        rows = ws.get("O1:R5")
        if not rows:
            return ok("_(data not available)_")
        lines = [
            f"{'Amount (Ks)':<14} {'Warrior':>9} {'Master':>9} {'Immortal':>10}",
            "-" * 48,
        ]
        for row in rows[1:]:
            if len(row) < 4:
                continue
            amt = int_safe(row[0])
            if amt == 0:
                continue
            lines.append(f"{amt:>14,}  {int_safe(row[1]):>8,}  {int_safe(row[2]):>8,}  {int_safe(row[3]):>9,}")
        return ok("\n".join(lines))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_alltime_effective_rate
# ═══════════════════════════════════════
@app.get("/api/fetch_alltime_effective_rate", tags=["Analytics"])
async def api_fetch_alltime_effective_rate(auth=Depends(verify_api_key)):
    """Calculate all-time average Ks/min across every TopUp_Log row."""
    try:
        ws = get_worksheet(SHEET_TOPUP_LOG)
        rows = ws.get_all_values()
        total_ks, total_mins = 0, 0
        for row in rows[1:]:
            if len(row) < 5:
                continue
            ks = int_safe(row[3]) if row[3].strip() else 0
            mins = int_safe(row[4]) if row[4].strip() else 0
            if ks and mins:
                total_ks += ks
                total_mins += mins
        rate = round(total_ks / total_mins, 4) if total_mins > 0 else 0.0
        return ok(rate)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_effective_rate
# ═══════════════════════════════════════
@app.get("/api/fetch_member_effective_rate/{member_id}", tags=["Members"])
async def api_fetch_member_effective_rate(member_id: str, auth=Depends(verify_api_key)):
    """Fetch a member's stored effective rate from Card_Wallet col L."""
    try:
        for row in get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                val = row[11].strip() if len(row) > 11 else ""
                return ok(float(val) if val else 0.0)
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  build_member_rate_dict
# ═══════════════════════════════════════
@app.get("/api/build_member_rate_dict", tags=["Members"])
async def api_build_member_rate_dict(auth=Depends(verify_api_key)):
    """Build dict of member_id -> stored effective rate from Card_Wallet."""
    try:
        result = {}
        for row in get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip():
                m_id = row[1].strip()
                val = row[11].strip() if len(row) > 11 else ""
                if val:
                    try:
                        result[m_id] = float(val)
                    except ValueError:
                        pass
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_base_salaries
# ═══════════════════════════════════════
@app.get("/api/fetch_base_salaries", tags=["Staff"])
async def api_fetch_base_salaries(auth=Depends(verify_api_key)):
    """Fetch staff base salaries from Setting!S:T columns."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        staff = ws.col_values(19)[1:]
        salaries = ws.col_values(20)[1:]
        result = {}
        for i, name in enumerate(staff):
            name = name.strip()
            if not name:
                continue
            sal_str = salaries[i].strip() if i < len(salaries) else "0"
            result[name] = int_safe(sal_str)
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_attendance
# ═══════════════════════════════════════
@app.get("/api/fetch_attendance/{month_str}", tags=["Attendance"])
async def api_fetch_attendance(month_str: str, auth=Depends(verify_api_key)):
    """Fetch attendance records for a month from Attendance_Log."""
    try:
        ws = get_worksheet(SHEET_ATTENDANCE_LOG)
        rows = ws.get_all_values()
        result = {}
        for row in rows[1:]:
            if len(row) < 4:
                continue
            if row[0].strip() != month_str:
                continue
            staff = row[1].strip()
            if not staff:
                continue
            result[staff] = {
                "leave_days": int_safe(row[2]) if len(row) > 2 else 0,
                "late_count": int_safe(row[3]) if len(row) > 3 else 0,
                "deduct_per_late": int_safe(row[4]) if len(row) > 4 and row[4].strip() else 500,
            }
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_salary_advances
# ═══════════════════════════════════════
@app.get("/api/fetch_salary_advances/{month_str}", tags=["Attendance"])
async def api_fetch_salary_advances(month_str: str, auth=Depends(verify_api_key)):
    """Return {staff: {total, cash, kpay}} for the given month (YYYY-MM)."""
    try:
        ws = get_worksheet(SHEET_SALARY_ADVANCE)
        rows = ws.get_all_values()
        result = {}
        for row in rows[1:]:
            if len(row) < 5:
                continue
            date_val = row[0].strip()
            staff = row[1].strip()
            if not staff or not date_val:
                continue
            if month_str.replace("-", "/") not in date_val and month_str not in date_val:
                continue
            amt = int_safe(row[2]) if row[2].strip() else 0
            pay_method = row[3].strip().upper() if len(row) > 3 else "CASH"
            if staff not in result:
                result[staff] = {"total": 0, "cash": 0, "kpay": 0}
            result[staff]["total"] += amt
            if "KPAY" in pay_method:
                result[staff]["kpay"] += amt
            else:
                result[staff]["cash"] += amt
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_promotions_cached
# ═══════════════════════════════════════
@app.get("/api/fetch_promotions_cached", tags=["Promotions"])
async def api_fetch_promotions_cached(auth=Depends(verify_api_key)):
    """Fetch active promotions (extend w/ real API)."""
    return ok([], "Promotions API not yet integrated")


# ═══════════════════════════════════════
#  fetch_allowed_staff_ids
# ═══════════════════════════════════════
@app.get("/api/fetch_allowed_staff_ids", tags=["Staff"])
async def api_fetch_allowed_staff_ids(auth=Depends(verify_api_key)):
    """Fetch dynamic staff whitelist from Setting!B30."""
    data = _fetch_allowed_staff_ids_from_mysql()
    if data is not None:
        return ok(data)
    try:
        ws = get_worksheet(SHEET_SETTING)
        val = ws.cell(30, 2).value
        if not val:
            return ok([])
        ids = [int(x.strip()) for x in str(val).split(",") if x.strip().isdigit()]
        return ok(ids)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  next_voucher / next_member_id / next_member_row_no
# ═══════════════════════════════════════
@app.get("/api/next_voucher", tags=["Sales"])
async def api_next_voucher(auth=Depends(verify_api_key)):
    """Generate next voucher number from Sales_Daily col B."""
    try:
        ws = get_worksheet(SHEET_SALES_DAILY)
        col = ws.col_values(2)
        ids = [v for v in col[1:] if v.upper().startswith("V-")]
        if ids:
            try:
                return ok(f"V-{int(ids[-1].split('-')[1]) + 1:03d}")
            except (IndexError, ValueError):
                pass
        return ok("V-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/next_member_id", tags=["Members"])
async def api_next_member_id(auth=Depends(verify_api_key)):
    """Auto-increment member ID: PSV_A_003 -> PSV_A_004."""
    try:
        ws = get_worksheet(SHEET_CARD_WALLET)
        ids = [v.strip() for v in ws.col_values(2)[1:] if v.strip()]
        if not ids:
            return ok("PSV_A_001")
        last = ids[-1]
        m = re.search(r'(\d+)$', last)
        if m:
            prefix = last[:m.start()]
            num = int(m.group(1)) + 1
            width = len(m.group(1))
            return ok(f"{prefix}{num:0{width}d}")
        return ok(last + "_1")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/next_member_row_no", tags=["Members"])
async def api_next_member_row_no(auth=Depends(verify_api_key)):
    """Return next sequential row number for Card_Wallet Column A."""
    try:
        ws = get_worksheet(SHEET_CARD_WALLET)
        col_a = ws.col_values(1)[1:]
        nums = []
        for v in col_a:
            try:
                nums.append(int(str(v).strip()))
            except (ValueError, TypeError):
                pass
        return ok((max(nums) + 1) if nums else 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_referral_code
# ═══════════════════════════════════════
@app.get("/api/fetch_referral_code/{member_id}", tags=["Members"])
async def api_fetch_referral_code(member_id: str, auth=Depends(verify_api_key)):
    """Fetch referral code for a member from Card_Wallet."""
    try:
        for row in get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                code = row[13].strip() if len(row) > 13 else ""
                return ok(code or None)
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — create_booking
# ═══════════════════════════════════════
@app.post("/api/create_booking", tags=["Bookings"])
async def api_create_booking(req: dict, auth=Depends(verify_api_key)):
    """Append a row to Console_Booking and return BookingID."""
    try:
        sh = get_worksheet(SHEET_CONSOLE_BOOKING)
        now = now_mmt()
        date = now.strftime("%-m/%-d/%Y")
        time_s = now.strftime("%H:%M")
        seq = now.strftime("%H%M")
        console_id = req.get("console_id", "")
        member_id = req.get("member_id", "")
        staff = req.get("staff", "")
        notes = req.get("notes", "")
        bk_id = f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ','').replace('-','')}-{seq}"
        sh.append_row([bk_id, date, console_id, member_id, time_s, "", "Active", staff, notes],
                      value_input_option="USER_ENTERED")
        invalidate_cache("bookings")
        return ok({"booking_id": bk_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — end_booking
# ═══════════════════════════════════════
@app.put("/api/end_booking/{booking_id}", tags=["Bookings"])
async def api_end_booking(booking_id: str, auth=Depends(verify_api_key)):
    """Mark a booking as Done and fill EndTime."""
    try:
        sh = get_worksheet(SHEET_CONSOLE_BOOKING)
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == booking_id:
                now = now_mmt()
                sh.update(f"F{i}:G{i}", [[now.strftime("%H:%M"), "Done"]])
                invalidate_cache("bookings")
                return ok({"booking_id": booking_id, "status": "Done"})
        raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — cancel_booking
# ═══════════════════════════════════════
@app.put("/api/cancel_booking/{booking_id}", tags=["Bookings"])
async def api_cancel_booking(booking_id: str, auth=Depends(verify_api_key)):
    """Mark a booking as Cancelled."""
    try:
        sh = get_worksheet(SHEET_CONSOLE_BOOKING)
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == booking_id:
                sh.update(f"G{i}", [["Cancelled"]])
                invalidate_cache("bookings")
                return ok({"booking_id": booking_id, "status": "Cancelled"})
        raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#  CREATE/UPDATE — bookings (customer bot format)
# ═══════════════════════════════════════
@app.post("/api/bookings", tags=["Bookings"])
async def api_bookings(req: dict, auth=Depends(verify_api_key)):
    """Create a booking from customer bot payload."""
    try:
        import json as _json
        sh = get_worksheet(SHEET_CONSOLE_BOOKING)
        now = now_mmt()
        date_formatted = now.strftime("%-m/%-d/%Y")
        time_s = now.strftime("%H:%M")
        seq = now.strftime("%H%M")
        console_type = req.get("consoleType", "")
        console_id = console_type  # Store as-is

        notes_data = {
            "customerName": req.get("customerName", ""),
            "phone": req.get("phone", ""),
            "timeSlot": req.get("timeSlot", ""),
            "consoleType": req.get("consoleType", ""),
            "durationMins": req.get("durationMins", 0),
            "gameName": req.get("gameName", ""),
            "telegramChatId": req.get("telegramChatId", ""),
            "username": req.get("username", ""),
        }
        notes_json = _json.dumps(notes_data)
        bk_id = f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ', '').replace('-', '')}-{seq}"
        sh.append_row([bk_id, date_formatted, console_id, "", time_s, "", "Pending", "", notes_json],
                      value_input_option="USER_ENTERED")
        invalidate_cache("bookings")
        return ok({"id": bk_id, "status": "Pending"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




# ═══════════════════════════════════════
#  MUTATION — save_attendance
# ═══════════════════════════════════════
@app.post("/api/save_attendance", tags=["Attendance"])
async def api_save_attendance(req: dict, auth=Depends(verify_api_key)):
    """Save/update attendance record for a staff in Attendance_Log."""
    try:
        month_str = req.get("month_str", "")
        staff = req.get("staff", "")
        leave_days = req.get("leave_days", 0)
        late_count = req.get("late_count", 0)
        deduct_per_late = req.get("deduct_per_late", 500)

        sh = get_worksheet(SHEET_ATTENDANCE_LOG)
        rows = sh.get_all_values()
        found = False
        for i, row in enumerate(rows[1:], start=2):
            if row[0].strip() == month_str and row[1].strip() == staff:
                sh.update(f"A{i}:E{i}", [[month_str, staff, leave_days, late_count, deduct_per_late]])
                found = True
                break
        if not found:
            sh.append_row([month_str, staff, leave_days, late_count, deduct_per_late])
        return ok({"staff": staff, "month": month_str})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — save_receipt_json
# ═══════════════════════════════════════
@app.post("/api/save_receipt_json", tags=["Receipts"])
async def api_save_receipt_json(req: dict, auth=Depends(verify_api_key)):
    """Persist receipt data to MySQL."""
    try:
        receipt_no = req.get("receipt_no", "")
        member_id = req.get("member_id", "")
        amount = req.get("amount", 0)
        payment_method = req.get("payment_method", "")
        items = req.get("items", "")
        receipt_date = req.get("receipt_date", "")
        staff_name = req.get("staff_name", "")

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
@app.post("/api/add_console_game", tags=["Games"])
async def api_add_console_game(req: dict, auth=Depends(verify_api_key)):
    """Add a game installation record to Console_Games."""
    try:
        console_id = req.get("console_id", "")
        game_title = req.get("game_title", "")
        install_type = req.get("install_type", "")
        notes = req.get("notes", "")

        sh = get_worksheet(SHEET_CONSOLE_GAMES)
        date = now_mmt().strftime("%-m/%-d/%Y")
        sh.append_row([console_id, game_title, install_type, date, notes],
                      value_input_option="USER_ENTERED")
        invalidate_cache("console_games")
        return ok({"console_id": console_id, "game_title": game_title})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/remove_console_game", tags=["Games"])
async def api_remove_console_game(req: dict, auth=Depends(verify_api_key)):
    """Remove a game installation record from Console_Games."""
    try:
        console_id = req.get("console_id", "")
        game_title = req.get("game_title", "")

        sh = get_worksheet(SHEET_CONSOLE_GAMES)
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if (len(row) >= 2
                    and row[0].strip().upper() == console_id.strip().upper()
                    and row[1].strip().lower() == game_title.strip().lower()):
                sh.delete_rows(i)
                invalidate_cache("console_games")
                return ok({"console_id": console_id, "game_title": game_title})
        raise HTTPException(status_code=404, detail=f"Game {game_title} not found on console {console_id}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — set_game_disc_count
# ═══════════════════════════════════════
@app.put("/api/set_game_disc_count", tags=["Games"])
async def api_set_game_disc_count(req: dict, auth=Depends(verify_api_key)):
    """Update column D (Available Discs) for a game row in Game_Library."""
    try:
        row_num = req.get("row_num", 0)
        count = req.get("count", 0)
        sh = get_worksheet(SHEET_GAME_LIBRARY)
        sh.update_cell(row_num, 4, count)
        invalidate_cache("games")
        return ok({"row_num": row_num, "count": count})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — update_game_library_install
# ═══════════════════════════════════════
@app.put("/api/update_game_library_install", tags=["Games"])
async def api_update_game_library_install(req: dict, auth=Depends(verify_api_key)):
    """Set TRUE/FALSE checkbox in Game_Library for (game_title, console_id)."""
    try:
        game_title = req.get("game_title", "")
        console_id = req.get("console_id", "")
        installed = req.get("installed", False)

        sh = get_worksheet(SHEET_GAME_LIBRARY)
        rows = sh.get_all_values()
        if not rows:
            raise HTTPException(status_code=404, detail="Game_Library sheet is empty")

        cid_norm = _norm_cid(console_id)
        col_idx = None
        for i, h in enumerate(rows[0]):
            if _norm_cid(h) == cid_norm:
                col_idx = i
                break
        if col_idx is None:
            raise HTTPException(status_code=404, detail=f"Console {console_id} not found in headers")

        game_lower = game_title.strip().lower()
        row_idx = None
        for i, row in enumerate(rows[1:], start=2):
            cell_val = row[1].strip().lower() if len(row) > 1 else ""
            if cell_val == game_lower:
                row_idx = i
                break
        if row_idx is None:
            raise HTTPException(status_code=404, detail=f"Game {game_title} not found")

        n = col_idx + 1
        col_letter = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            col_letter = chr(65 + r) + col_letter
        cell_addr = f"{col_letter}{row_idx}"

        sh.update(cell_addr, [[True if installed else ""]])
        invalidate_cache("games")
        return ok({"game": game_title, "console": console_id, "installed": installed})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — update_member_effective_rate
# ═══════════════════════════════════════
@app.put("/api/update_member_effective_rate", tags=["Members"])
async def api_update_member_effective_rate(req: dict, auth=Depends(verify_api_key)):
    """Update or insert member effective rate in Card_Wallet col L."""
    try:
        member_id = req.get("member_id", "")
        rate = req.get("rate", 0.0)
        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                ws.update_cell(i, 12, round(float(rate), 4))
                invalidate_cache("members")
                return ok({"member_id": member_id, "rate": rate})
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — save_referral_code
# ═══════════════════════════════════════
@app.post("/api/save_referral_code", tags=["Members"])
async def api_save_referral_code(req: dict, auth=Depends(verify_api_key)):
    """Save referral code for a member in Card_Wallet."""
    try:
        member_id = req.get("member_id", "")
        code = req.get("code", "")
        ws = get_worksheet(SHEET_CARD_WALLET)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                ws.update_cell(i, 14, code)
                invalidate_cache("members")
                return ok({"member_id": member_id, "code": code})
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — add_console_to_setting
# ═══════════════════════════════════════
@app.post("/api/add_console_to_setting", tags=["Console"])
async def api_add_console_to_setting(req: dict, auth=Depends(verify_api_key)):
    """Append a new console to Setting!H:J."""
    try:
        console_id = req.get("console_id", "")
        ctype = req.get("ctype", "")
        multiplier = req.get("multiplier", 1.0)
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)
        next_row = len(names) + 1
        ws.update(f"H{next_row}:J{next_row}", [[console_id, ctype, str(multiplier)]],
                  value_input_option="USER_ENTERED")
        return ok({"console_id": console_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  MUTATION — remove_console_from_setting
# ═══════════════════════════════════════
@app.delete("/api/remove_console_from_setting/{console_id}", tags=["Console"])
async def api_remove_console_from_setting(console_id: str, auth=Depends(verify_api_key)):
    """Clear a console row from Setting!H:J."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        names = ws.col_values(8)
        for i, name in enumerate(names):
            if name.strip() == console_id.strip():
                row = i + 1
                ws.update(f"H{row}:J{row}", [["", "", ""]])
                return ok({"console_id": console_id})
        raise HTTPException(status_code=404, detail=f"Console {console_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  META — Config (used by bot cache)
# ═══════════════════════════════════════
@app.get("/api/sheets/config", tags=["Meta"])
async def api_sheets_config(auth=Depends(verify_api_key)):
    """Return cached config used by the bot (base_rate, thresholds, etc.)."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        base_rate = int_safe(ws.cell(2, 2).value)
        master_thresh = int_safe(ws.cell(3, 13).value)
        immortal_thresh = int_safe(ws.cell(4, 13).value)
        card_price = int_safe(ws.cell(20, 2).value)
        base_mins = int_safe(ws.cell(21, 2).value)

        names = ws.col_values(8)[1:]
        mults = ws.col_values(10)[1:]
        console_multipliers = {}
        for name, mult in zip(names, mults):
            if name.strip():
                try:
                    console_multipliers[name.strip()] = float(float_safe(mult)) or 1.0
                except ValueError:
                    console_multipliers[name.strip()] = 1.0

        food_names = ws.col_values(4)[1:]
        food_prices_raw = ws.col_values(5)[1:]
        food_costs_raw = ws.col_values(6)[1:]
        food_prices = {}
        food_costs = {}
        for n, p, c in zip(food_names, food_prices_raw, food_costs_raw):
            if n.strip():
                food_prices[n.strip()] = int_safe(p)
                food_costs[n.strip()] = int_safe(c) if str(c).strip() else 0

        bonus_rows = ws.get("O2:R5")
        bonus_table = []
        for row in bonus_rows:
            if len(row) >= 4:
                try:
                    bonus_table.append([int_safe(row[0]), int_safe(row[1]),
                                        int_safe(row[2]), int_safe(row[3])])
                except Exception:
                    continue

        return ok({
            "base_rate": base_rate,
            "master_threshold": master_thresh,
            "immortal_threshold": immortal_thresh,
            "new_member_card_price": card_price,
            "new_member_base_mins": base_mins,
            "console_multipliers": console_multipliers,
            "food_prices": food_prices,
            "food_costs": food_costs,
            "bonus_table": bonus_table,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ═══════════════════════════════════════
#  BI DASHBOARD — Analytics Endpoints
# ═══════════════════════════════════════
@app.get("/api/analytics/daily_sales", tags=["Analytics"])
async def api_analytics_daily_sales(
    date: str = Query(None, description="Date in M/D/YYYY format, defaults to today"),
    auth=Depends(verify_api_key),
):
    """Return today's (or specified date's) sales KPIs from Sales_Daily."""
    data = _fetch_daily_sales_from_mysql(date)
    if data is not None:
        return ok(data)
    try:
        from analytics import get_daily_sales
        return ok(get_daily_sales(date))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/topups", tags=["Analytics"])
async def api_analytics_topups(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    auth=Depends(verify_api_key),
):
    """Return top-up trends: daily/weekly aggregates, all-time effective rate, top members."""
    data = _fetch_topups_from_mysql(days)
    if data is not None:
        return ok({"topup_log": data, "total_topups": len(data)})
    try:
        from analytics import get_topup_trends
        return ok(get_topup_trends(days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/member_activity", tags=["Analytics"])
async def api_analytics_member_activity(auth=Depends(verify_api_key)):
    """Return member activity stats: total members, tier distribution, active today, wallet totals."""
    try:
        from analytics import get_member_activity
        return ok(get_member_activity())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/console_usage", tags=["Analytics"])
async def api_analytics_console_usage(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    auth=Depends(verify_api_key),
):
    """Return console usage stats: bookings per console, utilization rate, daily series."""
    try:
        from analytics import get_console_usage
        return ok(get_console_usage(days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/dashboard", tags=["Analytics"])
async def api_analytics_dashboard(auth=Depends(verify_api_key)):
    """Return full BI dashboard summary with all KPIs."""
    try:
        from analytics import get_dashboard_summary
        return ok(get_dashboard_summary())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/weekly_trends", tags=["Analytics"])
async def api_analytics_weekly_trends(
    weeks: int = Query(4, ge=1, le=52, description="Number of weeks to analyze"),
    auth=Depends(verify_api_key),
):
    """Return weekly trends: sales, top-ups, and console usage aggregated by week."""
    try:
        from analytics import get_topup_trends, get_console_usage, get_daily_sales
        days = weeks * 7
        topups = get_topup_trends(days)
        consoles = get_console_usage(days)
        return ok({
            "period_weeks": weeks,
            "topup_weekly": topups.get("weekly_aggregates", []),
            "topup_daily": topups.get("daily_series", []),
            "console_daily": consoles.get("daily_series", []),
            "console_summary": {
                "total_bookings": consoles["total_bookings"],
                "utilization_rate": consoles.get("avg_bookings_per_console_day", 0),
                "active_now": consoles["active_now"],
            },
            "all_time_rate": topups.get("all_time_effective_rate", 0),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  BI DASHBOARD — Web Dashboard (HTML)
# ═══════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>PS VIBE — BI Dashboard</title>
<style>
:root {
  --bg: #0a0e17;
  --card: #111827;
  --border: #1f2937;
  --text: #e5e7eb;
  --dim: #9ca3af;
  --accent: #6366f1;
  --green: #10b981;
  --amber: #f59e0b;
  --red: #ef4444;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: var(--bg); color: var(--text); line-height:1.5; }
.header { background: linear-gradient(135deg, #1e1b4b, #111827); padding: 1.5rem 2rem;
          border-bottom: 1px solid var(--border); display:flex; justify-content:space-between;
          align-items:center; flex-wrap:wrap; gap:1rem; }
.header h1 { font-size: 1.75rem; font-weight: 700; }
.header h1 span { color: var(--accent); }
.header .ts { font-size: 0.85rem; color: var(--dim); }
.container { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
.kpi-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap:1rem; margin-bottom: 1.5rem; }
.kpi-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
            padding: 1.25rem; }
.kpi-card .label { font-size:0.8rem; color:var(--dim); text-transform:uppercase;
                   letter-spacing:0.05em; margin-bottom:0.25rem; }
.kpi-card .value { font-size:1.75rem; font-weight:700; }
.kpi-card .value.accent { color: var(--accent); }
.kpi-card .value.green { color: var(--green); }
.kpi-card .value.amber { color: var(--amber); }
.row { display:grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap:1.5rem; }
.panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
         overflow:hidden; margin-bottom:1.5rem; }
.panel h3 { padding:1rem 1.25rem; border-bottom:1px solid var(--border); font-size:1rem;
            font-weight:600; }
.panel-body { padding:1.25rem; }
table { width:100%; border-collapse:collapse; font-size:0.9rem; }
th, td { padding:0.6rem 0.75rem; text-align:left; border-bottom:1px solid var(--border); }
th { color: var(--dim); font-weight:500; font-size:0.8rem; text-transform:uppercase; }
tr:hover { background: rgba(255,255,255,0.02); }
.bar { height:8px; border-radius:4px; background:var(--border); overflow:hidden; margin-top:0.25rem; }
.bar-fill { height:100%; border-radius:4px; transition: width 0.5s; }
.loading { text-align:center; padding:3rem; color:var(--dim); }
.error { background: rgba(239,68,68,0.1); color: var(--red); padding:1rem; border-radius:8px; }
.footer { text-align:center; padding:2rem; color:var(--dim); font-size:0.8rem;
          border-top:1px solid var(--border); margin-top:2rem; }
</style>
</head>
<body>
<div class="header">
  <h1>🎮 PS <span>VIBE</span> — BI Dashboard</h1>
  <div class="ts" id="ts">Loading...</div>
</div>
<div class="container" id="app"><div class="loading">Loading dashboard data...</div></div>
<div class="footer">PS VIBE Gaming Lounge &copy; 2026 · BI Dashboard v3.0</div>
<script>
const F = (n) => n != null ? Number(n).toLocaleString() : '-';
const FKS = (n) => n != null ? Number(n).toLocaleString() + ' Ks' : '-';
const P = (n, t) => n != null && t > 0 ? (n/t*100).toFixed(1) + '%' : '0%';

async function load() {
  try {
    const r = await fetch(DASHBOARD_API_URL);
    const d = (await r.json()).data;
    if (!d) throw new Error('No data');
    render(d);
    document.getElementById('ts').textContent = 'Generated: ' + d.generated_at;
  } catch(e) {
    document.getElementById('app').innerHTML =
      '<div class="error">⚠ Failed to load dashboard: ' + e.message + '</div>';
  }
}

function render(d) {
  const s = d.summary, sales = d.daily_sales, mem = d.member_activity,
        con = d.console_usage_today, tup = d.topup_trends_7d;
  let h = '';
  h += '<div class="kpi-grid">';
  h += kpi('Today Sales', FKS(s.today_sales_ks), 'accent');
  h += kpi('Vouchers', F(s.today_vouchers));
  h += kpi('Avg Ticket', FKS(s.today_avg_ticket_ks));
  h += kpi('Active Members', F(s.active_members_today), 'green');
  h += kpi('Active Consoles', s.active_consoles + '/' + s.total_consoles);
  h += kpi('Week Top-ups', FKS(s.week_topup_ks), 'amber');
  h += '</div>';

  h += '<div class="row">';
  h += '<div class="panel"><h3>📊 Today's Sales Breakdown</h3><div class="panel-body">';
  if (sales.by_payment && Object.keys(sales.by_payment).length) {
    h += '<table><tr><th>Payment</th><th>Count</th><th>Amount</th></tr>';
    for (const [k,v] of Object.entries(sales.by_payment)) {
      h += '<tr><td>'+k+'</td><td>'+F(v.count)+'</td><td>'+FKS(v.amount)+'</td></tr>';
    }
    h += '</table>';
  } else { h += '<p style="color:var(--dim)">No sales recorded today</p>'; }
  h += '</div></div>';

  h += '<div class="panel"><h3>👥 Member Activity</h3><div class="panel-body">';
  h += '<table>';
  h += '<tr><td>Total Members</td><td><strong>'+F(mem.total_members)+'</strong></td></tr>';
  h += '<tr><td>Active Today</td><td><strong style="color:var(--green)">'+F(mem.active_today)+'</strong></td></tr>';
  h += '<tr><td>Active (Last 7d)</td><td>'+F(mem.active_last_7d)+'</td></tr>';
  h += '<tr><td>Total Wallet Mins</td><td>'+F(mem.total_wallet_mins)+'</td></tr>';
  h += '<tr><td>Avg Spend/Member</td><td>'+FKS(mem.avg_spend_per_member)+'</td></tr>';
  h += '</table>';
  if (mem.tier_distribution && mem.tier_distribution.length) {
    h += '<h4 style="margin:1rem 0 0.5rem">Tier Distribution</h4><table><tr><th>Tier</th><th>Members</th><th>Share</th><th></th></tr>';
    for (const t of mem.tier_distribution) {
      h += '<tr><td>'+t.tier+'</td><td>'+F(t.count)+'</td><td>'+t.pct+'%</td>';
      h += '<td style="width:120px"><div class="bar"><div class="bar-fill" style="width:'+t.pct+'%;background:var(--accent)"></div></div></td></tr>';
    }
    h += '</table>';
  }
  h += '</div></div>';
  h += '</div>';

  h += '<div class="row">';
  h += '<div class="panel"><h3>🎮 Console Usage (Today)</h3><div class="panel-body">';
  if (con.consoles && con.consoles.length) {
    h += '<table><tr><th>Console</th><th>Type</th><th>Bookings</th><th>Hours</th><th>Active</th></tr>';
    for (const c of con.consoles) {
      h += '<tr><td><strong>'+c.console_id+'</strong></td><td>'+c.type+'</td><td>'+F(c.total_bookings)+'</td>';
      h += '<td>'+c.total_hours+'h</td>';
      h += '<td>'+(c.active_bookings>0?'<span style="color:var(--green)">● Active</span>':'<span style="color:var(--dim)">○ Free</span>')+'</td></tr>';
    }
    h += '</table>';
  } else { h += '<p style="color:var(--dim)">No console data available</p>'; }
  h += '</div></div>';

  h += '<div class="panel"><h3>💰 Top-Up Trends (7 Days)</h3><div class="panel-body">';
  h += '<table>';
  h += '<tr><td>Total Top-ups</td><td><strong>'+F(tup.total_topups)+'</strong></td></tr>';
  h += '<tr><td>Total Amount</td><td><strong style="color:var(--amber)">'+FKS(tup.total_amount_ks)+'</strong></td></tr>';
  h += '<tr><td>Total Mins</td><td>'+F(tup.total_mins)+'</td></tr>';
  h += '<tr><td>All-Time Eff. Rate</td><td>'+tup.all_time_effective_rate+' Ks/min</td></tr>';
  h += '</table>';
  if (tup.top_members && tup.top_members.length) {
    h += '<h4 style="margin:1rem 0 0.5rem">Top Top-Up Members</h4>';
    h += '<table><tr><th>#</th><th>Member</th><th>Amount</th><th>Mins</th><th>Rate</th></tr>';
    tup.top_members.slice(0,5).forEach((m,i) => {
      h += '<tr><td>'+(i+1)+'</td><td><strong>'+m.member_id+'</strong></td><td>'+FKS(m.amount)+'</td><td>'+F(m.mins)+'</td><td>'+m.rate+' Ks/min</td></tr>';
    });
    h += '</table>';
  }
  h += '</div></div>';
  h += '</div>';

  if (tup.daily_series && tup.daily_series.length) {
    h += '<div class="panel"><h3>📈 Daily Top-Up Trend</h3><div class="panel-body">';
    h += '<table><tr><th>Date</th><th>Count</th><th>Amount</th><th>Mins</th><th>Rate</th></tr>';
    tup.daily_series.slice(-7).reverse().forEach(d => {
      h += '<tr><td>'+d.date+'</td><td>'+F(d.count)+'</td><td>'+FKS(d.amount)+'</td><td>'+F(d.mins)+'</td><td>'+d.rate+'</td></tr>';
    });
    h += '</table></div></div>';
  }

  document.getElementById('app').innerHTML = h;
}

function kpi(label, value, cls) {
  return '<div class="kpi-card"><div class="label">'+label+'</div><div class="value'+(cls?' '+cls:'')+'">'+value+'</div></div>';
}

load();
</script>
</body>
</html>"""

@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def web_dashboard():
    """Serve the BI web dashboard with embedded API key."""
    api_url = f"/api/analytics/dashboard?api_key={API_KEY}" if API_KEY else "/api/analytics/dashboard"
    html = DASHBOARD_HTML.replace("DASHBOARD_API_URL", f"'{api_url}'")
    return HTMLResponse(html)

@app.get("/api/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def web_dashboard_api():
    """Alias: Serve the BI web dashboard."""
    api_url = f"/api/analytics/dashboard?api_key={API_KEY}" if API_KEY else "/api/analytics/dashboard"
    html = DASHBOARD_HTML.replace("DASHBOARD_API_URL", f"'{api_url}'")
    return HTMLResponse(html)

# ═══════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════

# ═══════════════════════════════════════
#  BOOKING QUERIES — bookings/search
# ═══════════════════════════════════════
@app.get("/api/bookings/search", tags=["Bookings"])
async def api_bookings_search(
    telegram_chat_id: str = Query(None),
    date: str = Query(None),
    status: str = Query(None),
    auth=Depends(verify_api_key),
):
    """Search bookings by telegram_chat_id, date, and/or status."""
    try:
        rows = get_booking_rows()
        results = []
        for row in rows[1:]:
            if not row or len(row) < 7:
                continue
            bkid = row[0].strip() if len(row) > 0 else ""
            bk_date = row[1].strip() if len(row) > 1 else ""
            console_id = row[2].strip() if len(row) > 2 else ""
            member_id = row[3].strip() if len(row) > 3 else ""
            time_slot = row[4].strip() if len(row) > 4 else ""
            end_time = row[5].strip() if len(row) > 5 else ""
            bk_status = row[6].strip() if len(row) > 6 else ""
            staff = row[7].strip() if len(row) > 7 else ""
            notes = row[8].strip() if len(row) > 8 else ""

            # Apply filters
            if date and bk_date != date:
                continue
            if status and bk_status.lower() != status.lower():
                continue
            if telegram_chat_id:
                tg_col = row[9].strip() if len(row) > 9 else ""
                if tg_col != telegram_chat_id:
                    continue

            results.append({
                "booking_id": bkid,
                "date": bk_date,
                "console_id": console_id,
                "member_id": member_id,
                "timeSlot": time_slot,
                "endTime": end_time,
                "status": bk_status,
                "staff": staff,
                "notes": notes,
                "telegramChatId": tg_col if len(row) > 9 else "",
                "consoleType": "",
                "gameName": "",
                "durationMins": 0,
            })
        return ok(results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  BOOKING — get by ID
# ═══════════════════════════════════════
@app.get("/api/bookings/{booking_id}", tags=["Bookings"])
async def api_get_booking(booking_id: str, auth=Depends(verify_api_key)):
    """Get a single booking by ID."""
    try:
        rows = get_booking_rows()
        for row in rows[1:]:
            if row and row[0].strip() == booking_id:
                return ok({
                    "booking_id": row[0].strip(),
                    "date": row[1].strip() if len(row) > 1 else "",
                    "console_id": row[2].strip() if len(row) > 2 else "",
                    "member_id": row[3].strip() if len(row) > 3 else "",
                    "timeSlot": row[4].strip() if len(row) > 4 else "",
                    "endTime": row[5].strip() if len(row) > 5 else "",
                    "status": row[6].strip() if len(row) > 6 else "",
                    "staff": row[7].strip() if len(row) > 7 else "",
                    "notes": row[8].strip() if len(row) > 8 else "",
                    "telegramChatId": row[9].strip() if len(row) > 9 else "",
                })
        raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  FEEDBACK — submit
# ═══════════════════════════════════════
@app.post("/api/feedback/submit", tags=["Feedback"])
async def api_feedback_submit(req: dict, auth=Depends(verify_api_key)):
    """Accept customer feedback (fire-and-forget log)."""
    try:
        tg_id = req.get("tg_id", "")
        username = req.get("username", "")
        booking_id = req.get("booking_id", "")
        rating = req.get("rating", 0)
        comment = req.get("comment", "")
        logger.info("Feedback: tg=%s rating=%s bk=%s comment=%s", tg_id, rating, booking_id, comment[:80])
        return ok({"received": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  LOG — sheets log
# ═══════════════════════════════════════
@app.post("/api/sheets/log", tags=["Logging"])
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


# ═══════════════════════════════════════
#  BOT USERS — track
# ═══════════════════════════════════════
@app.post("/api/bot-users/track", tags=["Bot Users"])
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
#  P4 Phase 1 — NEW ENDPOINTS (auto-generated 2026-05-29)
# ═══════════════════════════════════════

# ── Endpoint 1: POST /api/finance/opex ──
@app.post("/api/finance/opex", tags=["Finance"])
async def api_finance_opex(req: dict, auth=Depends(verify_api_key)):
    """Record an operational expense into accounts table."""
    try:
        import json as _json
        date = req.get("date", "")
        category = req.get("category", "")
        description = req.get("description", "")
        amount = req.get("amount", 0)
        payment_method = req.get("payment_method", "")
        staff_name = req.get("staff_name", "")

        notes = _json.dumps({
            "date": date,
            "payment_method": payment_method,
            "staff_name": staff_name
        })

        logger.info("Recording opex: %s | %s | %s", category, description, amount)

        mysql_execute(
            "INSERT INTO accounts (account_name, account_type, balance, notes) "
            "VALUES (%s, %s, %s, %s)",
            (description, category, amount, notes)
        )

        logger.info("Opex recorded: %s", description)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Opex save error: %s", str(e))
        return {"status": "error", "detail": str(e)}


# ── Endpoint 2: POST /api/staff/salary-advance ──
@app.post("/api/staff/salary-advance", tags=["Staff"])
async def api_staff_salary_advance(req: dict, auth=Depends(verify_api_key)):
    """Record a salary advance for a staff member."""
    try:
        staff_name = req.get("staff_name", "")
        amount = req.get("amount", 0)
        date = req.get("date", "")
        note = req.get("note", "")

        logger.info("Recording salary advance: %s | %s | %s", staff_name, amount, date)

        mysql_execute(
            "INSERT INTO salary_advance (staff_name, amount, advance_date, repayment_status, notes) "
            "VALUES (%s, %s, %s, %s, %s)",
            (staff_name, amount, date, "pending", note)
        )

        result = mysql_query_one("SELECT LAST_INSERT_ID() as id")
        insert_id = result["id"] if result else 0

        logger.info("Salary advance recorded: id=%s", insert_id)
        return {"status": "ok", "id": insert_id}
    except Exception as e:
        logger.error("Salary advance save error: %s", str(e))
        return {"status": "error", "detail": str(e)}


# ── Endpoint 3: POST /api/sales/record ──
@app.post("/api/sales/record", tags=["Sales"])
async def api_sales_record(req: dict, auth=Depends(verify_api_key)):
    """Record a daily sale into sales_daily table."""
    try:
        import json as _json
        receipt_no = req.get("receipt_no", "")
        items = req.get("items", "")
        amount = req.get("amount", 0)
        payment_method = req.get("payment_method", "")
        member_id = req.get("member_id", "")
        staff_name = req.get("staff_name", "")
        receipt_date = req.get("receipt_date", "")
        food_items = req.get("food_items", "")
        food_cost = req.get("food_cost", 0)

        notes = _json.dumps({
            "receipt_no": receipt_no,
            "items": items,
            "food_items": food_items,
            "food_cost": food_cost
        })

        logger.info("Recording sale: receipt=%s amount=%s", receipt_no, amount)

        mysql_execute(
            "INSERT INTO sales_daily (sale_date, member_id, amount, staff_name, payment_method, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (receipt_date, member_id, amount, staff_name, payment_method, notes)
        )

        logger.info("Sale recorded: receipt_no=%s", receipt_no)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Sale record error: %s", str(e))
        return {"status": "error", "detail": str(e)}


# ── Endpoint 4: POST /api/inventory/stock-out ──
@app.post("/api/inventory/stock-out", tags=["Inventory"])
async def api_inventory_stock_out(req: dict, auth=Depends(verify_api_key)):
    """Record a stock-out (consumption/usage) event."""
    try:
        import json as _json
        item_name = req.get("item_name", "")
        quantity = req.get("quantity", 0)
        unit = req.get("unit", "")
        reason = req.get("reason", "")
        staff_name = req.get("staff_name", "")
        date = req.get("date", "")

        notes = _json.dumps({
            "unit": unit,
            "reason": reason
        })

        logger.info("Recording stock-out: %s qty=%s", item_name, quantity)

        mysql_execute(
            "INSERT INTO stock_out (item_name, quantity, sale_date, staff_name, notes) "
            "VALUES (%s, %s, %s, %s, %s)",
            (item_name, quantity, date, staff_name, notes)
        )

        logger.info("Stock-out recorded: %s", item_name)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Stock-out save error: %s", str(e))
        return {"status": "error", "detail": str(e)}


# ── Endpoint 5: POST /api/inventory/stock-in ──
@app.post("/api/inventory/stock-in", tags=["Inventory"])
async def api_inventory_stock_in(req: dict, auth=Depends(verify_api_key)):
    """Record stock-in (restock/purchase) into inventory table."""
    try:
        item_name = req.get("item_name", "")
        quantity = req.get("quantity", 0)
        unit = req.get("unit", "")
        supplier = req.get("supplier", "")
        cost = req.get("cost", 0)
        staff_name = req.get("staff_name", "")
        date = req.get("date", "")

        logger.info("Recording stock-in: %s qty=%s from %s", item_name, quantity, supplier)

        # Map: supplier→category, cost→unit_price
        mysql_execute(
            "INSERT INTO inventory (item_name, category, quantity, unit_price) "
            "VALUES (%s, %s, %s, %s)",
            (item_name, supplier, quantity, cost)
        )

        logger.info("Stock-in recorded: %s", item_name)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Stock-in save error: %s", str(e))
        return {"status": "error", "detail": str(e)}


# ── Endpoint 6: POST /api/members/register ──
@app.post("/api/members/register", tags=["Members"])
async def api_members_register(req: dict, auth=Depends(verify_api_key)):
    """Register a new member and optionally create wallet entry."""
    try:
        name = req.get("name", "")
        phone = req.get("phone", "")
        email = req.get("email", "")
        join_date = req.get("join_date", "")
        staff_name = req.get("staff_name", "")

        # Generate a member_id
        member_id = f"M-{int(time.time())}"

        logger.info("Registering member: %s | phone=%s", name, phone)

        mysql_execute(
            "INSERT INTO members (member_id, name, phone) "
            "VALUES (%s, %s, %s)",
            (member_id, name, phone)
        )

        # Also create wallet entry
        try:
            mysql_execute(
                "INSERT IGNORE INTO member_wallets (member_id, member_name, phone, balance_mins) "
                "VALUES (%s, %s, %s, 0)",
                (member_id, name, phone)
            )
        except Exception as wallet_err:
            logger.warning("Could not create wallet for member %s: %s", member_id, str(wallet_err))

        logger.info("Member registered: member_id=%s", member_id)
        return {"status": "ok", "member_id": member_id}
    except Exception as e:
        logger.error("Member register error: %s", str(e))
        return {"status": "error", "detail": str(e)}


# ── Endpoint 7: POST /api/topup/log ──
@app.post("/api/topup/log", tags=["Topup"])
async def api_topup_log(req: dict, auth=Depends(verify_api_key)):
    """Log a top-up transaction into topup_log table."""
    try:
        member_id = req.get("member_id", "")
        amount = req.get("amount", 0)
        payment_method = req.get("payment_method", "")
        staff_name = req.get("staff_name", "")
        date = req.get("date", "")

        logger.info("Logging topup: member=%s amount=%s", member_id, amount)

        mysql_execute(
            "INSERT INTO topup_log (member_id, amount, topup_date, staff_name, payment_method) "
            "VALUES (%s, %s, %s, %s, %s)",
            (member_id, amount, date, staff_name, payment_method)
        )

        logger.info("Topup logged: member=%s amount=%s", member_id, amount)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Topup log error: %s", str(e))
        return {"status": "error", "detail": str(e)}



@app.on_event("startup")
async def startup():
    try:
        wb = get_workbook()
        logger.info("Connected to Google Sheets: %s", wb.title)
        sheets = [s.title for s in wb.worksheets()]
        logger.info("Available sheets: %s", sheets)
    except Exception as e:
        logger.warning("Could not connect to Google Sheets on startup: %s", e)


import patch_routes
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
