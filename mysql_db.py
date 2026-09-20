"""MySQL connection module for PS VIBE API Server.
Uses DBUtils PooledDB for connection pooling (Fix #27).
"""
import pymysql
import os
from typing import Any, Dict, List, Optional
from dbutils.pooled_db import PooledDB

DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "psvibe_api"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor
}

# Connection pool (Fix #27: replaces single-connection approach)
# Lazy-initialized to not crash on import without env vars
_pool: Optional[PooledDB] = None

def _get_pool() -> PooledDB:
    """Lazy-init the connection pool.

    ``ping=1`` checks (and transparently reconnects) a pooled connection before each
    use, so a stale/idle or server-dropped connection never surfaces as
    'MySQL server has gone away' (a 500 the Staff App must never hit).
    """
    global _pool
    if _pool is None:
        try:
            _pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                mincached=2,
                maxcached=5,
                blocking=True,
                ping=1,
                **DB_CONFIG
            )
        except TypeError:
            # Older DBUtils without ping kwarg — fall back to the plain pool.
            _pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                mincached=2,
                maxcached=5,
                blocking=True,
                **DB_CONFIG
            )
    return _pool

def get_connection() -> pymysql.Connection:
    """Get a connection from the pool."""
    return _get_pool().connection()


# Legacy single-connection kept for backward compatibility
_pool_legacy: Optional[pymysql.Connection] = None

def get_db() -> pymysql.Connection:
    """Legacy: get a connection from the pool (same as get_connection).
    Kept for backward compatibility with existing code."""
    return get_connection()

def close_db() -> None:
    """Close all idle connections in the pool."""
    global _pool_legacy, _pool
    if _pool_legacy and _pool_legacy.open:
        _pool_legacy.close()
    _pool_legacy = None
    if _pool:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None

def query(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()  # Returns connection to pool

def query_one(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = query(sql, args)
    return rows[0] if rows else None

def execute(sql: str, args: tuple = ()) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()  # Returns connection to pool

def delete_rows(sql: str, args: tuple = ()) -> int:
    """Execute DELETE/UPDATE and return affected row count."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            conn.commit()
            return cur.rowcount
    finally:
        conn.close()  # Returns connection to pool
