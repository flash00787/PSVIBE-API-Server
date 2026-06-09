"""MySQL connection module for PS VIBE API Server."""
import pymysql
import os
from typing import Optional

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "psvibe_user",
    "password": "PsVibe@2026_Rotated!",
    "database": "psvibe_api",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor
}

_pool: Optional[pymysql.Connection] = None

def get_db() -> pymysql.Connection:
    global _pool
    try:
        if _pool is None or not _pool.open:
            _pool = pymysql.connect(**DB_CONFIG)
        return _pool
    except pymysql.Error as e:
        raise RuntimeError(f"MySQL connection failed: {e}")

def close_db():
    global _pool
    if _pool and _pool.open:
        _pool.close()
    _pool = None

def query(sql: str, args: tuple = ()) -> list:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.commit()

def query_one(sql: str, args: tuple = ()) -> Optional[dict]:
    rows = query(sql, args)
    return rows[0] if rows else None

def execute(sql: str, args: tuple = ()) -> int:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            conn.commit()
            return cur.lastrowid
    finally:
        conn.commit()

def delete_rows(sql: str, args: tuple = ()) -> int:
    """Execute DELETE/UPDATE and return affected row count."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            conn.commit()
            return cur.rowcount
    finally:
        conn.commit()