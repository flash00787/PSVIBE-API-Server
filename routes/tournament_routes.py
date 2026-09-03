"""PS VIBE API — Tournament Routes (brackets + registration links).

Endpoints:
  Admin (auth required):
    POST   /api/tournaments                 Create tournament
    GET    /api/tournaments                 List tournaments
    GET    /api/tournaments/{id}            Get tournament detail
    PATCH  /api/tournaments/{id}            Update tournament
    POST   /api/tournaments/{id}/players    Admin add player
    POST   /api/tournaments/{id}/players/{pid}/confirm  Confirm payment
    DELETE /api/tournaments/{id}/players/{pid}          Remove player
    POST   /api/tournaments/{id}/start                  Start bracket
    POST   /api/tournaments/{id}/matches/{mid}/score    Submit score
    GET    /api/tournaments/{id}/standings  Winners / eliminated

  Public (no auth):
    GET    /t/{token}                       HTML registration page
    POST   /api/tournaments/{id}/register   Public registration
    GET    /api/tournaments/{id}/bracket    View bracket
    GET    /api/tournaments/{id}/players    Registered players
"""

import logging
import secrets
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import ok, error_response
from deps import verify_api_key

logger = logging.getLogger(__name__)

# Uploads directory for tournament payment screenshots
_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "tournaments")

# Myanmar Standard Time (Asia/Yangon) = UTC+6:30. scheduled_at is stored as a NAIVE
# Myanmar wall-clock datetime (matching tournament start_at/reg times).
_MMT = timezone(timedelta(hours=6, minutes=30))

router = APIRouter(tags=["Tournaments"])


# ── helpers ──

def _ensure_team_columns():
    """Idempotent schema guard: add team1/team2/team3/rules_ack columns if missing.

    Runs cheaply at import/startup and self-heals fresh installs.
    """
    try:
        existing = _mysql_query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = 'psvibe_api' AND TABLE_NAME = 'tournament_players' "
            "AND COLUMN_NAME IN ('team1','team2','team3','rules_ack')"
        )
        have = {r["COLUMN_NAME"] for r in (existing or [])}
        missing = []
        for col in ("team1", "team2", "team3", "rules_ack"):
            if col not in have:
                missing.append(col)
        if not missing:
            return
        stmts = []
        for col in missing:
            if col == "rules_ack":
                stmts.append("ADD COLUMN rules_ack TINYINT(1) NOT NULL DEFAULT 0")
            else:
                stmts.append(f"ADD COLUMN {col} VARCHAR(100) NULL")
        _mysql_exec(f"ALTER TABLE tournament_players {', '.join(stmts)}")
        logger.info("Tournament schema migrated (added columns: %s)", ", ".join(missing))
    except Exception as e:
        logger.warning("_ensure_team_columns failed: %s", e)


# Self-heal tournament_players schema at import/startup (team1-3 + rules_ack).
try:
    _ensure_team_columns()
except Exception:
    pass


def _ensure_match_columns():
    """Idempotent schema guard: add bracket + feeds_from + console_id columns to tournament_matches.

    Supports double-elimination: `bracket` tags each match as winners/losers/final,
    and `feeds_from` records the winners-bracket match whose loser drops into this
    losers-bracket match. `console_id` + `scheduled_at` allow staff to assign which
    PS5 console a match is played on and when. Runs cheaply at import/startup.
    """
    try:
        existing = _mysql_query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = 'psvibe_api' AND TABLE_NAME = 'tournament_matches' "
            "AND COLUMN_NAME IN ('bracket','feeds_from','console_id','scheduled_at')"
        )
        have = {r["COLUMN_NAME"] for r in (existing or [])}
        stmts = []
        if "bracket" not in have:
            stmts.append("ADD COLUMN bracket ENUM('winners','losers','final') NOT NULL DEFAULT 'winners' AFTER status")
        if "feeds_from" not in have:
            stmts.append("ADD COLUMN feeds_from INT DEFAULT NULL AFTER bracket")
        if "console_id" not in have:
            stmts.append("ADD COLUMN console_id VARCHAR(20) DEFAULT NULL AFTER feeds_from")
        if "scheduled_at" not in have:
            stmts.append("ADD COLUMN scheduled_at DATETIME DEFAULT NULL AFTER console_id")
        if stmts:
            _mysql_exec(f"ALTER TABLE tournament_matches {', '.join(stmts)}")
            logger.info("Tournament matches schema migrated (added: %s)", ", ".join(x.split()[1] for x in stmts))
        # Double-elim reuses (round, position) across winners/losers/final lanes,
        # so the uniqueness key must span bracket too (idempotent).
        _ensure_match_unique_key()
    except Exception as e:
        logger.warning("_ensure_match_columns failed: %s", e)


def _ensure_hybrid_schema():
    """Idempotent schema guard for the 'hybrid' format (single-elim + Top-4 double-elim tail).

    - Adds tournament_players.placement INT NULL (records 2nd/3rd/4th place finishes).
    - Extends tournaments.format ENUM to include 'hybrid'.
    """
    # placement column
    try:
        cols = _mysql_query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA='psvibe_api' AND TABLE_NAME='tournament_players' AND COLUMN_NAME='placement'"
        )
        if not cols:
            _mysql_exec("ALTER TABLE tournament_players ADD COLUMN placement INT DEFAULT NULL AFTER status")
            logger.info("tournament_players.placement column added")
    except Exception as e:
        logger.warning("_ensure_hybrid_schema (placement) failed: %s", e)

    # format enum
    try:
        row = _mysql_query_one(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA='psvibe_api' AND TABLE_NAME='tournaments' AND COLUMN_NAME='format'"
        )
        ctype = (row or {}).get("COLUMN_TYPE", "") or ""
        if "hybrid" not in ctype:
            _mysql_exec("ALTER TABLE tournaments MODIFY COLUMN format ENUM('single','double','hybrid') NOT NULL DEFAULT 'single'")
            logger.info("tournaments.format enum extended with 'hybrid'")
    except Exception as e:
        logger.warning("_ensure_hybrid_schema (format) failed: %s", e)


def _ensure_match_unique_key():
    """Ensure tournament_matches uniqueness spans (tournament_id, round, position, bracket).

    The legacy key `uq_tourn_match` covers only (tournament_id, round, position),
    which collides for double-elimination (winners & losers both use round=1, pos=1).
    """
    try:
        keys = _mysql_query(
            "SHOW INDEX FROM tournament_matches WHERE Key_name='uq_tourn_match'"
        )
        cols = [r["Column_name"] for r in (keys or [])]
        if cols and "bracket" in cols:
            return  # already spans bracket
        # Drop legacy unique key and re-add with bracket included.
        # The legacy key doubles as the index backing fk_tm_tournament (tournament_id),
        # so add a standalone tournament_id index first.
        _mysql_exec("ALTER TABLE tournament_matches ADD KEY idx_tournament (tournament_id)")
        _mysql_exec("ALTER TABLE tournament_matches DROP INDEX uq_tourn_match")
        _mysql_exec(
            "ALTER TABLE tournament_matches ADD UNIQUE KEY uq_tourn_match "
            "(tournament_id, round, position, bracket)"
        )
        logger.info("tournament_matches unique key now spans bracket")
    except Exception as e:
        logger.warning("_ensure_match_unique_key failed: %s", e)


# Self-heal tournament_matches schema at import/startup (bracket + feeds_from).
try:
    _ensure_match_columns()
except Exception:
    pass

# Self-heal hybrid-format schema (tournament_players.placement + tournaments.format enum).
try:
    _ensure_hybrid_schema()
except Exception:
    pass


def _calculate_total_rounds(player_count: int) -> int:
    """Calculate number of rounds needed for a single-elimination bracket."""
    if player_count <= 1:
        return 0
    return math.ceil(math.log2(player_count))


def _player_count_for_round(round_num: int, total_rounds: int) -> int:
    """Number of matches in a given round (round_num starts at 1)."""
    return 2 ** (total_rounds - round_num)


def _parse_payment_method(raw: str) -> str:
    """Normalize payment method string to canonical key."""
    if not raw:
        return None
    r = raw.strip().lower()
    # Map common variations
    mapping = {
        "kpay": "kpay",
        "kbz pay": "kpay",
        "kbzpay": "kpay",
        "kbz": "kbz_bank",
        "kbz bank": "kbz_bank",
        "kbzbank": "kbz_bank",
        "wavepay": "wavepay",
        "wave pay": "wavepay",
        "aya pay": "aya_pay",
        "ayapay": "aya_pay",
        "aya": "aya_pay",
    }
    return mapping.get(r, r[:20])  # fallback: use trimmed raw value


def _parse_prize_ks(prize_raw) -> float:
    """Parse a prize string like '100,000 Ks' → 100000.00, else return 0."""
    if prize_raw is None:
        return 0.0
    import re as _re
    if isinstance(prize_raw, (int, float)):
        return float(prize_raw)
    s = str(prize_raw)
    # Match a number possibly containing commas/decimals embedded in text
    m = _re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _get_prizes(tid: int) -> list:
    """Return ordered prize-tier list for a tournament (empty if none)."""
    rows = _mysql_query(
        "SELECT place, label, amount_ks FROM tournament_prizes WHERE tournament_id=%s ORDER BY place ASC",
        (tid,),
    )
    return [{"place": int(r["place"]), "label": r.get("label"), "amount_ks": float(r["amount_ks"] or 0)} for r in rows]


def _resolve_champion_prize_ks(tid: int, prize_raw, prize_pool) -> float:
    """Resolve champion prize amount with tier priority:
    1st-place tier → prize_pool_ks → parsed prize text."""
    tier = _mysql_query_one(
        "SELECT amount_ks FROM tournament_prizes WHERE tournament_id=%s AND place=1", (tid,)
    )
    if tier and tier.get("amount_ks") is not None:
        return float(tier["amount_ks"])
    if prize_pool is not None:
        return float(prize_pool)
    return _parse_prize_ks(prize_raw)


_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _place_medal(place: int) -> str:
    return _MEDALS.get(place, "🏅")


def _fmt_amount(amount) -> str:
    return f"{float(amount or 0):,.0f}"


def _get_champion(tid: int):
    """Return champion dict for a tournament, or None."""
    row = _mysql_query_one(
        "SELECT * FROM tournament_champions WHERE tournament_id=%s", (tid,)
    )
    if not row:
        return None
    return {
        "player_id": row["player_id"],
        "player_name": row["player_name"],
        "prize_ks": float(row["prize_ks"] or 0),
        "prize_text": row["prize_text"],
        "won_at": str(row["won_at"]) if row.get("won_at") else None,
    }


def _insert_notification(tid: int, player_id, telegram_id, message: str, kind: str = "match"):
    """Insert a notification row for a player (only if telegram_id is present)."""
    if not telegram_id:
        return
    try:
        _mysql_exec(
            """INSERT INTO tournament_notifications
               (tournament_id, player_id, telegram_id, message, kind)
               VALUES (%s, %s, %s, %s, %s)""",
            (tid, player_id, telegram_id, message, kind),
        )
    except Exception as e:
        logger.warning("Failed to insert notification for player %s: %s", player_id, e)


def _player_by_id(pid: int):
    return _mysql_query_one("SELECT * FROM tournament_players WHERE id=%s", (pid,))


def _record_placement(tid: int, player_id, place: int):
    """Record a player's final placement (2nd/3rd/4th) and mark them eliminated."""
    if not player_id:
        return
    _mysql_exec(
        "UPDATE tournament_players SET placement=%s, status='eliminated' WHERE id=%s AND tournament_id=%s",
        (place, player_id, tid),
    )


def _get_placements(tid: int) -> list:
    """Return top-4 placements: place 1 = champion row, places 2-4 = players.placement."""
    result = []
    champ = _get_champion(tid)
    if champ:
        result.append({"place": 1, "player_id": champ["player_id"], "name": champ["player_name"]})
    rows = _mysql_query(
        "SELECT id, name, placement FROM tournament_players WHERE tournament_id=%s AND placement IS NOT NULL ORDER BY placement ASC",
        (tid,),
    )
    for r in rows:
        result.append({"place": int(r["placement"]), "player_id": r["id"], "name": r["name"]})
    return result


def _crown_champion(tid: int, winner_id: int):
    """Insert/replace champion row for a tournament based on winner player + prize parsing."""
    winner = _player_by_id(winner_id)
    if not winner:
        logger.warning("_crown_champion: winner player %s not found", winner_id)
        return
    t = _mysql_query_one("SELECT prize, prize_pool_ks FROM tournaments WHERE id=%s", (tid,))
    prize_raw = t.get("prize") if t else None
    prize_pool = t.get("prize_pool_ks") if t else None
    prize_ks = _resolve_champion_prize_ks(tid, prize_raw, prize_pool)
    prize_text = prize_raw
    # Upsert champion (unique on tournament_id)
    _mysql_exec(
        """INSERT INTO tournament_champions
           (tournament_id, player_id, player_name, prize_ks, prize_text, won_at)
           VALUES (%s, %s, %s, %s, %s, NOW())
           ON DUPLICATE KEY UPDATE
             player_id=VALUES(player_id), player_name=VALUES(player_name),
             prize_ks=VALUES(prize_ks), prize_text=VALUES(prize_text), won_at=NOW()""",
        (tid, winner["id"], winner["name"], prize_ks, prize_text),
    )
    logger.info("Champion crowned for tournament %s: player %s (%s) prize=%s", tid, winner["id"], winner["name"], prize_text)


# ═══════════════════════════════════════
#  Admin — CRUD
# ═══════════════════════════════════════

@router.post("/api/tournaments")
async def create_tournament(body: dict, auth=Depends(verify_api_key)):
    """Create a new tournament. Auto-generates a unique registration token."""
    try:
        name = (body.get("name") or "").strip()
        game = (body.get("game") or "").strip()
        if not name or not game:
            return error_response(message="name and game are required")

        reg_token = secrets.token_hex(6)
        # Ensure uniqueness (collision is astronomically unlikely, but safe)
        while _mysql_query_one("SELECT 1 FROM tournaments WHERE reg_token=%s", (reg_token,)):
            reg_token = secrets.token_hex(6)

        fmt = body.get("format", "single")
        if fmt not in ("single", "double", "hybrid"):
            fmt = "single"

        _pool = body.get("prize_pool_ks")
        if _pool is not None and _pool != "":
            try:
                _pool = float(_pool)
            except (ValueError, TypeError):
                _pool = None
        t_id = _mysql_exec(
            """INSERT INTO tournaments
               (name, game, description, format, entry_fee_ks, max_players, min_players,
                status, reg_token, reg_open_at, reg_close_at, start_at, prize, prize_pool_ks, rules, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                name,
                game,
                body.get("description"),
                fmt,
                body.get("entry_fee_ks", 0),
                int(body.get("max_players", 16)),
                int(body.get("min_players", 2)),
                body.get("status", "draft"),
                reg_token,
                body.get("reg_open_at"),
                body.get("reg_close_at"),
                body.get("start_at"),
                body.get("prize"),
                _pool,
                body.get("rules"),
                body.get("created_by"),
            ),
        )

        tournament = _mysql_query_one("SELECT * FROM tournaments WHERE id=%s", (t_id,))
        tournament["reg_url"] = f"https://ps-vibe.com/t/{reg_token}"
        # Convert datetime fields for JSON
        for f in ("created_at", "updated_at", "reg_open_at", "reg_close_at", "start_at"):
            if tournament.get(f):
                tournament[f] = str(tournament[f])

        logger.info("Tournament created: id=%s name=%s token=%s", t_id, name, reg_token)
        return ok(tournament)

    except Exception as e:
        logger.error("create_tournament error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments")
async def list_tournaments(
    status: Optional[str] = Query(None),
    auth=Depends(verify_api_key),
):
    """List all tournaments with player counts."""
    try:
        where = ""
        params = []
        if status:
            where = "WHERE t.status = %s"
            params.append(status)

        rows = _mysql_query(
            f"""SELECT t.*, COUNT(p.id) as player_count
                FROM tournaments t
                LEFT JOIN tournament_players p ON t.id = p.tournament_id
                {where}
                GROUP BY t.id
                ORDER BY t.created_at DESC""",
            tuple(params),
        )

        result = []
        for r in rows:
            d = dict(r)
            d["reg_url"] = f"https://ps-vibe.com/t/{r['reg_token']}"
            for f in ("created_at", "updated_at", "reg_open_at", "reg_close_at", "start_at"):
                if d.get(f):
                    d[f] = str(d[f])
            d["prizes"] = _get_prizes(r["id"])
            result.append(d)

        return ok(result)
    except Exception as e:
        logger.error("list_tournaments error: %s", e)
        return error_response(message=str(e))


# ── Public literal routes (MUST be declared before /{tid} to avoid path collision) ──

@router.get("/api/tournaments/champions")
async def list_champions():
    """Public: recent champions JOIN tournaments, ordered by won_at DESC."""
    try:
        rows = _mysql_query(
            """SELECT c.player_id, c.player_name, c.prize_ks, c.prize_text, c.won_at,
                      t.id AS tournament_id, t.name AS tournament_name, t.game
               FROM tournament_champions c
               JOIN tournaments t ON t.id = c.tournament_id
               ORDER BY c.won_at DESC
               LIMIT 20""",
        )
        result = []
        for r in rows:
            d = dict(r)
            d["prize_ks"] = float(d["prize_ks"] or 0)
            if d.get("won_at"):
                d["won_at"] = str(d["won_at"])
            result.append(d)
        return ok(result)
    except Exception as e:
        logger.error("list_champions error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments/notifications/pending")
async def pending_notifications(
    limit: int = Query(20),
    auth=Depends(verify_api_key),
):
    """Admin/bot: unsent notification rows with tournament name."""
    try:
        limit = max(1, min(int(limit), 100))
        rows = _mysql_query(
            """SELECT n.id, n.tournament_id, n.player_id, n.telegram_id, n.message, n.kind, n.created_at,
                      t.name AS tournament_name
               FROM tournament_notifications n
               LEFT JOIN tournaments t ON t.id = n.tournament_id
               WHERE n.sent = 0
               ORDER BY n.id ASC
               LIMIT %s""",
            (limit,),
        )
        result = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = str(d["created_at"])
            result.append(d)
        return ok(result)
    except Exception as e:
        logger.error("pending_notifications error: %s", e)
        return error_response(message=str(e))


@router.post("/api/tournaments/notifications/{nid}/done")
async def mark_notification_done(nid: int, auth=Depends(verify_api_key)):
    """Mark a notification as sent."""
    try:
        _mysql_exec(
            "UPDATE tournament_notifications SET sent=1, sent_at=NOW() WHERE id=%s",
            (nid,),
        )
        return ok({"marked": True})
    except Exception as e:
        logger.error("mark_notification_done error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments/{tid}")
async def get_tournament(tid: int, auth=Depends(verify_api_key)):
    """Get single tournament with player count."""
    try:
        t = _mysql_query_one(
            """SELECT t.*, COUNT(p.id) as player_count
               FROM tournaments t
               LEFT JOIN tournament_players p ON t.id = p.tournament_id
               WHERE t.id = %s
               GROUP BY t.id""",
            (tid,),
        )
        if not t:
            return error_response(message=f"Tournament {tid} not found", code=404)
        t = dict(t)
        t["reg_url"] = f"https://ps-vibe.com/t/{t['reg_token']}"
        for f in ("created_at", "updated_at", "reg_open_at", "reg_close_at", "start_at"):
            if t.get(f):
                t[f] = str(t[f])
        t["champion"] = _get_champion(tid)
        t["prizes"] = _get_prizes(tid)
        t["placements"] = _get_placements(tid)
        return ok(t)
    except Exception as e:
        logger.error("get_tournament error: %s", e)
        return error_response(message=str(e))


_PLACE_LABELS = {1: "Champion", 2: "Runner-up", 3: "3rd Place", 4: "4th Place"}


def _default_place_label(place: int) -> str:
    if place in _PLACE_LABELS:
        return _PLACE_LABELS[place]
    return f"{place}th Place"


@router.get("/api/tournaments/{tid}/prizes")
async def get_tournament_prizes(tid: int):
    """Public: return prize tiers ordered by place."""
    try:
        t = _mysql_query_one("SELECT id FROM tournaments WHERE id=%s", (tid,))
        if not t:
            return error_response(message=f"Tournament {tid} not found", code=404)
        return ok(_get_prizes(tid))
    except Exception as e:
        logger.error("get_tournament_prizes error: %s", e)
        return error_response(message=str(e))


@router.put("/api/tournaments/{tid}/prizes")
async def put_tournament_prizes(tid: int, body: dict, auth=Depends(verify_api_key)):
    """Replace-all prize tiers for a tournament: {prizes: [{place, label?, amount_ks}]}."""
    try:
        t = _mysql_query_one("SELECT id FROM tournaments WHERE id=%s", (tid,))
        if not t:
            return error_response(message=f"Tournament {tid} not found", code=404)

        prizes = body.get("prizes") if isinstance(body, dict) else None
        if not isinstance(prizes, list):
            return error_response(message="prizes must be a list", code=400)

        normalized = []
        seen_places = set()
        for p in prizes:
            if not isinstance(p, dict):
                return error_response(message="each prize must be an object", code=400)
            try:
                place = int(p.get("place"))
            except (ValueError, TypeError):
                return error_response(message="place must be an integer", code=400)
            if place < 1:
                return error_response(message="place must be >= 1", code=400)
            if place in seen_places:
                return error_response(message=f"duplicate place {place}", code=400)
            seen_places.add(place)
            try:
                amount_ks = float(p.get("amount_ks", 0) or 0)
            except (ValueError, TypeError):
                return error_response(message="amount_ks must be a number", code=400)
            if amount_ks < 0:
                return error_response(message="amount_ks must be >= 0", code=400)
            label = (p.get("label") or "").strip()
            if not label:
                label = _default_place_label(place)
            normalized.append({"place": place, "label": label, "amount_ks": amount_ks})

        # Replace-all
        _mysql_exec("DELETE FROM tournament_prizes WHERE tournament_id=%s", (tid,))
        for n in normalized:
            _mysql_exec(
                "INSERT INTO tournament_prizes (tournament_id, place, label, amount_ks) VALUES (%s, %s, %s, %s)",
                (tid, n["place"], n["label"], n["amount_ks"]),
            )

        logger.info("Tournament %s prize tiers replaced: %s", tid, [n["place"] for n in normalized])
        return ok(_get_prizes(tid))
    except Exception as e:
        logger.error("put_tournament_prizes error: %s", e)
        return error_response(message=str(e))


@router.patch("/api/tournaments/{tid}")
async def update_tournament(tid: int, body: dict, auth=Depends(verify_api_key)):
    """Update tournament fields (name, game, status, etc.)."""
    try:
        existing = _mysql_query_one("SELECT * FROM tournaments WHERE id=%s", (tid,))
        if not existing:
            return error_response(message=f"Tournament {tid} not found", code=404)

        allowed = [
            "name", "game", "description", "format", "entry_fee_ks",
            "max_players", "min_players", "status", "reg_open_at",
            "reg_close_at", "start_at", "prize", "prize_pool_ks", "rules",
        ]
        updates = []
        params = []
        for k in allowed:
            if k in body:
                v = body[k]
                if k == "prize_pool_ks":
                    if v is None or v == "":
                        v = None
                    else:
                        try:
                            v = float(v)
                        except (ValueError, TypeError):
                            continue
                updates.append(f"{k} = %s")
                params.append(v)

        if not updates:
            return error_response(message="No valid fields to update")

        params.append(tid)
        _mysql_exec(
            f"UPDATE tournaments SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )
        logger.info("Tournament %s updated: %s", tid, list(body.keys()))
        return ok({"updated": True})
    except Exception as e:
        logger.error("update_tournament error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  Admin — Player Management
# ═══════════════════════════════════════

@router.post("/api/tournaments/{tid}/players")
async def admin_add_player(tid: int, body: dict, auth=Depends(verify_api_key)):
    """Admin manually adds a player to a tournament."""
    try:
        _ensure_tournament(tid)

        name = (body.get("name") or "").strip()
        phone = (body.get("phone") or "").strip()
        if not name or not phone:
            return error_response(message="name and phone are required")

        mid = body.get("member_id")
        paid = int(body.get("paid", 0))

        pid = _mysql_exec(
            """INSERT INTO tournament_players
               (tournament_id, member_id, name, phone, telegram_id, paid, payment_method, paid_at, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                tid,
                mid,
                name,
                phone,
                body.get("telegram_id"),
                paid,
                body.get("payment_method") if paid else None,
                datetime.now() if paid else None,
                "confirmed" if paid else "registered",
            ),
        )

        player = _mysql_query_one("SELECT * FROM tournament_players WHERE id=%s", (pid,))
        if player and player.get("created_at"):
            player["created_at"] = str(player["created_at"])
        if player and player.get("paid_at"):
            player["paid_at"] = str(player["paid_at"])

        logger.info("Admin added player %s to tournament %s", pid, tid)
        return ok(player)
    except Exception as e:
        err = str(e)
        if "Duplicate" in err or "uq_tourn" in err.lower():
            return error_response(message="Phone already registered for this tournament", code=409)
        logger.error("admin_add_player error: %s", e)
        return error_response(message=err)


