"""PS VIBE API — Payment-account balances & account→account transfers (Staff App WAVE 5).

Owner requirements (Osmo 2026-09-19):
  * Account Balances view → ONLY "Main (ACM)" + "KBZ Bank", owner/admin only.
  * Account→account balance transfer, with role rules:
        staff  → Cash / WavePay / KPay / AYA Pay / MMQR - CB
        admin  → ALL accounts (also Main (ACM), KBZ Bank)

Endpoints (mounted under /api/accounts):
  GET  /api/accounts/transferable   — accounts the caller may move money between (+ balances)
  GET  /api/accounts/balances       — role-aware read-only balances (staff: 5 shop accts; admin: all 7, owner-only flagged)
  POST /api/accounts/transfer       — atomic 2-leg transfer (cash_movements transfer_out+transfer_in)
  GET  /api/accounts/transfers      — transfer history (admin: all, staff: own only)

Design notes
------------
* Balances come from the SAME calculator the dashboard/finance uses
  (routes.finance_routes.get_finance_balances) so an app balance == dashboard balance.
* The 'Wave'/'WavePay' merge lives inside that calculator and only ever affects the
  WavePay account — it cannot leak into Main (ACM) / KBZ Bank here.
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
# ACTIVE operational accounts every staff member may select/use (2026-09-28 Osmo:
# KPay / WavePay / AYA Pay are now INACTIVE — money stays visible to the owner,
# but staff can no longer select them).
_OPERATIONAL = ["Cash", "MMQR - CB"]
# Inactive accounts: balances remain visible + admin can still transfer, but staff
# cannot pick them in any payment/balance picker.
_INACTIVE_ACCOUNTS = ["KPay", "WavePay", "AYA Pay"]
# Owner-only accounts (never visible/usable to staff).
_OWNER_ONLY = ["Main (ACM)", "KBZ Bank"]
ALL_ACCOUNTS = _OPERATIONAL + _INACTIVE_ACCOUNTS + _OWNER_ONLY

# display label -> finance calculator account key (used for balance lookup)
_KEY_BY_NAME = {
    "Cash": "cash",
    "WavePay": "wave",
    "KPay": "kpay",
    "AYA Pay": "aya_pay",
    "MMQR - CB": "mmqr_cb",
    "Main (ACM)": "acm_acc",
    "KBZ Bank": "kbz_bank",
}

_MAX_AMOUNT = 999999999999  # cash_movements.amount is DECIMAL(12,0)

_table_ready = False


def _is_admin(user: dict) -> bool:
    return str((user or {}).get("role") or "").strip().lower() == "admin"


def _is_active_account(name: str) -> bool:
    return name not in _INACTIVE_ACCOUNTS


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


async def _account_rows_by_name() -> dict:
    """Return {label: {balance, fee_pct, fee_amount, net_balance, fee_note}} from
    the shared finance calculator (which now also emits the MMQR net-of-fee fields)."""
    from routes.finance_routes import get_finance_balances
    res = await get_finance_balances(year=None, month=None, user={"username": "accounts_api", "role": "admin"})
    out = {}
    if isinstance(res, dict) and res.get("success"):
        for a in res.get("accounts", []) or []:
            out[str(a.get("label"))] = {
                "balance": float(a.get("balance") or 0),
                "fee_pct": float(a.get("fee_pct") or 0),
                "fee_amount": float(a.get("fee_amount") or 0),
                "net_balance": float(a.get("net_balance") or 0),
                "fee_note": str(a.get("fee_note") or ""),
            }
    return out


CUSTODY_FILE = "/etc/psvibe/custody_balance.json"


def _custody_overrides() -> dict:
    """Balance overrides for owner-held custody accounts.

    PS VIBE derives account balances purely from cash_movements, so Main (ACM)
    shows the flow-net since June (15.94M) instead of the real custody balance
    (221,281,103 = capital + IC transfer + handoff). VIBE Group holds the
    authoritative figure; this file mirrors it for display.
    """
    try:
        import json as _json
        with open(CUSTODY_FILE) as fh:
            return {str(k): float(v) for k, v in (_json.load(fh).get("balances") or {}).items()}
    except Exception:
        return {}


async def _balances_by_name() -> dict:
    """Current numeric balance per account label, from the shared finance calculator."""
    meta = await _account_rows_by_name()
    out = {k: v["balance"] for k, v in meta.items()}
    out.update({k: v for k, v in _custody_overrides().items() if k in out})
    return out


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ── Daily account balance open/close (Staff App) ────────────────────────────
# Owner 2026-09-24: staff opens/closes the daily balance for the 5 shop accounts
# (Cash, WavePay, KPay, AYA Pay, MMQR - CB). Owner accounts (Main (ACM) + KBZ Bank) excluded.

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
                    expected_balance DECIMAL(12,2) DEFAULT NULL,
                    variance DECIMAL(12,2) DEFAULT NULL,
                    status ENUM('open','closed') NOT NULL DEFAULT 'open',
                    opened_by VARCHAR(100) DEFAULT NULL,
                    opened_at DATETIME DEFAULT NULL,
                    closed_by VARCHAR(100) DEFAULT NULL,
                    closed_at DATETIME DEFAULT NULL,
                    UNIQUE KEY uniq_date_account (balance_date, account)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # Migration for pre-existing installs: MySQL 8 has no
            # ADD COLUMN IF NOT EXISTS, so attempt each new column defensively.
            for _col in ("expected_balance", "variance"):
                try:
                    cur.execute(
                        f"ALTER TABLE daily_account_balances "
                        f"ADD COLUMN {_col} DECIMAL(12,2) DEFAULT NULL"
                    )
                except Exception:  # pragma: no cover - column already exists
                    pass
        conn.commit()
        _daily_bal_table_ready = True
    finally:
        conn.close()


def _prev_closing(account: str, date_str: str):
    """Most recent earlier CLOSING balance for an account (usual case: yesterday).

    Owner Osmo 2026-09-29: opening balance must be carried from the previous
    close instead of being re-typed by staff (that drift broke the daily numbers).
    Returns None when no earlier closed row exists (then the value must be typed).
    """
    try:
        r = _q(
            "SELECT closing_balance FROM daily_account_balances "
            "WHERE account=%s AND balance_date < %s AND status='closed' "
            "ORDER BY balance_date DESC LIMIT 1",
            (account, date_str),
        )
        if r:
            return float(r[0].get("closing_balance") or 0)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"prev_closing lookup failed for {account}: {e}")
    return None


@router.get("/daily-balances/today")
async def daily_balances_today(user: dict = Depends(get_staff_or_dashboard_user)):
    """Today's daily balance open/close state for the ACTIVE operational accounts.

    2026-09-28 Osmo: KPay / WavePay / AYA Pay are inactive — only Cash + MMQR - CB
    are auto-created/returned here. Historical (past-day) rows for the inactive
    accounts stay readable via /daily-balances/history; no rows are deleted.
    """
    _ensure_daily_balances_table()
    today = _mmt_today_str()
    for acc in _OPERATIONAL:
        if not _q("SELECT id FROM daily_account_balances WHERE balance_date=%s AND account=%s", (today, acc)):
            _ex("INSERT INTO daily_account_balances (balance_date, account, status) VALUES (%s,%s,'open')", (today, acc))
    rows = _q("SELECT * FROM daily_account_balances WHERE balance_date=%s ORDER BY id", (today,))
    # Only ACTIVE accounts participate in today's open/close ritual; inactive ones
    # (with rows already created earlier today) are excluded from this list but
    # remain in the DB (and in history).
    rows = [r for r in rows if r["account"] in _OPERATIONAL]
    bals = await _balances_by_name()
    out = []
    for r in rows:
        acc = r["account"]
        opening = float(r["opening_balance"] or 0)
        closing = float(r["closing_balance"] or 0)
        live = round(float(bals.get(acc, 0)))
        # Owner 2026-09-29: surface the carried opening + any mismatch so the
        # app can warn instead of silently accepting a wrong typed number.
        prev_c = _prev_closing(acc, today)
        # Variance = actually counted (closing) − recorded/system (live) balance.
        # Prefer the value frozen at close time; fall back to live for legacy rows.
        if r["status"] == "closed":
            stored_var = r.get("variance")
            variance = round(float(stored_var)) if stored_var is not None else round(closing - live)
        else:
            variance = None
        out.append({
            "account": acc,
            "status": r["status"],
            "active": _is_active_account(acc),
            "opening_balance": round(opening),
            "closing_balance": round(closing),
            "variance": variance,
            "live_balance": live,
            "prev_closing": None if prev_c is None else round(prev_c),
            "suggested_opening": None if prev_c is None else round(prev_c),
            "carried": bool(prev_c is not None and not opening),
            "opening_mismatch": (round(opening - prev_c) if (prev_c is not None and opening) else None),
            "can_reset": _is_admin(user),
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
        raise HTTPException(status_code=400, detail="Account must be one of Cash / MMQR - CB (KPay / WavePay / AYA Pay are inactive)")
    try:
        opening = float(body.get("opening_balance", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="opening_balance must be a number")
    opened_by = str(body.get("opened_by") or _caller_name(user) or user.get("username") or "").strip()
    now_str = _mmt_now_str()
    # Owner 2026-09-29 (1): carry yesterday's closing forward when no value is typed.
    prev = _prev_closing(account, today)
    if not body.get("opening_balance") and prev is not None:
        opening = float(prev)
    # Once per day: a second open is rejected for staff, but ADMINS may correct it.
    existing = _q("SELECT opened_at FROM daily_account_balances WHERE balance_date=%s AND account=%s", (today, account))
    warning = None
    if existing and existing[0].get("opened_at"):
        if not _is_admin(user):
            raise HTTPException(status_code=400, detail=f"{account} already opened today — ပြင်ရန် owner ကို ပြောပါ")
        # admin correction (2): overwrite the recorded opening.
    if prev is not None and abs(float(opening) - float(prev)) > 0.5:
        warning = f"မနေ့ closing {prev:,.0f} Ks နှင့် မကိုက် (ကွာ {float(opening) - float(prev):+,.0f} Ks)"
    _ex(
        """INSERT INTO daily_account_balances (balance_date, account, opening_balance, status, opened_by, opened_at)
           VALUES (%s,%s,%s,'open',%s,%s)
           ON DUPLICATE KEY UPDATE opening_balance=VALUES(opening_balance), status='open',
             opened_by=VALUES(opened_by), opened_at=VALUES(opened_at)""",
        (today, account, opening, opened_by, now_str),
    )
    msg = f"{account} opened with {opening:,.0f} Ks"
    if warning:
        msg += " ⚠️ " + warning
    return {"success": True, "message": msg, "warning": warning, "prev_closing": None if prev is None else round(prev)}


@router.post("/daily-balances/close")
async def daily_balances_close(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Set today's closing balance for an operational account."""
    _ensure_daily_balances_table()
    today = _mmt_today_str()
    account = str(body.get("account") or "").strip()
    if account not in _OPERATIONAL:
        raise HTTPException(status_code=400, detail="Account must be one of Cash / MMQR - CB (KPay / WavePay / AYA Pay are inactive)")
    try:
        closing = float(body.get("closing_balance", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="closing_balance must be a number")
    closed_by = str(body.get("closed_by") or _caller_name(user) or user.get("username") or "").strip()
    now_str = _mmt_now_str()
    row = _q("SELECT status FROM daily_account_balances WHERE balance_date=%s AND account=%s", (today, account))
    # Once per day for staff; admins may re-close to correct a wrong number (2).
    if row and (row[0].get("status") or "").lower() == "closed" and not _is_admin(user):
        raise HTTPException(status_code=400, detail=f"{account} already closed today — ပြင်ရန် owner ကို ပြောပါ")
    # Expected = the recorded/system (live) balance computed AT close time, so a closed
    # day's variance stays stable even as new cash movements land afterwards.
    bals = await _balances_by_name()
    expected = round(float(bals.get(account, 0)))
    variance = round(closing - expected)
    _ex(
        """INSERT INTO daily_account_balances (balance_date, account, closing_balance, status, closed_by, closed_at, expected_balance, variance)
           VALUES (%s,%s,%s,'closed',%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE closing_balance=VALUES(closing_balance), status='closed',
             closed_by=VALUES(closed_by), closed_at=VALUES(closed_at),
             expected_balance=VALUES(expected_balance), variance=VALUES(variance)""",
        (today, account, closing, closed_by, now_str, expected, variance),
    )
    return {"success": True, "message": f"{account} closed. Variance (Actual − Recorded) {variance:+,.0f} Ks", "variance": round(variance)}


@router.post("/daily-balances/reset")
async def daily_balances_reset(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """ADMIN only (2): wipe today's (or a given day's) open/close row so it can be redone.

    Owner Osmo 2026-09-29: 'မှားလိုက်ရင် ပြန်ပြင်လမ်း မရှိ' — this is that path.
    """
    _ensure_daily_balances_table()
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Owner (admin) only")
    account = str(body.get("account") or "").strip()
    day = str(body.get("date") or "").strip() or _mmt_today_str()
    if account not in _OPERATIONAL and not account:
        raise HTTPException(status_code=400, detail="account required")
    who = str(body.get("by") or _caller_name(user) or user.get("username") or "").strip()
    if account:
        _ex(
            """UPDATE daily_account_balances
               SET opening_balance=0, closing_balance=0, expected_balance=NULL, variance=NULL,
                   status='open', opened_by=NULL, opened_at=NULL, closed_by=NULL, closed_at=NULL
               WHERE balance_date=%s AND account=%s""",
            (day, account),
        )
        return {"success": True, "message": f"{account} ({day}) reset — open/close ပြန်လုပ်နိုင်ပါပြီ", "by": who}
    _ex(
        """UPDATE daily_account_balances
           SET opening_balance=0, closing_balance=0, expected_balance=NULL, variance=NULL,
               status='open', opened_by=NULL, opened_at=NULL, closed_by=NULL, closed_at=NULL
           WHERE balance_date=%s""",
        (day,),
    )
    return {"success": True, "message": f"{day} balances reset", "by": who}


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
        stored_var = r.get("variance")
        # Only return the frozen variance; legacy rows without a stored value
        # get null so the app renders "—" instead of a misleading number.
        variance = round(float(stored_var)) if stored_var is not None else None
        out.append({
            "date": str(r["balance_date"]),
            "account": r["account"],
            "status": r["status"],
            "opening_balance": round(opening),
            "closing_balance": round(closing),
            "variance": variance,
            "opened_by": r.get("opened_by") or "",
            "closed_by": r.get("closed_by") or "",
        })
    return {"success": True, "history": out}


# ── GET /api/accounts/transferable ───────────────────────────────────────────
@router.get("/transferable")
async def list_transferable(user: dict = Depends(get_staff_or_dashboard_user)):
    """Accounts the caller may transfer from/to, each with its current balance.

    Staff  → the 5 shop accounts (Cash, WavePay, KPay, AYA Pay, MMQR - CB).
    Admin  → all 7 accounts, with the two owner-only ones flagged (`owner_only`).
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
            "active": _is_active_account(name),
        }
        for name in allowed
    ]
    # Explicit per-account balances block (keyed by label) + list of owner-only ones.
    balances_block = {
        a["account"]: {"balance": a["balance"], "owner_only": a["owner_only"], "active": a["active"]}
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

    Staff  → the 5 shop accounts. Admin → all 7, with owner-only ones flagged.
    (The strictly owner-only Main (ACM) + KBZ Bank view is unchanged and remains
     available at GET /api/finance/account-balances.)
    """
    _ensure_table()
    role = "admin" if _is_admin(user) else "staff"
    allowed = _allowed_accounts(user)
    meta = await _account_rows_by_name()
    bals = {k: v["balance"] for k, v in meta.items()}
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
            "active": _is_active_account(name),
            "last_movement": last.get(name) if name in _OWNER_ONLY else None,
            "fee_pct": (meta.get(name) or {}).get("fee_pct", 0.0),
            "fee_amount": (meta.get(name) or {}).get("fee_amount", 0.0),
            "net_balance": (meta.get(name) or {}).get("net_balance", round(bals.get(name, 0), 2)),
            "fee_note": (meta.get(name) or {}).get("fee_note", ""),
        }
        for name in allowed
    ]
    return {
        "success": True,
        "role": role,
        "owner_only_view": role == "admin",
        "accounts": rows,
        "balances": {
            r["account"]: {"balance": r["balance"], "owner_only": r["owner_only"], "active": r["active"]}
            for r in rows
        },
        # kept for backward-compat with the old owner-only alias consumers
        "operating": [r for r in rows if r["account"] == "Main (ACM)"],
        "capital": [r for r in rows if r["account"] == "KBZ Bank"],
    }


