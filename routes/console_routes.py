"""PS VIBE API — Console & Settings Routes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
import mysql.connector as _mc
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import _mysql_get_setting, ok, error_response, insert_cash_movement_if_absent
from field_utils import normalize_list
from deps import verify_api_key
from models import GenericResponse
from session_timer import schedule_session_timer, _active_timers
from booking_reminder import add_booking_reminder
import json
from console_status import sync_console_status as _sync_console_status
import logging, os, json
from datetime import datetime, timezone, timedelta
import asyncio

# ── Session Reminder Bridge (API → Bot) ─────────────────────────────────────
_BOT_REMINDER_FILE = "/root/psvibe-sales-bot/data/session_reminders.json"

def _persist_session_for_bot(console_id: str, member_id: str, duration_mins: int,
                               start_dt, chat_id: int = 0, message_thread_id: int = 0):
    """Write session info to the bot's session_reminders.json so the bot can
    pick up API-started sessions and spawn _remind_loop tasks."""
    if not duration_mins or duration_mins <= 0 or not console_id:
        return
    try:
        # Use bot's target chat as default (STAFF_NOTIFY_CHAT)
        if not chat_id:
            # Try to read from env — fall back to 0 (bot will use default)
            chat_id = 0
        end_dt = start_dt + timedelta(minutes=duration_mins) if start_dt else datetime.now(timezone(timedelta(hours=6, minutes=30))) + timedelta(minutes=duration_mins)
        end_t = end_dt.strftime("%H:%M")
        end_dt_iso = end_dt.isoformat()
        key = f"{console_id}|{abs(chat_id)}"
        
        store = {}
        if os.path.isfile(_BOT_REMINDER_FILE):
            try:
                with open(_BOT_REMINDER_FILE, "r") as f:
                    store = json.load(f)
                if not isinstance(store, dict):
                    store = {}
            except (json.JSONDecodeError, OSError):
                store = {}
        
        store[key] = {
            "cid": console_id,
            "chat_id": chat_id,
            "member_id": member_id or "Guest",
            "planned_mins": duration_mins,
            "end_t": end_t,
            "end_dt_iso": end_dt_iso,
            "message_thread_id": message_thread_id,
            "total_plan_mins": duration_mins,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        tmp = _BOT_REMINDER_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, _BOT_REMINDER_FILE)
        logger.info(f"session_reminder_bridge: wrote {key} (dur={duration_mins}min)")
    except Exception as e:
        logger.warning(f"session_reminder_bridge: write failed for {console_id}: {e}")


def now_mmt():
    """Return current datetime in MMT (UTC+6:30)"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=6, minutes=30)))
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Consoles'])

