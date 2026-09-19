"""PS VIBE API — Payment-account balances & account→account transfers (Staff App WAVE 5).

Owner requirements (Osmo 2026-09-19):
  * Account Balances view → ONLY "ACM's Acc" + "KBZ Bank", owner/admin only.
  * Account→account balance transfer, with role rules:
        staff  → Cash / WavePay / KPay / AYA Pay
        admin  → ALL accounts (also ACM's Acc, KBZ Bank)

Endpoints (mounted under /api/accounts):
  GET  /api/accounts/transferable   — accounts the caller may move money between (+ balances)
  GET  /api/accounts/balances       — owner-only restricted balances (ACM's Acc + KBZ Bank)  [alias]
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
"""
import logging
import random
import string
from datetime import datetime, timezone

import pymysql
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from auth import get_staff_or_dashboard_user
from mysql_db import get_connection as _get_conn
from mysql_db import query as _q

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


# ── GET /api/accounts/transferable ───────────────────────────────────────────
@router.get("/transferable")
async def list_transferable(user: dict = Depends(get_staff_or_dashboard_user)):
    """Accounts the caller may transfer from/to, each with its current balance."""
    _ensure_table()
    role = "admin" if _is_admin(user) else "staff"
    allowed = _allowed_accounts(user)
    bals = await _balances_by_name()
    return {
        "success": True,
        "role": role,
        "accounts": [
            {
                "account": name,
                "label": name,
                "balance": round(bals.get(name, 0)),
                "owner_only": name in _OWNER_ONLY,
            }
            for name in allowed
        ],
    }


# ── GET /api/accounts/balances (owner-only alias) ────────────────────────────
@router.get("/balances")
async def owner_balances(user: dict = Depends(get_staff_or_dashboard_user)):
    """Owner-only restricted balances: ACM's Acc + KBZ Bank only (403 for staff)."""
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Owner-only — admin access required")
    from routes.finance_routes import get_finance_balances
    res = await get_finance_balances(year=None, month=None, user={"username": "accounts_api", "role": "admin"})
    accounts = (res.get("accounts") or []) if isinstance(res, dict) else []
    last = {r["account"]: _iso(r.get("last_movement")) for r in _q(
        "SELECT account, MAX(created_at) AS last_movement FROM cash_movements "
        "WHERE account IN (%s, %s) GROUP BY account",
        tuple(_OWNER_ONLY),
    )}
    rows = []
    for name in _OWNER_ONLY:
        bal = 0.0
        for a in accounts:
            if str(a.get("label")) == name:
                bal = float(a.get("balance") or 0)
                break
        rows.append({"account": name, "label": name, "balance": round(bal),
                     "last_movement": last.get(name)})
    operating = [r for r in rows if r["account"] == "ACM's Acc"]
    capital = [r for r in rows if r["account"] == "KBZ Bank"]
    return {"success": True, "owner_only": True, "accounts": rows,
            "operating": operating, "capital": capital}


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
            cur.execute(
                """INSERT INTO cash_movements (movement_type, account, amount, note, staff_name)
                   VALUES ('transfer_out', %s, %s, %s, %s)""",
                (from_account, amount, out_note, staff_name),
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
    """Transfer history — admin sees all, staff sees only their own."""
    _ensure_table()
    try:
        if _is_admin(user):
            rows = _q(
                "SELECT id, ref, from_account, to_account, amount, note, staff_name, created_at "
                "FROM account_transfers ORDER BY id DESC LIMIT %s",
                (limit,),
            )
        else:
            rows = _q(
                "SELECT id, ref, from_account, to_account, amount, note, staff_name, created_at "
                "FROM account_transfers WHERE created_by_user = %s ORDER BY id DESC LIMIT %s",
                (_caller_identity(user), limit),
            )
        out = [{
            "id": r.get("id"),
            "ref": r.get("ref"),
            "from_account": r.get("from_account"),
            "to_account": r.get("to_account"),
            "amount": round(float(r.get("amount") or 0)),
            "note": r.get("note"),
            "staff_name": r.get("staff_name"),
            "created_at": _iso(r.get("created_at")),
        } for r in rows]
        return {"success": True, "transfers": out, "count": len(out)}
    except Exception as e:
        logger.error(f"GET /accounts/transfers error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
