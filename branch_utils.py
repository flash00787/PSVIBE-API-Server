"""Branch management utilities for Multi-Branch Support
Phase 1 — Branch filtering infrastructure for PS VIBE API Server.
"""
import logging
import re
import copy
from datetime import timedelta
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute

logger = logging.getLogger(__name__)

DEFAULT_BRANCH_ID = 1


# ── Time formatting helpers ───────────────────────────────
def _fmt_time(val):
    """Format a TIME/timedelta value to HH:MM:SS string."""
    if val is None:
        return None
    if isinstance(val, timedelta):
        total_secs = int(val.total_seconds())
        h, remainder = divmod(total_secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return str(val)


def _format_branch_row(row):
    """Format a branch row for API response (handle TIME fields)."""
    if not row:
        return row
    r = copy.copy(row)
    for key in ("open_time", "close_time"):
        if key in r:
            r[key] = _fmt_time(r[key])
    return r


# ── Branch CRUD ───────────────────────────────────────────
def get_branches():
    """Get all active branches"""
    rows = _mysql_query(
        "SELECT id, name, code, address, phone, is_active, open_time, close_time, telegram_group_id "
        "FROM branches WHERE is_active = 1 ORDER BY id"
    )
    return [_format_branch_row(r) for r in rows]


def get_all_branches():
    """Get all branches including inactive"""
    rows = _mysql_query(
        "SELECT id, name, code, address, phone, is_active, open_time, close_time, telegram_group_id "
        "FROM branches ORDER BY id"
    )
    return [_format_branch_row(r) for r in rows]


def get_branch(branch_id: int):
    """Get single branch by ID"""
    rows = _mysql_query(
        "SELECT id, name, code, address, phone, is_active, open_time, close_time, telegram_group_id "
        "FROM branches WHERE id = %s", (branch_id,)
    )
    return _format_branch_row(rows[0]) if rows else None


def get_branch_by_code(code: str):
    """Get branch by code (e.g. 'MAIN')"""
    rows = _mysql_query(
        "SELECT id, name, code, address, phone, is_active, open_time, close_time, telegram_group_id "
        "FROM branches WHERE code = %s", (code.upper(),)
    )
    return _format_branch_row(rows[0]) if rows else None


# ── Branch filtering helpers ──────────────────────────────

def add_branch_filter(sql: str, branch_id: int = None, table_alias: str = None) -> str:
    """
    Add branch_id filter to a SQL query.
    If branch_id is None, defaults to DEFAULT_BRANCH_ID.
    If table_alias is provided, uses 'alias.branch_id' instead of 'branch_id'.

    Inserts the WHERE clause before GROUP BY / ORDER BY / LIMIT clauses.
    If WHERE already exists, appends AND condition instead.
    """
    bid = branch_id if branch_id is not None else DEFAULT_BRANCH_ID
    col = f"{table_alias}.branch_id" if table_alias else "branch_id"
    condition = f"{col} = {bid}"

    if re.search(r'\bWHERE\b', sql, re.IGNORECASE):
        return f"{sql} AND {condition}"

    clauses = re.split(r'(?i)(\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b)', sql, maxsplit=1)
    if len(clauses) > 1:
        return f"{clauses[0]} WHERE {condition} {''.join(clauses[1:])}"

    return f"{sql} WHERE {condition}"


def query_with_branch(sql: str, params: tuple = None, branch_id: int = None, table_alias: str = None):
    """Run query with branch_id filter automatically injected.

    Use this as a drop-in replacement for mysql_db.query() when the query
    doesn't already contain a branch_id filter.

    Set branch_id=0 to skip branch filtering entirely.
    Returns list of dict rows.
    """
    if branch_id is not None and branch_id > 0:
        sql = add_branch_filter(sql, branch_id, table_alias)
    return _mysql_query(sql, params or ())


def query_one_with_branch(sql: str, params: tuple = None, branch_id: int = None, table_alias: str = None):
    """Like query_with_branch but returns single row or None."""
    rows = query_with_branch(sql, params, branch_id, table_alias)
    return rows[0] if rows else None


# ── Resolve branch from request ────────────────────────────
def resolve_branch_id(branch_id: int = None, branch_code: str = None) -> int:
    """Resolve branch_id from explicit id, code, or default.

    Priority: branch_id > branch_code > DEFAULT_BRANCH_ID
    """
    if branch_id is not None and branch_id > 0:
        return branch_id
    if branch_code:
        branch = get_branch_by_code(branch_code)
        if branch:
            return branch["id"]
    return DEFAULT_BRANCH_ID