# ── POST /api/accounts/adjust-balance (admin/owner only) ────────────────────
@router.post("/adjust-balance")
async def adjust_balance(body: dict = Body(...), user: dict = Depends(get_staff_or_dashboard_user)):
    """Owner/admin ABSOLUTE live-balance edit.

    Balances are DERIVED (finance_routes.get_finance_balances), never stored, so an
    "edit" MUST be an audited adjustment ledger row in cash_movements — never a raw
    UPDATE. Targeting an absolute value makes double-submit naturally idempotent.

    Money correctness (Osmo 2026-10-12):
      * delta = target − current; |delta| < 1 → nothing written (delta 0).
      * delta > 0 → inject; delta < 0 → eject (both stored positive).
      * category='owner_adjust' + note prefix 'Owner balance edit → …' NEVER match the
        calculator's subtraction patterns (inject category='topup', note LIKE 'Deposit #%'
        /'BK#%'; eject note LIKE '%deposit reverse%'/'Test deposit'/'[Refund]'/'Refund Deposit'),
        so the adjustment always lands exactly once.
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Owner (admin) only")

    account = str(body.get("account") or "").strip()
    if account not in ALL_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"Unknown account: {account}")

    raw_target = body.get("target_balance")
    if raw_target is None:
        raise HTTPException(status_code=400, detail="target_balance is required")
    try:
        target = float(raw_target)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="target_balance must be a number")
    if target != target:  # NaN
        raise HTTPException(status_code=400, detail="target_balance must be a number")
    if target < 0 or target > _MAX_AMOUNT:
        raise HTTPException(status_code=400, detail=f"target_balance out of range (0 .. {_MAX_AMOUNT:,})")
    target = round(target)

    reason = str(body.get("note") or "").strip()

    current = round(float((await _balances_by_name()).get(account, 0)))
    delta = round(target - current)
    if abs(delta) < 1:
        return {
            "success": True,
            "account": account,
            "old_balance": current,
            "new_balance": round(target),
            "delta": 0,
            "movement_id": None,
            "note": "no change — nothing written",
        }

    movement_type = "inject" if delta > 0 else "eject"
    amount = abs(delta)
    db_account = "Wave" if account == "WavePay" else account
    note = f"Owner balance edit → {round(target):,} Ks" + (f" · {reason}" if reason else "")
    staff_name = _caller_name(user) or user.get("username") or ""

    movement_id = _ex(
        """INSERT INTO cash_movements
           (movement_type, account, amount, note, staff_name, category, created_at)
           VALUES (%s, %s, %s, %s, %s, 'owner_adjust', NOW())""",
        (movement_type, db_account, amount, note, staff_name),
    )

    logger.info("Owner balance edit %s: %s → %s (%+d) by %s",
                account, current, round(target), delta, staff_name)
    return {
        "success": True,
        "account": account,
        "old_balance": current,
        "new_balance": round(target),
        "delta": delta,
        "movement_id": movement_id,
        "movement_type": movement_type,
        "amount": amount,
        "note": note,
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
            reason = "owner-only" if a in _OWNER_ONLY else "inactive"
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted for your role ({role}) — '{a}' is {reason}. "
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
