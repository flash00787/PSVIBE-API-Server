"""PS VIBE API — Payment-account balances & account→account transfers (Staff App WAVE 5).

Owner requirements (Osmo 2026-09-19):
  * Account Balances view → ONLY "ACM's Acc" + "KBZ Bank", owner/admin only.
  * Account→account balance transfer, with role rules:
        staff  → Cash / WavePay / KPay / AYA Pay
        admin  → ALL accounts (also ACM's Acc, KBZ Bank)

Endpoints (mounted under /api/accounts):
  GET  /api/accounts/transferable   — accounts the caller may move money between (+ balances)
  GET  /api/accounts/balances       — role-aware read-only balances (staff: 4 shop accts; admin: all 6, owner-only flagged)
  POST /api/accounts/transfer       — atomic 2-leg transfer (cash_movements transfer_out+transfer_in)
  GET  /api/accounts/transfers      — transfer history (admin: all, staff: own only)

Design notes
------------
* Balances come from the SAME calculator the dashboard/finance uses
  (routes.finance_routes.get_finance_balances) so an app balance == dashboard balance.
* The 'Wave'/'WavePay' merge lives inside that calculator and only ever affects the
  WavePay account — it cannot leak into ACM's Acc / KBZ Bank here.
* A dedicated `account_transfers` ledger row is written in the SAME transaction as the
  two cash_movements legs, giving (a) idempotency via UNIQUE(client_ref) and
  (b) clean per-transfer history with a stable shared reference "TRF-...".
* CANONICAL SIGN CONVENTION (enforced here + in readers): `transfer_out` is stored
  NEGATIVE on the source account, `transfer_in` POSITIVE on the destination. Readers
  use SUM(ABS(amount)) for the "amount that left an account" semantics, so old and new
  rows both compute correctly regardless of historical sign drift.
"""
import logging
import random
import string
from datetime import datetime, timezone, timedelta

import pymysql
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from auth import get_staff_or_dashboard_user
from mysql_db import get_connection as _get_conn
from mysql_db import query as _q
from mysql_db import execute as _ex

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# ── Account catalogue ────────────────────────────────────────────────────────
# Operational accounts every staff member may use.
_OPERATIONAL = ["Cash", "WavePay", "KPay", "AYA Pay"]
# Owner-only accounts (never visible/usable to staff).
_OWNER_ONLY = ["ACM's Acc", "KBZ Bank"]
ALL_ACCOUNTS = _OPERATIONAL + _OWNER_ONLY

# display label -> finance calculator account key (used for balance lookup)
_KEY_BY_NAME = {
    "Cash": "cash",
    "WavePay": "wave",
    "KPay": "kpay",
    "AYA Pay": "aya_pay",
    "ACM's Acc": "acm_acc",
    "KBZ Bank": "kbz_bank",
}

_MAX_AMOUNT = 999999999999  # cash_movements.amount is DECIMAL(12,0)

_table_ready = False


def _is_admin(user: dict) -> bool:
    return str((user or {}).get("role") or "").strip().lower() == "admin"


def _allowed_accounts(user: dict):
    return list(ALL_ACCOUNTS) if _is_admin(user) else list(_OPERATIONAL)


def _caller_identity(user: dict) -> str:
    """Stable per-caller id used to scope a staff member's transfer history."""
    return str((user or {}).get("username") or "").strip()


def _caller_name(user: dict) -> str:
    u = user or {}
    return str(u.get("name") or u.get("display_name") or u.get("username") or "").strip()


def _ensure_table() -> None:
    """Idempotent CREATE TABLE for the transfer ledger + idempotency key."""
    global _table_ready
    if _table_ready:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS account_transfers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    ref VARCHAR(40) NOT NULL,
                    client_ref VARCHAR(80) DEFAULT NULL,
                    from_account VARCHAR(50) NOT NULL,
                    to_account VARCHAR(50) NOT NULL,
                    amount DECIMAL(12,0) NOT NULL,
                    note VARCHAR(255) DEFAULT NULL,
                    staff_name VARCHAR(100) DEFAULT NULL,
                    created_by_user VARCHAR(80) DEFAULT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_ref (ref),
                    UNIQUE KEY uniq_client_ref (client_ref),
                    KEY idx_created_by (created_by_user),
                    KEY idx_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()
        _table_ready = True
    except Exception as e:  # pragma: no cover - defensive
        conn.rollback()
        logger.error(f"account_transfers table init failed: {e}")
    finally:
        conn.close()


