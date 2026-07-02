"""
dual_write.py — Phase 3: Parallel write to console_booking (legacy) + console_bookings/sessions (new)

Usage:
    from dual_write import dual_create_booking, dual_update_booking_status, dual_checkin, dual_start_session, dual_end_session, dual_extend_duration, dual_discrepancy_check

All functions follow the pattern:
    1. Execute on legacy table FIRST (business-critical)
    2. Then execute on new table(s) SECOND (best-effort, errors logged)
    3. Return the legacy result (new table errors are non-blocking)

Column Mapping:
    console_booking.console_id     →  console_bookings.console_id (nullable)
    console_booking.start_time     →  console_bookings.planned_start / console_sessions.actual_start
    console_booking.end_time       →  console_bookings.planned_end   / console_sessions.actual_end
    console_booking.duration_mins  →  console_bookings.planned_duration_mins / console_sessions.actual_duration_mins
    console_booking.status         →  console_bookings.status (booking statuses) / console_sessions.status (session statuses)
"""

import logging
import traceback
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Column mapping helpers
# ──────────────────────────────────────────────────────────────────

# Booking statuses — mirror to console_bookings
BOOKING_STATUSES = frozenset({"pending", "confirmed", "rejected", "cancelled", "pending_check_in", "waiting", "notified"})

# Session statuses — mirror to console_sessions
SESSION_STATUSES = frozenset({"checked_in", "Active", "Done"})


def _safe_dual_exec(sql_new: str, params: tuple, operation_name: str = "dual_write"):
    """Execute on new table — catch and log errors, never propagate."""
    try:
        _mysql_exec(sql_new, params)
    except Exception as e:
        logger.warning("⚠️ dual_write.%s FAILED (non-blocking): %s\n%s", operation_name, e, traceback.format_exc())


def _status_to_booking(new_status: str) -> str:
    """Normalize status for console_bookings."""
    if new_status == "Waiting":
        return "waiting"
    if new_status == "Notified":
        return "notified"
    return new_status


def _status_to_session(new_status: str) -> str:
    """Normalize status for console_sessions."""
    if new_status == "Active":
        return "active"
    if new_status == "Done":
        return "done"
    if new_status == "checked_in":
        return "checked_in"
    return new_status.lower()


# ──────────────────────────────────────────────────────────────────
# Dual-write operations
# ──────────────────────────────────────────────────────────────────

