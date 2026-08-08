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
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import ok, error_response
from deps import verify_api_key

logger = logging.getLogger(__name__)

# Uploads directory for tournament payment screenshots
_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "tournaments")

router = APIRouter(tags=["Tournaments"])


# ── helpers ──

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
        if fmt not in ("single", "double"):
            fmt = "single"

        t_id = _mysql_exec(
            """INSERT INTO tournaments
               (name, game, description, format, entry_fee_ks, max_players, min_players,
                status, reg_token, reg_open_at, reg_close_at, start_at, prize, rules, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
            result.append(d)

        return ok(result)
    except Exception as e:
        logger.error("list_tournaments error: %s", e)
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
        return ok(t)
    except Exception as e:
        logger.error("get_tournament error: %s", e)
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
            "reg_close_at", "start_at", "prize", "rules",
        ]
        updates = []
        params = []
        for k in allowed:
            if k in body:
                updates.append(f"{k} = %s")
                params.append(body[k])

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

@router.post("/api/tournaments/{tid}/start")
async def start_tournament(tid: int, auth=Depends(verify_api_key)):
    """Start the tournament: seed players, generate round-1 bracket.

    Status must be 'closed' or 'open'. Must have >= min_players confirmed players.
    Players are randomly seeded. If count isn't a power of 2, higher seeds get byes.
    """
    try:
        t = _ensure_tournament(tid)
        if t["status"] not in ("open", "closed"):
            return error_response(
                message=f"Cannot start tournament with status '{t['status']}'. Must be 'open' or 'closed'."
            )

        players = _mysql_query(
            "SELECT * FROM tournament_players WHERE tournament_id=%s AND status IN ('registered','confirmed') ORDER BY id",
            (tid,),
        )
        if len(players) < int(t["min_players"]):
            return error_response(
                message=f"Need at least {t['min_players']} players. Currently have {len(players)}."
            )

        # Random shuffle for seeding
        import random
        shuffled = list(players)
        random.shuffle(shuffled)
        for idx, p in enumerate(shuffled):
            _mysql_exec("UPDATE tournament_players SET seed=%s WHERE id=%s", (idx + 1, p["id"]))

        total_rounds = _calculate_total_rounds(len(shuffled))
        if total_rounds == 0:
            return error_response(message="Need at least 2 players to start")

        matches_in_r1 = 2 ** total_rounds  # next power of 2 >= player_count
        r1_matches = matches_in_r1 // 2

        # Assign players to round-1 matches; higher seeds get byes if needed
        for pos in range(1, r1_matches + 1):
            # Standard seeding: 1 vs last, 2 vs second-last, etc.
            idx_a = pos - 1
            idx_b = matches_in_r1 - pos
            p1_id = shuffled[idx_a]["id"] if idx_a < len(shuffled) else None
            p2_id = shuffled[idx_b]["id"] if idx_b < len(shuffled) and idx_b != idx_a else None

            _mysql_exec(
                """INSERT INTO tournament_matches
                   (tournament_id, round, position, player1_id, player2_id, status)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (tid, 1, pos, p1_id, p2_id, "pending"),
            )

        _mysql_exec(
            "UPDATE tournaments SET status='live', start_at=NOW() WHERE id=%s",
            (tid,),
        )
        logger.info(
            "Tournament %s started: %s players, %s rounds, %s r1 matches",
            tid, len(shuffled), total_rounds, r1_matches,
        )
        return ok({
            "started": True,
            "players": len(shuffled),
            "total_rounds": total_rounds,
            "r1_matches": r1_matches,
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
        _ensure_tournament(tid)
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
            "SELECT id, name, phone, seed, status FROM tournament_players WHERE tournament_id=%s AND status='eliminated'",
            (tid,),
        )
        registered = _mysql_query(
            "SELECT id, name, phone, seed, status, paid FROM tournament_players WHERE tournament_id=%s AND status IN ('registered','confirmed')",
            (tid,),
        )
        return ok({
            "winners": [dict(r) for r in winners],
            "eliminated": [dict(r) for r in eliminated],
            "active": [dict(r) for r in registered],
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
        start_str = str(t.get("start_at", ""))[:16] if t.get("start_at") else ""
        reg_close_str = str(t.get("reg_close_at", ""))[:16] if t.get("reg_close_at") else ""
        description = t.get("description") or ""
        
        # Escape for JS/HTML safety
        import html as _html
        tn_name = _html.escape(t["name"])
        tn_game = _html.escape(t["game"])
        tn_desc = _html.escape(description)
        tn_prize = _html.escape(prize_str)
        tn_format = "Single Elimination" if t.get("format") == "single" else "Double Elimination"
        tn_id = int(t["id"])

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
      
      {f'<div class="text-gray-400">🏅 Prize</div><div class="text-right text-amber-300 font-semibold">{tn_prize}</div>' if prize_str else ''}
      {f'<div class="text-gray-400">📅 Date</div><div class="text-right text-white">{start_str}</div>' if start_str else ''}
    </div>
    {f'<p class="mt-4 pt-3 border-t border-gray-700/50 text-gray-400 text-xs leading-relaxed">{tn_desc}</p>' if description else ''}
  </div>

  <!-- Message area -->
  <div id="msg" class="hidden rounded-lg p-3 mb-4 text-sm font-medium"></div>

  <!-- Registration Form -->
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
    const regBody = {{ name, phone, payment_method: selectedPay || null }};
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
        if not name or not phone:
            return error_response(message="name and phone are required")

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
                   (tournament_id, member_id, name, phone, paid, payment_method, ss_file, paid_at, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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

        return ok({
            "tournament": t_info,
            "player_count": player_count,
            "rounds": rounds,
        })
    except Exception as e:
        logger.error("view_bracket error: %s", e)
        return error_response(message=str(e))


@router.get("/api/tournaments/{tid}/players")
async def list_players_public(tid: int):
    """Public player list with masked phone numbers."""
    try:
        _ensure_tournament(tid)
        players = _mysql_query(
            "SELECT id, name, phone, seed, status, paid, payment_method, ss_file FROM tournament_players WHERE tournament_id=%s ORDER BY seed ASC, id ASC",
            (tid,),
        )
        result = []
        for p in players:
            p = dict(p)
            # Mask phone: show first 3 and last 2 digits
            raw = p.get("phone", "")
            if len(raw) >= 5:
                p["phone"] = raw[:3] + "*" * (len(raw) - 5) + raw[-2:]
            p["ss_url"] = _player_ss_url(p)
            result.append(p)

        count = len(result)
        return ok({"count": count, "players": result})
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


def _render_bracket_html(t: dict, rounds: dict, player_count: int) -> HTMLResponse:
    """Render a styled HTML bracket page (Tailwind CDN)."""
    import html as _html
    tn_name = _html.escape(t.get("name", "Tournament"))
    tn_game = _html.escape(t.get("game", ""))
    
    # Build round columns
    round_cols = []
    for rnum in sorted(rounds.keys()):
        matches = rounds[rnum]
        match_cards = []
        for m in matches:
            p1_n = m["player1"]["name"] or "BYE"
            p2_n = m["player2"]["name"] or "BYE"
            s1 = m.get("score1")
            s2 = m.get("score2")
            winner = m.get("winner")
            status = m.get("status", "pending")
            
            p1_cls = "font-bold text-vibe-400" if winner and winner == p1_n else ""
            p2_cls = "font-bold text-vibe-400" if winner and winner == p2_n else ""
            p1_strike = "line-through text-gray-500" if winner and winner != p1_n and p1_n != "BYE" else ""
            p2_strike = "line-through text-gray-500" if winner and winner != p2_n and p2_n != "BYE" else ""
            
            # Scores
            s1_str = f'<span class="ml-auto text-xs font-mono px-1.5 py-0.5 rounded bg-surface-900 text-gray-300">{s1}</span>' if s1 is not None else ""
            s2_str = f'<span class="ml-auto text-xs font-mono px-1.5 py-0.5 rounded bg-surface-900 text-gray-300">{s2}</span>' if s2 is not None else ""
            
            match_cards.append(f'''
            <div class="bg-surface-600 rounded-lg p-2.5 border {'border-vibe-600/40' if status == 'complete' else 'border-gray-700'} {'opacity-60' if status == 'complete' else ''} mb-2">
              <div class="flex items-center gap-1.5 text-xs {'text-green-400' if winner and winner == p1_n else 'text-gray-300'}">
                <span class="{p1_cls} {p1_strike}">{_html.escape(p1_n)}</span>{s1_str}
              </div>
              <div class="flex items-center gap-1.5 text-xs {'text-red-400' if (winner and winner != p2_n and p2_n != 'BYE') else 'text-gray-300'} mt-0.5">
                <span class="{p2_cls} {p2_strike}">{_html.escape(p2_n)}</span>{s2_str}
              </div>
            </div>''')
        
        round_cols.append(f'''
        <div class="flex-shrink-0 w-40">
          <div class="text-center text-xs font-semibold text-vibe-400 mb-2 py-1 bg-surface-700 rounded">{'🏆 Final' if rnum == len(rounds) else f'Semi' if rnum == len(rounds)-1 else f'Round {rnum}'}</div>
          {"".join(match_cards)}
        </div>''')
    
    html = f"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{tn_name} — Bracket</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={{theme:{{extend:{{colors:{{vibe:{{500:'#8b5cf6',600:'#7c3aed'}},surface:{{900:'#0f0f1a',800:'#1a1a2e',700:'#16213e',600:'#111827'}}}}}}}}}}</script>
</head>
<body class="bg-surface-900 min-h-screen text-white font-sans p-4">
<div class="max-w-4xl mx-auto">
  <div class="text-center mb-6">
    <h1 class="text-2xl font-bold">🏆 {tn_name}</h1>
    <p class="text-vibe-400 text-sm">{tn_game} &middot; {player_count} players</p>
  </div>
  <div class="flex gap-4 overflow-x-auto pb-4">
    {"".join(round_cols)}
  </div>
  <p class="text-center text-gray-600 text-xs mt-8">PS VIBE Gaming Lounge</p>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)
