"""PS VIBE API — New Game/Console Management Endpoints (MySQL)"""
import json as _json

# ═══════════════════════════════════════
#  CONSOLE SETTINGS — add/remove/update console multipliers
# ═══════════════════════════════════════
@app.post("/api/add_console_to_setting", response_model=GenericResponse, tags=["Settings"], summary="Add console to multipliers [MySQL]")
async def api_add_console_to_setting(req: dict, auth=Depends(verify_api_key)):
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


@app.delete("/api/remove_console_from_setting/{console_id}", response_model=GenericResponse, tags=["Settings"], summary="Remove console from multipliers [MySQL]")
async def api_remove_console_from_setting(console_id: str, auth=Depends(verify_api_key)):
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


@app.put("/api/update_console_multiplier/{console_id}", response_model=GenericResponse, tags=["Settings"], summary="Update console multiplier [MySQL]")
async def api_update_console_multiplier(console_id: str, req: dict, auth=Depends(verify_api_key)):
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
@app.put("/api/set_game_disc_count", response_model=GenericResponse, tags=["Games"], summary="Update game disc count [MySQL]")
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


@app.put("/api/update_game_library_install", response_model=GenericResponse, tags=["Games"], summary="Update game installation status [MySQL]")
async def api_update_game_library_install(req: dict, auth=Depends(verify_api_key)):
    try:
        game_title = req.get("game_title", "").strip()
        console_id = req.get("console_id", "").strip()
        installed = req.get("installed", True)
        status_val = "Installed" if installed else "false"
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
        return ok({"game_title": game_title, "console_id": console_id, "installed": installed})
    except Exception as e:
        return error_response(message=str(e))


@app.post("/api/add_game", response_model=GenericResponse, tags=["Games"], summary="Add new game to library [MySQL]")
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


@app.put("/api/edit_game", response_model=GenericResponse, tags=["Games"], summary="Edit game metadata [MySQL]")
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


@app.delete("/api/delete_game/{title}", response_model=GenericResponse, tags=["Games"], summary="Delete game from library [MySQL]")
async def api_delete_game(title: str, auth=Depends(verify_api_key)):
    try:
        _mysql_exec("DELETE FROM games_library WHERE game_title=%s", (title,))
        return ok({"title": title, "deleted": True})
    except Exception as e:
        return error_response(message=str(e))


@app.delete("/api/delete_session_game/{console_id}", response_model=GenericResponse, tags=["Games"], summary="Delete session game entry [MySQL]")
async def api_delete_session_game(console_id: str, auth=Depends(verify_api_key)):
    try:
        _mysql_exec(
            "DELETE FROM console_games WHERE console_id=%s AND status='Session'",
            (console_id,))
        return ok({"console_id": console_id, "deleted": True})
    except Exception as e:
        return error_response(message=str(e))