@router.post("/api/tournaments/{tid}/players/{pid}/confirm")
async def confirm_player_payment(tid: int, pid: int, body: dict = {}, auth=Depends(verify_api_key)):
    """Confirm payment for a tournament player. Sets paid=1, paid_at, payment_method."""
    try:
        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE id=%s AND tournament_id=%s",
            (pid, tid),
        )
        if not player:
            return error_response(message="Player not found", code=404)

        if player["paid"]:
            return error_response(message="Player already paid")

        pm = body.get("payment_method", "Cash")
        _mysql_exec(
            """UPDATE tournament_players
               SET paid=1, payment_method=%s, paid_at=NOW(), status='confirmed'
               WHERE id=%s""",
            (pm, pid),
        )
        logger.info("Payment confirmed for player %s in tournament %s", pid, tid)
        return ok({"confirmed": True, "player_id": pid})
    except Exception as e:
        logger.error("confirm_player_payment error: %s", e)
        return error_response(message=str(e))


@router.delete("/api/tournaments/{tid}/players/{pid}")
async def remove_player(tid: int, pid: int, auth=Depends(verify_api_key)):
    """Remove a player from a tournament."""
    try:
        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE id=%s AND tournament_id=%s",
            (pid, tid),
        )
        if not player:
            return error_response(message="Player not found", code=404)

        _mysql_exec("DELETE FROM tournament_players WHERE id=%s", (pid,))
        logger.info("Player %s removed from tournament %s", pid, tid)
        return ok({"removed": True})
    except Exception as e:
        logger.error("remove_player error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  Admin — Bracket Operations
# ═══════════════════════════════════════

def _generate_seeded_bracket(tid: int, t: dict, players: list):
    """Shared seed + round-1 bracket generator.

    Randomly shuffles players, assigns seed numbers, generates round-1 matches.
    Round-1 pairing is a pure random draw (adjacent players in the shuffled
    order); seeds are only a label and do not drive matchups. Returns
    (total_rounds, r1_matches, shuffled). Inserts notifications for r1 matches.

    Balanced bye redistribution: when the player count is NOT a power of two,
    the `byes = matches_in_r1 - len(shuffled)` extra slots become single-player
    bye matches placed in the FIRST `byes` positions (p1 = bye player, p2 = NULL).
    The leftover players (always an even count) then fill the remaining positions
    as adjacent pairs in the shuffled order, preserving the pure random draw.
    Every round-1 slot therefore produces exactly one winner, so round 2 is always
    a clean power-of-2 bracket (no trailing empty-vs-empty matches).
    """
    import random
    shuffled = list(players)
    random.shuffle(shuffled)
    for idx, p in enumerate(shuffled):
        _mysql_exec("UPDATE tournament_players SET seed=%s WHERE id=%s", (idx + 1, p["id"]))

    total_rounds = _calculate_total_rounds(len(shuffled))
    if total_rounds == 0:
        return 0, 0, shuffled

    matches_in_r1 = 2 ** total_rounds  # next power of 2 >= player_count
    r1_matches = matches_in_r1 // 2
    byes = matches_in_r1 - len(shuffled)  # 0 for exact powers of 2

    _id_to_player = {p["id"]: p for p in shuffled}

    # First `byes` positions: single-player bye matches (auto-advance to round 2).
    for pos in range(1, byes + 1):
        p = shuffled[pos - 1]
        _mysql_exec(
            """INSERT INTO tournament_matches
               (tournament_id, round, position, player1_id, player2_id, status)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (tid, 1, pos, p["id"], None, "pending"),
        )

    # Remaining positions: adjacent pairs of the leftover shuffled players.
    # When byes == 0 this is identical to the pure adjacent pairing (unchanged).
    for pair_pos in range(byes + 1, r1_matches + 1):
        idx_a = byes + 2 * (pair_pos - byes - 1)
        idx_b = idx_a + 1
        p1_id = shuffled[idx_a]["id"] if idx_a < len(shuffled) else None
        p2_id = shuffled[idx_b]["id"] if idx_b < len(shuffled) else None

        _mysql_exec(
            """INSERT INTO tournament_matches
               (tournament_id, round, position, player1_id, player2_id, status)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (tid, 1, pair_pos, p1_id, p2_id, "pending"),
        )

        # Notify both players in this r1 match (bye matches have no opponent → no notify)
        if p1_id and p2_id:
            p1, p2 = _id_to_player[p1_id], _id_to_player[p2_id]
            _insert_notification(
                tid, p1["id"], p1.get("telegram_id"),
                f"🔔 {t['name']} — မင်းရဲ့ Round 1 Match စပါပြီ! ပြိုင်ဘက်: {p2['name']} (#{p2['seed']}) — PS VIBE",
            )
            _insert_notification(
                tid, p2["id"], p2.get("telegram_id"),
                f"🔔 {t['name']} — မင်းရဲ့ Round 1 Match စပါပြီ! ပြိုင်ဘက်: {p1['name']} (#{p1['seed']}) — PS VIBE",
            )

    # Pre-create ALL winners-bracket rounds (2..total_rounds) upfront so the bracket
    # page shows the FULL tree immediately (empty future slots render as TBD) instead
    # of lazily INSERTing rows as matches get scored. Players fill into these
    # pre-existing rows via _advance_winner_single / submit_match_score advance paths.
    for r in range(2, total_rounds + 1):
        count = 2 ** (total_rounds - r)
        for pos in range(1, count + 1):
            _mysql_exec(
                """INSERT INTO tournament_matches
                   (tournament_id, round, position, status, bracket)
                   VALUES (%s, %s, %s, 'pending', 'winners')""",
                (tid, r, pos),
            )

    # Advance bye players (single-player R1 matches) into round 2 so every R1 slot
    # contributes exactly one winner. Needed by BOTH single-elim and hybrid formats
    # when N is not a power of two (idempotent: no-op for exact powers of two).
    _auto_advance_byes(tid, total_rounds)

    return total_rounds, r1_matches, shuffled


# ═══════════════════════════════════════════
#  Double-Elimination Bracket Generator
# ═══════════════════════════════════════════

_DBL_STRUCT = {
    # r -> (matches_in_round, slot_type) where slot_type describes how player slots
    # are sourced. 'seed' = fill from shuffled seeds at generation time (W1 only).
    # 'winner_loser' = two feeder losers; 'win_lose' = winner+loser mix.
}


def _generate_double_elim_bracket(tid: int, t: dict, players: list):
    """Generate a full double-elimination bracket for `players`.

    For N=64 (k=6, no byes, no bracket reset):
      Winners bracket: W1..W6 (round column 1..6): 32+16+8+4+2+1 = 63 matches.
      Losers bracket:  L1..L10 (round column 1..10): 16+16+8+8+4+4+2+2+1+1 = 62 matches.
      Grand final:     1 match (bracket='final', round=1).
    Total = 126 = 2N - 2.

    All matches are inserted upfront with `bracket` / `feeds_from` set; players are
    filled only into W1 matches (and later rounds are populated on score submission).
    Returns (rounds_used, total_matches, shuffled).
    """
    import random
    n = len(players)
    if n < 4:
        return 0, 0, []

    shuffled = list(players)
    random.shuffle(shuffled)
    for idx, p in enumerate(shuffled):
        _mysql_exec("UPDATE tournament_players SET seed=%s WHERE id=%s", (idx + 1, p["id"]))

    k = math.ceil(math.log2(n))  # winners-bracket rounds (6 for 64)
    if 2 ** k != n:
        # Non-power-of-2 would need byes; for now require exact power of two for
        # double-elim (we generate the full 2N-2 structure).
        k = k  # still proceed with next-power-of-two assumption (W1 slots = 2^(k-1))

    w1_matches = 2 ** (k - 1)  # 32

    id_by_pos = {}  # (bracket, round, position) -> match id (for feeds_from/lookup)

    def _wb_count(r):
        return 2 ** (k - r)

    # ── Winners bracket ──
    for r in range(1, k + 1):
        count = _wb_count(r)
        for pos in range(1, count + 1):
            p1_id = p2_id = None
            if r == 1:
                # R1 pairing = pure random draw (adjacent in shuffled order).
                idx_a = 2 * (pos - 1)
                idx_b = 2 * pos - 1
                p1_id = shuffled[idx_a]["id"] if idx_a < n else None
                p2_id = shuffled[idx_b]["id"] if idx_b < n else None
            _mysql_exec(
                """INSERT INTO tournament_matches
                   (tournament_id, round, position, player1_id, player2_id, status, bracket)
                   VALUES (%s, %s, %s, %s, %s, 'pending', 'winners')""",
                (tid, r, pos, p1_id, p2_id),
            )
            mid = _mysql_query_one(
                "SELECT id FROM tournament_matches WHERE tournament_id=%s AND bracket='winners' AND round=%s AND position=%s",
                (tid, r, pos),
            )["id"]
            id_by_pos[("winners", r, pos)] = mid

    # ── Losers bracket (round column L1..L10 maps to 1..10) ──
    lb_structure = []
    # L1: 16 matches — losers of W1[2p-1] & W1[2p]
    lb_structure.append(("L1", w1_matches // 2, "ww"))              # 16
    # L2: 16 — winner L1[p] + loser W2[p]
    lb_structure.append(("L2", _wb_count(2), "wl"))                  # 16
    # L3: 8 — winners of L2[2p-1] & L2[2p]
    lb_structure.append(("L3", _wb_count(3), "ww"))                  # 8
    # L4: 8 — winner L3[p] + loser W3[p]
    lb_structure.append(("L4", _wb_count(3), "wl"))                  # 8
    # L5: 4 — winners L4[2p-1] & L4[2p]
    lb_structure.append(("L5", _wb_count(4), "ww"))                  # 4
    # L6: 4 — winner L5[p] + loser W4[p]
    lb_structure.append(("L6", _wb_count(4), "wl"))                  # 4
    # L7: 2 — winners L6[2p-1] & L6[2p]
    lb_structure.append(("L7", _wb_count(5), "ww"))                  # 2
    # L8: 2 — winner L7[p] + loser W5[p]
    lb_structure.append(("L8", _wb_count(5), "wl"))                  # 2
    # L9: 1 — winners L8[1] & L8[2]
    lb_structure.append(("L9", 1, "ww"))                             # 1
    # L10: 1 — winner L9[1] + loser W6[1]
    lb_structure.append(("L10", 1, "wl"))                            # 1

    for lb_round, (label, count, kind) in enumerate(lb_structure, start=1):
        for pos in range(1, count + 1):
            _mysql_exec(
                """INSERT INTO tournament_matches
                   (tournament_id, round, position, status, bracket)
                   VALUES (%s, %s, %s, 'pending', 'losers')""",
                (tid, lb_round, pos),
            )
            mid = _mysql_query_one(
                "SELECT id FROM tournament_matches WHERE tournament_id=%s AND bracket='losers' AND round=%s AND position=%s",
                (tid, lb_round, pos),
            )["id"]
            id_by_pos[("losers", lb_round, pos)] = mid

    # ── Grand final ──
    _mysql_exec(
        """INSERT INTO tournament_matches
           (tournament_id, round, position, status, bracket)
           VALUES (%s, 1, 1, 'pending', 'final')""",
        (tid,),
    )
    final_mid = _mysql_query_one(
        "SELECT id FROM tournament_matches WHERE tournament_id=%s AND bracket='final' AND round=1 AND position=1",
        (tid,),
    )["id"]
    id_by_pos[("final", 1, 1)] = final_mid

    # ── Set feeds_from on losers-bracket matches ──
    # For each LB match, feeds_from = the WB match whose LOSER feeds its player2
    # (the "drops down" stream). player1 of LB matches always comes from the LB
    # advance path (winner of a prior LB round), except L1 where BOTH come from W1
    # losers. We tag feeds_from with the primary WB source for traceability:
    #   L1 pos p  -> W1[2p-1]
    #   L2 pos p  -> W2[p]
    #   L4 pos p  -> W3[p]
    #   L6 pos p  -> W4[p]
    #   L8 pos p  -> W5[p]
    #   L10 pos 1 -> W6[1]
    for p in range(1, w1_matches // 2 + 1):  # L1
        wb_p1 = id_by_pos[("winners", 1, 2 * p - 1)]
        _mysql_exec("UPDATE tournament_matches SET feeds_from=%s WHERE id=%s", (wb_p1, id_by_pos[("losers", 1, p)]))
    for p in range(1, _wb_count(2) + 1):      # L2 <- W2
        _mysql_exec("UPDATE tournament_matches SET feeds_from=%s WHERE id=%s",
                    (id_by_pos[("winners", 2, p)], id_by_pos[("losers", 2, p)]))
    for p in range(1, _wb_count(3) + 1):      # L4 <- W3
        _mysql_exec("UPDATE tournament_matches SET feeds_from=%s WHERE id=%s",
                    (id_by_pos[("winners", 3, p)], id_by_pos[("losers", 4, p)]))
    for p in range(1, _wb_count(4) + 1):      # L6 <- W4
        _mysql_exec("UPDATE tournament_matches SET feeds_from=%s WHERE id=%s",
                    (id_by_pos[("winners", 4, p)], id_by_pos[("losers", 6, p)]))
    for p in range(1, _wb_count(5) + 1):      # L8 <- W5
        _mysql_exec("UPDATE tournament_matches SET feeds_from=%s WHERE id=%s",
                    (id_by_pos[("winners", 5, p)], id_by_pos[("losers", 8, p)]))
    _mysql_exec("UPDATE tournament_matches SET feeds_from=%s WHERE id=%s",
                (id_by_pos[("winners", 6, 1)], id_by_pos[("losers", 10, 1)]))

    # Notify W1 players (random-draw R1, matching the fill above)
    _id_to_player = {p["id"]: p for p in shuffled}
    for pos in range(1, w1_matches + 1):
        idx_a = 2 * (pos - 1)
        idx_b = 2 * pos - 1
        p1_id = shuffled[idx_a]["id"] if idx_a < n else None
        p2_id = shuffled[idx_b]["id"] if idx_b < n else None
        if p1_id and p2_id:
            p1, p2 = _id_to_player[p1_id], _id_to_player[p2_id]
            _insert_notification(
                tid, p1["id"], p1.get("telegram_id"),
                f"🔔 {t['name']} — မင်းရဲ့ Winners R1 Match စပါပြီ! ပြိုင်ဘက်: {p2['name']} (#{p2['seed']}) — PS VIBE",
            )
            _insert_notification(
                tid, p2["id"], p2.get("telegram_id"),
                f"🔔 {t['name']} — မင်းရဲ့ Winners R1 Match စပါပြီ! ပြိုင်ဘက်: {p1['name']} (#{p1['seed']}) — PS VIBE",
            )

    total_matches = (2 ** (k + 1) - 2)  # 63 + 62 + 1 = 126 = 2N - 2
    return k, total_matches, shuffled


def _set_match_player(mid: int, slot: str, pid: int):
    """Fill player1/player2 slot of a match row (leave status pending)."""
    m = _mysql_query_one("SELECT player1_id, player2_id FROM tournament_matches WHERE id=%s", (mid,))
    if not m:
        return
    col = "player1_id" if slot == "p1" else "player2_id"
    cur = m[col]
    if cur is None:
        _mysql_exec(f"UPDATE tournament_matches SET {col}=%s WHERE id=%s", (pid, mid))


def _double_elim_advance(tid: int, t: dict, match: dict, winner_id, loser_id, s1, s2, mid):
    """Advance a winner/loser across the double-elimination bracket after a score.

    Returns an `ok`-shaped dict via the same semantics as single-elim (match_id,
    score, winner, is_final). Handles WB losers dropping into LB, LB winners
    advancing, and the grand final crowning the champion.
    """
    mbracket = match.get("bracket") or "winners"
    rround = int(match["round"])
    pos = int(match["position"])

    def _find(bracket, round_no, position):
        return _mysql_query_one(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s AND bracket=%s AND round=%s AND position=%s",
            (tid, bracket, round_no, position),
        )

    if mbracket == "winners":
        # Advance winner in WB
        if rround < 6:
            nxt = _find("winners", rround + 1, math.ceil(pos / 2))
            if nxt:
                slot = "p1" if (pos % 2 == 1) else "p2"
                _set_match_player(nxt["id"], slot, winner_id)
        else:
            # WB final (round 6) winner -> grand final player1
            gf = _find("final", 1, 1)
            if gf:
                _set_match_player(gf["id"], "p1", winner_id)
        # Loser drops into losers bracket (NOT eliminated)
        if loser_id:
            if rround == 1:
                lb = _find("losers", 1, math.ceil(pos / 2))
                if lb:
                    slot = "p1" if (pos % 2 == 1) else "p2"
                    _set_match_player(lb["id"], slot, loser_id)
            elif 2 <= rround <= 5:
                # L(2r-2), pos = pos, player2 slot
                lb_round = 2 * rround - 2
                lb = _find("losers", lb_round, pos)
                if lb:
                    _set_match_player(lb["id"], "p2", loser_id)
            elif rround == 6:
                # WB final loser -> L10 player2
                lb = _find("losers", 10, 1)
                if lb:
                    _set_match_player(lb["id"], "p2", loser_id)
        is_final = False

    elif mbracket == "losers":
        # Loser of LB = 2nd loss -> eliminated
        if loser_id:
            _mysql_exec("UPDATE tournament_players SET status='eliminated' WHERE id=%s", (loser_id,))
        if rround == 10:
            # L10 winner -> grand final player2
            gf = _find("final", 1, 1)
            if gf:
                _set_match_player(gf["id"], "p2", winner_id)
        else:
            if rround % 2 == 1:
                # odd LB round -> L(r+1) same position, player1 slot
                nxt = _find("losers", rround + 1, pos)
                if nxt:
                    _set_match_player(nxt["id"], "p1", winner_id)
            else:
                # even LB round -> L(r+1) ceil(pos/2); slot depends on source parity
                nxt = _find("losers", rround + 1, math.ceil(pos / 2))
                if nxt:
                    slot = "p1" if (pos % 2 == 1) else "p2"
                    _set_match_player(nxt["id"], slot, winner_id)
        is_final = False

    else:  # 'final'
        if loser_id:
            _mysql_exec("UPDATE tournament_players SET status='eliminated' WHERE id=%s", (loser_id,))
        _mysql_exec("UPDATE tournaments SET status='completed' WHERE id=%s", (tid,))
        _crown_champion(tid, winner_id)
        winner = _player_by_id(winner_id)
        if winner:
            _insert_notification(
                tid, winner["id"], winner.get("telegram_id"),
                f"🏆 ချန်ပီယံဖြစ်ပြီ! {t['name']} — ဆုကြေး: {t.get('prize') or '—'} — ဂုဏ်ယူပါတယ်!",
                kind="champion",
            )
        logger.info("Double-elim tournament %s completed! Winner: player %s", tid, winner_id)
        is_final = True

    logger.info("Match %s scored (double-elim, %s): %s-%s, winner=%s", mid, mbracket, s1, s2, winner_id)
    return ok({
        "match_id": mid,
        "score1": s1,
        "score2": s2,
        "winner_id": winner_id,
        "bracket": mbracket,
        "is_final": is_final,
    })


def _generate_hybrid_bracket(tid: int, t: dict, players: list):
    """Generate the 'hybrid' bracket: single-elimination until 4 players remain, then a
    4-team double-elimination tail with NO grand-final reset.

    Reuses single-elim seeding + round-1 generation (bracket defaults to 'winners'),
    which now pre-creates the FULL winners bracket (R1..total_rounds). It then adds
    the Top-4 tail rows (players fill in as scores arrive):
        WB final       : bracket='winners', round=total_rounds, pos=1  (from seeded bracket)
        LB R1           : bracket='losers',  round=1, pos=1
        LB R2           : bracket='losers',  round=2, pos=1
        Grand Final     : bracket='final',   round=1, pos=1

    For N < 4 (total_rounds < 2) there is no 4-player tail — behaves exactly like
    single elimination. Returns (total_rounds, r1_matches, shuffled).
    """
    total_rounds, r1_matches, shuffled = _generate_seeded_bracket(tid, t, players)
    if len(shuffled) < 4:
        # N < 4: no 4-player tail can form — behave exactly like single elimination.
        return total_rounds, r1_matches, shuffled

    # Pre-create the Top-4 double-elim tail rows (players fill in as scores arrive).
    # The WB final (round=total_rounds, bracket='winners') is already pre-created by
    # _generate_seeded_bracket's full-round pre-creation, so only the losers lanes +
    # grand final are added here.
    _mysql_exec(
        """INSERT INTO tournament_matches (tournament_id, round, position, status, bracket)
           VALUES (%s, 1, 1, 'pending', 'losers')""",
        (tid,),
    )
    _mysql_exec(
        """INSERT INTO tournament_matches (tournament_id, round, position, status, bracket)
           VALUES (%s, 2, 1, 'pending', 'losers')""",
        (tid,),
    )
    _mysql_exec(
        """INSERT INTO tournament_matches (tournament_id, round, position, status, bracket)
           VALUES (%s, 1, 1, 'pending', 'final')""",
        (tid,),
    )
    # Auto-advance round-1 bye players into the semifinal is handled inside
    # _generate_seeded_bracket (called above), so nothing extra is needed here.
    return total_rounds, r1_matches, shuffled


def _auto_advance_byes(tid: int, total_rounds: int):
    """Advance round-1 bye players (player2 = NULL) into round 2 using parity-based slots.

    With balanced bye redistribution, single-player bye matches sit in the FIRST
    `byes` R1 positions (p1 = bye player, p2 = NULL). Those players must still
    reach round 2 so the bracket stays a clean power-of-2 and (for hybrid) the
    4-player tail forms correctly. Each bye match contributes exactly one winner,
    so round 2 always fills fully. Only relevant when total_rounds >= 3 (N >= 5);
    N=4 has no byes and N<4 has total_rounds < 3.
    """
    if total_rounds < 3:
        return
    r1 = _mysql_query(
        "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=1 AND bracket='winners'",
        (tid,),
    )
    for m in r1:
        if m["player1_id"] and not m["player2_id"]:
            pos = int(m["position"])
            next_pos = math.ceil(pos / 2)
            slot = "p1" if (pos % 2 == 1) else "p2"
            nxt = _mysql_query_one(
                "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=2 AND position=%s AND bracket='winners'",
                (tid, next_pos),
            )
            if nxt:
                _set_match_player(nxt["id"], slot, m["player1_id"])
            else:
                col = "player1_id" if slot == "p1" else "player2_id"
                _mysql_exec(
                    f"INSERT INTO tournament_matches (tournament_id, round, position, {col}, status, bracket) VALUES (%s,2,%s,%s,'pending','winners')",
                    (tid, next_pos, m["player1_id"]),
                )


def _advance_winner_single(tid: int, rround: int, pos: int, winner_id):
    """Advance a single-elim winners-bracket winner to the next round (parity-based slot)."""
    next_round = rround + 1
    next_pos = math.ceil(pos / 2)
    slot = "p1" if (pos % 2 == 1) else "p2"
    nxt = _mysql_query_one(
        "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=%s AND position=%s AND bracket='winners'",
        (tid, next_round, next_pos),
    )
    if not nxt:
        col = "player1_id" if slot == "p1" else "player2_id"
        _mysql_exec(
            f"INSERT INTO tournament_matches (tournament_id, round, position, {col}, status, bracket) VALUES (%s,%s,%s,%s,'pending','winners')",
            (tid, next_round, next_pos, winner_id),
        )
    else:
        _set_match_player(nxt["id"], slot, winner_id)


def _hybrid_advance(tid: int, t: dict, match: dict, winner_id, loser_id, s1, s2, mid):
    """Advance a match in the hybrid (single-elim + Top-4 double-elim tail) bracket.

    Early rounds (round < total_rounds-1) are pure single-elim (loser eliminated).
    The Top-4 tail:
      semifinal (round=total_rounds-1, bracket='winners') -> winner to WB final,
        loser to LB R1 (NOT eliminated).
      WB final (round=total_rounds, bracket='winners') -> winner to GF p1,
        loser to LB R2 p2 (NOT eliminated).
      LB R1 (bracket='losers', round=1) -> winner to LB R2 p1; loser = 4th place.
      LB R2 (bracket='losers', round=2) -> winner to GF p2; loser = 3rd place.
      GF (bracket='final') -> winner = champion (crown + completed); loser = 2nd place.
    """
    mbracket = match.get("bracket") or "winners"
    rround = int(match["round"])
    pos = int(match["position"])

    def _find(bracket, round_no, position):
        return _mysql_query_one(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s AND bracket=%s AND round=%s AND position=%s",
            (tid, bracket, round_no, position),
        )

    r1_info = _mysql_query_one(
        "SELECT COUNT(*) as cnt FROM tournament_matches WHERE tournament_id=%s AND round=1 AND bracket='winners'",
        (tid,),
    )
    total_rounds = _calculate_total_rounds(r1_info["cnt"] * 2)
    semifinal_round = total_rounds - 1
    has_tail = _find("final", 1, 1) is not None

    is_final = False

    if mbracket == "winners":
        if not has_tail:
            # N <= 3: no Top-4 tail — behave exactly like single elimination.
            if loser_id:
                _mysql_exec("UPDATE tournament_players SET status='eliminated' WHERE id=%s", (loser_id,))
            if rround == total_rounds:
                _mysql_exec("UPDATE tournaments SET status='completed' WHERE id=%s", (tid,))
                _crown_champion(tid, winner_id)
                winner = _player_by_id(winner_id)
                if winner:
                    _insert_notification(
                        tid, winner["id"], winner.get("telegram_id"),
                        f"🏆 ချန်ပီယံဖြစ်ပြီ! {t['name']} — ဆုကြေး: {t.get('prize') or '—'} — ဂုဏ်ယူပါတယ်!",
                        kind="champion",
                    )
                logger.info("Hybrid (no-tail) tournament %s completed! Winner: player %s", tid, winner_id)
                is_final = True
            else:
                _advance_winner_single(tid, rround, pos, winner_id)
        elif rround < semifinal_round:
            # Early single-elim round (tail mode): loser eliminated, winner advances.
            if loser_id:
                _mysql_exec("UPDATE tournament_players SET status='eliminated' WHERE id=%s", (loser_id,))
            _advance_winner_single(tid, rround, pos, winner_id)
        elif rround == semifinal_round:
            # Semifinal: winner -> WB final; loser drops to LB R1 (still alive).
            wbf = _find("winners", total_rounds, 1)
            if wbf:
                slot = "p1" if (pos % 2 == 1) else "p2"
                _set_match_player(wbf["id"], slot, winner_id)
            if loser_id:
                lbr1 = _find("losers", 1, 1)
                if lbr1:
                    slot = "p1" if (pos % 2 == 1) else "p2"
                    _set_match_player(lbr1["id"], slot, loser_id)
                lp = _player_by_id(loser_id)
                if lp:
                    _insert_notification(
                        tid, loser_id, lp.get("telegram_id"),
                        f"🔄 {t['name']} — ဆီမီးဖိုင်နယ်မှာ ရှုံးပေမယ့် Losers Bracket မှာ ဆက်ကစားနိုင်ပါသေးတယ်! — PS VIBE",
                        kind="match",
                    )
        else:
            # WB final (round == total_rounds): winner -> GF p1; loser -> LB R2 p2.
            gf = _find("final", 1, 1)
            if gf:
                _set_match_player(gf["id"], "p1", winner_id)
            if loser_id:
                lbr2 = _find("losers", 2, 1)
                if lbr2:
                    _set_match_player(lbr2["id"], "p2", loser_id)

    elif mbracket == "losers":
        if rround == 1:
            # LB R1: winner -> LB R2 p1; loser = 4th place.
            if loser_id:
                _record_placement(tid, loser_id, 4)
            nxt = _find("losers", 2, 1)
            if nxt:
                _set_match_player(nxt["id"], "p1", winner_id)
        else:
            # LB R2: winner -> GF p2; loser = 3rd place.
            if loser_id:
                _record_placement(tid, loser_id, 3)
            gf = _find("final", 1, 1)
            if gf:
                _set_match_player(gf["id"], "p2", winner_id)

    else:  # 'final' -> Grand Final
        if loser_id:
            _record_placement(tid, loser_id, 2)
        _mysql_exec("UPDATE tournaments SET status='completed' WHERE id=%s", (tid,))
        _crown_champion(tid, winner_id)
        winner = _player_by_id(winner_id)
        if winner:
            _insert_notification(
                tid, winner["id"], winner.get("telegram_id"),
                f"🏆 ချန်ပီယံဖြစ်ပြီ! {t['name']} — ဆုကြေး: {t.get('prize') or '—'} — ဂုဏ်ယူပါတယ်!",
                kind="champion",
            )
        logger.info("Hybrid tournament %s completed! Winner: player %s", tid, winner_id)
        is_final = True

    logger.info("Match %s scored (hybrid, %s): %s-%s, winner=%s", mid, mbracket, s1, s2, winner_id)
    return ok({
        "match_id": mid,
        "score1": s1,
        "score2": s2,
        "winner_id": winner_id,
        "bracket": mbracket,
        "is_final": is_final,
    })


@router.post("/api/tournaments/{tid}/start")
async def start_tournament(tid: int, auth=Depends(verify_api_key)):
    """Start the tournament: seed players, generate round-1 bracket.

    Status must be 'closed' or 'open'. Must have >= min_players eligible players.
    Eligible players = status IN ('registered','confirmed','checked_in').
    Non-eligible players (e.g. not checked-in) are marked 'dnf' (no-show).
    Players are randomly seeded. If count isn't a power of 2, higher seeds get byes.
    """
    try:
        t = _ensure_tournament(tid)
        if t["status"] not in ("open", "closed"):
            return error_response(
                message=f"Cannot start tournament with status '{t['status']}'. Must be 'open' or 'closed'."
            )

        # Mark any non-eligible players (status not in eligible set, and not already eliminated/dnf) as no-show
        eligible_statuses = ("registered", "confirmed", "checked_in")

        players = _mysql_query(
            "SELECT * FROM tournament_players WHERE tournament_id=%s AND status IN ('registered','confirmed','checked_in') ORDER BY id",
            (tid,),
        )
        if len(players) < int(t["min_players"]):
            missing = int(t["min_players"]) - len(players)
            return error_response(
                message=f"Need at least {t['min_players']} eligible players. Currently have {len(players)} (missing {missing}).",
                code=400,
            )

        # Mark no-shows (anything that isn't eligible and isn't already finished)
        _mysql_exec(
            """UPDATE tournament_players SET status='dnf'
               WHERE tournament_id=%s AND status NOT IN ('registered','confirmed','checked_in','eliminated','dnf')""",
            (tid,),
        )

        # Idempotent: clear any existing matches before (re)generating bracket
        _mysql_exec("DELETE FROM tournament_matches WHERE tournament_id=%s", (tid,))
        _mysql_exec("DELETE FROM tournament_notifications WHERE tournament_id=%s", (tid,))

        if t.get("format") == "double":
            total_rounds, match_count, shuffled = _generate_double_elim_bracket(tid, t, players)
            if total_rounds == 0:
                return error_response(message="Need at least 4 players to start a double-elimination tournament")
        elif t.get("format") == "hybrid":
            total_rounds, r1_matches, shuffled = _generate_hybrid_bracket(tid, t, players)
            match_count = r1_matches
            if total_rounds == 0:
                return error_response(message="Need at least 2 players to start")
        else:
            total_rounds, r1_matches, shuffled = _generate_seeded_bracket(tid, t, players)
            match_count = r1_matches
            if total_rounds == 0:
                return error_response(message="Need at least 2 players to start")

        _mysql_exec(
            "UPDATE tournaments SET status='live', start_at=NOW() WHERE id=%s",
            (tid,),
        )
        logger.info(
            "Tournament %s started: %s players, %s rounds (format=%s), %s matches generated",
            tid, len(shuffled), total_rounds, t.get("format"), match_count,
        )
        return ok({
            "started": True,
            "players": len(shuffled),
            "format": t.get("format"),
            "total_rounds": total_rounds,
            "r1_matches": match_count if t.get("format") == "double" else r1_matches,
            "matches_generated": match_count,
        })
    except Exception as e:
        logger.error("start_tournament error: %s", e)
        return error_response(message=str(e))


@router.post("/api/tournaments/{tid}/matches/{mid}/score")
async def submit_match_score(tid: int, mid: int, body: dict, auth=Depends(verify_api_key)):
    """Submit score for a match. Winner advances to next round.

    Body: {score1: int, score2: int}. Ties are rejected (400).
    If final round → tournament marked 'completed'.
    """
    try:
        t = _ensure_tournament(tid)
        match = _mysql_query_one("SELECT * FROM tournament_matches WHERE id=%s AND tournament_id=%s", (mid, tid))
        if not match:
            return error_response(message="Match not found", code=404)
        if match["status"] == "complete":
            return error_response(message="Match already scored")

        s1 = int(body.get("score1", 0))
        s2 = int(body.get("score2", 0))

        if s1 == s2:
            return error_response(message="Ties are not allowed in tournament matches", code=400)

        winner_id = match["player1_id"] if s1 > s2 else match["player2_id"]
        loser_id = match["player2_id"] if s1 > s2 else match["player1_id"]

        # Mark match complete
        _mysql_exec(
            "UPDATE tournament_matches SET score1=%s, score2=%s, winner_id=%s, status='complete' WHERE id=%s",
            (s1, s2, winner_id, mid),
        )

        mbracket = match.get("bracket") or "winners"
        if t.get("format") == "hybrid":
            return _hybrid_advance(tid, t, match, winner_id, loser_id, s1, s2, mid)
        if t.get("format") == "double" or mbracket in ("losers", "final"):
            return _double_elim_advance(tid, t, match, winner_id, loser_id, s1, s2, mid)

        # ── Single-elimination (unchanged) ──
        # Mark loser as eliminated (unless bye — player2 was None)
        if loser_id:
            _mysql_exec(
                "UPDATE tournament_players SET status='eliminated' WHERE id=%s",
                (loser_id,),
            )

        # Determine total rounds from R1 match count (power-of-2 bracket size)
        r1_info = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM tournament_matches WHERE tournament_id=%s AND round=1",
            (tid,),
        )
        total_rounds = _calculate_total_rounds(r1_info["cnt"] * 2)
        is_final = (match["round"] == total_rounds)

        if is_final:
            _mysql_exec("UPDATE tournaments SET status='completed' WHERE id=%s", (tid,))
            # Insert champion row
            _crown_champion(tid, winner_id)
            # Notify winner
            winner = _player_by_id(winner_id)
            if winner:
                _insert_notification(
                    tid, winner["id"], winner.get("telegram_id"),
                    f"🏆 ချန်ပီယံဖြစ်ပြီ! {t['name']} — ဆုကြေး: {t.get('prize') or '—'} — ဂုဏ်ယူပါတယ်!",
                    kind="champion",
                )
            logger.info("Tournament %s completed! Winner: player %s", tid, winner_id)
        else:
            # Advance winner to next round
            next_round = match["round"] + 1
            next_pos = math.ceil(match["position"] / 2)

            next_match = _mysql_query_one(
                "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=%s AND position=%s",
                (tid, next_round, next_pos),
            )
            if not next_match:
                _mysql_exec(
                    """INSERT INTO tournament_matches
                       (tournament_id, round, position, player1_id, status)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (tid, next_round, next_pos, winner_id, "pending"),
                )
            elif not next_match["player1_id"]:
                _mysql_exec(
                    "UPDATE tournament_matches SET player1_id=%s, status='pending' WHERE id=%s",
                    (winner_id, next_match["id"]),
                )
            elif not next_match["player2_id"]:
                _mysql_exec(
                    "UPDATE tournament_matches SET player2_id=%s, status='pending' WHERE id=%s",
                    (winner_id, next_match["id"]),
                )

            # Notify the advanced winner about their next opponent (only if next match has both players)
            _refreshed_next = _mysql_query_one(
                "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=%s AND position=%s",
                (tid, next_round, next_pos),
            )
            if _refreshed_next and _refreshed_next["player1_id"] and _refreshed_next["player2_id"]:
                winner = _player_by_id(winner_id)
                opp_id = (
                    _refreshed_next["player2_id"]
                    if _refreshed_next["player1_id"] == winner_id
                    else _refreshed_next["player1_id"]
                )
                opp = _player_by_id(opp_id)
                if winner and opp:
                    _insert_notification(
                        tid, winner["id"], winner.get("telegram_id"),
                        f"🎮 Round {match['round']} ကို တက်ပြီ — နောက်ပြိုင်ဘက်: {opp['name']} (#{opp['seed'] or ''}) — PS VIBE",
                    )

        logger.info("Match %s scored: %s-%s, winner=%s", mid, s1, s2, winner_id)
        return ok({
            "match_id": mid,
            "score1": s1,
            "score2": s2,
            "winner_id": winner_id,
            "is_final": is_final,
        })
    except Exception as e:
        logger.error("submit_match_score error: %s", e)
        return error_response(message=str(e))


@router.post("/api/tournaments/{tid}/matches/{mid}/schedule")
async def schedule_match(tid: int, mid: int, body: dict, auth=Depends(verify_api_key)):
    """Assign console + play time for a tournament match.

    Body (all optional): {console_id: str (e.g. "C-01"), scheduled_at: str|None,
    notes: str|None}. Clears a field when null/"".
    """
    try:
        _ensure_tournament(tid)
        match = _mysql_query_one(
            "SELECT * FROM tournament_matches WHERE id=%s AND tournament_id=%s", (mid, tid)
        )
        if not match:
            return error_response(message="Match not found", code=404)

        console_id = body.get("console_id")
        scheduled_at = body.get("scheduled_at")
        notes = body.get("notes")

        console_id = (console_id or "").strip() or None
        notes = (notes or "").strip() or None

        sched_dt = None
        if scheduled_at:
            sched_str = str(scheduled_at).strip()
            if sched_str:
                try:
                    sched_dt = datetime.fromisoformat(sched_str.replace("Z", "+00:00"))
                    # Normalize any tz-aware value (e.g. Z/offset from older cached UI)
                    # to a NAIVE Myanmar wall-clock time (Asia/Yangon, UTC+6:30).
                    # Naive values are assumed already-MMT and left unchanged.
                    if sched_dt.tzinfo is not None and sched_dt.utcoffset() is not None:
                        sched_dt = sched_dt.astimezone(_MMT).replace(tzinfo=None)
                except ValueError:
                    return error_response(message="scheduled_at must be ISO datetime", code=400)

        _mysql_exec(
            "UPDATE tournament_matches SET console_id=%s, scheduled_at=%s, notes=%s WHERE id=%s",
            (console_id, sched_dt, notes, mid),
        )
        logger.info("Match %s scheduled: console=%s at=%s", mid, console_id, sched_dt)
        return ok({
            "match_id": mid,
            "console_id": console_id,
            "scheduled_at": str(sched_dt) if sched_dt else None,
            "notes": notes,
        })
    except Exception as e:
        logger.error("schedule_match error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments/{tid}/standings")
async def get_standings(tid: int, auth=Depends(verify_api_key)):
    """Get tournament standings: winners and eliminated players."""
    try:
        _ensure_tournament(tid)
        winners = _mysql_query(
            """SELECT p.id, p.name, p.phone, p.seed,
                      m.round as won_at_round, m.score1, m.score2
               FROM tournament_players p
               JOIN tournament_matches m ON m.winner_id = p.id
               WHERE p.tournament_id = %s AND m.tournament_id = %s
               ORDER BY m.round DESC""",
            (tid, tid),
        )
        eliminated = _mysql_query(
            "SELECT id, name, phone, seed, status, placement FROM tournament_players WHERE tournament_id=%s AND status='eliminated'",
            (tid,),
        )
        registered = _mysql_query(
            "SELECT id, name, phone, seed, status, paid, team1, team2, team3, rules_ack FROM tournament_players WHERE tournament_id=%s AND status IN ('registered','confirmed')",
            (tid,),
        )
        return ok({
            "winners": [dict(r) for r in winners],
            "eliminated": [dict(r) for r in eliminated],
            "active": [dict(r) for r in registered],
            "placements": _get_placements(tid),
        })
    except Exception as e:
        logger.error("get_standings error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  Public — Registration Page
# ═══════════════════════════════════════

@router.get("/t/{token}", response_class=HTMLResponse)
async def registration_page(token: str, request: Request):
    """Render a mobile-friendly tournament registration page."""
    try:
        t = _mysql_query_one("SELECT * FROM tournaments WHERE reg_token=%s", (token,))
        if not t:
            return HTMLResponse(content="<h2>Tournament not found</h2>", status_code=404)

        reg_count = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM tournament_players WHERE tournament_id=%s",
            (t["id"],),
        )["cnt"] or 0

        spots_left = max(0, int(t["max_players"]) - reg_count)
        fee = int(t["entry_fee_ks"] or 0)

        # Format prize and dates for display
        prize_str = t.get("prize") or ""
        prize_pool = t.get("prize_pool_ks")
        prizes = _get_prizes(t["id"])
        try:
            prize_pool_float = float(prize_pool) if prize_pool is not None else None
        except (ValueError, TypeError):
            prize_pool_float = None
        start_str = str(t.get("start_at", ""))[:16] if t.get("start_at") else ""
        reg_close_str = str(t.get("reg_close_at", ""))[:16] if t.get("reg_close_at") else ""
        description = t.get("description") or ""
        rules = t.get("rules") or ""
        
        # Escape for JS/HTML safety
        import html as _html
        tn_name = _html.escape(t["name"])
        tn_game = _html.escape(t["game"])
        tn_desc = _html.escape(description)
        tn_prize = _html.escape(prize_str)
        tn_rules = _html.escape(rules)
        tn_format = {"single": "Single Elimination", "double": "Double Elimination", "hybrid": "Top 4 Double Elimination"}.get(t.get("format"), "Single Elimination")
        tn_id = int(t["id"])

        # Prize breakdown block (structured tiers)
        prize_breakdown = ''
        if prizes:
            rows = ''.join(
                f'<div class="flex items-center justify-between text-sm py-1"><span class="text-gray-400">{_place_medal(p["place"])} {_html.escape(p["label"] or "")}</span><span class="text-amber-300 font-semibold">{_fmt_amount(p["amount_ks"])} Ks</span></div>'
                for p in prizes
            )
            prize_breakdown = f'''<div class="mt-4 pt-3 border-t border-gray-700/50">
    <p class="text-xs text-gray-500 font-semibold mb-1">🏆 ဆုစာရင်း (Prize Breakdown)</p>
    {rows}
  </div>'''

        # Registration availability: full / closed → show notice instead of form
        t_status = (t.get("status") or "").lower()
        is_full = spots_left <= 0
        reg_expired = False
        _rc = t.get("reg_close_at")
        if _rc:
            try:
                from datetime import datetime
                if isinstance(_rc, datetime):
                    reg_expired = datetime.utcnow() > _rc
            except Exception:
                reg_expired = False
        if is_full:
            closed_reason = "full"
        elif t_status != "open" or reg_expired:
            closed_reason = "closed"
        else:
            closed_reason = None

        if closed_reason == "full":
            form_html = f'''  <!-- Full Notice -->
  <div class="bg-surface-800 rounded-xl border border-amber-900/50 p-6 text-center shadow-xl shadow-amber-950/20">
    <div class="text-5xl mb-3">😔</div>
    <h2 class="text-xl font-bold text-amber-400 mb-2">နေရာအပြည့် ဖြစ်နေပါပြီ</h2>
    <p class="text-gray-400 text-sm leading-relaxed">ဒီပြိုင်ပွဲအတွက် Spots ({t["max_players"]}) အားလုံး ပြည့်သွားပါပြီ။<br>နောက်ပြိုင်ပွဲတွေရှိတဲ့အခါ PS VIBE TikTok / Facebook မှာ announce ထပ်လုပ်ပေးသွားပါ့မယ်ဗျ 🙏<br>ပြိုင်ပွဲစတင်သွားပြီဆိုရင်တော့ Bracket ကို နှိပ်ပြီး Live Bracket ကို ဝင်ကြည့်လို့ရပါတယ်။</p>
  </div>'''
        elif closed_reason == "closed":
            form_html = f'''  <!-- Closed Notice -->
  <div class="bg-surface-800 rounded-xl border border-gray-700 p-6 text-center shadow-xl">
    <div class="text-5xl mb-3">🔒</div>
    <h2 class="text-xl font-bold text-gray-300 mb-2">Registration ပိတ်ထားပါသည်</h2>
    <p class="text-gray-500 text-sm leading-relaxed">ဒီပြိုင်ပွဲအတွက် မှတ်ပုံတင်ခြင်း ပိတ်ထားပါသည်။<br>အချက်အလက်များအတွက် <a href="https://t.me/psvibeofficial" class="text-vibe-400 hover:underline">@psvibeofficial</a> ကို ဆက်သွယ်ပါဦး 🙏</p>
  </div>'''
        else:
            form_html = f'''  <!-- Registration Form -->
  <form id="regForm" onsubmit="return handleSubmit(event)" class="bg-surface-800 rounded-xl border border-vibe-900/50 p-5 shadow-xl shadow-vibe-950/30">
    
    <!-- Name -->
    <div class="mb-4">
      <label class="block text-gray-400 text-xs font-medium mb-1.5 ml-1">အမည် <span class="text-red-400">*</span></label>
      <input type="text" id="name" placeholder="သင့်နာမည် ရိုက်ထည့်ပါ" required
        class="w-full px-3.5 py-2.5 rounded-lg bg-surface-600 border border-gray-700 text-white text-sm
               placeholder-gray-500 focus:outline-none focus:border-vibe-500 focus:ring-1 focus:ring-vibe-500/30 transition-colors">
    </div>

    <!-- Phone -->
    <div class="mb-4">
      <label class="block text-gray-400 text-xs font-medium mb-1.5 ml-1">ဖုန်းနံပါတ် <span class="text-red-400">*</span></label>
      <input type="tel" id="phone" placeholder="09xxxxxxxxx" required
        class="w-full px-3.5 py-2.5 rounded-lg bg-surface-600 border border-gray-700 text-white text-sm
               placeholder-gray-500 focus:outline-none focus:border-vibe-500 focus:ring-1 focus:ring-vibe-500/30 transition-colors">
    </div>

    <!-- Teams (used throughout the tournament) -->
    <div class="mb-4">
      <p class="block text-gray-400 text-xs font-medium mb-1.5 ml-1">⚽ အသင်း သုံးသင်း <span class="text-red-400">*</span></p>
      <input type="text" id="team1" placeholder="အသင်း ၁" required
        class="w-full px-3.5 py-2.5 rounded-lg bg-surface-600 border border-gray-700 text-white text-sm mb-2
               placeholder-gray-500 focus:outline-none focus:border-vibe-500 focus:ring-1 focus:ring-vibe-500/30 transition-colors">
      <input type="text" id="team2" placeholder="အသင်း ၂" required
        class="w-full px-3.5 py-2.5 rounded-lg bg-surface-600 border border-gray-700 text-white text-sm mb-2
               placeholder-gray-500 focus:outline-none focus:border-vibe-500 focus:ring-1 focus:ring-vibe-500/30 transition-colors">
      <input type="text" id="team3" placeholder="အသင်း ၃" required
        class="w-full px-3.5 py-2.5 rounded-lg bg-surface-600 border border-gray-700 text-white text-sm
               placeholder-gray-500 focus:outline-none focus:border-vibe-500 focus:ring-1 focus:ring-vibe-500/30 transition-colors">
      <p class="text-gray-500 text-xs mt-1">ပြိုင်ပွဲတစ်လျှောက် အသုံးပြုမည့် အသင်း သုံးသင်းကို ဖြည့်ပါ</p>
    </div>

    {f'''<!-- Payment Method -->
    <div class="mb-4" id="paySection">
      <label class="block text-gray-400 text-xs font-medium mb-1.5 ml-1">💳 ငွေပေးချေနည်း ({fee:,} Ks)</label>
      <div class="grid grid-cols-1 gap-2 mb-2">
        <button type="button" class="pay-btn text-left px-3 py-2.5 rounded-lg border border-gray-700 bg-surface-600 text-gray-300 text-sm hover:border-vibe-500 hover:bg-surface-700" onclick="selectPay('kpay')" data-pay="kpay">
          <span class="text-vibe-400 font-semibold">KPay</span>
          <span class="block text-gray-500 text-xs mt-0.5">09254824024 · U Tin Sein</span>
        </button>
        <button type="button" class="pay-btn text-left px-3 py-2.5 rounded-lg border border-gray-700 bg-surface-600 text-gray-300 text-sm hover:border-vibe-500 hover:bg-surface-700" onclick="selectPay('aya_pay')" data-pay="aya_pay">
          <span class="text-emerald-400 font-semibold">AYA Pay</span>
          <span class="block text-gray-500 text-xs mt-0.5">09254824024 · U Tin Sein</span>
        </button>
        <button type="button" class="pay-btn text-left px-3 py-2.5 rounded-lg border border-gray-700 bg-surface-600 text-gray-300 text-sm hover:border-vibe-500 hover:bg-surface-700" onclick="selectPay('wavepay')" data-pay="wavepay">
          <span class="text-blue-400 font-semibold">WavePay</span>
          <span class="block text-gray-500 text-xs mt-0.5">09254824024 · U Tin Sein</span>
        </button>
      </div>
      <p class="text-gray-500 text-xs mt-1">ငွေလွှဲပြီးပါက Screenshot ပူးတွဲပေးပါ 🙏</p>
    </div>

    <!-- SS Upload -->
    <div class="mb-4" id="ssSection">
      <label class="block text-gray-400 text-xs font-medium mb-1.5 ml-1">📎 ငွေလွှဲ Screenshot <span class="text-red-400">*</span></label>
      <input type="file" id="ssFile" accept="image/*"
        class="w-full text-sm text-gray-400 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0
               file:text-sm file:font-semibold file:bg-vibe-600 file:text-white file:cursor-pointer
               hover:file:bg-vibe-700 file:transition-colors cursor-pointer">
      <p class="text-gray-500 text-xs mt-1">JPEG, PNG, WebP — max 10MB</p>
    </div>''' if fee > 0 else ''}

    <!-- Submit -->
    <div class="mb-3">
      <label class="flex items-start gap-2 text-sm text-gray-300 cursor-pointer select-none">
        <input type="checkbox" id="rulesAck" required class="mt-0.5 h-4 w-4 accent-vibe-600 shrink-0">
        <span>📖 စည်းကမ်းများကို ဖတ်ရှုပြီး သဘောတူပါသည် <a href="#" onclick="openRules(); return false;" class="text-vibe-400 hover:underline">(Rules)</a></span>
      </label>
    </div>
    <button type="submit" id="submitBtn"
      class="w-full py-3 rounded-lg font-bold text-white text-base mt-2
             bg-gradient-to-r from-vibe-600 to-vibe-800 hover:from-vibe-500 hover:to-vibe-700
             disabled:from-gray-700 disabled:to-gray-700 disabled:text-gray-400 disabled:cursor-not-allowed
             transition-all duration-200 shadow-lg shadow-vibe-950/40">
      {f'Register &mdash; {fee:,} Ks' if fee > 0 else 'Register Now — Free 🎉'}
    </button>
  </form>

  <!-- Success Screen -->
  <div id="successScreen" class="hidden bg-surface-800 rounded-xl border border-green-900/50 p-6 text-center shadow-xl shadow-green-950/20">
    <div class="text-5xl mb-3">✅</div>
    <h2 class="text-xl font-bold text-green-400 mb-2">အောင်မြင်ပါပြီ!</h2>
    <p class="text-gray-300 text-sm leading-relaxed">
      <span class="font-semibold text-white">{tn_name}</span> အတွက် <br>
      Register ပြုလုပ်ခြင်း အောင်မြင်ပါသည်။
    </p>
    <p class="text-gray-400 text-xs mt-3 leading-relaxed">
      ⏳ Staff မှ ငွေပေးချေမှု အတည်ပြုပြီးပါက<br>
      အပြီးသတ် အတည်ဖြစ်ပါမည်။
    </p>
    <p class="text-gray-500 text-xs mt-3">📲 Updates — @psvibeofficial</p>
  </div>

'''

        html = f"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="PS VIBE - PS5 Gaming Lounge တွင် ကျင်းပမည့် {tn_name} ({tn_game}) ပြိုင်ပွဲအတွက် တရားဝင် မှတ်ပုံတင်စာမျက်နှာ။ Yangon အခြေစိုက် PS VIBE Gaming Lounge — Play The Game. Share The VIBE!">
<meta name="robots" content="index, follow">
<meta property="og:title" content="{tn_name} — PS VIBE Tournament">
<meta property="og:description" content="PS VIBE - PS5 Gaming Lounge တရားဝင် ပြိုင်ပွဲ မှတ်ပုံတင်ခြင်း — {tn_game}">
<meta property="og:type" content="website">
<meta name="theme-color" content="#0f0f1a">
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "SportsEvent",
  "name": "{tn_name}",
  "description": "{tn_desc}",
  "organizer": {{
    "@type": "SportsOrganization",
    "name": "PS VIBE - PS5 Gaming Lounge",
    "url": "https://ps-vibe.com",
    "sameAs": ["https://www.facebook.com/ps5gamecenter", "https://www.tiktok.com/@ps.vibe.gameing.lounge"],
    "address": {{"@type": "PostalAddress", "streetAddress": "No. 17, Mau Pin Street, Sanchaung", "addressLocality": "Yangon", "addressCountry": "MM"}}
  }},
  "location": {{"@type": "Place", "name": "PS VIBE - PS5 Gaming Lounge", "address": {{"@type": "PostalAddress", "streetAddress": "No. 17, Mau Pin Street, Sanchaung", "addressLocality": "Yangon", "addressCountry": "MM"}}}}
}}
</script>
<title>{tn_name} — PS VIBE Tournament</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {{
    theme: {{
      extend: {{
        colors: {{
          vibe: {{ 50: '#f4f4fb', 100: '#e8eaf6', 200: '#d2d5ee', 300: '#a5aadd', 400: '#7780cb', 500: '#4a55ba', 600: '#1D2BA9', 700: '#172287', 800: '#111a65', 900: '#0c1144', 950: '#060922' }},
          surface: {{ 900: '#0f0f1a', 800: '#1a1a2e', 700: '#16213e', 600: '#111827' }}
        }},
        fontFamily: {{ sans: ['Inter', 'Noto Sans Myanmar', 'system-ui', 'sans-serif'] }}
      }}
    }}
  }}
</script>
<style>
  body {{ background: #0f0f1a; }}
  .text-glow {{ text-shadow: 0 0 20px rgba(29,43,169,0.4); }}
  input[type=file]::file-selector-button {{
    background: linear-gradient(135deg, #1D2BA9, #111a65);
    border: none; color: white; padding: 8px 16px; border-radius: 6px;
    cursor: pointer; font-weight: 600; margin-right: 12px;
  }}
  .pay-btn {{ transition: all 0.2s ease; }}
  .pay-btn:hover {{ transform: translateY(-1px); box-shadow: 0 4px 12px rgba(29,43,169,0.3); }}
  .pay-btn.active {{ background: linear-gradient(135deg, #1D2BA9, #111a65) !important; color: white !important; border-color: #1D2BA9 !important; box-shadow: 0 0 16px rgba(29,43,169,0.4); }}
  .spinner {{ display: inline-block; width: 20px; height: 20px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: white; animation: spin 0.6s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body class="min-h-screen flex items-center justify-center px-4 pt-8 pb-12">
<div class="w-full max-w-lg">

  <!-- Header -->
  <div class="text-center mb-6">
    <img src="/uploads/psvibe-logo-v2.png" alt="PS VIBE - PS5 Gaming Lounge" class="w-64 mx-auto mb-6 drop-shadow-[0_0_18px_rgba(29,43,169,0.35)]">
    <h1 class="text-2xl font-bold text-white text-glow">{tn_name}</h1>
    <p class="text-vibe-400 text-sm font-medium">{tn_game} &middot; {tn_format}</p>
    <p class="text-gray-500 text-xs mt-1">Play The Game. Share The VIBE!</p>
    <span class="inline-block mt-2.5 px-3 py-1 rounded-full bg-vibe-950/60 border border-vibe-800/50 text-vibe-300 text-[11px] font-medium">🏠 PS VIBE - PS5 Gaming Lounge · Yangon</span>
  </div>

  <!-- Tournament Info Card -->
  <div class="bg-surface-800 rounded-xl border border-vibe-900/50 p-5 mb-5 shadow-xl shadow-vibe-950/30">
    <div class="grid grid-cols-2 gap-y-3 gap-x-4 text-sm">
      <div class="text-gray-400">💰 Entry Fee</div>
      <div class="text-right font-semibold text-white">{f'{fee:,} Ks' if fee > 0 else '<span class="text-green-400">အခမဲ့ (Free)</span>'}</div>
      
      <div class="text-gray-400">👥 Spots</div>
      <div class="text-right font-semibold {'text-red-400' if spots_left == 0 else 'text-amber-400'}">{spots_left} left / {t['max_players']}</div>
      
      <div class="text-gray-400">📐 Format</div>
      <div class="text-right text-white">{tn_format}</div>
      
      {f'<div class="text-gray-400">💰 ဆုကြေး စုစုပေါင်း (Prize Pool)</div><div class="text-right text-amber-300 font-semibold">{prize_pool_float:,.0f} Ks</div>' if prize_pool_float is not None else ''}
      {f'<div class="text-gray-400">ဆုခွဲဝေမှု</div><div class="text-right text-amber-300 font-semibold">{tn_prize}</div>' if (prize_str and prize_pool_float is not None) else ''}
      {f'<div class="text-gray-400">🏅 Prize</div><div class="text-right text-amber-300 font-semibold">{tn_prize}</div>' if (prize_str and prize_pool_float is None) else ''}
      {f'<div class="text-gray-400">📅 Date</div><div class="text-right text-white">{start_str}</div>' if start_str else ''}
    </div>
    {prize_breakdown}
    {f'<p class="mt-4 pt-3 border-t border-gray-700/50 text-gray-400 text-xs leading-relaxed">{tn_desc}</p>' if description else ''}
  </div>

  {f'''<!-- Rules Button -->
  <div class="bg-surface-800 rounded-xl border border-vibe-900/50 p-5 mb-5 shadow-xl shadow-vibe-950/30">
    <button type="button" onclick="openRules()" class="w-full text-sm font-semibold px-3 py-2.5 rounded-lg bg-vibe-600 hover:bg-vibe-500 text-white transition-colors">📖 စည်းကမ်းများ ကြည့်ရန် (Rules)</button>
  </div>''' if rules else ''}

  {form_html}

  <!-- Message area -->
  <div id="msg" class="hidden rounded-lg p-3 mb-4 text-sm font-medium"></div>

  <!-- Registered Players + Bracket -->
  <div class="bg-surface-800 rounded-xl border border-vibe-900/50 p-5 mb-5 shadow-xl shadow-vibe-950/30">
    <div class="flex items-center justify-between mb-3">
      <h2 class="text-white font-bold text-sm">👥 Registered Players <span id="playersCount" class="text-vibe-400 font-semibold">(0)</span></h2>
      <button type="button" onclick="openBracket()" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-vibe-600 hover:bg-vibe-500 text-white transition-colors">🏆 Bracket ကြည့်မယ်</button>
    </div>
    <div id="playersList" class="space-y-1.5 max-h-72 overflow-y-auto pr-1">
      <p class="text-gray-500 text-xs py-2">Loading...</p>
    </div>
  </div>

  <!-- Footer -->
  <div class="text-center mt-8 space-y-1.5">
    <p class="text-gray-500 text-xs font-medium">PS VIBE - PS5 Gaming Lounge · Yangon</p>
    <p class="text-gray-600 text-xs">📍 No. 17, Mau Pin Street, Sanchaung, Yangon</p>
    <p class="text-gray-600 text-xs">🕘 9:00 AM - 9:00 PM (နေ့စဉ်) · 📞 09 773355 915</p>
    <p class="text-gray-600 text-xs">Facebook: <a href="https://www.facebook.com/ps5gamecenter" class="text-vibe-400 hover:underline">PS5 Game Center</a> · TikTok: <a href="https://www.tiktok.com/@ps.vibe.gameing.lounge" class="text-vibe-400 hover:underline">@ps.vibe.gameing.lounge</a></p>
    <p class="text-gray-600 text-xs">📲 အကူအညီလိုလျှင် — <a href="https://t.me/psvibeofficial" class="text-vibe-400 hover:underline">@psvibeofficial</a></p>
    <p class="text-gray-700 text-xs pt-1">&copy; 2026 PS VIBE Gaming Lounge. All rights reserved.</p>
  </div>
</div>

<script>
const FEE = {fee};
const TID = {tn_id};
let selectedPay = null;

function selectPay(method) {{
  selectedPay = method;
  document.querySelectorAll('.pay-btn').forEach(b => b.classList.remove('active'));
  document.querySelector('[data-pay="' + method + '"]').classList.add('active');
}}

function showMsg(type, text) {{
  const m = document.getElementById('msg');
  m.className = 'rounded-lg p-3 mb-4 text-sm font-medium ' + 
    (type === 'success' ? 'bg-emerald-900/40 border border-emerald-700 text-emerald-300' :
     type === 'error' ? 'bg-red-900/40 border border-red-700 text-red-300' :
     'bg-amber-900/40 border border-amber-700 text-amber-300');
  m.textContent = text;
}}

async function handleSubmit(e) {{
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  const name = document.getElementById('name').value.trim();
  const phone = document.getElementById('phone').value.trim();
  const team1 = document.getElementById('team1').value.trim();
  const team2 = document.getElementById('team2').value.trim();
  const team3 = document.getElementById('team3').value.trim();
  const rulesAck = document.getElementById('rulesAck').checked;
  document.getElementById('msg').className = 'hidden';

  // Validation
  if (!name || !phone) {{
    showMsg('error', '❌ အမည် နှင့် ဖုန်းနံပါတ် ဖြည့်ပေးပါ');
    return;
  }}
  if (phone.length < 8 || !/^0[0-9]/.test(phone)) {{
    showMsg('error', '❌ ဖုန်းနံပါတ် မှန်မမှန် ပြန်စစ်ပေးပါ (09xxx...)');
    return;
  }}
  if (!team1 || !team2 || !team3) {{
    showMsg('error', '❌ အသင်း သုံးသင်းလုံး ဖြည့်ပေးပါ');
    return;
  }}
  if (!rulesAck) {{
    showMsg('error', '❌ စည်းကမ်းများကို ဖတ်ရှုပြီး သဘောတူပါ');
    return;
  }}
  if (FEE > 0 && !selectedPay) {{
    showMsg('error', '❌ ငွေပေးချေနည်း ရွေးပေးပါ');
    return;
  }}
  if (FEE > 0) {{
    const ssInp = document.getElementById('ssFile');
    if (!ssInp || !ssInp.files || !ssInp.files[0]) {{
      showMsg('error', '❌ ငွေလွှဲ Screenshot ပူးတွဲပေးပါ');
      return;
    }}
  }}

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';

  try {{
    // Step 1: Register
    const regBody = {{ name, phone, team1, team2, team3, rules_ack: true, payment_method: selectedPay || null }};
    // For digital payment, pass a temporary ss_file placeholder
    // The actual SS will be uploaded in step 2
    const resp = await fetch('/api/tournaments/' + TID + '/register', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(regBody)
    }});
    const data = await resp.json();
    if (!data.success) {{
      showMsg('error', '❌ ' + (data.error || 'Registration failed'));
      btn.disabled = false;
      btn.innerHTML = '{f'Register &mdash; {fee:,} Ks' if fee > 0 else 'Register Now — Free 🎉'}'.replace('&mdash;', '—').replace('&mdash;', '—');
      return;
    }}
    const playerId = data.data.player_id;

    // Step 2: Upload SS
    if (FEE > 0 && playerId) {{
      const ssInp = document.getElementById('ssFile');
      if (ssInp && ssInp.files && ssInp.files[0]) {{
        const formData = new FormData();
        formData.append('file', ssInp.files[0]);
        const ssResp = await fetch('/api/tournaments/' + TID + '/players/' + playerId + '/ss', {{
          method: 'POST',
          body: formData
        }});
        const ssData = await ssResp.json();
        if (!ssData.success) {{
          // Registration succeeded but SS upload failed — user is still registered
          showMsg('info', '⚠️ Registration အောင်မြင်သော်လည်း Screenshot upload မအောင်မြင်ပါ။ Staff ကို ဆက်သွယ်ပါ။ — @psvibeofficial');
          document.getElementById('regForm').classList.add('hidden');
          return;
        }}
      }}
    }}

    // Success!
    document.getElementById('regForm').classList.add('hidden');
    document.getElementById('msg').className = 'hidden';
    document.getElementById('successScreen').classList.remove('hidden');
    window.scrollTo({{ top: 0, behavior: 'smooth' }});

  }} catch (err) {{
    showMsg('error', '❌ Network error — ခဏနေ ပြန်ကြိုးစားပါ');
  }} finally {{
    btn.disabled = false;
    btn.innerHTML = '{f'Register &mdash; {fee:,} Ks' if fee > 0 else 'Register Now — Free 🎉'}'.replace('&mdash;', '—').replace('&mdash;', '—');
  }}
  return false;
}}

// ── Public: Registered Players + Bracket ──
function escapeHtml(s) {{
  return String(s || '').replace(/[&<>"']/g, function(c) {{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];
  }});
}}
function openBracket() {{
  window.open('/api/tournaments/' + TID + '/bracket?view=html', 'psvibe-tournament-bracket');
}}
function openRules() {{
  window.open('/api/tournaments/' + TID + '/rules?view=html', 'psvibe-tournament-rules');
}}
async function loadPlayers() {{
  try {{
    const r = await fetch('/api/tournaments/' + TID + '/players');
    const d = await r.json();
    if (!d.success) {{ throw new Error('fail'); }}
    const ps = d.data.players || [];
    document.getElementById('playersCount').textContent = '(' + ps.length + ')';
    const el = document.getElementById('playersList');
    if (!ps.length) {{
      el.innerHTML = '<p class="text-gray-500 text-xs py-2">လူဦးရေ မရှိသေးပါ — ပထမဆုံး Register လုပ်သူ ဖြစ်လိုက်ပါ! 🎉</p>';
      return;
    }}
    let hint = '';
    if (d.data.anonymous) {{
      hint = '<p class="text-[11px] text-gray-400 italic mb-2">🎭 Registration ပိတ်ချိန်အထိ အမည်များကို ဖော်ပြမထားပါ</p>';
    }}
    el.innerHTML = hint + ps.map(function(p) {{
      const badge = p.paid
        ? '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-900/50 border border-emerald-700 text-emerald-300">✅ Confirmed</span>'
        : '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-900/40 border border-amber-700 text-amber-300">⏳ Pending</span>';
      const seed = p.seed ? '<span class="text-gray-500 text-[11px] w-7 inline-block">#' + p.seed + '</span>' : '';
      return '<div class="flex items-center justify-between bg-surface-600 rounded-lg px-3 py-2">' +
        '<div class="flex items-center gap-2 text-sm text-gray-200 min-w-0">' + seed +
        '<span class="truncate">' + escapeHtml(p.name) + '</span></div>' + badge + '</div>';
    }}).join('');
  }} catch (e) {{
    document.getElementById('playersList').innerHTML = '<p class="text-gray-500 text-xs py-2">Players list ဖွင့်မရပါ — ခဏနေ ပြန်ကြိုးစားပါ</p>';
  }}
}}
loadPlayers();
</script>
</body>
</html>"""
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error("registration_page error: %s", e)
        return HTMLResponse(content="<h2>Server error</h2>", status_code=500)


# ═══════════════════════════════════════
#  Public — Registration Endpoint
# ═══════════════════════════════════════

@router.post("/api/tournaments/{tid}/register")
async def register_player(tid: int, body: dict):
    """Public registration for a tournament.

    - If entry_fee_ks > 0 AND member_id given (website flow): checks wallet balance, deducts,
      inserts cash_movements 'eject' row, marks paid=1.
    - If entry_fee_ks > 0 AND payment_method + ss_file given (digital payment from bot):
      paid=0, stores payment_method + ss_file, status='registered' (pending staff confirm).
    - If entry_fee_ks > 0 and NO member_id/payment_method: paid=0 (pay at counter — legacy).
    - If entry_fee_ks == 0: paid=1.
    - Duplicate phone → 409.
    """
    try:
        t = _ensure_tournament(tid)
        if t["status"] not in ("open", "draft"):
            return error_response(message="Tournament is not open for registration")

        name = (body.get("name") or "").strip()
        phone = (body.get("phone") or "").strip()
        team1 = (body.get("team1") or "").strip()
        team2 = (body.get("team2") or "").strip()
        team3 = (body.get("team3") or "").strip()
        rules_ack = body.get("rules_ack")
        if not name or not phone:
            return error_response(message="name and phone are required")

        # All 3 team names are required (used throughout the tournament).
        if not team1 or not team2 or not team3:
            return error_response(
                message="အသင်း သုံးသင်း ဖြည့်ပေးပါ (team1/team2/team3 required)", code=400
            )

        # Rules acknowledgment is mandatory.
        if not rules_ack or rules_ack in (0, "0", False, "false", "False"):
            return error_response(message="rules must be acknowledged", code=400)

        # Check max players
        reg_count = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM tournament_players WHERE tournament_id=%s", (tid,)
        )["cnt"] or 0
        if reg_count >= int(t["max_players"]):
            return error_response(message="Tournament is full")

        fee = int(t["entry_fee_ks"] or 0)
        member_id = body.get("member_id")
        ss_file = body.get("ss_file")
        pm_raw = body.get("payment_method")
        payment_method = _parse_payment_method(pm_raw) if pm_raw else None
        paid = 0

        if fee > 0 and member_id:
            # Website / dashboard wallet deduction flow
            wallet = _mysql_query_one(
                "SELECT balance_mins, member_name FROM member_wallets WHERE member_id=%s",
                (str(member_id),),
            )
            if not wallet:
                return error_response(message=f"Member {member_id} not found")

            balance = int(wallet.get("balance_mins", 0) or 0)
            if balance < fee:
                return error_response(message=f"Insufficient balance: need {fee} Ks, have {balance} Ks")

            # Deduct wallet balance
            new_bal = balance - fee
            _mysql_exec(
                "UPDATE member_wallets SET balance_mins=%s, last_updated=NOW() WHERE member_id=%s",
                (new_bal, str(member_id)),
            )

            # Record cash_movements as 'eject' (wallet deduction = money going out)
            _mysql_exec(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())""",
                (
                    "eject",
                    "Cash",
                    fee,
                    f"Tournament entry: {t['name']} (player: {name}) — wallet deduction",
                    "system",
                ),
            )

            paid = 1
            payment_method = "wallet"
            logger.info(
                "Tournament reg: member %s wallet deducted %s Ks for tournament %s. Bal: %s → %s",
                member_id, fee, tid, balance, new_bal,
            )

        elif fee > 0 and payment_method and ss_file:
            # Digital payment from bot (KPay/WavePay/KBZ) — pending confirmation
            paid = 0
            logger.info(
                "Tournament reg: digital payment via %s (ss_file=%s) for tournament %s",
                payment_method, ss_file[:40] if ss_file else "none", tid,
            )

        elif fee == 0:
            paid = 1
            payment_method = "free"

        # Insert player — handle duplicate phone gracefully
        pid = None
        try:
            pid = _mysql_exec(
                """INSERT INTO tournament_players
                   (tournament_id, member_id, name, phone, paid, payment_method, ss_file, paid_at, status, team1, team2, team3, rules_ack)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tid,
                    member_id,
                    name,
                    phone,
                    paid,
                    payment_method,
                    ss_file,
                    datetime.now() if paid else None,
                    "confirmed" if paid else "registered",
                    team1,
                    team2,
                    team3,
                    1 if rules_ack else 0,
                ),
            )
        except Exception as dup_err:
            err_str = str(dup_err).lower()
            if "duplicate" in err_str or "uq_tourn" in err_str:
                # Refund if we already deducted
                if paid and fee > 0 and member_id:
                    _mysql_exec(
                        "UPDATE member_wallets SET balance_mins=balance_mins+%s, last_updated=NOW() WHERE member_id=%s",
                        (fee, str(member_id)),
                    )
                    _mysql_exec(
                        """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at)
                           VALUES (%s, %s, %s, %s, %s, NOW())""",
                        (
                            "inject",
                            "Cash",
                            fee,
                            f"Tournament entry refund (duplicate): {t['name']} (player: {name})",
                            "system",
                        ),
                    )
                return error_response(message="Phone already registered for this tournament", code=409)
            raise

        logger.info("Player %s registered for tournament %s (paid=%s, id=%s)", name, tid, paid, pid)
        return ok({"registered": True, "player_name": name, "paid": bool(paid), "player_id": pid})
    except Exception as e:
        logger.error("register_player error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  Public — Bracket / Players View
# ═══════════════════════════════════════

@router.get("/api/tournaments/{tid}/bracket")
async def view_bracket(tid: int, request: Request = None, view: str = Query(None)):
    """Public bracket view: tournament info + all matches grouped by round.
    
    Pass ?view=html for a styled HTML bracket page (shareable).
    Default returns JSON.
    """
    try:
        t = _mysql_query_one("SELECT * FROM tournaments WHERE id=%s", (tid,))
        if not t:
            return error_response(message="Tournament not found", code=404)

        matches = _mysql_query(
            """SELECT m.*, 
                      p1.name as player1_name, p1.seed as player1_seed,
                      p2.name as player2_name, p2.seed as player2_seed,
                      w.name as winner_name
               FROM tournament_matches m
               LEFT JOIN tournament_players p1 ON m.player1_id = p1.id
               LEFT JOIN tournament_players p2 ON m.player2_id = p2.id
               LEFT JOIN tournament_players w ON m.winner_id = w.id
               WHERE m.tournament_id = %s
               ORDER BY m.round ASC, m.position ASC""",
            (tid,),
        )

        rounds = {}
        for m in matches:
            r = m["round"]
            if r not in rounds:
                rounds[r] = []
            rounds[r].append({
                "match_id": m["id"],
                "position": m["position"],
                "bracket": m.get("bracket") or "winners",
                "console_id": m.get("console_id"),
                "scheduled_at": str(m["scheduled_at"]) if m.get("scheduled_at") else None,
                "notes": m.get("notes"),
                "player1": {"id": m["player1_id"], "name": m.get("player1_name"), "seed": m.get("player1_seed")},
                "player2": {"id": m["player2_id"], "name": m.get("player2_name"), "seed": m.get("player2_seed")},
                "score1": m["score1"],
                "score2": m["score2"],
                "winner": m.get("winner_name"),
                "status": m["status"],
            })

        t_info = dict(t)
        for f in ("created_at", "updated_at", "reg_open_at", "reg_close_at", "start_at"):
            if t_info.get(f):
                t_info[f] = str(t_info[f])

        player_count = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM tournament_players WHERE tournament_id=%s", (tid,)
        )["cnt"] or 0

        # If HTML view requested, return styled bracket page
        if view == "html":
            return _render_bracket_html(t, rounds, player_count)

        result = {
            "tournament": t_info,
            "player_count": player_count,
            "rounds": rounds,
        }
        if t.get("format") in ("double", "hybrid"):
            # Build per-bracket view: winners (round 1..k), losers (round 1..10), final.
            brackets = {"winners": {}, "losers": {}, "final": []}
            for m in matches:
                b = m.get("bracket") or "winners"
                rr = m["round"]
                entry = {
                    "match_id": m["id"],
                    "position": m["position"],
                    "bracket": b,
                    "feeds_from": m.get("feeds_from"),
                    "console_id": m.get("console_id"),
                    "scheduled_at": str(m["scheduled_at"]) if m.get("scheduled_at") else None,
                    "notes": m.get("notes"),
                    "player1": {"id": m["player1_id"], "name": m.get("player1_name"), "seed": m.get("player1_seed")},
                    "player2": {"id": m["player2_id"], "name": m.get("player2_name"), "seed": m.get("player2_seed")},
                    "score1": m["score1"],
                    "score2": m["score2"],
                    "winner": m.get("winner_name"),
                    "status": m["status"],
                }
                if b == "final":
                    brackets["final"].append(entry)
                else:
                    brackets[b].setdefault(rr, []).append(entry)
            result["brackets"] = brackets

        return ok(result)
    except Exception as e:
        logger.error("view_bracket error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments/{tid}/rules")
async def view_rules(tid: int, request: Request = None, view: str = Query(None)):
    """Public standalone rules page: tournament info + rules content.

    Pass ?view=html for a styled HTML rules page (shareable, opens in new tab).
    Default returns JSON.
    """
    try:
        t = _ensure_tournament(tid)
        t_info = dict(t)
        for f in ("created_at", "updated_at", "reg_open_at", "reg_close_at", "start_at"):
            if t_info.get(f):
                t_info[f] = str(t_info[f])

        if view == "html":
            return _render_rules_html(t)

        return ok({
            "tournament": t_info,
            "rules": t.get("rules") or "",
        })
    except Exception as e:
        logger.error("view_rules error: %s", e)
        return error_response(message=str(e))


def _fmt_sched(dt_val) -> str:
    """Format scheduled_at as 'Sep 25, 10:30 PM' (HH:MM AM/PM + short date)."""
    if not dt_val:
        return ""
    try:
        s = str(dt_val).strip()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    except Exception:
        return s


def _is_authenticated(request: Request) -> bool:
    """True if request carries a valid API key or JWT (dashboard/admin).

    Non-raising variant of deps.verify_api_key — used to decide whether to
    return full phone numbers in public player list endpoints.
    """
    try:
        from config import API_KEY
        key = request.headers.get("X-API-Key")
        if key and API_KEY and key == API_KEY:
            return True
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            from auth import decode_token
            payload = decode_token(auth[7:])
            if payload and payload.get("sub"):
                return True
    except Exception:
        pass
    return False


@router.get("/api/tournaments/{tid}/players")
async def list_players_public(tid: int, request: Request):
    """Player list — phone masked for anonymous visitors, full number for authenticated dashboard.

    Player NAMES are always shown publicly (real names). Phones remain masked for
    anonymous visitors; full number only for authenticated dashboard.
    """
    try:
        _ensure_tournament(tid)
        show_full_phone = _is_authenticated(request)
        players = _mysql_query(
            "SELECT id, name, phone, seed, status, paid, payment_method, ss_file, team1, team2, team3, rules_ack, placement FROM tournament_players WHERE tournament_id=%s ORDER BY seed ASC, id ASC",
            (tid,),
        )
        result = []
        for p in players:
            p = dict(p)
            if not show_full_phone:
                # Mask phone: show first 3 and last 2 digits
                raw = p.get("phone", "")
                if len(raw) >= 5:
                    p["phone"] = raw[:3] + "*" * (len(raw) - 5) + raw[-2:]
            p["ss_url"] = _player_ss_url(p)
            result.append(p)

        count = len(result)
        return ok({"count": count, "players": result, "anonymous": False})
    except Exception as e:
        logger.error("list_players_public error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  SS Upload (Payment Screenshot for Web Registration)
# ═══════════════════════════════════════

@router.post("/api/tournaments/{tid}/players/{pid}/ss")
async def upload_player_ss(tid: int, pid: int, file: UploadFile = File(...)):
    """Upload a payment screenshot for a tournament player.
    
    Multipart form upload. Validates image type, saves to uploads/tournaments/,
    updates tournament_players.ss_file, returns ss_url.
    
    Also works as an update: if player already has a legacy file_id, it gets overwritten.
    """
    try:
        try:
            _ensure_tournament(tid)
        except HTTPException as he:
            return error_response(message=he.detail, code=he.status_code)
        
        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE id=%s AND tournament_id=%s",
            (pid, tid),
        )
        if not player:
            return error_response(message="Player not found", code=404)
        
        # Validate content type
        ct = file.content_type or ""
        if not ct.startswith("image/"):
            return error_response(message="Only image files are accepted (JPEG, PNG, etc.)", code=400)
        
        # Determine file extension
        ext_map = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/bmp": ".bmp",
        }
        ext = ext_map.get(ct, ".jpg")
        
        # Save file
        os.makedirs(_UPLOADS_DIR, exist_ok=True)
        filename = f"t{tid}_p{pid}_{int(time.time())}{ext}"
        filepath = os.path.join(_UPLOADS_DIR, filename)
        
        contents = await file.read()
        with open(filepath, "wb") as f:
            f.write(contents)
        
        # Relative path for DB
        rel_path = f"uploads/tournaments/{filename}"
        
        # Update player record
        _mysql_exec(
            "UPDATE tournament_players SET ss_file=%s WHERE id=%s",
            (rel_path, pid),
        )
        
        logger.info("SS uploaded for player %s in tournament %s: %s (%s bytes)", pid, tid, filename, len(contents))
        
        return ok({
            "player_id": pid,
            "ss_file": rel_path,
            "ss_url": f"/{rel_path}",
            "bytes": len(contents),
        })
    except Exception as e:
        logger.error("upload_player_ss error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  FEATURE 1 — Check-in System
# ═══════════════════════════════════════

@router.post("/api/tournaments/{tid}/checkin")
async def public_checkin(tid: int, body: dict):
    """Public check-in: find player by tournament_id + phone, set status='checked_in'."""
    try:
        name = (body.get("name") or "").strip()
        phone = (body.get("phone") or "").strip()
        if not phone:
            return error_response(message="phone is required", code=400)

        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE tournament_id=%s AND TRIM(phone)=%s",
            (tid, phone),
        )
        if not player:
            return error_response(message="Player not found", code=404)

        if player["status"] not in ("registered", "confirmed", "checked_in"):
            return error_response(
                message="Player already eliminated or dropped out", code=400
            )

        _mysql_exec(
            "UPDATE tournament_players SET status='checked_in' WHERE id=%s",
            (player["id"],),
        )
        return ok({"checked_in": True, "player_name": player["name"]})
    except Exception as e:
        logger.error("public_checkin error: %s", e)
        return error_response(message=str(e))


