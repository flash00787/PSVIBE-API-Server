"""PS VIBE API Server — Google Sheets → MySQL Sync Service

Periodically syncs data from Google Sheets into MySQL tables for faster API reads.
Supports background threading for continuous sync at configurable intervals.

Usage:
    from sync_service import SyncService

    svc = SyncService()
    svc.sync_all()                          # one-time full sync
    svc.start_background_sync(interval=300) # background sync every 5 min
    svc.stop_background_sync()              # stop background thread

    # Or run standalone:
    #   python sync_service.py
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

import mysql.connector
from mysql.connector import Error as MySQLError

# ── Imports from project ──
from sheets_client import (
    get_member_rows,
    get_game_rows,
    get_booking_rows,
    get_worksheet,
    int_safe,
    float_safe,
    SheetsPermissionError,
)
from config import (
    SHEET_CARD_WALLET,
    SHEET_GAME_LIBRARY,
    SHEET_CONSOLE_BOOKING,
    SHEET_SETTING,
    MMT_HOURS,
    MMT_MINUTES,
)

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═════════════════════════════════════════════════════════════════════

MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "psvibe_user")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "PsVibe@User2024!")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "psvibe_api")

# Default sync interval (seconds)
DEFAULT_SYNC_INTERVAL = 300  # 5 minutes

# ── MMT timezone helper ──
MMT = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))


def now_mmt() -> datetime:
    """Return current datetime in MMT (UTC+6:30)."""
    return datetime.now(MMT)


# ═════════════════════════════════════════════════════════════════════
#  TABLE DDL (CREATE IF NOT EXISTS)
# ═════════════════════════════════════════════════════════════════════

CREATE_TABLES_SQL = {
    "member_wallets": """
        CREATE TABLE IF NOT EXISTS member_wallets (
            member_id     VARCHAR(50)    PRIMARY KEY,
            balance_mins  INT            DEFAULT 0,
            member_name   VARCHAR(200),
            phone         VARCHAR(50),
            effective_rate DECIMAL(10,2) DEFAULT 1.00,
            tier          VARCHAR(50),
            total_spend   DECIMAL(15,2)  DEFAULT 0.00,
            last_updated  DATETIME       DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "games_library": """
        CREATE TABLE IF NOT EXISTS games_library (
            game_title   VARCHAR(200) PRIMARY KEY,
            final_status VARCHAR(50)  DEFAULT '',
            disc_count   INT          DEFAULT 0,
            solo_multi   VARCHAR(50)  DEFAULT '',
            genre        VARCHAR(100),
            last_updated DATETIME     DEFAULT CURRENT_TIMESTAMP
                         ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "console_status": """
        CREATE TABLE IF NOT EXISTS console_status (
            console_id      VARCHAR(20)  PRIMARY KEY,
            status          VARCHAR(50),
            current_game    TEXT,
            current_member  VARCHAR(100),
            start_time      DATETIME,
            last_updated    DATETIME     DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "staff_records": """
        CREATE TABLE IF NOT EXISTS staff_records (
            staff_id     INT AUTO_INCREMENT PRIMARY KEY,
            staff_name   VARCHAR(200)  NOT NULL,
            base_salary  DECIMAL(12,2) DEFAULT 0.00,
            role         VARCHAR(100),
            is_active    TINYINT(1)    DEFAULT 1,
            last_updated DATETIME      DEFAULT CURRENT_TIMESTAMP
                         ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "sync_status": """
        CREATE TABLE IF NOT EXISTS sync_status (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            sheet_name  VARCHAR(100) NOT NULL UNIQUE,
            last_sync_at DATETIME    NOT NULL,
            rows_synced INT          DEFAULT 0,
            status      VARCHAR(20)  DEFAULT 'pending',
            created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}


# ═════════════════════════════════════════════════════════════════════
#  SyncService
# ═════════════════════════════════════════════════════════════════════

class SyncService:
    """Syncs Google Sheets data into MySQL tables.

    Reads from sheets via sheets_client, writes to MySQL via mysql-connector-python.
    Supports one-time syncs and background-threaded periodic syncs.
    """

    def __init__(self):
        """Initialise the sync service (lazy MySQL connection)."""
        self._conn: Optional[mysql.connector.MySQLConnection] = None
        self._bg_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ── MySQL Connection ──────────────────────────────────────────

    def _get_connection(self) -> mysql.connector.MySQLConnection:
        """Return a live MySQL connection; reconnect if needed."""
        try:
            if self._conn is None or not self._conn.is_connected():
                self._conn = mysql.connector.connect(
                    host=MYSQL_HOST,
                    port=MYSQL_PORT,
                    user=MYSQL_USER,
                    password=MYSQL_PASSWORD,
                    database=MYSQL_DATABASE,
                    charset="utf8mb4",
                    autocommit=False,
                )
                logger.info(
                    "Connected to MySQL: %s:%s/%s",
                    MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE,
                )
            return self._conn
        except MySQLError as e:
            logger.error("MySQL connection failed: %s", e)
            raise

    def _ensure_tables(self) -> None:
        """Create all required tables if they don't already exist."""
        conn = self._get_connection()
        with conn.cursor() as cur:
            for table_name, ddl in CREATE_TABLES_SQL.items():
                try:
                    cur.execute(ddl)
                    logger.debug("Ensured table exists: %s", table_name)
                except MySQLError as e:
                    logger.error("Failed to create table %s: %s", table_name, e)
                    raise
        conn.commit()

    # ── Sync Status Helpers ───────────────────────────────────────

    def _update_sync_status(
        self,
        sheet_name: str,
        rows_synced: int,
        status: str,
    ) -> None:
        """Upsert a row in sync_status after a sync run."""
        conn = self._get_connection()
        now = now_mmt()
        sql = """
            INSERT INTO sync_status (sheet_name, last_sync_at, rows_synced, status)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_sync_at = VALUES(last_sync_at),
                rows_synced  = VALUES(rows_synced),
                status       = VALUES(status)
        """
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (sheet_name, now, rows_synced, status))
            conn.commit()
            logger.info(
                "sync_status updated: %s → %d rows [%s]",
                sheet_name, rows_synced, status,
            )
        except MySQLError as e:
            logger.error("Failed to update sync_status for %s: %s", sheet_name, e)
            conn.rollback()

    def get_last_sync_time(self, sheet_name: str) -> Optional[datetime]:
        """Return the last successful sync time for *sheet_name*, or None.

        Args:
            sheet_name: Sync-status key (e.g. 'member_wallets').

        Returns:
            datetime of last sync, or None if never synced.
        """
        conn = self._get_connection()
        sql = "SELECT last_sync_at FROM sync_status WHERE sheet_name = %s"
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (sheet_name,))
                row = cur.fetchone()
                return row[0] if row else None
        except MySQLError as e:
            logger.warning("Could not read sync_status for %s: %s", sheet_name, e)
            return None

    def is_data_stale(self, sheet_name: str, max_age_seconds: int = 300) -> bool:
        """Check whether data for *sheet_name* is older than *max_age_seconds*.

        Args:
            sheet_name: Sync-status key.
            max_age_seconds: Maximum age in seconds (default 300 / 5 min).

        Returns:
            True if never synced or last sync exceeds max_age_seconds.
        """
        last = self.get_last_sync_time(sheet_name)
        if last is None:
            return True
        # last comes from DB as naive datetime (no tz).
        # We treat DB times as MMT.
        age = (now_mmt().replace(tzinfo=None) - last).total_seconds()
        return age > max_age_seconds

    # ── Upsert helpers ────────────────────────────────────────────

    def _upsert_member_wallets(self, rows: List[Dict[str, Any]]) -> int:
        """Upsert member_wallets rows; return count."""
        conn = self._get_connection()
        sql = """
            INSERT INTO member_wallets
                (member_id, balance_mins, member_name, phone,
                 effective_rate, tier, total_spend)
            VALUES
                (%(member_id)s, %(balance_mins)s, %(member_name)s, %(phone)s,
                 %(effective_rate)s, %(tier)s, %(total_spend)s)
            ON DUPLICATE KEY UPDATE
                balance_mins   = VALUES(balance_mins),
                member_name    = VALUES(member_name),
                phone          = VALUES(phone),
                effective_rate = VALUES(effective_rate),
                tier           = VALUES(tier),
                total_spend    = VALUES(total_spend)
        """
        count = 0
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                count = cur.rowcount
            conn.commit()
        except MySQLError as e:
            logger.error("member_wallets upsert failed: %s", e)
            conn.rollback()
            raise
        return count

    def _upsert_games_library(self, rows: List[Dict[str, Any]]) -> int:
        """Upsert games_library rows; return count."""
        conn = self._get_connection()
        sql = """
            INSERT INTO games_library
                (game_title, final_status, disc_count, solo_multi, genre)
            VALUES
                (%(game_title)s, %(final_status)s, %(disc_count)s, %(solo_multi)s, %(genre)s)
            ON DUPLICATE KEY UPDATE
                final_status = VALUES(final_status),
                disc_count   = VALUES(disc_count),
                solo_multi   = VALUES(solo_multi),
                genre        = VALUES(genre)
        """
        count = 0
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                count = cur.rowcount
            conn.commit()
        except MySQLError as e:
            logger.error("games_library upsert failed: %s", e)
            conn.rollback()
            raise
        return count

    def _upsert_console_status(self, rows: List[Dict[str, Any]]) -> int:
        """Upsert console_status rows; return count."""
        conn = self._get_connection()
        sql = """
            INSERT INTO console_status
                (console_id, status, current_game, current_member, start_time)
            VALUES
                (%(console_id)s, %(status)s, %(current_game)s,
                 %(current_member)s, %(start_time)s)
            ON DUPLICATE KEY UPDATE
                status         = VALUES(status),
                current_game   = VALUES(current_game),
                current_member = VALUES(current_member),
                start_time     = VALUES(start_time)
        """
        count = 0
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                count = cur.rowcount
            conn.commit()
        except MySQLError as e:
            logger.error("console_status upsert failed: %s", e)
            conn.rollback()
            raise
        return count

    def _upsert_staff_records(self, rows: List[Dict[str, Any]]) -> int:
        """Upsert staff_records rows; return count.

        Uses INSERT … ON DUPLICATE KEY on staff_name (unique key is expected).
        If a staff_name doesn't exist, a new row with auto_increment staff_id
        is inserted. If it exists, base_salary is updated and is_active reset to 1.
        """
        conn = self._get_connection()
        # First ensure unique index on staff_name for upsert to work correctly
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_name "
                    "ON staff_records (staff_name)"
                )
            conn.commit()
        except MySQLError:
            # Index may already exist; ignore
            pass

        sql = """
            INSERT INTO staff_records
                (staff_name, base_salary, role, is_active)
            VALUES
                (%(staff_name)s, %(base_salary)s, %(role)s, 1)
            ON DUPLICATE KEY UPDATE
                base_salary = VALUES(base_salary),
                role        = VALUES(role),
                is_active   = 1
        """
        count = 0
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                count = cur.rowcount
            conn.commit()
        except MySQLError as e:
            logger.error("staff_records upsert failed: %s", e)
            conn.rollback()
            raise
        return count

    # ── Sync Functions ────────────────────────────────────────────

    def sync_member_wallets(self) -> int:
        """Sync Card_Wallet sheet → member_wallets MySQL table.

        Card_Wallet column mapping (0-indexed):
            1 → member_id
            2 → member_name
            3 → phone
            5 → total_spend (net_spend)
            6 → tier
            7 → balance_mins (wallet_mins)
            11 → effective_rate

        Returns:
            Number of rows synced.
        """
        logger.info("▶ Syncing member_wallets from %s …", SHEET_CARD_WALLET)
        try:
            rows_raw = get_member_rows()
        except SheetsPermissionError:
            logger.error("Cannot sync member_wallets — Sheets permission denied")
            self._update_sync_status("member_wallets", 0, "error")
            return 0
        except Exception as e:
            logger.error("Failed to read %s: %s", SHEET_CARD_WALLET, e)
            self._update_sync_status("member_wallets", 0, "error")
            return 0

        if len(rows_raw) < 2:
            logger.warning("%s has no data rows (only header)", SHEET_CARD_WALLET)
            self._update_sync_status("member_wallets", 0, "ok")
            return 0

        parsed: List[Dict[str, Any]] = []
        for row in rows_raw[1:]:
            if len(row) < 2:
                continue
            member_id = row[1].strip() if len(row) > 1 else ""
            if not member_id:
                continue
            parsed.append({
                "member_id":      member_id,
                "balance_mins":   int_safe(row[7]) if len(row) > 7 else 0,
                "member_name":    row[2].strip() if len(row) > 2 else "",
                "phone":          row[3].strip() if len(row) > 3 else "",
                "effective_rate": float_safe(row[11]) if len(row) > 11 else 1.0,
                "tier":           (row[6].strip() if len(row) > 6 and row[6].strip() else "Warrior"),
                "total_spend":    float_safe(row[5]) if len(row) > 5 else 0.0,
            })

        count = self._upsert_member_wallets(parsed)
        self._update_sync_status("member_wallets", len(parsed), "ok")
        logger.info("✔ member_wallets synced: %d rows", len(parsed))
        return len(parsed)

    def sync_games_library(self) -> int:
        """Sync Game_Library sheet → games_library MySQL table.

        Game_Library column mapping (0-indexed):
            1 → game_title
            2 → final_status
            3 → disc_count (available discs)
            20 → solo_multi | genre (parsed from Installed_On)

        Returns:
            Number of rows synced.
        """
        logger.info("▶ Syncing games_library from %s …", SHEET_GAME_LIBRARY)
        try:
            rows_raw = get_game_rows()
        except SheetsPermissionError:
            logger.error("Cannot sync games_library — Sheets permission denied")
            self._update_sync_status("games_library", 0, "error")
            return 0
        except Exception as e:
            logger.error("Failed to read %s: %s", SHEET_GAME_LIBRARY, e)
            self._update_sync_status("games_library", 0, "error")
            return 0

        if len(rows_raw) < 2:
            logger.warning("%s has no data rows", SHEET_GAME_LIBRARY)
            self._update_sync_status("games_library", 0, "ok")
            return 0

        parsed: List[Dict[str, Any]] = []
        for row in rows_raw[1:]:
            if not row:
                continue
            title = row[1].strip() if len(row) > 1 else ""
            if not title:
                continue
            # Parse col U (Installed_On) = "solo_multi|genre"
            meta_raw = row[20].strip() if len(row) > 20 else ""
            solo_multi = ""
            genre = ""
            if "|" in meta_raw:
                parts = meta_raw.split("|", 1)
                solo_multi = parts[0].strip()
                genre      = parts[1].strip()
            parsed.append({
                "game_title":   title,
                "final_status": row[2].strip() if len(row) > 2 else "",
                "disc_count":   int_safe(row[3]) if len(row) > 3 else 0,
                "solo_multi":   solo_multi,
                "genre":        genre,
            })

        count = self._upsert_games_library(parsed)
        self._update_sync_status("games_library", len(parsed), "ok")
        logger.info("✔ games_library synced: %d rows", len(parsed))
        return len(parsed)

    def sync_console_status(self) -> int:
        """Sync Console_Booking + Setting → console_status MySQL table.

        Reads console definitions from Setting (cols H-J) and overlays
        active bookings from Console_Booking to determine live status.

        Setting column mapping (0-indexed):
            7 (col H) → console name/id
            8 (col I) → console type
            9 (col J) → multiplier

        Console_Booking column mapping (0-indexed):
            0 → booking_id
            1 → date
            2 → console_id
            3 → member_id
            4 → start_time
            5 → end_time
            6 → status (Active/Scheduled/Done/Cancelled)

        Returns:
            Number of rows synced.
        """
        logger.info("▶ Syncing console_status from %s …", SHEET_CONSOLE_BOOKING)

        # ── Step 1: read console definitions from Setting ──
        try:
            setting_ws = get_worksheet(SHEET_SETTING)
            names  = setting_ws.col_values(8)[1:]   # col H, skip header
            types  = setting_ws.col_values(9)[1:]   # col I
            mults  = setting_ws.col_values(10)[1:]  # col J
        except Exception as e:
            logger.error("Failed to read Setting sheet: %s", e)
            self._update_sync_status("console_status", 0, "error")
            return 0

        # Build initial console list from Setting
        console_map: Dict[str, Dict[str, Any]] = {}
        for i, name in enumerate(names):
            cid = name.strip()
            if not cid:
                continue
            console_map[cid] = {
                "console_id":     cid,
                "status":         "Free",
                "current_game":   None,
                "current_member": None,
                "start_time":     None,
            }

        # ── Step 2: overlay active bookings ──
        try:
            mmt = now_mmt()
            today_str = mmt.strftime("%-m/%-d/%Y")
            bk_rows = get_booking_rows()
            for row in bk_rows[1:]:
                if len(row) < 7:
                    continue
                bk_date   = row[1].strip() if len(row) > 1 else ""
                bk_cid    = row[2].strip() if len(row) > 2 else ""
                bk_stat   = row[6].strip() if len(row) > 6 else ""
                if bk_date == today_str and bk_stat in ("Active", "Scheduled"):
                    if bk_cid in console_map:
                        console_map[bk_cid]["status"] = bk_stat
                        console_map[bk_cid]["current_member"] = (
                            row[3].strip() if len(row) > 3 else "Guest"
                        )
                        # Parse start time
                        start_str = row[4].strip() if len(row) > 4 else ""
                        if start_str:
                            try:
                                # Attempt HH:MM format
                                parts = start_str.split(":")
                                hh = int(parts[0])
                                mm_val = int(parts[1]) if len(parts) > 1 else 0
                                start_dt = mmt.replace(
                                    hour=hh, minute=mm_val, second=0, microsecond=0,
                                )
                                console_map[bk_cid]["start_time"] = start_dt
                            except (ValueError, IndexError):
                                console_map[bk_cid]["start_time"] = None
        except Exception as e:
            logger.warning("Booking overlay failed (console_status sync): %s", e)

        parsed = list(console_map.values())

        count = self._upsert_console_status(parsed)
        self._update_sync_status("console_status", len(parsed), "ok")
        logger.info("✔ console_status synced: %d rows", len(parsed))
        return len(parsed)

    def sync_staff_records(self) -> int:
        """Sync Setting sheet → staff_records MySQL table.

        Setting column mapping (0-indexed):
            18 (col S) → staff_name
            19 (col T) → base_salary

        Returns:
            Number of rows synced.
        """
        logger.info("▶ Syncing staff_records from %s …", SHEET_SETTING)
        try:
            setting_ws = get_worksheet(SHEET_SETTING)
            staff    = setting_ws.col_values(19)[1:]  # col S, skip header
            salaries = setting_ws.col_values(20)[1:]  # col T
        except Exception as e:
            logger.error("Failed to read Setting sheet for staff: %s", e)
            self._update_sync_status("staff_records", 0, "error")
            return 0

        parsed: List[Dict[str, Any]] = []
        for i, name in enumerate(staff):
            name = name.strip()
            if not name:
                continue
            sal_str = salaries[i].strip() if i < len(salaries) else "0"
            parsed.append({
                "staff_name":   name,
                "base_salary":  float_safe(sal_str),
                "role":         "",  # Role not currently in sheets (col T after S is salary)
            })

        count = self._upsert_staff_records(parsed)
        self._update_sync_status("staff_records", len(parsed), "ok")
        logger.info("✔ staff_records synced: %d rows", len(parsed))
        return len(parsed)

    # ── Sync All ──────────────────────────────────────────────────

    def sync_all(self) -> Dict[str, int]:
        """Run all sync functions in sequence and return row counts.

        Returns:
            Dict mapping sheet_name → rows_synced.
        """
        logger.info("══════════ Starting full sync ══════════")
        self._ensure_tables()

        results: Dict[str, int] = {}
        sync_order = [
            ("member_wallets", self.sync_member_wallets),
            ("games_library",  self.sync_games_library),
            ("console_status", self.sync_console_status),
            ("staff_records",  self.sync_staff_records),
        ]

        for name, fn in sync_order:
            try:
                count = fn()
                results[name] = count
            except Exception as e:
                logger.exception("sync_all: %s failed with error: %s", name, e)
                results[name] = -1
                self._update_sync_status(name, 0, "error")

        total = sum(v for v in results.values() if v > 0)
        logger.info("══════════ Full sync complete: %d total rows ══════════", total)
        return results

    # ── Background Sync (Threaded) ────────────────────────────────

    def start_background_sync(self, interval_seconds: int = DEFAULT_SYNC_INTERVAL) -> None:
        """Start a background daemon thread that calls sync_all() periodically.

        Args:
            interval_seconds: Seconds between sync runs (default 300 / 5 min).

        If a background thread is already running, this is a no-op.
        """
        with self._lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                logger.warning(
                    "Background sync already running (thread %s)",
                    self._bg_thread.name,
                )
                return
            self._stop_event.clear()
            self._bg_thread = threading.Thread(
                target=self._background_loop,
                args=(interval_seconds,),
                name="psvibe-sync-bg",
                daemon=True,
            )
            self._bg_thread.start()
            logger.info(
                "Background sync started (interval=%ds, thread=%s)",
                interval_seconds, self._bg_thread.name,
            )

    def stop_background_sync(self, timeout: float = 10.0) -> None:
        """Signal the background thread to stop and wait for it to join.

        Args:
            timeout: Max seconds to wait for the thread to finish.
        """
        with self._lock:
            if self._bg_thread is None:
                logger.info("No background sync thread to stop")
                return
            logger.info("Stopping background sync thread …")
            self._stop_event.set()
            self._bg_thread.join(timeout=timeout)
            if self._bg_thread.is_alive():
                logger.warning(
                    "Background sync thread did not stop within %.1fs", timeout,
                )
            else:
                logger.info("Background sync thread stopped")
            self._bg_thread = None

    def _background_loop(self, interval_seconds: int) -> None:
        """Internal loop: calls sync_all() every *interval_seconds* until stopped."""
        logger.info("Background sync loop started (interval=%ds)", interval_seconds)
        while not self._stop_event.is_set():
            try:
                self.sync_all()
            except Exception as e:
                logger.exception("Background sync iteration failed: %s", e)
            # Wait with periodic stop checks
            waited = 0
            while waited < interval_seconds and not self._stop_event.is_set():
                time.sleep(1)
                waited += 1
        logger.info("Background sync loop exited")

    # ── Cleanup ───────────────────────────────────────────────────

    def close(self) -> None:
        """Stop background sync and close MySQL connection."""
        self.stop_background_sync()
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info("MySQL connection closed")
            except MySQLError:
                pass
            self._conn = None