async def _balances_by_name() -> dict:
    """Current balance per account label, from the shared finance calculator."""
    from routes.finance_routes import get_finance_balances
    res = await get_finance_balances(year=None, month=None, user={"username": "accounts_api", "role": "admin"})
    out = {}
    if isinstance(res, dict) and res.get("success"):
        for a in res.get("accounts", []) or []:
            out[str(a.get("label"))] = float(a.get("balance") or 0)
    return out


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ── Daily account balance open/close (Staff App) ────────────────────────────
# Owner 2026-09-24: staff opens/closes the daily balance for the 4 shop accounts
# (Cash, WavePay, KPay, AYA Pay). Owner accounts (ACM's Acc + KBZ Bank) excluded.

_daily_bal_table_ready = False


def _mmt_now_str() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")


def _mmt_today_str() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)).strftime("%Y-%m-%d")


def _ensure_daily_balances_table() -> None:
    global _daily_bal_table_ready
    if _daily_bal_table_ready:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_account_balances (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    branch_id INT NOT NULL DEFAULT 1,
                    balance_date DATE NOT NULL,
                    account VARCHAR(50) NOT NULL,
                    opening_balance DECIMAL(12,2) DEFAULT 0,
                    closing_balance DECIMAL(12,2) DEFAULT 0,
                    status ENUM('open','closed') NOT NULL DEFAULT 'open',
                    opened_by VARCHAR(100) DEFAULT NULL,
                    opened_at DATETIME DEFAULT NULL,
                    closed_by VARCHAR(100) DEFAULT NULL,
                    closed_at DATETIME DEFAULT NULL,
                    UNIQUE KEY uniq_date_account (balance_date, account)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()
        _daily_bal_table_ready = True
    finally:
        conn.close()


@router.get("/daily-balances/today")
async def daily_balances_today(user: dict = Depends(get_staff_or_dashboard_user)):
    """Today's daily balance open/close state for the 4 operational accounts."""
    _ensure_daily_balances_table()
    today = _mmt_today_str()
    for acc in _OPERATIONAL:
        if not _q("SELECT id FROM daily_account_balances WHERE balance_date=%s AND account=%s", (today, acc)):
            _ex("INSERT INTO daily_account_balances (balance_date, account, status) VALUES (%s,%s,'open')", (today, acc))
    rows = _q("SELECT * FROM daily_account_balances WHERE balance_date=%s ORDER BY id", (today,))
    bals = await _balances_by_name()
    out = []
    for r in rows:
        acc = r["account"]
        opening = float(r["opening_balance"] or 0)
        closing = float(r["closing_balance"] or 0)
        out.append({
            "account": acc,
            "status": r["status"],
            "opening_balance": round(opening),
            "closing_balance": round(closing),
            "variance": round(closing - opening),
            "live_balance": round(float(bals.get(acc, 0))),
            "opened_by": r.get("opened_by") or "",
            "closed_by": r.get("closed_by") or "",
            "opened_at": _iso(r.get("opened_at")) if r.get("opened_at") else None,
            "closed_at": _iso(r.get("closed_at")) if r.get("closed_at") else None,
        })
    return {"success": True, "date": today, "accounts": out}


@router.post("/daily-balances/open")
async def daily_balances_open(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Set today's opening balance for an operational account."""
    _ensure_daily_balances_table()
    today = _mmt_today_str()
    account = str(body.get("account") or "").strip()
    if account not in _OPERATIONAL:
        raise HTTPException(status_code=400, detail="Account must be one of Cash / WavePay / KPay / AYA Pay")
    try:
        opening = float(body.get("opening_balance", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="opening_balance must be a number")
    opened_by = str(body.get("opened_by") or _caller_name(user) or user.get("username") or "").strip()
    now_str = _mmt_now_str()
    _ex(
        """INSERT INTO daily_account_balances (balance_date, account, opening_balance, status, opened_by, opened_at)
           VALUES (%s,%s,%s,'open',%s,%s)
           ON DUPLICATE KEY UPDATE opening_balance=VALUES(opening_balance), status='open',
             opened_by=VALUES(opened_by), opened_at=VALUES(opened_at)""",
        (today, account, opening, opened_by, now_str),
    )
    return {"success": True, "message": f"{account} opened with {opening:,.0f} Ks"}


@router.post("/daily-balances/close")
async def daily_balances_close(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Set today's closing balance for an operational account."""
    _ensure_daily_balances_table()
    today = _mmt_today_str()
    account = str(body.get("account") or "").strip()
    if account not in _OPERATIONAL:
        raise HTTPException(status_code=400, detail="Account must be one of Cash / WavePay / KPay / AYA Pay")
    try:
        closing = float(body.get("closing_balance", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="closing_balance must be a number")
    closed_by = str(body.get("closed_by") or _caller_name(user) or user.get("username") or "").strip()
    now_str = _mmt_now_str()
    row = _q("SELECT opening_balance FROM daily_account_balances WHERE balance_date=%s AND account=%s", (today, account))
    opening = float(row[0]["opening_balance"] or 0) if row else 0.0
    _ex(
        """INSERT INTO daily_account_balances (balance_date, account, closing_balance, status, closed_by, closed_at)
           VALUES (%s,%s,%s,'closed',%s,%s)
           ON DUPLICATE KEY UPDATE closing_balance=VALUES(closing_balance), status='closed',
             closed_by=VALUES(closed_by), closed_at=VALUES(closed_at)""",
        (today, account, closing, closed_by, now_str),
    )
    variance = closing - opening
    return {"success": True, "message": f"{account} closed. Variance {variance:+,.0f} Ks", "variance": round(variance)}


@router.get("/daily-balances/history")
async def daily_balances_history(days: int = Query(7, ge=1, le=90),
                                 user: dict = Depends(get_staff_or_dashboard_user)):
    """Recent daily balance history."""
    _ensure_daily_balances_table()
    rows = _q(
        "SELECT * FROM daily_account_balances WHERE balance_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) ORDER BY balance_date DESC, id",
        (days,),
    )
    out = []
    for r in rows:
        opening = float(r["opening_balance"] or 0)
        closing = float(r["closing_balance"] or 0)
        out.append({
            "date": str(r["balance_date"]),
            "account": r["account"],
            "status": r["status"],
            "opening_balance": round(opening),
            "closing_balance": round(closing),
            "variance": round(closing - opening),
            "opened_by": r.get("opened_by") or "",
            "closed_by": r.get("closed_by") or "",
        })
    return {"success": True, "history": out}


# ── GET /api/accounts/transferable ───────────────────────────────────────────
@router.get("/transferable")
async def list_transferable(user: dict = Depends(get_staff_or_dashboard_user)):
    """Accounts the caller may transfer from/to, each with its current balance.

    Staff  → the 4 shop accounts (Cash, WavePay, KPay, AYA Pay).
    Admin  → all 6 accounts, with the two owner-only ones flagged (`owner_only`).
    """
    _ensure_table()
    role = "admin" if _is_admin(user) else "staff"
    allowed = _allowed_accounts(user)
    bals = await _balances_by_name()
    accounts_out = [
        {
            "account": name,
            "label": name,
            "balance": round(bals.get(name, 0)),
            "owner_only": name in _OWNER_ONLY,
        }
        for name in allowed
    ]
    # Explicit per-account balances block (keyed by label) + list of owner-only ones.
    balances_block = {
        a["account"]: {"balance": a["balance"], "owner_only": a["owner_only"]}
        for a in accounts_out
    }
    return {
        "success": True,
        "role": role,
        "accounts": accounts_out,
        "balances": balances_block,
        "owner_only_accounts": [n for n in allowed if n in _OWNER_ONLY],
    }


# ── GET /api/accounts/balances (role-aware read-only) ─────────────────────
@router.get("/balances")
async def list_balances(user: dict = Depends(get_staff_or_dashboard_user)):
    """Role-aware read-only balances.

    Staff  → the 4 shop accounts. Admin → all 6, with owner-only ones flagged.
    (The strictly owner-only ACM's Acc + KBZ Bank view is unchanged and remains
     available at GET /api/finance/account-balances.)
    """
    _ensure_table()
    role = "admin" if _is_admin(user) else "staff"
    allowed = _allowed_accounts(user)
    bals = await _balances_by_name()
    # Last movement for the owner-only accounts (informational).
    last = {}
    try:
        for r in _q(
            "SELECT account, MAX(created_at) AS last_movement FROM cash_movements "
            "WHERE account IN (%s, %s) GROUP BY account",
            tuple(_OWNER_ONLY),
        ):
            last[r["account"]] = _iso(r.get("last_movement"))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"balances last_movement lookup failed: {e}")
    rows = [
        {
            "account": name,
            "label": name,
            "balance": round(bals.get(name, 0)),
            "owner_only": name in _OWNER_ONLY,
            "last_movement": last.get(name) if name in _OWNER_ONLY else None,
        }
        for name in allowed
    ]
    return {
        "success": True,
        "role": role,
        "owner_only_view": role == "admin",
        "accounts": rows,
        "balances": {
            r["account"]: {"balance": r["balance"], "owner_only": r["owner_only"]}
            for r in rows
        },
        # kept for backward-compat with the old owner-only alias consumers
        "operating": [r for r in rows if r["account"] == "ACM's Acc"],
        "capital": [r for r in rows if r["account"] == "KBZ Bank"],
    }


# ── POST /api/accounts/transfer ──────────────────────────────────────────────
@router.post("/transfer")
async def create_transfer(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Atomically move money between two allowed accounts (two cash_movements legs)."""
    _ensure_table()
    role = "admin" if _is_admin(user) else "staff"
    allowed = _allowed_accounts(user)

    from_account = str(body.get("from_account") or "").strip()
    to_account = str(body.get("to_account") or "").strip()
    note = str(body.get("note") or "").strip()
    staff_name = str(body.get("staff_name") or "").strip() or _caller_name(user)
    client_ref = body.get("client_ref")
    client_ref = str(client_ref).strip() if client_ref not in (None, "") else None

    # ── validation ──
    if not from_account or not to_account:
        raise HTTPException(status_code=400, detail="from_account and to_account are required")
    if from_account == to_account:
        raise HTTPException(status_code=400, detail="from_account and to_account must be different")
    bad = [a for a in (from_account, to_account) if a not in ALL_ACCOUNTS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown account: {', '.join(bad)}")
    for a in (from_account, to_account):
        if a not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted for your role ({role}) — '{a}' is owner-only. "
                       f"Allowed: {', '.join(allowed)}",
            )
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a number")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be greater than 0")
    if amount > _MAX_AMOUNT:
        raise HTTPException(status_code=400, detail=f"amount too large (max {_MAX_AMOUNT:,})")
    amount = round(amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be at least 1 Ks")

    # ── sufficient balance in source ──
    bals = await _balances_by_name()
    src_balance = round(bals.get(from_account, 0))
    if amount > src_balance:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance in {from_account}: available {src_balance:,} Ks, "
                   f"requested {amount:,.0f} Ks",
        )

    # ── idempotency short-circuit ──
    if client_ref:
        dupe = _q("SELECT ref FROM account_transfers WHERE client_ref = %s", (client_ref,))
        if dupe:
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate transfer — client_ref '{client_ref}' was already posted (ref {dupe[0]['ref']})",
            )

    ref = "TRF-{}-{}".format(
        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "".join(random.choices(string.ascii_uppercase + string.digits, k=4)),
    )
    out_note = f"Transfer to {to_account} [ref {ref}]" + (f" — {note}" if note else "")
    in_note = f"Transfer from {from_account} [ref {ref}]" + (f" — {note}" if note else "")

    conn = _get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO account_transfers
                   (ref, client_ref, from_account, to_account, amount, note, staff_name, created_by_user)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (ref, client_ref, from_account, to_account, amount, note or None,
                 staff_name, _caller_identity(user)),
            )
            # CANONICAL SIGN CONVENTION (legacy, enforced end-to-end):
            #   transfer_out -> stored NEGATIVE on the source account (money leaves)
            #   transfer_in  -> stored POSITIVE on the destination account (money arrives)
            # This is what the finance engine (balance-sheet / cash-flow / balances)
            # and all historical rows were built on. Readers additionally use
            # SUM(ABS(amount)) so any legacy sign drift still computes correctly.
            cur.execute(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                   VALUES ('transfer_out', %s, %s, %s, %s)""",
                (from_account, -amount, out_note, staff_name),
            )
            cur.execute(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                   VALUES ('transfer_in', %s, %s, %s, %s)""",
                (to_account, amount, in_note, staff_name),
            )
        conn.commit()
    except pymysql.err.IntegrityError as e:  # duplicate ref / client_ref race
        conn.rollback()
        if client_ref and (e.args or [None])[0] == 1062:
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate transfer — client_ref '{client_ref}' was already posted",
            )
        logger.error(f"account transfer integrity error: {e}")
        raise HTTPException(status_code=500, detail="Transfer failed (integrity error)")
    except Exception as e:
        conn.rollback()
        logger.error(f"account transfer failed: {e}")
        raise HTTPException(status_code=500, detail=f"Transfer failed: {e}")
    finally:
        conn.close()

    new_bals = await _balances_by_name()
    logger.info("Account transfer %s: %s -> %s %s Ks by %s/%s",
                ref, from_account, to_account, amount, staff_name, _caller_identity(user))
    return {
        "success": True,
        "ref": ref,
        "from_account": from_account,
        "to_account": to_account,
        "amount": amount,
        "staff_name": staff_name,
        "balances": {
            from_account: round(new_bals.get(from_account, 0)),
            to_account: round(new_bals.get(to_account, 0)),
        },
    }


# ── GET /api/accounts/transfers ──────────────────────────────────────────────
@router.get("/transfers")
async def list_transfers(limit: int = Query(20, ge=1, le=200),
                         user: dict = Depends(get_staff_or_dashboard_user)):
    """Transfer history — admin sees all (incl. created_by_user), staff sees only their own."""
    _ensure_table()
    _admin = _is_admin(user)
    try:
        if _admin:
            rows = _q(
                "SELECT id, ref, from_account, to_account, amount, note, staff_name, "
                "created_by_user, created_at "
                "FROM account_transfers ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            total = int((_q("SELECT COUNT(*) AS c FROM account_transfers")[0] or {}).get("c") or 0)
        else:
            rows = _q(
                "SELECT id, ref, from_account, to_account, amount, note, staff_name, created_at "
                "FROM account_transfers WHERE created_by_user = %s ORDER BY id DESC LIMIT %s",
                (_caller_identity(user), limit),
            )
            total = int((_q("SELECT COUNT(*) AS c FROM account_transfers WHERE created_by_user = %s",
                            (_caller_identity(user),))[0] or {}).get("c") or 0)
        out = []
        for r in rows:
            item = {
                "id": r.get("id"),
                "ref": r.get("ref"),
                "from_account": r.get("from_account"),
                "to_account": r.get("to_account"),
                "amount": round(float(r.get("amount") or 0)),
                "note": r.get("note"),
                "staff_name": r.get("staff_name"),
                "created_at": _iso(r.get("created_at")),
            }
            if _admin:
                item["created_by_user"] = r.get("created_by_user")
            out.append(item)
        return {"success": True, "transfers": out, "count": len(out), "total": total}
    except Exception as e:
        logger.error(f"GET /accounts/transfers error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
