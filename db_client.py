"""PS VIBE API Server — MySQL Database Client

Provides a DatabaseManager class with connection pooling for all PS VIBE tables:
  - console_status   (live console tracking)
  - games_library    (game catalogue)
  - member_wallets   (member balances & profiles)
  - staff_records    (staff details)
  - sync_status      (Google Sheets sync tracking)

Time zone helpers for MMT (UTC+6:30) conversion are included.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — read from environment with sensible defaults
# ---------------------------------------------------------------------------

MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT: int = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER: str = os.environ.get("MYSQL_USER", "psvibe_user")
MYSQL_PASSWORD: str = os.environ.get("MYSQL_PASSWORD", "PsVibe@User2024!")
MYSQL_DATABASE: str = os.environ.get("MYSQL_DATABASE", "psvibe_api")

POOL_SIZE: int = int(os.environ.get("MYSQL_POOL_SIZE", "5"))
POOL_NAME: str = os.environ.get("MYSQL_POOL_NAME", "psvibe_pool")

# ---------------------------------------------------------------------------
# MMT timezone (UTC+6:30)
# ---------------------------------------------------------------------------

MMT_OFFSET = timedelta(hours=6, minutes=30)
MMT = timezone(MMT_OFFSET)


def utc_to_mmt(dt: datetime) -> datetime:
    """Convert a UTC datetime to MMT (UTC+6:30).

    If *dt* is naive it is assumed to be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MMT)


