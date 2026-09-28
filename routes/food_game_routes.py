"""PS VIBE API — Food & Game Library Routes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import ok, error_response, _mysql_get_setting
from deps import verify_api_key
from models import GenericResponse
import logging
from datetime import datetime
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Food & Games'])

FOOD_CATEGORIES = ("Food", "Drinks", "Instant Noodles", "Snacks", "Candy", "Other", "Soft Drinks", "Coffee")
_FOOD_CAT_PLACEHOLDERS = ",".join(["%s"] * len(FOOD_CATEGORIES))

# POS sellable predicate (owner 2026-09-28 Osmo): a made dish (item_type='dish') is
# made-to-order and NEVER stock-gated (it starts at quantity 0 yet must be sellable in
# the Food Menu / Food & drink sale / session food note); resale (ready-made) items stay
# stock-gated (quantity > 0); ingredients (kind='ingredient') are never sellable.
SELLABLE_FOOD_WHERE = (
    f"(item_type='dish' OR (category IN ({_FOOD_CAT_PLACEHOLDERS}) AND quantity > 0)) "
    "AND item_name NOT IN (SELECT name FROM ingredients WHERE kind='ingredient')"
)


@router.get("/fetch_food_prices", response_model=GenericResponse, tags=["Food"], summary="Fetch food prices [MySQL]")
async def api_fetch_food_prices(auth=Depends(verify_api_key)):
    """Fetch food prices from inventory: made dishes always (never stock-gated),
    resale items only when in stock (quantity > 0)."""
    try:
        rows = _mysql_query(
            f"SELECT item_name, unit_price FROM inventory WHERE {SELLABLE_FOOD_WHERE}",
            FOOD_CATEGORIES)
        result = {}
        for r in rows:
            name = r["item_name"]
            price = int(float(r["unit_price"])) if r["unit_price"] else 0
            result[name] = price
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/fetch_food_menu", response_model=GenericResponse, tags=["Food"], summary="Fetch food menu grouped by category [MySQL]")
async def api_fetch_food_menu(all: int = 0, auth=Depends(verify_api_key)):
    """Fetch food items grouped by category from inventory.

    all=1 (owner 2026-09-20 16:30) returns EVERY inventory item regardless of stock level or
    category, for the Stock In picker: an item that is out of stock (quantity 0) is exactly the
    item staff need to stock in, so the quantity>0 / category filter must not apply there.
    """
    try:
        if int(all or 0) == 1:
            rows = _mysql_query("SELECT item_name, unit_price, category FROM inventory WHERE item_name NOT IN (SELECT name FROM ingredients WHERE kind='ingredient') ORDER BY category, item_name")
        else:
            rows = _mysql_query(
                f"SELECT item_name, unit_price, category FROM inventory WHERE {SELLABLE_FOOD_WHERE} ORDER BY category, item_name",
                FOOD_CATEGORIES)
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


@router.get("/fetch_food_costs", response_model=GenericResponse, tags=["Food"], summary="Fetch food costs [MySQL]")
async def api_fetch_food_costs(auth=Depends(verify_api_key)):
    """Fetch food costs from inventory: made dishes always (never stock-gated),
    resale items only when in stock (quantity > 0)."""
    try:
        rows = _mysql_query(
            f"SELECT item_name, unit_price FROM inventory WHERE {SELLABLE_FOOD_WHERE}",
            FOOD_CATEGORIES)
        result = {}
        for r in rows:
            name = r["item_name"]
            price = int(float(r["unit_price"])) if r["unit_price"] else 0
            result[name] = price
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ═══════════════════════════════════════
@router.get("/fetch_games", response_model=GenericResponse, tags=["Games"], summary="Fetch game list [MySQL]")
async def api_fetch_games(auth=Depends(verify_api_key)):
    """Fetch game list from MySQL games_library."""
    try:
        rows = _mysql_query("SELECT game_title, genre, solo_multi, final_status, disc_count FROM games_library ORDER BY game_title")
        return ok({"games": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fetch_game_library", response_model=GenericResponse, tags=["Games"], summary="Fetch game library [MySQL]")
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
@router.get("/fetch_console_games", response_model=GenericResponse, tags=["Games"], summary="Fetch console games [MySQL]")
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
@router.get("/get_games_on_console/{console_id}", response_model=GenericResponse, tags=["Games"], summary="Get games on console [MySQL]")
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
@router.get("/get_consoles_with_game", response_model=GenericResponse, tags=["Games"], summary="Get consoles with a game [MySQL]")
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



# ═══════════════════════════════════════
#  MUTATION — add_console_game / remove_console_game
# ═══════════════════════════════════════
@router.post("/add_console_game", response_model=GenericResponse, tags=["Games"], summary="Add game to console [MySQL]")
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


@router.delete("/remove_console_game", response_model=GenericResponse, tags=["Games"], summary="Remove game from console [MySQL]")
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

# ═══════════════════════════════════════
@router.put("/set_game_disc_count", tags=["Games"], summary="Set game disc count [MySQL]")
async def api_set_game_disc_count(req: dict, auth=Depends(verify_api_key)):
    try:
        game_title = req.get("game_title", "").strip()
        discs = int(req.get("discs", 0))
        if not game_title:
            return error_response(message="game_title required")
        _mysql_exec("UPDATE games_library SET disc_count=%s WHERE game_title=%s", (discs, game_title))
        return ok({"game_title": game_title, "discs": discs, "updated": True})
    except Exception as e:
        return error_response(message=str(e))


@router.put("/update_game_library_install", tags=["Games"], summary="Update game library install status [MySQL]")
async def api_update_game_library_install(req: dict, auth=Depends(verify_api_key)):
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


@router.post("/add_game", tags=["Games"], summary="Add game to library [MySQL]")
async def api_add_game(req: dict, auth=Depends(verify_api_key)):
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


@router.put("/edit_game", tags=["Games"], summary="Edit game in library [MySQL]")
async def api_edit_game(req: dict, auth=Depends(verify_api_key)):
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


@router.delete("/delete_game/{title}", tags=["Games"], summary="Delete game from library [MySQL]")
async def api_delete_game(title: str, auth=Depends(verify_api_key)):
    try:
        _mysql_exec("DELETE FROM games_library WHERE game_title=%s", (title,))
        return ok({"title": title, "deleted": True})
    except Exception as e:
        return error_response(message=str(e))


@router.delete("/delete_session_game/{console_id}", tags=["Games"], summary="Delete session game from console [MySQL]")
async def api_delete_session_game(console_id: str, auth=Depends(verify_api_key)):
    try:
        _cid = console_id.replace(" ", "")
        _mysql_exec(
            "DELETE FROM console_games WHERE REPLACE(console_id, ' ', '')=%s AND status='Session'",
            (_cid,))
        return ok({"console_id": console_id, "deleted": True})
    except Exception as e:
        return error_response(message=str(e))
@router.post("/move_console_game", response_model=GenericResponse, tags=["Games"], summary="Move game between console/SSD [MySQL]")
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

