"""Console status synchronization — shared module to avoid circular imports.

Extracted from app.py to resolve circular import: dashboard_routes.py needs
_sync_console_status but can't import it from app.py without creating a cycle.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

import mysql.connector

from config import MMT_HOURS, MMT_MINUTES

logger = logging.getLogger("psvibe_api")

_MYSQL_CFG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "user": os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "psvibe_api"),
}


def _now_mmt():
    """Get current time in MMT (Myanmar Timezone)."""
    mmt = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))
    return datetime.now(mmt)


def sync_console_status(console_id):
    """Derive console_status from current booking table.

    Called after each booking mutation to prevent status drift.
    All queries run within a single transaction to prevent race conditions.
    """
    conn = mysql.connector.connect(**_MYSQL_CFG)
    try:
        conn.start_transaction()
        cur = conn.cursor(dictionary=True)
        # Use MMT time for comparisons (DB stores times in MMT, not UTC)
        _now_mmt_dt = _now_mmt()
        _now_mmt_str = _now_mmt_dt.strftime('%Y-%m-%d %H:%M:%S')
        _now_plus_30 = (_now_mmt_dt + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(
            "SELECT cs.booking_id AS id, cs.member_id, cs.game_name, cs.actual_start AS start_time FROM console_sessions cs "
            "WHERE cs.console_id=%s AND cs.status='active' ORDER BY cs.id DESC LIMIT 1",
            (console_id,)
        )
        active = cur.fetchone()
        if active:
            # Preserve existing start_time for normal extends (Active→Active),
            # but fix stale start_time from a previous session.
            cur.execute("SELECT status, start_time FROM console_status WHERE console_id=%s", (console_id,))
            current_status = cur.fetchone()
            booking_start = active.get('start_time')
            if current_status and current_status.get('status') == 'Active':
                cs_start = current_status.get('start_time')
                # If console_status start_time is stale (>5 min diff from booking), fix it
                if cs_start and booking_start:
                    diff = abs((booking_start - cs_start).total_seconds())
                    if diff > 300:  # >5 min mismatch -> update to booking time
                        cur.execute(
                            "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                            (active.get('member_id', ''), active.get('game_name', ''), booking_start, console_id)
                        )
                        conn.commit()
                        return
                cur.execute(
                    "UPDATE console_status SET status='Active', current_member=%s, current_game=%s WHERE console_id=%s",
                    (active.get('member_id', ''), active.get('game_name', ''), console_id)
                )
            else:
                # New activation — use booking's start_time, fallback to NOW()
                st = booking_start if booking_start else None
                if st:
                    cur.execute(
                        "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                        (active.get('member_id', ''), active.get('game_name', ''), st, console_id)
                    )
                else:
                    cur.execute(
                        "UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=%s WHERE console_id=%s",
                        (active.get('member_id', ''), active.get('game_name', ''), _now_mmt_str, console_id)
                    )
            conn.commit()
            return
        # Check for confirmed or checked_in bookings within 60-min window
        cur.execute(
            "SELECT cb.id, cb.member_id, cb.game_name FROM console_bookings cb "
            "WHERE cb.console_id=%s AND cb.status IN ('confirmed','checked_in') AND cb.planned_start <= %s AND cb.planned_end >= %s ORDER BY cb.id DESC LIMIT 1",
            (console_id, _now_plus_30, _now_mmt_str)
        )
        confirmed = cur.fetchone()
        if confirmed:
            cur.execute(
                "UPDATE console_status SET status='Reserved', current_member=%s, current_game=%s, start_time=NULL WHERE console_id=%s",
                (confirmed.get('member_id', ''), confirmed.get('game_name', ''), console_id)
            )
            conn.commit()
            return
        cur.execute(
            "UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s",
            (console_id,)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning("sync_console_status failed for %s: %s", console_id, e)
    finally:
        conn.close()