def mmt_to_utc(dt: datetime) -> datetime:
    """Convert an MMT datetime to UTC.

    If *dt* is naive it is assumed to be MMT.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MMT)
    return dt.astimezone(timezone.utc)


def mmt_now() -> datetime:
    """Return the current time in MMT (UTC+6:30)."""
    return datetime.now(timezone.utc).astimezone(MMT)


# ---------------------------------------------------------------------------
# SQL column-selector fragments  (keep column lists DRY)
# ---------------------------------------------------------------------------

_COLS_MEMBER = (
    "member_id, balance_mins, member_name, phone, "
    "effective_rate, tier, total_spend, last_updated"
)

_COLS_GAME = "game_title, final_status, disc_count, solo_multi, genre, last_updated"

_COLS_STAFF = "staff_id, staff_name, base_salary, role, is_active, last_updated"

_COLS_CONSOLE = (
    "console_id, status, current_game, current_member, "
    "start_time, last_updated"
)

_COLS_SYNC = (
    "id, sheet_name, last_sync_at, rows_synced, status, created_at, updated_at"
)


# ---------------------------------------------------------------------------
# DatabaseManager — connection pool + typed query helpers
# ---------------------------------------------------------------------------


class DatabaseManager:
    """Manages a pool of MySQL connections and provides read/write helpers
    for every PS VIBE table."""

    def __init__(self) -> None:
        self._pool: MySQLConnectionPool = self._build_pool()

    # -- pool ---------------------------------------------------------------

    def _build_pool(self) -> MySQLConnectionPool:
        """Create and return a pooled connection factory."""
        config: dict[str, Any] = {
            "host": MYSQL_HOST,
            "port": MYSQL_PORT,
            "user": MYSQL_USER,
            "password": MYSQL_PASSWORD,
            "database": MYSQL_DATABASE,
            "charset": "utf8mb4",
            "collation": "utf8mb4_unicode_ci",
            "autocommit": True,
            "raise_on_warnings": False,
            "use_pure": True,  # pure-Python mode — zero native deps
        }
        try:
            pool = MySQLConnectionPool(
                pool_name=POOL_NAME,
                pool_size=POOL_SIZE,
                pool_reset_session=True,
                **config,
            )
            logger.info(
                "MySQL connection pool ready — host=%s:%s db=%s pool=%s size=%s",
                MYSQL_HOST,
                MYSQL_PORT,
                MYSQL_DATABASE,
                POOL_NAME,
                POOL_SIZE,
            )
            return pool
        except Exception:
            logger.critical(
                "Failed to create MySQL connection pool (host=%s:%s db=%s)",
                MYSQL_HOST,
                MYSQL_PORT,
                MYSQL_DATABASE,
                exc_info=True,
            )
            raise

    @contextmanager
    def _connection(self):
        """Yield a connection from the pool, returning it afterwards."""
        conn = None
        try:
            conn = self._pool.get_connection()
            yield conn
        except mysql.connector.Error as exc:
            logger.error("MySQL error: %s (errno=%s)", exc.msg, exc.errno)
            raise
        except Exception:
            logger.error("Unexpected database error", exc_info=True)
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("Error closing connection (ignored)", exc_info=True)

    # -- internal helpers ---------------------------------------------------

    def _fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute *query* and return every row as a dict."""
        with self._connection() as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()

    def _fetch_one(self, query: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        """Execute *query* and return the first row as a dict, or ``None``."""
        with self._connection() as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()

    def _execute(self, query: str, params: tuple = ()) -> int:
        """Execute a write statement and return the number of affected rows."""
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.rowcount

    # -- member_wallets -----------------------------------------------------

    def get_members(self) -> list[dict[str, Any]]:
        """Return every member in member_wallets, ordered by member_id."""
        try:
            return self._fetch_all(
                f"SELECT {_COLS_MEMBER} FROM member_wallets ORDER BY member_id"
            )
        except Exception:
            logger.exception("get_members failed")
            return []

    def get_member_by_id(self, member_id: str) -> Optional[dict[str, Any]]:
        """Return a single member row or ``None``."""
        try:
            return self._fetch_one(
                f"SELECT {_COLS_MEMBER} FROM member_wallets WHERE member_id = %s",
                (member_id,),
            )
        except Exception:
            logger.exception("get_member_by_id(%s) failed", member_id)
            return None

    def get_members_by_tier(self, tier: str) -> list[dict[str, Any]]:
        """Return members filtered by tier."""
        try:
            return self._fetch_all(
                f"SELECT {_COLS_MEMBER} FROM member_wallets "
                "WHERE tier = %s ORDER BY member_id",
                (tier,),
            )
        except Exception:
            logger.exception("get_members_by_tier(%s) failed", tier)
            return []

    def update_member_balance(self, member_id: str, balance_mins: int) -> bool:
        """Set a member's balance_mins. Returns ``True`` on success."""
        try:
            rows = self._execute(
                "UPDATE member_wallets SET balance_mins = %s WHERE member_id = %s",
                (balance_mins, member_id),
            )
            return rows > 0
        except Exception:
            logger.exception("update_member_balance(%s) failed", member_id)
            return False

    # -- games_library ------------------------------------------------------

    def get_games(self) -> list[dict[str, Any]]:
        """Return every game in the library."""
        try:
            return self._fetch_all(
                f"SELECT {_COLS_GAME} FROM games_library ORDER BY game_title"
            )
        except Exception:
            logger.exception("get_games failed")
            return []

    def get_game_by_title(self, game_title: str) -> Optional[dict[str, Any]]:
        """Return a single game row or ``None``."""
        try:
            return self._fetch_one(
                f"SELECT {_COLS_GAME} FROM games_library WHERE game_title = %s",
                (game_title,),
            )
        except Exception:
            logger.exception("get_game_by_title(%s) failed", game_title)
            return None

    # -- staff_records ------------------------------------------------------

    def get_staff(self, active_only: bool = True) -> list[dict[str, Any]]:
        """Return staff records, optionally limited to active staff only."""
        try:
            query = f"SELECT {_COLS_STAFF} FROM staff_records"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY staff_id"
            return self._fetch_all(query)
        except Exception:
            logger.exception("get_staff failed")
            return []

    def get_staff_by_id(self, staff_id: int) -> Optional[dict[str, Any]]:
        """Return a single staff record or ``None``."""
        try:
            return self._fetch_one(
                f"SELECT {_COLS_STAFF} FROM staff_records WHERE staff_id = %s",
                (staff_id,),
            )
        except Exception:
            logger.exception("get_staff_by_id(%s) failed", staff_id)
            return None

    # -- console_status -----------------------------------------------------

    def get_console_status(self) -> list[dict[str, Any]]:
        """Return every console status record."""
        try:
            return self._fetch_all(
                f"SELECT {_COLS_CONSOLE} FROM console_status ORDER BY console_id"
            )
        except Exception:
            logger.exception("get_console_status failed")
            return []

    def get_console_by_id(self, console_id: str) -> Optional[dict[str, Any]]:
        """Return a single console status row or ``None``."""
        try:
            return self._fetch_one(
                f"SELECT {_COLS_CONSOLE} FROM console_status WHERE console_id = %s",
                (console_id,),
            )
        except Exception:
            logger.exception("get_console_by_id(%s) failed", console_id)
            return None

    def update_console_status(
        self,
        console_id: str,
        *,
        status: Optional[str] = None,
        current_game: Optional[str] = None,
        current_member: Optional[str] = None,
        start_time: Optional[datetime] = None,
    ) -> bool:
        """Update fields on a console_status row.

        Only non-``None`` keyword arguments are applied.  Returns ``True``
        when at least one row was updated.
        """
        try:
            sets: list[str] = []
            params: list[Any] = []

            if status is not None:
                sets.append("status = %s")
                params.append(status)
            if current_game is not None:
                sets.append("current_game = %s")
                params.append(current_game)
            if current_member is not None:
                sets.append("current_member = %s")
                params.append(current_member)
            if start_time is not None:
                sets.append("start_time = %s")
                params.append(start_time)

            if not sets:
                return False

            params.append(console_id)
            query = f"UPDATE console_status SET {', '.join(sets)} WHERE console_id = %s"
            rows = self._execute(query, tuple(params))
            return rows > 0
        except Exception:
            logger.exception("update_console_status(%s) failed", console_id)
            return False

    # -- sync_status --------------------------------------------------------

    def get_sync_status(
        self, sheet_name: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return sync-status rows, optionally filtered by *sheet_name*."""
        try:
            if sheet_name:
                return self._fetch_all(
                    f"SELECT {_COLS_SYNC} FROM sync_status WHERE sheet_name = %s",
                    (sheet_name,),
                )
            return self._fetch_all(
                f"SELECT {_COLS_SYNC} FROM sync_status ORDER BY sheet_name"
            )
        except Exception:
            logger.exception("get_sync_status failed")
            return []

    def record_sync(
        self,
        sheet_name: str,
        rows_synced: int = 0,
        status: str = "ok",
    ) -> int:
        """Upsert a sync_status row and return the auto-generated id."""
        try:
            with self._connection() as conn:
                with conn.cursor(dictionary=True) as cursor:
                    cursor.execute(
                        "INSERT INTO sync_status (sheet_name, last_sync_at, rows_synced, status) "
                        "VALUES (%s, NOW(), %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "  last_sync_at = NOW(), rows_synced = %s, status = %s",
                        (sheet_name, rows_synced, status, rows_synced, status),
                    )
                    conn.commit()
                    # Retrieve the id (works for insert or update)
                    cursor.execute(
                        "SELECT id FROM sync_status WHERE sheet_name = %s",
                        (sheet_name,),
                    )
                    row = cursor.fetchone()
                    return row["id"] if row else 0
        except Exception:
            logger.exception("record_sync(%s) failed", sheet_name)
            return -1

    # -- health check -------------------------------------------------------

    def health_check(self) -> bool:
        """Return ``True`` when the database is reachable and the schema exists."""
        try:
            with self._connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM DUAL")
                    _ = cursor.fetchone()
            return True
        except Exception:
            logger.warning("Database health check failed", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Module-level singleton — lazy initialised, thread-safe via GIL + logging
# ---------------------------------------------------------------------------

_db: Optional[DatabaseManager] = None


def get_db() -> DatabaseManager:
    """Return the module-level DatabaseManager singleton."""
    global _db
    if _db is None:
        _db = DatabaseManager()
    return _db
