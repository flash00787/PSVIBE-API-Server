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
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import ok, error_response
from deps import verify_api_key

logger = logging.getLogger(__name__)

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

        html = f"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t['name']} — PS VIBE Tournament</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, 'Noto Sans Myanmar', sans-serif; background: #0f0f1a; color: #e0e0f0; min-height: 100vh; padding: 16px; }}
.card {{ max-width: 480px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 24px; box-shadow: 0 4px 24px rgba(0,0,0,0.4); }}
h1 {{ font-size: 1.5rem; color: #7c3aed; margin-bottom: 4px; }}
.game {{ font-size: 0.9rem; color: #a78bfa; margin-bottom: 16px; }}
.info {{ background: #16213e; border-radius: 10px; padding: 12px 16px; margin-bottom: 16px; }}
.info-row {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 0.9rem; }}
.info-row span:first-child {{ color: #9ca3af; }}
.info-row span:last-child {{ font-weight: 600; }}
.spots {{ color: #f59e0b; }}
.fulfilled {{ color: #ef4444; }}
input, button {{ width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #374151; background: #111827; color: #e0e0f0; font-size: 1rem; margin-bottom: 12px; font-family: inherit; }}
input:focus {{ outline: none; border-color: #7c3aed; }}
button {{ background: #7c3aed; border: none; color: #fff; font-weight: 700; font-size: 1.1rem; cursor: pointer; }}
button:disabled {{ background: #4b5563; cursor: not-allowed; }}
.msg {{ text-align: center; padding: 8px; font-size: 0.85rem; border-radius: 6px; margin-bottom: 12px; display: none; }}
.msg.success {{ background: #065f46; color: #6ee7b7; display: block; }}
.msg.error {{ background: #7f1d1d; color: #fca5a5; display: block; }}
.desc {{ font-size: 0.85rem; color: #9ca3af; line-height: 1.5; margin-bottom: 16px; }}
.footer {{ text-align: center; font-size: 0.75rem; color: #6b7280; margin-top: 16px; }}
</style>
</head>
<body>
<div class="card">
<h1>🏆 {t['name']}</h1>
<div class="game">🎮 {t['game']}</div>
{ f'<div class="desc">{t["description"]}</div>' if t.get('description') else '' }
<div class="info">
<div class="info-row"><span>Entry Fee</span><span>{fee if fee > 0 else 'အခမဲ့ (Free)'} Ks</span></div>
<div class="info-row"><span>Spots</span><span class="{'fulfilled' if spots_left == 0 else 'spots'}">{spots_left} left / {t['max_players']} max</span></div>
<div class="info-row"><span>Format</span><span>{'Single Elimination' if t['format'] == 'single' else 'Double Elimination'}</span></div>
{ f'<div class="info-row"><span>Prize</span><span>🏅 {t["prize"]}</span></div>' if t.get('prize') else '' }
</div>
<div id="msg" class="msg"></div>
<form id="regForm" onsubmit="return registerPlayer(event)">
<input type="text" id="name" placeholder="အမည် (Name)" required>
<input type="tel" id="phone" placeholder="ဖုန်းနံပါတ် (Phone)" required>
{f'<p style="font-size:0.8rem;color:#9ca3af;margin-bottom:12px">💰 Entry fee {fee} Ks — Pay at counter or use member wallet</p>' if fee > 0 else ''}
<button type="submit" id="submitBtn">Register Now</button>
</form>
<div class="footer">PS VIBE Gaming Lounge © 2026</div>
</div>
<script>
async function registerPlayer(e) {{
e.preventDefault();
const btn = document.getElementById('submitBtn');
const msg = document.getElementById('msg');
btn.disabled = true;
btn.textContent = 'Registering...';
msg.className = 'msg';
try {{
const resp = await fetch('/api/tournaments/{t["id"]}/register', {{
method: 'POST',
headers: {{'Content-Type': 'application/json'}},
body: JSON.stringify({{
name: document.getElementById('name').value.trim(),
phone: document.getElementById('phone').value.trim()
}})
}});
const data = await resp.json();
if (data.success) {{
msg.className = 'msg success';
msg.textContent = '✅ Registered successfully!';
document.getElementById('regForm').reset();
}} else {{
msg.className = 'msg error';
msg.textContent = '❌ ' + (data.error || 'Registration failed');
}}
}} catch (err) {{
msg.className = 'msg error';
msg.textContent = '❌ Network error. Please try again.';
}} finally {{
btn.disabled = false;
btn.textContent = 'Register Now';
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

    - If entry_fee_ks > 0 AND member_id given: checks wallet balance, deducts,
      inserts cash_movements 'eject' row, marks paid=1.
    - If entry_fee_ks > 0 and NO member_id: paid=0 (pay at counter).
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
        paid = 0
        payment_method = None

        if fee > 0 and member_id:
            # Verify member exists and has sufficient balance
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
            # Uses existing enum: 'eject' is the only compatible type for deductions
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

        elif fee == 0:
            paid = 1
            payment_method = "free"

        # Insert player — handle duplicate phone gracefully
        try:
            _mysql_exec(
                """INSERT INTO tournament_players
                   (tournament_id, member_id, name, phone, paid, payment_method, paid_at, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tid,
                    member_id,
                    name,
                    phone,
                    paid,
                    payment_method,
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

        logger.info("Player %s registered for tournament %s (paid=%s)", name, tid, paid)
        return ok({"registered": True, "player_name": name, "paid": bool(paid)})
    except Exception as e:
        logger.error("register_player error: %s", e)
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  Public — Bracket / Players View
# ═══════════════════════════════════════

@router.get("/api/tournaments/{tid}/bracket")
async def view_bracket(tid: int):
    """Public bracket view: tournament info + all matches grouped by round."""
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
            "SELECT id, name, phone, seed, status, paid FROM tournament_players WHERE tournament_id=%s ORDER BY seed ASC, id ASC",
            (tid,),
        )
        result = []
        for p in players:
            p = dict(p)
            # Mask phone: show first 3 and last 2 digits
            raw = p.get("phone", "")
            if len(raw) >= 5:
                p["phone"] = raw[:3] + "*" * (len(raw) - 5) + raw[-2:]
            result.append(p)

        count = len(result)
        return ok({"count": count, "players": result})
    except Exception as e:
        logger.error("list_players_public error: %s", e)
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