@router.post("/api/tournaments/{tid}/players/{pid}/checkin")
async def admin_checkin(tid: int, pid: int, auth=Depends(verify_api_key)):
    """Admin: toggle/set a specific player's check-in status."""
    try:
        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE id=%s AND tournament_id=%s",
            (pid, tid),
        )
        if not player:
            return error_response(message="Player not found", code=404)

        new_status = "checked_in" if player["status"] != "checked_in" else "confirmed"
        _mysql_exec(
            "UPDATE tournament_players SET status=%s WHERE id=%s",
            (new_status, pid),
        )
        return ok({"player_id": pid, "status": new_status})
    except Exception as e:
        logger.error("admin_checkin error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments/{tid}/checkins")
async def admin_checkins(tid: int, auth=Depends(verify_api_key)):
    """Admin: players with check-in status + counts."""
    try:
        _ensure_tournament(tid)
        players = _mysql_query(
            "SELECT id, name, phone, seed, status, paid FROM tournament_players WHERE tournament_id=%s ORDER BY seed ASC, id ASC",
            (tid,),
        )
        total = len(players)
        checked_in = sum(1 for p in players if p["status"] == "checked_in")
        return ok({
            "total": total,
            "checked_in": checked_in,
            "missing": total - checked_in,
            "players": [dict(p) for p in players],
        })
    except Exception as e:
        logger.error("admin_checkins error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  FEATURE 2 — Seeding / Randomize
# ═══════════════════════════════════════

@router.post("/api/tournaments/{tid}/seeds/randomize")
async def randomize_seeds(tid: int, auth=Depends(verify_api_key)):
    """Reshuffle seed numbers among confirmed+checked_in players."""
    try:
        t = _ensure_tournament(tid)
        if t["status"] not in ("open", "closed", "draft"):
            return error_response(
                message=f"Cannot randomize seeds with status '{t['status']}'. Must be open/closed/draft.",
                code=400,
            )
        import random
        players = _mysql_query(
            "SELECT id FROM tournament_players WHERE tournament_id=%s AND status IN ('confirmed','checked_in') ORDER BY id",
            (tid,),
        )
        ids = [p["id"] for p in players]
        random.shuffle(ids)
        for idx, pid in enumerate(ids):
            _mysql_exec("UPDATE tournament_players SET seed=%s WHERE id=%s", (idx + 1, pid))
        return ok({"randomized": len(ids)})
    except Exception as e:
        logger.error("randomize_seeds error: %s", e)
        return error_response(message=str(e))


@router.patch("/api/tournaments/{tid}/players/{pid}/seed")
async def set_player_seed(tid: int, pid: int, body: dict, auth=Depends(verify_api_key)):
    """Set a specific player's seed number (400 if tournament is live)."""
    try:
        t = _ensure_tournament(tid)
        if t["status"] == "live":
            return error_response(message="Cannot change seeds while tournament is live", code=400)
        seed = int(body.get("seed", 0))
        if seed < 1:
            return error_response(message="seed must be >= 1", code=400)
        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE id=%s AND tournament_id=%s",
            (pid, tid),
        )
        if not player:
            return error_response(message="Player not found", code=404)
        _mysql_exec("UPDATE tournament_players SET seed=%s WHERE id=%s", (seed, pid))
        return ok({"player_id": pid, "seed": seed})
    except Exception as e:
        logger.error("set_player_seed error: %s", e)
        return error_response(message=str(e))