# ═════════════════════════════════════════════════════════════════════
#  Convenience - module-level singleton
# ═════════════════════════════════════════════════════════════════════

_sync_service: Optional[SyncService] = None


def get_sync_service() -> SyncService:
    """Return a module-level SyncService singleton."""
    global _sync_service
    if _sync_service is None:
        _sync_service = SyncService()
    return _sync_service


def start_background_sync(interval_seconds: int = DEFAULT_SYNC_INTERVAL) -> None:
    """Convenience: start background sync on the module singleton."""
    get_sync_service().start_background_sync(interval_seconds)


def stop_background_sync() -> None:
    """Convenience: stop background sync on the module singleton."""
    get_sync_service().stop_background_sync()


def get_last_sync_time(sheet_name: str) -> Optional[datetime]:
    """Convenience: get last sync time from the module singleton."""
    return get_sync_service().get_last_sync_time(sheet_name)


def is_data_stale(sheet_name: str, max_age_seconds: int = 300) -> bool:
    """Convenience: check data staleness via module singleton."""
    return get_sync_service().is_data_stale(sheet_name, max_age_seconds)


# ═════════════════════════════════════════════════════════════════════
#  Standalone runner
# ═════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting PS VIBE Sync Service (standalone)")

    svc = SyncService()

    try:
        results = svc.sync_all()
        print("\n── Sync Results ──")
        for name, count in results.items():
            icon = "✔" if count >= 0 else "✘"
            print(f"  {icon} {name}: {count} rows")
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception("Fatal sync error: %s", e)
        sys.exit(1)
    finally:
        svc.close()