def dual_create_booking(booking_id: int, console_id: str, member_id: str,
                        booking_date, start_time, status: str,
                        staff_name: str, notes: str, phone: str = "",
                        telegram_chat_id: str = "", game_name: str = "",
                        duration_mins: int = 60, branch_id: int = 1):
    """
    Mirror a new booking INSERT to console_bookings.
    Call AFTER the legacy INSERT into console_booking succeeds.
    """
    booking_status = _status_to_booking(status)
    _safe_dual_exec(
        """INSERT INTO console_bookings
           (id, branch_id, console_id, member_id, booking_date,
            planned_start, planned_end, planned_duration_mins,
            status, staff_name, notes, telegram_chat_id, phone, game_name, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
        (booking_id, branch_id, console_id, member_id, booking_date,
         start_time, None, duration_mins,
         booking_status, staff_name, notes, telegram_chat_id, phone, game_name),
        "create_booking"
    )


def dual_update_booking_status(booking_id: int, new_status: str,
                               staff_name: str = None, notes: str = None):
    """
    Mirror a booking status UPDATE to console_bookings.
    Only for BOOKING statuses (confirmed, rejected, cancelled, pending_check_in).
    Does NOT handle session statuses (Active, Done, checked_in).
    """
    booking_status = _status_to_booking(new_status)
    if new_status == "cancelled":
        _safe_dual_exec(
            """UPDATE console_bookings
               SET status=%s, cancelled_at=NOW(),
                   staff_name=COALESCE(NULLIF(%s, ''), staff_name),
                   notes=COALESCE(NULLIF(%s, ''), notes)
               WHERE id=%s""",
            (booking_status, staff_name or "", notes or "", booking_id),
            "update_booking_status: cancelled"
        )
    else:
        _safe_dual_exec(
            """UPDATE console_bookings
               SET status=%s,
                   staff_name=COALESCE(NULLIF(%s, ''), staff_name),
                   notes=COALESCE(NULLIF(%s, ''), notes)
               WHERE id=%s""",
            (booking_status, staff_name or "", notes or "", booking_id),
            "update_booking_status"
        )


def dual_checkin(booking_id: int):
    """
    When a booking is checked-in (status='checked_in'):
    INSERT a new row into console_sessions, linking via booking_id.
    The row in console_bookings stays at 'confirmed' or 'pending_check_in'.
    """
    # Look up the booking row from legacy table (already exists since legacy write succeeded)
    row = _mysql_query_one(
        "SELECT id, branch_id, console_id, member_id, start_time, duration_mins, game_name, staff_name, notes "
        "FROM console_booking WHERE id=%s", (booking_id,)
    )
    if not row:
        logger.warning("dual_checkin: booking %s not found in legacy table, skipping", booking_id)
        return

    _safe_dual_exec(
        """INSERT INTO console_sessions
           (booking_id, branch_id, console_id, member_id,
            actual_start, actual_duration_mins,
            status, game_name, staff_name, notes, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
        (booking_id, row.get("branch_id", 1), row.get("console_id", ""),
         row.get("member_id", ""), row.get("start_time", None),
         row.get("duration_mins", 60), "checked_in",
         row.get("game_name", ""), row.get("staff_name", ""), row.get("notes", "")),
        "checkin"
    )


def dual_start_session(booking_id: int):
    """
    When a session is started (status='Active'):
    Update console_sessions status to 'active'.
    If no session row exists yet (direct start without checkin), create one.
    """
    existing = _mysql_query_one(
        "SELECT id FROM console_sessions WHERE booking_id=%s", (booking_id,)
    )
    if existing:
        _safe_dual_exec(
            "UPDATE console_sessions SET status='active' WHERE booking_id=%s AND status='checked_in'",
            (booking_id,), "start_session"
        )
    else:
        # No checkin was done — create session directly
        dual_checkin(booking_id)
        _safe_dual_exec(
            "UPDATE console_sessions SET status='active' WHERE booking_id=%s",
            (booking_id,), "start_session:fallback"
        )


def dual_end_session(booking_id: int, end_time, actual_mins: int):
    """
    When a session ends (status='Done'):
    Update console_sessions with actual_end, actual_duration_mins, and status='done'.
    """
    _safe_dual_exec(
        """UPDATE console_sessions
           SET status='done', actual_end=%s, actual_duration_mins=%s
           WHERE booking_id=%s AND status IN ('active','checked_in')""",
        (end_time, actual_mins, booking_id),
        "end_session"
    )


def dual_extend_duration(booking_id: int, new_duration_mins: int, new_end_time):
    """
    When a session duration is extended:
    Update actual_duration_mins in console_sessions.
    If new_end_time is provided, update actual_end too.
    """
    if new_end_time:
        _safe_dual_exec(
            """UPDATE console_sessions
               SET actual_duration_mins=%s, actual_end=%s
               WHERE booking_id=%s AND status='active'""",
            (new_duration_mins, new_end_time, booking_id),
            "extend_duration"
        )
    else:
        _safe_dual_exec(
            """UPDATE console_sessions
               SET actual_duration_mins=%s
               WHERE booking_id=%s AND status='active'""",
            (new_duration_mins, booking_id),
            "extend_duration"
        )


def dual_assign_console(booking_id: int, console_id: str):
    """When a console is assigned to a booking/session."""
    _safe_dual_exec(
        "UPDATE console_bookings SET console_id=%s WHERE id=%s",
        (console_id, booking_id), "assign_console:bookings"
    )
    _safe_dual_exec(
        "UPDATE console_sessions SET console_id=%s WHERE booking_id=%s AND status IN ('active','checked_in')",
        (console_id, booking_id), "assign_console:sessions"
    )


# ──────────────────────────────────────────────────────────────────
# Discrepancy checking (Phase 3 monitoring)
# ──────────────────────────────────────────────────────────────────

def dual_discrepancy_check() -> dict:
    """
    Compare legacy table vs new tables. Returns discrepancies.
    Called by the health-check endpoint during Phase 3 monitoring.

    Returns: {"ok": True/False, "discrepancies": [...], "totals": {...}}
    """
    discrepancies = []

    # 1. Total booking count
    legacy_total = _mysql_query_one("SELECT COUNT(*) as c FROM console_booking")["c"]
    new_bookings = _mysql_query_one("SELECT COUNT(*) as c FROM console_bookings")["c"]
    new_sessions = _mysql_query_one("SELECT COUNT(*) as c FROM console_sessions")["c"]

    # 2. Row-level ID comparison: all booking IDs should match
    legacy_ids = set(r["id"] for r in _mysql_query("SELECT id FROM console_booking"))
    booking_ids = set(r["id"] for r in _mysql_query("SELECT id FROM console_bookings"))
    session_ids = set(r["booking_id"] for r in _mysql_query("SELECT booking_id FROM console_sessions WHERE booking_id IS NOT NULL"))

    in_legacy_not_booking = legacy_ids - booking_ids
    in_booking_not_legacy = booking_ids - legacy_ids
    in_legacy_not_session = legacy_ids - session_ids
    in_session_not_legacy = session_ids - legacy_ids

    # 3. Status consistency
    active_in_legacy = _mysql_query_one("SELECT COUNT(*) as c FROM console_booking WHERE status IN ('Active','checked_in','Done')")["c"]
    active_in_new = _mysql_query_one("SELECT COUNT(*) as c FROM console_sessions")["c"]

    if in_legacy_not_booking:
        discrepancies.append({"type": "missing_bookings", "ids": sorted(in_legacy_not_booking)[:20], "total": len(in_legacy_not_booking)})
    if in_booking_not_legacy:
        discrepancies.append({"type": "orphan_bookings", "ids": sorted(in_booking_not_legacy)[:20], "total": len(in_booking_not_legacy)})
    if active_in_legacy != active_in_new:
        discrepancies.append({"type": "session_count_mismatch", "legacy": active_in_legacy, "new": active_in_new})

    return {
        "ok": len(discrepancies) == 0,
        "discrepancies": discrepancies,
        "totals": {
            "console_booking": legacy_total,
            "console_bookings": new_bookings,
            "console_sessions": new_sessions,
            "missing_bookings": len(in_legacy_not_booking),
            "orphan_bookings": len(in_booking_not_legacy),
        }
    }