@router.post("/api/tournaments/{tid}/bracket/regenerate")
async def regenerate_bracket(tid: int, auth=Depends(verify_api_key)):
    """Delete existing matches, re-run seeding + round-1 generation."""
    try:
        t = _ensure_tournament(tid)
        if t["status"] == "live":
            return error_response(message="Cannot regenerate bracket while tournament is live", code=400)
        players = _mysql_query(
            "SELECT * FROM tournament_players WHERE tournament_id=%s AND status IN ('registered','confirmed','checked_in') ORDER BY id",
            (tid,),
        )
        if len(players) < int(t["min_players"]):
            missing = int(t["min_players"]) - len(players)
            return error_response(
                message=f"Need at least {t['min_players']} eligible players. Currently have {len(players)} (missing {missing}).",
                code=400,
            )

        _mysql_exec("DELETE FROM tournament_matches WHERE tournament_id=%s", (tid,))
        _mysql_exec("DELETE FROM tournament_notifications WHERE tournament_id=%s", (tid,))
        if t.get("format") == "double":
            total_rounds, match_count, shuffled = _generate_double_elim_bracket(tid, t, players)
            if total_rounds == 0:
                return error_response(message="Need at least 4 players to regenerate a double-elimination bracket")
            return ok({"regenerated": True, "format": "double", "players": len(shuffled), "matches_generated": match_count})
        if t.get("format") == "hybrid":
            total_rounds, r1_matches, shuffled = _generate_hybrid_bracket(tid, t, players)
            if total_rounds == 0:
                return error_response(message="Need at least 2 players to start")
            return ok({"regenerated": True, "format": "hybrid", "players": len(shuffled), "r1_matches": r1_matches})
        total_rounds, r1_matches, shuffled = _generate_seeded_bracket(tid, t, players)
        if total_rounds == 0:
            return error_response(message="Need at least 2 players to start")
        return ok({"regenerated": True, "players": len(shuffled), "r1_matches": r1_matches})
    except Exception as e:
        logger.error("regenerate_bracket error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  FEATURE 3 — Winner Flow + Prize + Champion History
# ═══════════════════════════════════════

@router.post("/api/tournaments/{tid}/winner")
async def admin_set_winner(tid: int, body: dict, auth=Depends(verify_api_key)):
    """Admin fallback: set champion + mark tournament completed."""
    try:
        t = _ensure_tournament(tid)
        player_id = int(body.get("player_id", 0))
        if not player_id:
            return error_response(message="player_id is required", code=400)
        player = _mysql_query_one(
            "SELECT * FROM tournament_players WHERE id=%s AND tournament_id=%s",
            (player_id, tid),
        )
        if not player:
            return error_response(message="Player not found", code=404)

        prize_ks = body.get("prize_ks")
        prize_text = body.get("prize_text")
        if prize_ks is None and prize_text is None:
            prize_raw = t.get("prize")
            _pool = t.get("prize_pool_ks")
            prize_ks = _resolve_champion_prize_ks(tid, prize_raw, _pool)
            prize_text = prize_raw

        _mysql_exec(
            """INSERT INTO tournament_champions
               (tournament_id, player_id, player_name, prize_ks, prize_text, won_at)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON DUPLICATE KEY UPDATE
                 player_id=VALUES(player_id), player_name=VALUES(player_name),
                 prize_ks=VALUES(prize_ks), prize_text=VALUES(prize_text), won_at=NOW()""",
            (tid, player["id"], player["name"], prize_ks, prize_text),
        )

        if t["status"] != "completed":
            _mysql_exec("UPDATE tournaments SET status='completed' WHERE id=%s", (tid,))

        # Notify winner
        _insert_notification(
            tid, player["id"], player.get("telegram_id"),
            f"🏆 ချန်ပီယံဖြစ်ပြီ! {t['name']} — ဆုကြေး: {prize_text or '—'} — ဂုဏ်ယူပါတယ်!",
            kind="champion",
        )
        return ok({"champion": _get_champion(tid)})
    except Exception as e:
        logger.error("admin_set_winner error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  Utility
# ═══════════════════════════════════════

def _ensure_tournament(tid: int) -> dict:
    """Fetch tournament by ID or raise 404."""
    t = _mysql_query_one("SELECT * FROM tournaments WHERE id=%s", (tid,))
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return t


def _player_ss_url(player: dict) -> str | None:
    """Build ss_url from ss_file if it's a local upload path."""
    sf = player.get("ss_file") or ""
    if sf.startswith("uploads/"):
        return "/" + sf
    return None


def _render_double_elim_html(t: dict, rounds: dict, player_count: int, champion, prizes) -> HTMLResponse:
    """Render a double-elimination bracket page (neon esports theme): Winners + Losers + Grand Final lanes."""
    import html as _html
    tn_name = _html.escape(t.get("name", "Tournament"))
    tn_game = _html.escape(t.get("game", ""))
    tid = int(t["id"])
    status = (t.get("status") or "").lower()
    fmt_badge = "Top 4 Double Elimination" if t.get("format") == "hybrid" else "Double Elimination"

    winners = {}
    losers = {}
    final = []
    for rnum, ms in rounds.items():
        for m in ms:
            b = m.get("bracket") or "winners"
            if b == "final":
                final.append(m)
            elif b == "losers":
                losers.setdefault(int(rnum), []).append(m)
            else:
                winners.setdefault(int(rnum), []).append(m)

    def _match_card(m, rnum=None):
        bracket = m.get("bracket") or "winners"
        p1 = m.get("player1") or {}
        p2 = m.get("player2") or {}
        p1_id = p1.get("id")
        p2_id = p2.get("id")
        # Genuine bye = a round-1 winners match with exactly one real player
        # (bracket padded to a power of 2). Every other empty slot = TBD.
        is_bye = bracket == "winners" and rnum == 1 and bool(p1_id) != bool(p2_id)
        p1_n = p1.get("name") or ("BYE" if is_bye else "TBD")
        p2_n = p2.get("name") or ("BYE" if is_bye else "TBD")
        s1 = m.get("score1")
        s2 = m.get("score2")
        winner = m.get("winner")
        p1_seed = p1.get("seed")
        p2_seed = p2.get("seed")
        p1_w = bool(winner and winner == p1_n)
        p2_w = bool(winner and winner == p2_n)

        def _row(pn, seed, sc, is_w):
            name = pn or "TBD"
            if name in ("TBD", "BYE"):
                ph_style = (' style="color:#9b8bff;font-style:italic;opacity:.8;text-shadow:0 0 8px rgba(109,94,255,0.5)"'
                            if name == "TBD" else ' style="color:#8A8FA8;font-style:italic;opacity:.75"')
                return f'<div class="bracket-row"><span class="seed"></span><span class="player-name"{ph_style}>{_html.escape(name)}</span></div>'
            if seed is not None and str(seed) != "":
                seed_cls = "win" if is_w else ("loss" if winner else "")
                seed_chip = f'<span class="seed {seed_cls}">{_html.escape(str(seed))}</span>'
            else:
                seed_chip = '<span class="seed"></span>'
            if is_w:
                name_cls = "win"
            elif winner and name != "BYE":
                name_cls = "loss"
            else:
                name_cls = ""
            trophy = '<span class="win-trophy">🏆</span>' if is_w else ""
            sc_badge = ""
            if sc is not None:
                sc_cls = "win" if is_w else ""
                sc_badge = f'<span class="score-pill {sc_cls}">{_html.escape(str(sc))}</span>'
            return f'<div class="bracket-row">{seed_chip}<span class="player-name {name_cls}">{_html.escape(name)}{trophy}</span>{sc_badge}</div>'

        r1 = _row(p1_n, p1_seed, s1, p1_w)
        r2 = _row(p2_n, p2_seed, s2, p2_w)
        sched_line = ''
        if m.get('console_id') or m.get('scheduled_at'):
            parts = []
            if m.get('console_id'):
                parts.append(f'🎮 {_html.escape(str(m["console_id"]))}')
            if m.get('scheduled_at'):
                parts.append(f'🕐 {_html.escape(_fmt_sched(m["scheduled_at"]))}')
            sched_line = f'<div class="sched-line">{" · ".join(parts)}</div>'
        return f'<div class="bracket-card">{r1}{r2}{sched_line}</div>'

    def _active_round(data):
        rns = sorted(data.keys())
        if not rns:
            return None
        for rn in rns:
            if any((m.get("winner") is None) for m in data[rn]):
                return rn
        return rns[-1]

    def _round_pill(label, active=False):
        cls = "round-pill active" if active else "round-pill"
        dot = '<span class="dot"></span>' if active else ""
        return f'<div class="{cls}">{dot}{_html.escape(label)}</div>'

    def _lane(title, subtitle, data):
        if not data:
            return f'<div class="min-w-[240px]"><h3 class="lane-title">{title}</h3><p class="text-[11px] text-[#8A8FA8]">{subtitle}</p></div>'
        rns = sorted(data.keys())
        active_rn = _active_round(data)
        cols = []
        for rn in rns:
            ms = sorted(data[rn], key=lambda x: int(x.get("position") or 0))
            cards = "".join(_match_card(m, rn) for m in ms)
            cols.append(f'<div class="bracket-col">{_round_pill(f"Round {rn}", active=(rn == active_rn))}<div class="flex flex-col gap-3">{cards}</div></div>')
        return f'<div class="min-w-[240px]"><h3 class="lane-title">{title} <span class="lane-sub">{subtitle}</span></h3><div class="flex gap-6 items-start">{"".join(cols)}</div></div>'

    winners_html = _lane("🏆 Winners Bracket", f"{len(winners)} rounds", winners)
    losers_html = _lane("🔄 Losers Bracket", f"{len(losers)} rounds", losers)
    if final:
        final_cards = "".join(_match_card(m, None) for m in final)
        final_html = f'<div class="min-w-[240px]"><h3 class="lane-title">🏆 Grand Final</h3><div class="bracket-col">{_round_pill("Grand Final", active=True)}<div class="flex flex-col gap-3">{final_cards}</div></div></div>'
    else:
        final_html = ""

    champion_banner = ""
    if champion:
        cp = champion.get("prize_text") or (f"{champion.get('prize_ks', 0):,.0f} Ks" if champion.get("prize_ks") else "")
        pl = f'<p class="text-amber-200/90 text-sm mt-1.5">ဆုကြေး: {_html.escape(cp)}</p>' if cp else ""
        champion_banner = f'<div class="mb-5 rounded-2xl border border-amber-400/40 bg-gradient-to-r from-amber-500/20 via-amber-400/10 to-[#6D5EFF]/20 backdrop-blur-md p-5 text-center"><div class="text-4xl mb-1">🏆</div><p class="text-amber-300 font-black text-xl">ချန်ပီယံ: {_html.escape(champion["player_name"])}</p>{pl}</div>'

    live_badge = ""
    if status == "live":
        live_badge = '<span class="inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full bg-red-600 text-white animate-pulse">● LIVE</span>'
    elif status == "completed":
        live_badge = '<span class="inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full bg-emerald-700 text-white">✓ Completed</span>'

    html = f"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{tn_name} — Bracket</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={{theme:{{extend:{{colors:{{vibe:{{300:'#b9aaff',400:'#9b8bff',500:'#6D5EFF',600:'#5a4ae0',700:'#4a3cc0',800:'#3a3090',950:'#211d4d'}},surface:{{900:'#151628',800:'#1E1F30',700:'#262742',600:'#34384C'}}}}}}}}}}</script>
<style>
  body {{ background: radial-gradient(ellipse at 50% 0%, #262742 0%, #1E1F30 45%, #151628 100%); color: #D4D4DC; }}
  ::-webkit-scrollbar {{ height: 8px; width: 8px; }}
  ::-webkit-scrollbar-thumb {{ background: #6D5EFF; border-radius: 8px; }}
  ::-webkit-scrollbar-track {{ background: #1E1F30; border-radius: 8px; }}

  .lane-title {{ font-size: 14px; font-weight: 800; color: #ffffff; margin-bottom: 10px; letter-spacing: .01em; }}
  .lane-sub {{ color: #8A8FA8; font-weight: 400; font-size: 12px; }}

  .bracket-col {{ display: flex; flex-direction: column; gap: 10px; min-width: 220px; }}

  .round-pill {{
    display: inline-flex; align-items: center; gap: 6px; align-self: flex-start;
    padding: 5px 14px; border-radius: 9999px;
    background: #34384C; color: #D4D4DC; border: 1px solid rgba(255,255,255,0.06);
    font-size: 12px; font-weight: 700; letter-spacing: .02em; white-space: nowrap;
  }}
  .round-pill.active {{ background: #6D5EFF; color: #ffffff; border-color: transparent; box-shadow: 0 0 18px rgba(109,94,255,0.55); }}
  .round-pill .dot {{ width: 6px; height: 6px; border-radius: 50%; background: #FFC107; box-shadow: 0 0 8px #FFC107; }}

  .bracket-card {{
    background: rgba(38,39,66,0.9);
    border: 1px solid rgba(109,94,255,0.38);
    border-radius: 10px;
    padding: 8px 10px;
    box-shadow: 0 0 14px rgba(109,94,255,0.22), inset 0 1px 0 rgba(255,255,255,0.05);
  }}
  .bracket-card:hover {{ border-color: rgba(109,94,255,0.75); box-shadow: 0 0 22px rgba(109,94,255,0.42); }}

  .bracket-row {{ display: flex; align-items: center; gap: 8px; font-size: 12px; }}
  .bracket-row + .bracket-row {{ margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(109,94,255,0.16); }}

  .seed {{
    flex-shrink: 0; width: 20px; height: 20px; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 800; line-height: 1;
    background: #1E1F30; border: 1px solid rgba(109,94,255,0.5); color: #6D5EFF;
  }}
  .seed.loss {{ border-color: rgba(212,212,220,0.35); color: #D4D4DC; }}
  .seed.win {{ border-color: #FFC107; color: #FFC107; box-shadow: 0 0 6px rgba(255,193,7,0.4); }}

  .player-name {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #D4D4DC; }}
  .player-name.win {{ color: #ffffff; font-weight: 700; }}
  .player-name.loss {{ color: #8A8FA8; text-decoration: line-through; opacity: .75; }}

  .win-trophy {{ color: #FFC107; font-size: 10px; flex-shrink: 0; margin-left: 2px; }}

  .score-pill {{
    flex-shrink: 0; min-width: 26px; text-align: center;
    font-size: 11px; font-weight: 800; font-variant-numeric: tabular-nums;
    padding: 2px 7px; border-radius: 9999px;
    background: #34384C; color: #D4D4DC; border: 1px solid rgba(255,255,255,0.08);
  }}
  .score-pill.win {{ background: #FFC107; color: #1E1F30; border-color: transparent; box-shadow: 0 0 10px rgba(255,193,7,0.4); }}

  .sched-line {{
    display: flex; align-items: center; justify-content: center; gap: 6px;
    margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(109,94,255,0.16);
    font-size: 10px; color: #8A8FA8;
  }}
</style>
</head>
<body class="min-h-screen text-white font-sans p-4">
<div class="max-w-6xl mx-auto">
  <div class="text-center mb-5">
    <h1 class="text-3xl font-extrabold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white via-vibe-300 to-vibe-400">🏆 {tn_name}</h1>
    <div class="flex items-center justify-center flex-wrap gap-2 mt-2 text-xs text-gray-300">
      <span class="px-2 py-0.5 rounded-full bg-white/5 border border-white/10">🎮 {tn_game}</span>
      <span class="px-2 py-0.5 rounded-full bg-white/5 border border-white/10">👥 {player_count} players</span>
      <span class="px-2 py-0.5 rounded-full bg-white/5 border border-white/10">🏆 {fmt_badge}</span>
      {live_badge}
    </div>
  </div>
  {champion_banner}
  <div class="overflow-x-auto pb-4">
    <div class="flex gap-8 items-start min-w-max">
      {winners_html}
      {losers_html}
      {final_html}
    </div>
  </div>
  <div class="text-center mt-8"><p class="text-gray-600 text-xs">PS VIBE Gaming Lounge &middot; Yangon</p></div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


def _render_bracket_html(t: dict, rounds: dict, player_count: int) -> HTMLResponse:
    """Render a PREMIUM styled HTML bracket page (Tailwind CDN) with FULL bracket structure.

    Once started, ALL rounds (1..total_rounds) render as columns; rounds without
    matches yet (created lazily on score entry) show dashed TBD placeholder slots.
    Not-started tournaments show an empty-state message.

    Neon esports redesign (2026-09-03): dark indigo #1E1F30 canvas, electric
    #6D5EFF accent + glow, #D4D4DC text, golden #FFC107 winner accents,
    Challonge-style horizontal rounds with pill round headers, neon connector
    lines, score pills, seed circles. Console + schedule badges preserved.
    """
    import html as _html
    tn_name = _html.escape(t.get("name", "Tournament"))
    tn_game = _html.escape(t.get("game", ""))
    reg_token = t.get("reg_token") or ""
    reg_link = f"https://ps-vibe.com/t/{_html.escape(reg_token)}" if reg_token else "#"
    tid = int(t["id"])
    status = (t.get("status") or "").lower()
    prize = t.get("prize") or ""
    prize_pool = t.get("prize_pool_ks")
    try:
        prize_pool_float = float(prize_pool) if prize_pool is not None else None
    except (ValueError, TypeError):
        prize_pool_float = None
    rules = t.get("rules") or ""
    champion = _get_champion(tid)
    prizes = _get_prizes(tid)

    if t.get("format") in ("double", "hybrid"):
        return _render_double_elim_html(t, rounds, player_count, champion, prizes)

    # Total rounds = log2(next power of 2 >= player_count): 64→6, 32→5, 5→3, 4→2
    total_rounds = max(1, (player_count - 1).bit_length())

    # Round → label (premium headers)
    def round_label(rnum: int) -> str:
        if rnum == total_rounds:
            return "Final"
        if rnum == total_rounds - 1:
            return "Semi-Final"
        if total_rounds >= 4 and rnum == total_rounds - 2:
            return "Quarter-Final"
        return f"Round {rnum}"

    # Current/active round = earliest round with an undecided match; else the last.
    active_rn = total_rounds
    for rnum in range(1, total_rounds + 1):
        ms = rounds.get(rnum) or []
        if any((m.get("winner") is None) for m in ms):
            active_rn = rnum
            break

    if rounds:
        # ── Symmetric single-elimination bracket via one CSS Grid across all rounds ──
        # R = total_rounds; M = round-1 matches = 2^(R-1); total grid rows T = 2*M.
        # Round r (1-indexed), match position p (1-indexed): span = 2^r,
        # start row = (p-1)*2^r + 1. Each card is vertically centered in its span
        # → symmetric Challonge/jquery-bracket tree (later rounds centered, no dead space).
        M = 2 ** (total_rounds - 1)
        T = 2 * M

        def _score_badge(s, is_w):
            if s is None:
                return ""
            cls = "win" if is_w else ""
            return f'<span class="score-pill {cls}">{_html.escape(str(s))}</span>'

        cells_by_round = []
        headers = []
        for rnum in range(1, total_rounds + 1):
            pill_cls = "round-pill active" if rnum == active_rn else "round-pill"
            dot = '<span class="dot"></span>' if rnum == active_rn else ""
            headers.append(f'<div class="col-head" style="grid-column:{rnum}"><div class="{pill_cls}">{dot}{round_label(rnum)}</div></div>')
        for rnum in range(1, total_rounds + 1):
            matches = rounds.get(rnum) or []
            is_final_col = (rnum == total_rounds)
            slot_count = 2 ** (total_rounds - rnum)
            span = 2 ** rnum
            by_pos = {int(m.get("position") or 0): m for m in matches}
            final_cls = " final-col" if is_final_col else ""
            if rnum == 1:
                final_cls += " first-col"
            cells = []
            for pos in range(1, slot_count + 1):
                start = (pos - 1) * span + 1
                m = by_pos.get(pos)
                # per-cell connector divs (plain HTML + CSS, NO SVG / NO filters)
                conns = ''
                if not is_final_col:
                    conns += '<div class="conn conn-r"></div>'
                if rnum > 1:
                    conns += '<div class="conn conn-v"></div><div class="conn conn-l"></div>'
                if m is None:
                    cells.append(f'''<div class="match-cell{final_cls}" data-round="{rnum}" data-pos="{pos}" style="grid-column:{rnum};grid-row:{start} / span {span}">{conns}<div class="bracket-card dashed-card"><div class="py-1.5 text-center tbd"><span class="shimmer-dot inline-block w-1.5 h-1.5 rounded-full mr-1 animate-pulse"></span>TBD</div></div></div>''')
                    continue
                p1 = m["player1"] or {}
                p2 = m["player2"] or {}
                p1_id = p1.get("id")
                p2_id = p2.get("id")
                # Genuine bye = a round-1 match with exactly one real player
                # (bracket padded to a power of 2). All other empty slots = TBD.
                is_bye = (rnum == 1) and bool(p1_id) != bool(p2_id)
                p1_n = p1.get("name") or ("BYE" if is_bye else "TBD")
                p2_n = p2.get("name") or ("BYE" if is_bye else "TBD")
                s1 = m.get("score1")
                s2 = m.get("score2")
                winner = m.get("winner")
                status_m = m.get("status", "pending")
                p1_seed = p1.get("seed")
                p2_seed = p2.get("seed")

                p1_is_w = bool(winner and winner == p1_n)
                p2_is_w = bool(winner and winner == p2_n)

                def _row(pn, seed, s_str, is_w):
                    p_name = pn or "TBD"
                    if p_name in ("TBD", "BYE"):
                        ph_style = (' style="color:#9b8bff;font-style:italic;opacity:.8;text-shadow:0 0 8px rgba(109,94,255,0.5)"'
                                    if p_name == "TBD" else ' style="color:#8A8FA8;font-style:italic;opacity:.75"')
                        return f'<div class="bracket-row"><span class="seed"></span><span class="player-name"{ph_style}>{_html.escape(p_name)}</span></div>'
                    row_cls = "bracket-row bracket-row-winner" if is_w else "bracket-row"
                    if is_w:
                        name_cls = "win"
                    elif winner and p_name != "BYE":
                        name_cls = "loss"
                    else:
                        name_cls = ""
                    seed_cls = "win" if is_w else ("loss" if winner else "")
                    seed_chip = f'<span class="seed {seed_cls}">{_html.escape(str(seed))}</span>' if (seed is not None and str(seed) != "") else '<span class="seed"></span>'
                    trophy = '<span class="win-trophy">🏆</span>' if is_w else ""
                    return f'<div class="{row_cls}">{seed_chip}<span class="player-name {name_cls}">{_html.escape(p_name)}{trophy}</span>{s_str}</div>'

                row1 = _row(p1_n, p1_seed, _score_badge(s1, p1_is_w), p1_is_w)
                row2 = _row(p2_n, p2_seed, _score_badge(s2, p2_is_w), p2_is_w)

                # Console + scheduled time footer
                sched_line = ''
                if m.get('console_id') or m.get('scheduled_at'):
                    parts = []
                    if m.get('console_id'):
                        parts.append(f'🎮 {_html.escape(str(m["console_id"]))}')
                    if m.get('scheduled_at'):
                        parts.append(f'🕐 {_html.escape(_fmt_sched(m["scheduled_at"]))}')
                    sched_line = f'<div class="sched-line">{" · ".join(parts)}</div>'

                ring = ''
                if status == "live":
                    ring = '<span class="live-dot"></span>'
                    card_cls = "bracket-card card-live-gold" if is_final_col else "bracket-card card-live"
                else:
                    card_cls = "bracket-card"

                cells.append(f'''<div class="match-cell{final_cls}" data-round="{rnum}" data-pos="{pos}" style="grid-column:{rnum};grid-row:{start} / span {span}">{conns}<div class="{card_cls}">{ring}{row1}{row2}{sched_line}</div></div>''')
            cells_by_round.append("".join(cells))

        bracket_body = f'''<div class="bracket-scroll overflow-x-auto pb-4 pt-1 pr-4" id="bracketScroll">
    <div class="bracket-heads" style="grid-template-columns:repeat({total_rounds}, minmax(12rem, 1fr));">
      {"".join(headers)}
    </div>
    <div class="bracket-grid" id="bracketGrid" data-total-rounds="{total_rounds}" style="grid-template-columns:repeat({total_rounds}, minmax(12rem, 1fr));grid-template-rows:repeat({T}, 1fr);">
      {"".join(cells_by_round)}
    </div>
  </div>'''
    else:
        bracket_body = '''<div class="text-center py-14 fade-in">
    <div class="text-5xl mb-3">⏳</div>
    <p class="text-gray-300 font-semibold text-lg">Bracket မစရသေးပါ</p>
    <p class="text-gray-500 text-xs mt-2">Tournament စတင်ပြီးမှ bracket ပေါ်ပါမယ် — မကြာမီ ပြန်ကြည့်ပါဦး 🙏</p>
  </div>'''

    # ── LIVE badge ──
    live_badge = ''
    if status == "live":
        live_badge = '<span class="inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full bg-red-600 text-white animate-pulse shadow-lg shadow-red-600/30">● LIVE</span>'
    elif status == "completed":
        live_badge = '<span class="inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full bg-emerald-700 text-white shadow-lg shadow-emerald-700/30">✓ Completed</span>'

    # ── Champion banner (premium gold glass) ──
    champion_banner = ''
    if champion:
        champ_prize = champion.get("prize_text") or (f"{champion.get('prize_ks', 0):,.0f} Ks" if champion.get("prize_ks") else "")
        prize_line = f'<p class="text-amber-200/90 text-sm mt-1.5">ဆုကြေး: {_html.escape(champ_prize)}</p>' if champ_prize else ''
        champion_banner = f'''
  <div class="mb-5 rounded-2xl border border-amber-400/40 bg-gradient-to-r from-amber-500/20 via-amber-400/10 to-[#6D5EFF]/20 backdrop-blur-md p-5 text-center shadow-xl shadow-amber-900/20 champion-banner fade-in">
    <div class="text-4xl mb-1 sparkle">🏆</div>
    <p class="text-amber-300 font-black text-xl tracking-tight">ချန်ပီယံ: {_html.escape(champion['player_name'])}</p>
    {prize_line}
  </div>'''

    # ── Prize + Rules panels (glass cards) ──
    info_panels = ''
    if prize_pool_float is not None or prize or rules or champion or prizes:
        info_panels = '<div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-5 fade-in">'
        if prize_pool_float is not None:
            info_panels += f'<div class="bracket-card rounded-xl p-4"><span class="text-xs text-gray-400">🏅 ဆုကြေး စုစုပေါင်း (Prize Pool)</span><p class="text-amber-300 font-bold text-lg mt-1">💰 {prize_pool_float:,.0f} Ks</p></div>'
        if prize:
            info_panels += f'<div class="bracket-card rounded-xl p-4"><span class="text-xs text-gray-400">ဆုခွဲဝေမှု (Breakdown)</span><p class="text-amber-300 font-semibold mt-1">{_html.escape(prize)}</p></div>'
        if prizes:
            _pb = ''.join(
                f'<p class="text-amber-200 text-sm mt-0.5">{_place_medal(p["place"])} {_html.escape(p["label"] or "")}: <span class="font-mono text-amber-300">{_fmt_amount(p["amount_ks"])} Ks</span></p>'
                for p in prizes
            )
            info_panels += f'<div class="bracket-card rounded-xl p-4"><span class="text-xs text-gray-400">🏆 ဆုစာရင်း (Prize Breakdown)</span>{_pb}</div>'
        if rules:
            info_panels += f'<a href="/api/tournaments/{t["id"]}/rules?view=html" target="psvibe-tournament-rules" rel="noopener" class="inline-flex items-center justify-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg bg-gradient-to-r from-vibe-600 to-vibe-500 hover:from-vibe-500 hover:to-vibe-400 text-white transition-all shadow-md">📖 စည်းကမ်းများ ကြည့်ရန် (Rules)</a>'
        info_panels += '</div>'

    html = f"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{tn_name} — PS VIBE tournament bracket">
<title>{tn_name} — Bracket</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={{theme:{{extend:{{colors:{{vibe:{{300:'#b9aaff',400:'#9b8bff',500:'#6D5EFF',600:'#5a4ae0',700:'#4a3cc0',800:'#3a3090',950:'#211d4d'}},surface:{{900:'#151628',800:'#1E1F30',700:'#262742',600:'#34384C'}}}},fontFamily:{{sans:['Inter','ui-sans-serif','system-ui'],display:['Outfit','Inter','sans-serif']}}}}}}}}</script>
<style>
  body {{ background: radial-gradient(ellipse at 20% 0%, #262742 0%, #1E1F30 45%, #151628 100%); color: #D4D4DC; }}
  ::-webkit-scrollbar {{ height: 8px; width: 8px; }}
  ::-webkit-scrollbar-thumb {{ background: #6D5EFF; border-radius: 8px; }}
  ::-webkit-scrollbar-track {{ background: #1E1F30; border-radius: 8px; }}

  /* ── Neon match card ── */
  .bracket-card {{
    background: rgba(38,39,66,0.9);
    border: 1px solid rgba(109,94,255,0.38);
    border-radius: 10px;
    padding: 8px 10px;
    box-shadow: 0 0 14px rgba(109,94,255,0.22), inset 0 1px 0 rgba(255,255,255,0.05);
    transition: all .2s ease;
  }}
  .bracket-card:hover {{
    border-color: rgba(109,94,255,0.75);
    box-shadow: 0 0 22px rgba(109,94,255,0.42), inset 0 1px 0 rgba(255,255,255,0.06);
    transform: translateY(-1px);
  }}

  /* match cell: vertically centers card within its grid span */
  .match-cell {{ position: relative; display: flex; align-items: center; justify-content: center; padding: 0 0.5rem; min-height: 0; overflow: visible; }}
  .match-cell > .bracket-card {{ width: 100%; margin: 0.5rem 0; }}

  /* ── Connector lines (per-cell HTML divs — NO SVG / NO filters, 100% reliable) ── */
  .bracket-heads {{ display: grid; gap: 1.5rem; min-width: max-content; margin-bottom: 0.6rem; overflow: visible; }}
  .bracket-grid {{ display: grid; gap: 1.5rem; min-width: max-content; overflow: visible; --gap-half: 0.75rem; }}
  .col-head {{ display: flex; justify-content: center; }}
  .col-head > div {{ width: auto; }}

  .conn {{ position: absolute; z-index: 2; box-sizing: border-box; }}
  /* right stub: card right edge → next column gap */
  .conn-r {{
    top: 50%; transform: translateY(-50%);
    right: calc(-1 * var(--gap-half));
    width: calc(0.5rem + var(--gap-half));
    height: 3px;
    background: #6D5EFF;
    box-shadow: 0 0 6px rgba(109,94,255,0.85), 0 0 2px rgba(255,255,255,0.25);
  }}
  /* vertical join: links the two feeder card centers in the left gap */
  .conn-v {{
    left: calc(-1 * var(--gap-half));
    top: 25%; bottom: 25%;
    width: 3px;
    background: #6D5EFF;
    box-shadow: 0 0 6px rgba(109,94,255,0.85);
  }}
  /* left stub: gap center → card left edge */
  .conn-l {{
    top: 50%; transform: translateY(-50%);
    left: calc(-1 * var(--gap-half));
    width: calc(0.5rem + var(--gap-half));
    height: 3px;
    background: #6D5EFF;
    box-shadow: 0 0 6px rgba(109,94,255,0.85);
  }}

  /* card sits above connector divs */
  .bracket-card {{ position: relative; z-index: 3; }}

  /* live / gold match highlight */
  .card-live {{ border-color: rgba(109,94,255,0.7); box-shadow: 0 0 0 3px rgba(109,94,255,0.15), 0 0 24px rgba(109,94,255,0.35); }}
  .card-live-gold {{ border-color: rgba(255,193,7,0.6); box-shadow: 0 0 0 3px rgba(255,193,7,0.15), 0 0 28px rgba(255,193,7,0.35); }}

  /* live indicator dot (pulsing ring) */
  .live-dot {{
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 9999px;
    background: #34d399;
    box-shadow: 0 0 0 0 rgba(52,211,153,0.6);
    animation: liveRing 1.6s ease-out infinite;
    margin-right: 4px;
    flex-shrink: 0;
  }}
  @keyframes liveRing {{
    0% {{ box-shadow: 0 0 0 0 rgba(52,211,153,0.6); }}
    70% {{ box-shadow: 0 0 0 6px rgba(52,211,153,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(52,211,153,0); }}
  }}

  /* ── Round header pill ── */
  .round-pill {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 14px; border-radius: 9999px;
    background: #34384C; color: #D4D4DC; border: 1px solid rgba(255,255,255,0.06);
    font-size: 12px; font-weight: 700; letter-spacing: .02em; white-space: nowrap;
  }}
  .round-pill.active {{ background: #6D5EFF; color: #ffffff; border-color: transparent; box-shadow: 0 0 18px rgba(109,94,255,0.55); }}
  .round-pill .dot {{ width: 6px; height: 6px; border-radius: 50%; background: #FFC107; box-shadow: 0 0 8px #FFC107; }}

  /* ── Player row ── */
  .bracket-row {{ display: flex; align-items: center; gap: 8px; font-size: 12px; }}
  .bracket-row + .bracket-row {{ margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(109,94,255,0.16); }}

  /* winner row gold accent (left glow) */
  .bracket-row-winner {{
    background: linear-gradient(90deg, rgba(255,193,7,0.12), transparent 60%);
    border-radius: 6px;
  }}

  .seed {{
    flex-shrink: 0; width: 20px; height: 20px; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 800; line-height: 1;
    background: #1E1F30; border: 1px solid rgba(109,94,255,0.5); color: #6D5EFF;
  }}
  .seed.loss {{ border-color: rgba(212,212,220,0.35); color: #D4D4DC; }}
  .seed.win {{ border-color: #FFC107; color: #FFC107; box-shadow: 0 0 6px rgba(255,193,7,0.4); }}

  .player-name {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #D4D4DC; }}
  .player-name.win {{ color: #ffffff; font-weight: 700; }}
  .player-name.loss {{ color: #8A8FA8; text-decoration: line-through; opacity: .75; }}

  .win-trophy {{ color: #FFC107; font-size: 10px; flex-shrink: 0; margin-left: 2px; }}

  .score-pill {{
    flex-shrink: 0; min-width: 26px; text-align: center;
    font-size: 11px; font-weight: 800; font-variant-numeric: tabular-nums;
    padding: 2px 7px; border-radius: 9999px;
    background: #34384C; color: #D4D4DC; border: 1px solid rgba(255,255,255,0.08);
  }}
  .score-pill.win {{ background: #FFC107; color: #1E1F30; border-color: transparent; box-shadow: 0 0 10px rgba(255,193,7,0.4); }}

  .sched-line {{
    display: flex; align-items: center; justify-content: center; gap: 6px;
    margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(109,94,255,0.16);
    font-size: 10px; color: #8A8FA8;
  }}

  /* ── TBD placeholder (neon) ── */
  .dashed-card {{ border-style: dashed; border-color: rgba(109,94,255,0.45); background: rgba(109,94,255,0.04); }}
  .tbd {{ color: #6D5EFF; text-shadow: 0 0 8px rgba(109,94,255,0.7); font-size: 11px; font-weight: 700; letter-spacing: .08em; }}
  .shimmer-dot {{ background: #6D5EFF; box-shadow: 0 0 6px rgba(109,94,255,0.8); }}

  /* champion sparkle */
  .sparkle {{ animation: sparkle 2s ease-in-out infinite; display: inline-block; }}
  @keyframes sparkle {{
    0%,100% {{ transform: scale(1); filter: drop-shadow(0 0 4px rgba(245,158,11,0.5)); }}
    50% {{ transform: scale(1.12); filter: drop-shadow(0 0 14px rgba(245,200,80,0.9)); }}
  }}
  .champion-banner {{ position: relative; overflow: hidden; }}
  .champion-banner::after {{
    content: '✨';
    position: absolute;
    top: 0.5rem; right: 1rem;
    font-size: 1.25rem;
    opacity: 0.8;
    animation: sparkle 2.4s ease-in-out infinite;
  }}

  /* page load fade-in */
  .fade-in {{ animation: fadeIn .5s ease-out both; }}
  @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: translateY(0); }} }}

  h1, .font-display {{ font-family: 'Outfit','Inter',sans-serif; }}

  @media (max-width: 640px) {{
    .bracket-heads {{ gap: 1rem; }}
    .bracket-grid {{ gap: 1rem; --gap-half: 0.5rem; }}
  }}
</style>
</head>
<body class="min-h-screen text-white font-sans p-4">
<div class="max-w-5xl mx-auto">
  <div class="flex items-center justify-between mb-5 fade-in">
    <a href="{reg_link}" class="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg bg-white/5 hover:bg-white/10 text-gray-300 transition-colors border border-white/10">← Back</a>
    <div class="flex items-center gap-2">
      <a href="{reg_link}" class="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg bg-gradient-to-r from-vibe-600 to-vibe-500 hover:from-vibe-500 hover:to-vibe-400 text-white transition-all shadow-md">🏆 Tournament Page</a>
    </div>
  </div>
  <div class="text-center mb-5 fade-in">
    <img src="/uploads/psvibe-logo-v2.png" alt="PS VIBE" class="w-36 mx-auto mb-3 drop-shadow-lg">
    <h1 class="text-3xl font-extrabold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white via-vibe-300 to-vibe-400">🏆 {tn_name}</h1>
    <div class="flex items-center justify-center flex-wrap gap-2 mt-2 text-xs text-gray-300">
      <span class="px-2 py-0.5 rounded-full bg-white/5 border border-white/10">🎮 {tn_game}</span>
      <span class="px-2 py-0.5 rounded-full bg-white/5 border border-white/10">👥 {player_count} players</span>
      <span class="px-2 py-0.5 rounded-full bg-white/5 border border-white/10">🏆 Single Elimination</span>
      {live_badge}
    </div>
  </div>
  {champion_banner}
  {info_panels}
  <div id="bracketContainer">
  {bracket_body}
  </div>
  <div class="text-center mt-8 space-y-1">
    <p class="text-gray-600 text-xs">PS VIBE Gaming Lounge &middot; Yangon</p>
  </div>
</div>
<script>
// Auto-refresh bracket every 25s (in-place, keep scroll position)
(function() {{
  var TID = {tid};
  setInterval(function() {{
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/tournaments/' + TID + '/bracket?view=html', true);
    xhr.onload = function() {{
      if (xhr.status >= 200 && xhr.status < 400) {{
        var doc = new DOMParser().parseFromString(xhr.responseText, 'text/html');
        var fresh = doc.getElementById('bracketContainer');
        var cur = document.getElementById('bracketContainer');
        if (fresh && cur) {{ cur.innerHTML = fresh.innerHTML; }}
      }} else {{
        location.reload();
      }}
    }};
    xhr.onerror = function() {{ location.reload(); }};
    xhr.send();
  }}, 25000);
}})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


def _render_rules_html(t: dict) -> HTMLResponse:
    """Render a styled standalone Rules page (Tailwind CDN, VIBE dark theme).

    Public, shareable — opens in a new tab via the reg page / dashboard View button.
    Empty rules → friendly empty state (still HTTP 200).
    """
    import html as _html
    tn_name = _html.escape(t.get("name", "Tournament"))
    tn_game = _html.escape(t.get("game", ""))
    reg_token = t.get("reg_token") or ""
    reg_link = f"https://ps-vibe.com/t/{_html.escape(reg_token)}" if reg_token else "#"
    prize = t.get("prize") or ""
    prize_pool = t.get("prize_pool_ks")
    try:
        prize_pool_float = float(prize_pool) if prize_pool is not None else None
    except (ValueError, TypeError):
        prize_pool_float = None
    rules = t.get("rules") or ""
    prizes = _get_prizes(t["id"])

    prize_line = ''
    if prize_pool_float is not None:
        prize_line = f'<p class="text-amber-300 font-semibold text-sm mt-1">💰 ဆုကြေး စုစုပေါင်း (Prize Pool): {prize_pool_float:,.0f} Ks</p>'
        if prize:
            prize_line += f'<p class="text-gray-400 text-xs mt-0.5">ဆုခွဲဝေမှု: {_html.escape(prize)}</p>'
    elif prize:
        prize_line = f'<p class="text-amber-300 font-semibold text-sm mt-1">💰 ဆုကြေး: {_html.escape(prize)}</p>'
    if prizes:
        _pb = ''.join(
            f'<p class="text-gray-300 text-xs mt-0.5">{_place_medal(p["place"])} {_html.escape(p["label"] or "")}: {_fmt_amount(p["amount_ks"])} Ks</p>'
            for p in prizes
        )
        prize_line += f'<div class="mt-2"><p class="text-amber-300 font-semibold text-sm">🏆 ဆုစာရင်း (Prize Breakdown)</p>{_pb}</div>'

    if rules:
        rules_body = f'''<div class="bg-surface-800 rounded-xl border border-vibe-900/50 p-5 shadow-xl shadow-vibe-950/30">
      <h2 class="text-white font-bold text-sm mb-3">📖 စည်းကမ်းများ (Rules)</h2>
      <div class="text-gray-300 text-xs leading-relaxed whitespace-pre-wrap">{_html.escape(rules)}</div>
    </div>'''
    else:
        rules_body = '''<div class="bg-surface-800 rounded-xl border border-vibe-900/50 p-8 text-center shadow-xl shadow-vibe-950/30">
      <div class="text-4xl mb-3">📖</div>
      <p class="text-gray-300 font-semibold">စည်းကမ်းများ မရှိသေးပါ</p>
      <p class="text-gray-500 text-xs mt-2">Rules မကြာမီ ထည့်ပေးပါမယ် — ခဏနေ ပြန်ကြည့်ပါဦး 🙏</p>
    </div>'''

    html = f"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{tn_name} — PS VIBE tournament rules">
<title>{tn_name} — Rules</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={{theme:{{extend:{{colors:{{vibe:{{300:'#9dacfc',400:'#7585f8',500:'#4a55ba',600:'#1D2BA9',700:'#172287',800:'#111a65',950:'#0a0f33'}},surface:{{900:'#0f0f1a',800:'#1a1a2e',700:'#16213e',600:'#111827'}}}}}}}}}}</script>
<style>
  body {{ background: #0f0f1a; }}
  ::-webkit-scrollbar {{ width: 8px; }}
  ::-webkit-scrollbar-thumb {{ background: #1D2BA9; border-radius: 8px; }}
  ::-webkit-scrollbar-track {{ background: #1a1a2e; border-radius: 8px; }}
</style>
</head>
<body class="min-h-screen text-white font-sans p-4">
<div class="max-w-3xl mx-auto">
  <div class="flex items-center justify-between mb-5">
    <a href="{reg_link}" class="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg bg-surface-700 hover:bg-vibe-800 text-gray-300 transition-colors">← Back</a>
    <a href="{reg_link}" class="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg bg-vibe-600 hover:bg-vibe-500 text-white transition-colors">🏆 Tournament Page</a>
  </div>
  <div class="text-center mb-5">
    <img src="/uploads/psvibe-logo-v2.png" alt="PS VIBE" class="w-36 mx-auto mb-3">
    <h1 class="text-2xl font-bold">📖 {tn_name}</h1>
    <p class="text-vibe-400 text-sm mt-1">{tn_game} &middot; စည်းကမ်းများ (Rules)</p>
    {prize_line}
  </div>
  {rules_body}
  <div class="text-center mt-8 space-y-1">
    <p class="text-gray-600 text-xs">PS VIBE Gaming Lounge &middot; Yangon</p>
  </div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)