# ═══════════════════════════════════════
@router.get("/fetch_console_status", response_model=GenericResponse, tags=["Consoles"], summary="Fetch console statuses [MySQL]")
async def api_fetch_console_status(auth=Depends(verify_api_key)) -> JSONResponse:
    """Fetch live console statuses from MySQL console_status table."""
    try:
        rows = _mysql_query(
            "SELECT cs.console_id, cs.status, cs.console_type, cs.current_game, cs.current_member, DATE_FORMAT(cs.start_time, '%%H:%%i') as start_time, DATE_FORMAT(cs.start_time, '%%Y-%%m-%%d %%H:%%i:%%s') as start_time_dt, cb.id as booking_id, cb.staff_name, cb.customer_name, COALESCE(cb.actual_duration_mins, cobk.planned_duration_mins) as duration_mins FROM console_status cs LEFT JOIN (    SELECT cbsn1.booking_id as id, cbsn1.console_id, cbsn1.staff_name, cbsn1.customer_name, cbsn1.actual_duration_mins     FROM console_sessions cbsn1     INNER JOIN (        SELECT console_id, MAX(id) as max_id         FROM console_sessions WHERE status = 'active' GROUP BY console_id    ) cbsn2 ON cbsn1.id = cbsn2.max_id) cb ON REPLACE(cb.console_id, ' ', '') LIKE CONCAT(REPLACE(cs.console_id, ' ', ''), '%%') LEFT JOIN console_bookings cobk ON cobk.id=cb.id ORDER BY cs.console_id")
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
                "SELECT cb.console_id, cb.planned_start AS start_time, cb.staff_name, cb.customer_name, cb.planned_duration_mins AS duration_mins FROM console_bookings cb "
                "WHERE cb.status IN ('confirmed', 'pending_check_in') "
                "AND cb.planned_start <= %s AND cb.planned_end > %s "
                "AND cb.id = (SELECT MAX(cb2.id) FROM console_bookings cb2 WHERE cb2.console_id = cb.console_id AND cb2.status IN ('confirmed', 'pending_check_in') AND cb2.planned_start <= %s AND cb2.planned_end > %s)",
                (_window_end, _now_ref, _window_end, _now_ref)
            )
            reserved_ids = {r["console_id"].strip() for r in upcoming}
            reserved_info = {}
            for u in upcoming:
                cid = u["console_id"].strip()
                st = u.get("start_time")
                reserved_info[cid] = {
                    "reserved_for": u.get("customer_name") or u.get("staff_name") or "",
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

# ═══════════════════════════════════════
@router.get("/fetch_base_rate", response_model=GenericResponse, tags=["Settings"], summary="Fetch hourly base rate [MySQL]")
async def api_fetch_base_rate(auth=Depends(verify_api_key)):
    """Fetch hourly base rate from MySQL settings_config."""
    try:
        return ok(_mysql_get_setting("base_rate", 0))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_console_multiplier
# ═══════════════════════════════════════
@router.get("/fetch_console_multiplier/{console_id}", response_model=GenericResponse, tags=["Settings"], summary="Fetch console multiplier [MySQL]")
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
@router.get("/fetch_new_member_defaults", response_model=GenericResponse, tags=["Settings"], summary="Fetch new member defaults [MySQL]")
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
@router.get("/fetch_rank_thresholds", response_model=GenericResponse, tags=["Settings"], summary="Fetch rank thresholds [MySQL]")
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
@router.get("/fetch_bonus_table", response_model=GenericResponse, tags=["Settings"], summary="Fetch bonus table [MySQL]")
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
@router.get("/fetch_rank_table_display", response_model=GenericResponse, tags=["Settings"], summary="Fetch rank display table [MySQL]")
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



# ═══════════════════════════════════════



@router.post("/consoles/start-session", response_model=GenericResponse, tags=["Consoles"], summary="Start session, auto-checkin confirmed booking [MySQL]")
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
            "SELECT id, member_id, game_name, planned_duration_mins AS duration_mins, telegram_chat_id, staff_name, customer_name, phone FROM console_bookings "
            "WHERE console_id=%s AND status IN ('confirmed','checked_in') AND DATE(booking_date)=%s "
            "AND planned_start <= DATE_ADD(%s, INTERVAL 30 MINUTE) "
            "AND planned_end >= %s "
            "ORDER BY created_at DESC LIMIT 1",
            (console_id, today, _now_mmt, _now_mmt)
        )

        # CI-3: Wrap conflict check + console_status update in transaction with row locking
        _now_ref = now_mmt()
        # No Timer (duration=0): hold console with 6hr window to prevent overbooking
        _hold_window = 360 if duration_mins == 0 else duration_mins
        booking_id = None
        _start_conn = _mc.connect(host=os.environ.get("MYSQL_HOST","localhost"),user=os.environ.get("MYSQL_USER","root"),password=os.environ.get("MYSQL_PASSWORD","") or "PsVibe@User2024!",database=os.environ.get("MYSQL_DATABASE","psvibe"),port=int(os.environ.get("MYSQL_PORT","3306")),charset="utf8mb4")
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
                    "SELECT id, status FROM console_bookings WHERE console_id=%s"
                    " AND id != %s"
                    " AND (status IN ('confirmed','pending_check_in','checked_in','Active') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != ''))))"
                    " AND planned_start < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND planned_end > DATE_ADD(%s, INTERVAL 30 MINUTE)"
                    " LIMIT 1 FOR UPDATE",
                    (console_id, bk["id"], _now_ref, _hold_window, _now_ref)
                )
            else:
                _start_cur.execute(
                    "SELECT id, status FROM console_bookings WHERE console_id=%s"
                    " AND (status IN ('confirmed','pending_check_in','checked_in','Active') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != ''))))"
                    " AND planned_start < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND planned_end > DATE_ADD(%s, INTERVAL 30 MINUTE)"
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
                # If staff_name provided, use it (overrides customer name from booking's staff_name)
                if req.get("staff_name", "") or req.get("staff", ""):
                    _start_staff = req.get("staff_name", "") or req.get("staff", "")
                    _start_cur.execute("UPDATE console_sessions SET status='active', actual_duration_mins=%s, actual_start=%s, actual_end=DATE_ADD(%s, INTERVAL %s MINUTE), staff_name=%s, customer_name=COALESCE(NULLIF(customer_name,''), staff_name) WHERE booking_id=%s AND status='checked_in'", (duration_mins, _now_ref, _now_ref, _dur_end2, _start_staff, booking_id))
                else:
                    _start_cur.execute("UPDATE console_sessions SET status='active', actual_duration_mins=%s, actual_start=%s, actual_end=DATE_ADD(%s, INTERVAL %s MINUTE), customer_name=COALESCE(NULLIF(customer_name,''), staff_name) WHERE booking_id=%s AND status='checked_in'", (duration_mins, _now_ref, _now_ref, _dur_end2, booking_id))
                logger.info(f"Auto-checkin booking #{booking_id} for console {console_id}")
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
                    try: _persist_session_for_bot(console_id, member_id, duration_mins, _now_ref)
                    except Exception: pass
            else:
                _dur_end5 = 360 if duration_mins == 0 else duration_mins
                # Walk-in: create booking row in console_bookings
                _start_cur.execute(
                    "INSERT INTO console_bookings (console_id, member_id, status, booking_date, planned_start, planned_duration_mins, planned_end, game_name, staff_name, customer_name) VALUES (%s,%s,'active',%s,%s,%s,DATE_ADD(%s, INTERVAL %s MINUTE),%s,%s,%s)",
                    (console_id, member_id, today, _now_ref, duration_mins, _now_ref, _dur_end5, game_name, member_id, member_id)
                )
                booking_id = _start_cur.lastrowid
                # Walk-in: create session row in console_sessions
                _start_cur.execute(
                    "INSERT INTO console_sessions (booking_id, console_id, member_id, status, actual_start, actual_duration_mins, actual_end, game_name, staff_name, customer_name, created_at) VALUES (%s,%s,%s,'active',%s,%s,DATE_ADD(%s, INTERVAL %s MINUTE),%s,%s,%s,NOW())",
                    (booking_id, console_id, member_id, _now_ref, duration_mins, _now_ref, _dur_end5, game_name, member_id, member_id)
                )
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
                    try: _persist_session_for_bot(console_id, member_id, duration_mins, _now_ref)
                    except Exception: pass

            _start_cur.execute(
                "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                (member_id, game_name, _now_ref, console_id)
            )
            _start_conn.commit()

            # Restore deposit if previously forfeited (auto-cancel then reactivate) — 🐛 2026-07-28
            if bk:
                _dep_row = _mysql_query_one(
                    "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
                    (booking_id,)
                )
                if _dep_row and _dep_row.get('deposit_status') == 'forfeited' and int(_dep_row.get('deposit_amount', 0) or 0) > 0:
                    _mysql_exec(
                        "UPDATE console_bookings SET deposit_status='verified', cancelled_at=NULL WHERE id=%s AND deposit_status='forfeited' AND deposit_amount > 0",
                        (booking_id,)
                    )
                    # Reverse forfeit cash_movement — insert eject entry
                    _dep_amt = int(_dep_row.get('deposit_amount', 0) or 0)
                    _dep_meth = (_dep_row.get('deposit_method', '') or 'kpay').lower()
                    _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay"}.get(_dep_meth, "KPay")
                    if _dep_amt > 0:
                        insert_cash_movement_if_absent(
                            "eject", _meth_label, _dep_amt,
                            f"Refund forfeit BK#{booking_id}%",
                            f"Refund forfeit BK#{booking_id} (deposit reverse - reactivated) via {_meth_label}", "system")
                        logger.info(f"Console start BK#{booking_id}: deposit forfeit {_dep_amt}Ks reversed (eject)")

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
            except Exception:
                pass  # non-critical: connection close

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


@router.post("/sessions/start", response_model=GenericResponse, tags=["Sessions"], summary="Start a walk-in session (staff only) [MySQL]")
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
        req_staff_name = req.get("staff_name", "") or req.get("staff", "")
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
                "SELECT id, console_id, branch_id, member_id, booking_date, "
                "planned_start AS start_time, planned_end AS end_time, "
                "planned_duration_mins AS duration_mins, "
                "status, staff_name, notes, telegram_chat_id, phone, "
                "game_name, admin_notify_msg_id, created_at, cancelled_at "
                "FROM console_bookings WHERE id=%s "
                "AND EXISTS (SELECT 1 FROM console_sessions WHERE booking_id=%s AND status='checked_in')",
                (linked_booking_id, linked_booking_id)
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
        conn = _mc.connect(host=os.environ.get("MYSQL_HOST","localhost"),user=os.environ.get("MYSQL_USER","root"),password=os.environ.get("MYSQL_PASSWORD","") or "PsVibe@User2024!",database=os.environ.get("MYSQL_DATABASE","psvibe"),port=int(os.environ.get("MYSQL_PORT","3306")),charset="utf8mb4")
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
                "SELECT id FROM console_bookings WHERE console_id=%s AND status='Active' AND (planned_end IS NULL OR planned_end > %s) LIMIT 1 FOR UPDATE",
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
                    "SELECT id, status FROM console_bookings WHERE console_id=%s"
                    " AND id != %s"
                    " AND (status IN ('confirmed','pending_check_in','checked_in') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != ''))))"
                    " AND planned_start < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND planned_end > DATE_ADD(%s, INTERVAL 30 MINUTE)"
                    " LIMIT 1 FOR UPDATE",
                    (console_id, linked_booking_id, _now_ref, _conflict_dur, _now_ref)
                )
            else:
                cur.execute(
                    "SELECT id, status FROM console_bookings WHERE console_id=%s"
                    " AND (status IN ('confirmed','pending_check_in','checked_in') OR (status = 'pending' AND (deposit_status = 'paid' OR (member_id IS NOT NULL AND member_id != ''))))"
                    " AND planned_start < DATE_SUB(DATE_ADD(%s, INTERVAL %s MINUTE), INTERVAL 30 MINUTE)"
                    " AND planned_end > DATE_ADD(%s, INTERVAL 30 MINUTE)"
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
                # Update console_bookings with session info
                cur.execute(
                    "UPDATE console_bookings SET status='Active', planned_start=%s, planned_end=DATE_ADD(%s, INTERVAL %s MINUTE), planned_duration_mins=%s, game_name=COALESCE(NULLIF(%s, ''), game_name), console_id=%s, telegram_chat_id=COALESCE(NULLIF(%s, ''), telegram_chat_id) WHERE id=%s",
                    (_now_ref, _now_ref, _dur_end3, duration_mins, game_name, console_id, telegram_chat_id, linked_booking_id)
                )
                # Update console_sessions to 'active'
                # If staff_name provided, use it (overrides customer name from booking)
                if req_staff_name:
                    cur.execute(
                        "UPDATE console_sessions SET status='active', actual_start=%s, actual_end=DATE_ADD(%s, INTERVAL %s MINUTE), actual_duration_mins=%s, game_name=COALESCE(NULLIF(%s, ''), game_name), staff_name=%s, customer_name=COALESCE(NULLIF(customer_name,''), staff_name), console_id=%s WHERE booking_id=%s AND status='checked_in'",
                        (_now_ref, _now_ref, _dur_end3, duration_mins, game_name, req_staff_name, console_id, linked_booking_id)
                    )
                else:
                    cur.execute(
                        "UPDATE console_sessions SET status='active', actual_start=%s, actual_end=DATE_ADD(%s, INTERVAL %s MINUTE), actual_duration_mins=%s, game_name=COALESCE(NULLIF(%s, ''), game_name), customer_name=COALESCE(NULLIF(customer_name,''), staff_name), console_id=%s WHERE booking_id=%s AND status='checked_in'",
                        (_now_ref, _now_ref, _dur_end3, duration_mins, game_name, console_id, linked_booking_id)
                    )
                booking_id = linked_booking_id
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
                    try: _persist_session_for_bot(console_id, member_id, duration_mins, _now_ref)
                    except Exception: pass
            else:
                # Create booking + session rows for walk-in
                _dur_end4 = 360 if duration_mins == 0 else duration_mins
                cur.execute(
                    "INSERT INTO console_bookings (console_id, member_id, status, booking_date, planned_start, planned_end, planned_duration_mins, game_name, staff_name, customer_name) VALUES (%s, %s, 'Active', %s, %s, DATE_ADD(%s, INTERVAL %s MINUTE), %s, %s, %s, %s)",
                    (console_id, member_id, booking_date, _now_ref, _now_ref, _dur_end4, duration_mins, game_name, member_id, member_id)
                )
                booking_id = cur.lastrowid
                # Use req_staff_name if provided (actual staff), else member_name defaults
                _ses_staff = req_staff_name if req_staff_name else member_name
                cur.execute(
                    "INSERT INTO console_sessions (booking_id, console_id, member_id, status, actual_start, actual_end, actual_duration_mins, game_name, staff_name, customer_name, created_at) VALUES (%s, %s, %s, 'active', %s, DATE_ADD(%s, INTERVAL %s MINUTE), %s, %s, %s, %s, NOW())",
                    (booking_id, console_id, member_id, _now_ref, _now_ref, _dur_end4, duration_mins, game_name, _ses_staff, member_name)
                )
                if duration_mins and duration_mins > 0:
                    try: schedule_session_timer(booking_id, console_id, member_id, _now_ref, duration_mins)
                    except Exception: pass
                    try: _persist_session_for_bot(console_id, member_id, duration_mins, _now_ref)
                    except Exception: pass
            
            # Sync console_status
            cur.execute(
                "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                (member_id, game_name, _now_ref, console_id)
            )
            
            conn.commit()

            # Restore deposit if previously forfeited (auto-cancel then reactivate) — 🐛 2026-07-28
            if linked_booking:
                _dep_row = _mysql_query_one(
                    "SELECT deposit_status, deposit_amount, deposit_method FROM console_bookings WHERE id=%s",
                    (linked_booking_id,)
                )
                if _dep_row and _dep_row.get('deposit_status') == 'forfeited' and int(_dep_row.get('deposit_amount', 0) or 0) > 0:
                    _mysql_exec(
                        "UPDATE console_bookings SET deposit_status='verified', cancelled_at=NULL WHERE id=%s AND deposit_status='forfeited' AND deposit_amount > 0",
                        (linked_booking_id,)
                    )
                    # Reverse forfeit cash_movement — insert eject entry
                    _dep_amt = int(_dep_row.get('deposit_amount', 0) or 0)
                    _dep_meth = (_dep_row.get('deposit_method', '') or 'kpay').lower()
                    _meth_label = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay"}.get(_dep_meth, "KPay")
                    if _dep_amt > 0:
                        insert_cash_movement_if_absent(
                            "eject", _meth_label, _dep_amt,
                            f"Refund forfeit BK#{linked_booking_id}%",
                            f"Refund forfeit BK#{linked_booking_id} (deposit reverse - reactivated) via {_meth_label}", "system")
                        logger.info(f"Session start BK#{linked_booking_id}: deposit forfeit {_dep_amt}Ks reversed (eject)")

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
            except Exception:
                pass  # non-critical: connection close
        
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
@router.post("/sessions/move", response_model=GenericResponse, tags=["Sessions"], summary="Move a booking (Active/confirmed/checked_in) to another console [MySQL]")
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
        _conn = _mc.connect(host=os.environ.get("MYSQL_HOST","localhost"),user=os.environ.get("MYSQL_USER","root"),password=os.environ.get("MYSQL_PASSWORD","") or "PsVibe@User2024!",database=os.environ.get("MYSQL_DATABASE","psvibe"),port=int(os.environ.get("MYSQL_PORT","3306")),charset="utf8mb4")
        _conn.start_transaction()
        _cur = _conn.cursor(dictionary=True)
        try:
            # C1: Lock source booking row (Active, confirmed, or checked_in)
            _cur.execute(
                "SELECT id, console_id, member_id, planned_start AS start_time, planned_duration_mins AS duration_mins, game_name, status FROM console_bookings WHERE id=%s AND status IN ('Active','confirmed','checked_in') FOR UPDATE",
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
                    "UPDATE console_bookings SET console_id=%s WHERE id=%s",
                    (to_console_id, booking_id)
                )
                # Also update console_sessions
                _cur.execute(
                    "UPDATE console_sessions SET console_id=%s WHERE booking_id=%s AND status IN ('active','checked_in')",
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
                    "UPDATE console_bookings SET console_id=%s WHERE id=%s",
                    (to_console_id, booking_id)
                )
            
            _conn.commit()
            
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
            except Exception: pass  # non-critical: connection close
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"sessions/move error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/swap", response_model=GenericResponse, tags=["Sessions"], summary="Swap two Active bookings between consoles")
async def api_sessions_swap(req: dict, auth=Depends(verify_api_key)):
    """Swap two Active sessions between consoles.
    
    Request: { "booking_id_1": int, "booking_id_2": int }
    
    Both bookings must be Active. Swaps console_id, console_status, and timers.
    """
    try:
        _bid1 = req.get("booking_id_1")
        _bid2 = req.get("booking_id_2")
        
        if not _bid1 or not _bid2:
            return error_response(message="booking_id_1 and booking_id_2 are required")
        if _bid1 == _bid2:
            return error_response(message="Cannot swap the same booking")
        
        _conn = _mc.connect(host=os.environ.get("MYSQL_HOST","localhost"),user=os.environ.get("MYSQL_USER","root"),password=os.environ.get("MYSQL_PASSWORD","") or "PsVibe@User2024!",database=os.environ.get("MYSQL_DATABASE","psvibe"),port=int(os.environ.get("MYSQL_PORT","3306")),charset="utf8mb4")
        _conn.start_transaction()
        _cur = _conn.cursor(dictionary=True)
        try:
            # Lock both bookings
            _cur.execute(
                "SELECT id, console_id, member_id, planned_start AS start_time, planned_duration_mins AS duration_mins, game_name, status FROM console_bookings WHERE id=%s AND status='Active' FOR UPDATE",
                (_bid1,)
            )
            _bk1 = _cur.fetchone()
            if not _bk1:
                _conn.rollback()
                return error_response(message=f"Booking {_bid1} not found or not Active")
            
            _cur.execute(
                "SELECT id, console_id, member_id, planned_start AS start_time, planned_duration_mins AS duration_mins, game_name, status FROM console_bookings WHERE id=%s AND status='Active' FOR UPDATE",
                (_bid2,)
            )
            _bk2 = _cur.fetchone()
            if not _bk2:
                _conn.rollback()
                return error_response(message=f"Booking {_bid2} not found or not Active")
            
            _cid1 = _bk1["console_id"]
            _cid2 = _bk2["console_id"]
            
            if _cid1 == _cid2:
                _conn.rollback()
                return error_response(message="Both bookings are on the same console — nothing to swap")
            
            # Lock both console_status rows
            _cur.execute("SELECT status, current_member, current_game, start_time FROM console_status WHERE console_id=%s FOR UPDATE", (_cid1,))
            _cs1 = _cur.fetchone()
            _cur.execute("SELECT status, current_member, current_game, start_time FROM console_status WHERE console_id=%s FOR UPDATE", (_cid2,))
            _cs2 = _cur.fetchone()
            
            # ── Execute swap ──
            # 1. Swap console_id on bookings
            _cur.execute("UPDATE console_bookings SET console_id=%s WHERE id=%s", (_cid2, _bid1))
            _cur.execute("UPDATE console_bookings SET console_id=%s WHERE id=%s", (_cid1, _bid2))
            # Also swap in console_sessions
            _cur.execute("UPDATE console_sessions SET console_id=%s WHERE booking_id=%s AND status='active'", (_cid2, _bid1))
            _cur.execute("UPDATE console_sessions SET console_id=%s WHERE booking_id=%s AND status='active'", (_cid1, _bid2))
            
            # 2. Swap console_status
            #   Use a temp sentinel console_id to avoid unique constraint collision
            _cur.execute("UPDATE console_status SET console_id='__SWAP_TMP__' WHERE console_id=%s", (_cid1,))
            _cur.execute("UPDATE console_status SET console_id=%s WHERE console_id=%s", (_cid1, _cid2))
            _cur.execute("UPDATE console_status SET console_id=%s WHERE console_id='__SWAP_TMP__'", (_cid2,))
            
            # 3. Refresh console_status with correct booking data
            _cur.execute(
                "UPDATE console_status cs SET cs.current_member=(SELECT COALESCE(cb.member_id,'') FROM console_bookings cb WHERE cb.console_id=cs.console_id AND cb.status='Active' LIMIT 1), cs.current_game=(SELECT COALESCE(cb.game_name,'') FROM console_bookings cb WHERE cb.console_id=cs.console_id AND cb.status='Active' LIMIT 1), cs.start_time=(SELECT cb.planned_start FROM console_bookings cb WHERE cb.console_id=cs.console_id AND cb.status='Active' LIMIT 1) WHERE cs.console_id IN (%s,%s)",
                (_cid1, _cid2)
            )
            
            # 4. Swap session timers
            try:
                _old1 = _active_timers.pop(_bid1, None)
                _old2 = _active_timers.pop(_bid2, None)
                if _old1:
                    _old1["task"].cancel()
                if _old2:
                    _old2["task"].cancel()
                
                # Normalize start_time (MySQL returns naive)
                for _b, _cid in [(_bk1, _cid2), (_bk2, _cid1)]:
                    if _b["duration_mins"] and _b["duration_mins"] > 0:
                        _st = _b["start_time"]
                        if hasattr(_st, 'tzinfo') and _st.tzinfo is None:
                            _st = _st.replace(tzinfo=timezone(timedelta(hours=6, minutes=30)))
                        schedule_session_timer(_b["id"], _cid, _b["member_id"], _st, _b["duration_mins"])
            except Exception:
                pass  # timer failure is non-critical
            
            _conn.commit()
            
            logger.info(f"Sessions swapped: BK#{_bid1} ({_cid1}→{_cid2}) ↔ BK#{_bid2} ({_cid2}→{_cid1})")
            return ok({
                "swapped": True,
                "booking_1": {"id": _bid1, "from_console": _cid1, "to_console": _cid2},
                "booking_2": {"id": _bid2, "from_console": _cid2, "to_console": _cid1}
            })
        except Exception:
            _conn.rollback()
            raise
        finally:
            try: _conn.close()
            except Exception: pass  # non-critical: connection close
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"sessions/swap error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


        @router.get("/assets/FoodMenuRegister-{rest:path}", include_in_schema=False)
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
        @router.get("/favicon.svg")
        async def dashboard_favicon():
            return FileResponse(os.path.join(dashboard_dir, "favicon.svg"))
        
        # Serve Vue SPA at root - hash routing handles the rest client-side
        @router.get("/")
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

            @router.get("/currency-exchange")
            async def serve_sel_dashboard():
                idx = os.path.join(_sel_dash_dir, "index.html")
                if os.path.isfile(idx):
                    resp = FileResponse(idx, media_type="text/html")
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    resp.headers["Pragma"] = "no-cache"
                    return resp
                return HTMLResponse("<h1>SEL Dashboard not found</h1>", status_code=404)

            @router.get("/currency-exchange/login")
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
        @router.get("/{full_path:path}")
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

# ═══════════════════════════════════════
#  CONSOLE SETTINGS — add/remove/update console multipliers
# ═══════════════════════════════════════
@router.post("/add_console_to_setting", tags=["Settings"], summary="Add console to settings [MySQL]")
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


@router.delete("/remove_console_from_setting/{console_id}", tags=["Settings"], summary="Remove console from settings [MySQL]")
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


@router.put("/update_console_multiplier/{console_id}", tags=["Settings"], summary="Update console multiplier [MySQL]")
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



@router.post("/settings/console", response_model=GenericResponse, tags=["Settings"], summary="Add/update console in settings [MySQL]")
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


