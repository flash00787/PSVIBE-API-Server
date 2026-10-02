"""PS VIBE API — Daily deposit/session reconciliation (in-app, 2026-09-26).

Owner order: put the daily deposit/session reconciliation INSIDE the Staff App so
staff/owner can see (and act on) it without waiting for the nightly alert.

This mirrors the read-only detector ``/root/coordination/psvibe_deposit_reconcile.py``
(cron 00:10 MMT) with the SAME rule so the app and the nightly job always agree:

    console_bookings with deposit_amount > 0
      AND deposit_status IN ('verified','paid')
      AND no deposit_redemptions row
      AND (booking done OR a done console_session exists)

Endpoints
---------
GET  /api/reconciliation/deposits               → outstanding list (read-only)
POST /api/reconciliation/deposits/{id}/deduct   → guarded one-shot repair (single tx)

The deduct endpoint reproduces the standard manual-repair convention:
    net            = max(0, net - deposit)
    payment_method = payment_method + "|<Method>:<amt> (dep)"
    notes          = "DepositRedeemed: <amt> | " + notes
    console_bookings.deposit_status = 'redeemed' + deposit_redeemed_at
    insert deposit_redemptions (source 'app_repair')
all in ONE transaction.  A voucher is only ever touched when it can be resolved to
EXACTLY ONE candidate — otherwise we return a clear error and change nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

from auth import get_staff_or_dashboard_user
from deps import verify_api_key
from helpers import ok, error_response
from mysql_db import query as _q, query_one as _q1, get_connection as _conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])

MMT = timezone(timedelta(hours=6, minutes=30))

_DEP_LABELS = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "cash": "Cash"}

# The SAME detection rule as the nightly detector (kept verbatim).
_LIST_SQL = """
SELECT cb.id AS booking_id, cb.console_id, cb.customer_name, cb.member_id,
       cb.status AS booking_status, cb.deposit_status, cb.deposit_amount,
       cb.deposit_method, cb.booking_date,
       (SELECT cs.actual_end FROM console_sessions cs WHERE cs.booking_id = cb.id
         ORDER BY cs.id DESC LIMIT 1) AS session_end,
       (SELECT cs2.status FROM console_sessions cs2 WHERE cs2.booking_id = cb.id
         ORDER BY cs2.id DESC LIMIT 1) AS session_status
FROM console_bookings cb
WHERE cb.deposit_amount > 0
  AND cb.deposit_status IN ('verified','paid')
  -- 2026-09-30 depfix: never list deposits of cancelled/rejected/no-show bookings
  AND cb.status NOT IN ('cancelled','rejected','no_show')
  AND NOT EXISTS (SELECT 1 FROM deposit_redemptions dr WHERE dr.booking_id = cb.id)
  AND (cb.status = 'done'
       OR EXISTS (SELECT 1 FROM console_sessions c3 WHERE c3.booking_id = cb.id AND c3.status = 'done'))
ORDER BY cb.id DESC
"""

# How wide the "sale created at session end" window is (minutes) for voucher resolution.
_WINDOW_MIN = 20


def _to_int(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _age_hours(session_end):
    """Hours since the session ended (MMT now). None when the timestamp is unparsable."""
    if not session_end:
        return None
    try:
        if isinstance(session_end, datetime):
            dt = session_end
        else:
            dt = datetime.strptime(str(session_end)[:19], "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MMT)
        now = datetime.now(MMT)
        return max(0, round((now - dt).total_seconds() / 3600, 1))
    except Exception:
        return None


@router.get("/deposits")
async def reconciliation_deposits(request: Request, auth=Depends(verify_api_key)):
    """Outstanding (unredeemed) deposits whose session has ended. Read-only."""
    request.headers.get("Cache-Control")  # no-op; keep no-store below via response not used
    try:
        rows = _q(_LIST_SQL)
        items = []
        total = 0
        for r in rows:
            amt = _to_int(r.get("deposit_amount"))
            total += amt
            items.append({
                "booking_id": r.get("booking_id"),
                "console_id": r.get("console_id"),
                "customer_name": r.get("customer_name") or "",
                "member_id": r.get("member_id") or "",
                "deposit_status": r.get("deposit_status"),
                "deposit_amount": amt,
                "deposit_method": r.get("deposit_method"),
                "booking_date": str(r.get("booking_date")) if r.get("booking_date") else None,
                "session_end": str(r.get("session_end")) if r.get("session_end") else None,
                "session_status": r.get("session_status"),
                "age_hours": _age_hours(r.get("session_end")),
            })
        return ok({"count": len(items), "total": total, "items": items})
    except Exception as e:
        logger.error("GET /reconciliation/deposits error: %s", e)
        return error_response(str(e))


def _booking_row(booking_id: int):
    return _q1(
        "SELECT id, console_id, customer_name, member_id, deposit_status, deposit_amount, "
        "deposit_method, booking_date, status AS booking_status FROM console_bookings WHERE id=%s",
        (booking_id,),
    )


def _already_redeemed(booking_id: int) -> bool:
    row = _q1("SELECT id FROM deposit_redemptions WHERE booking_id=%s LIMIT 1", (booking_id,))
    return row is not None


def _resolve_voucher(booking):
    """Resolve the sale voucher to deduct against, or (None, reason).

    Priority:
      1. sales_daily.session_id → console_sessions.booking_id == this booking
         (a DB-enforced, unambiguous link).  Exactly one required.
      2. a sale created on the same console within ±_WINDOW_MIN of the session's
         actual_end.  Exactly one candidate required, and it must be a plausible
         voucher (net >= deposit, and either the payment_method already carries the
         deposit amount or the customer name matches the booking's).

    Any zero/ambiguous result returns ``(None, reason)`` and changes nothing.
    """
    booking_id = booking["id"]
    console_id = booking.get("console_id")
    deposit = _to_int(booking.get("deposit_amount"))
    method = (booking.get("deposit_method") or "").lower()

    # Tier 1 — session_id link.
    rows = _q(
        "SELECT sd.id, sd.voucher_no, sd.client_ref, sd.staff_name, sd.net, sd.payment_method, sd.notes "
        "FROM sales_daily sd JOIN console_sessions cs ON cs.id = sd.session_id "
        "WHERE cs.booking_id = %s ORDER BY sd.id DESC",
        (booking_id,),
    )
    if len(rows) == 1:
        return rows[0], None
    if len(rows) > 1:
        return None, "voucher မတွေ့/မသေချာ — manual စစ်ပါ"

    # Tier 2 — session-end window on the same console.
    ses = _q1(
        "SELECT actual_end FROM console_sessions WHERE booking_id=%s ORDER BY id DESC LIMIT 1",
        (booking_id,),
    )
    session_end = (ses or {}).get("actual_end") if ses else None
    if not session_end or not console_id:
        return None, "voucher မတွေ့/မသေချာ — manual စစ်ပါ"

    rows = _q(
        "SELECT id, voucher_no, client_ref, staff_name, net, payment_method, notes, customer_name "
        "FROM sales_daily WHERE console_id = %s "
        "AND created_at BETWEEN (%s - INTERVAL %s MINUTE) AND (%s + INTERVAL %s MINUTE) "
        "ORDER BY ABS(TIMESTAMPDIFF(SECOND, created_at, %s)), id",
        (console_id, session_end, _WINDOW_MIN, session_end, _WINDOW_MIN, session_end),
    )
    if len(rows) != 1:
        if not rows:
            return None, "voucher မတွေ့/မသေချာ — manual စစ်ပါ"
        return None, "voucher မတွေ့/မသေချာ — manual စစ်ပါ"

    cand = rows[0]
    net = _to_int(cand.get("net"))
    if net < deposit:
        return None, "voucher မတွေ့/မသေချာ — manual စစ်ပါ"

    # Corroboration: the payment string carries the deposit amount, or the names match.
    pm = (cand.get("payment_method") or "")
    pm_has_amt = str(deposit) in pm
    bk_name = (booking.get("customer_name") or "").strip().lower()
    cand_name = (cand.get("customer_name") or "").strip().lower()
    name_match = bool(bk_name and cand_name and bk_name == cand_name)
    if not (pm_has_amt or name_match):
        return None, "voucher မတွေ့/မသေချာ — manual စစ်ပါ"

    return cand, None


@router.post("/deposits/{booking_id}/deduct")
async def reconciliation_deduct(
    booking_id: int,
    request: Request,
    auth=Depends(verify_api_key),
    user=Depends(get_staff_or_dashboard_user),
):
    """Guarded one-shot deposit redemption against the resolved voucher (single tx)."""
    try:
        booking = _booking_row(booking_id)
        if not booking:
            return error_response("Booking not found", code=404)

        status = booking.get("deposit_status")
        deposit = _to_int(booking.get("deposit_amount"))
        method = (booking.get("deposit_method") or "").lower()

        # Never double-redeem: existing audit row OR already flipped → no-op.
        if _already_redeemed(booking_id):
            return ok({"booking_id": booking_id, "already": True,
                       "message": "Already redeemed — nothing to do"})
        if status not in ("verified", "paid") or deposit <= 0:
            return error_response(
                "This booking has no redeemable deposit (status=%s)" % status, code=409)

        voucher, reason = _resolve_voucher(booking)
        if voucher is None:
            return error_response(reason or "voucher မတွေ့/မသေချာ — manual စစ်ပါ", code=409)

        sale_id = voucher["id"]
        sale_ref = voucher.get("client_ref") or voucher.get("voucher_no") or None
        voucher_no = voucher.get("voucher_no") or None
        sale_staff = voucher.get("staff_name") or ""
        net_before = _to_int(voucher.get("net"))
        net_after = max(0, net_before - deposit)
        label = _DEP_LABELS.get(method, "KPay")
        marker = "%s:%d (dep)" % (label, deposit)
        staff_name = str((user or {}).get("username") or "").strip() or sale_staff

        conn = _conn()
        try:
            with conn.cursor() as cur:
                # 1) money maths on the voucher
                cur.execute(
                    "UPDATE sales_daily SET net=%s, "
                    "payment_method = CONCAT(IFNULL(payment_method,''), %s), "
                    "notes = CONCAT(%s, IFNULL(notes,'')) "
                    "WHERE id=%s",
                    (net_after, "|" + marker, "DepositRedeemed: %d | " % deposit, sale_id),
                )
                # 2) flip the booking (guarded single-winner transition)
                cur.execute(
                    "UPDATE console_bookings SET deposit_status='redeemed', deposit_redeemed_at=NOW() "
                    "WHERE id=%s AND deposit_status IN ('verified','paid') AND deposit_amount>0",
                    (booking_id,),
                )
                flipped = cur.rowcount
                # 3) audit row (idempotency key booking_id + sale_ref)
                cur.execute(
                    "INSERT INTO deposit_redemptions (booking_id, sale_ref, voucher_no, amount, method, staff_name, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'app_repair')",
                    (booking_id, sale_ref, voucher_no, deposit, method or "kpay", staff_name),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error("deduct tx failed BK#%s: %s", booking_id, e)
            return error_response("Deduct failed — nothing changed: %s" % e, code=500)
        finally:
            conn.close()

        if not flipped:
            return ok({"booking_id": booking_id, "already": True,
                       "message": "Deposit was already redeemed by another action"})

        logger.info(
            "Reconciliation deduct BK#%s voucher=%s sale=%s -%d Ks (net %d->%d) staff=%s source=app_repair",
            booking_id, voucher_no, sale_id, deposit, net_before, net_after, staff_name,
        )
        return ok({
            "booking_id": booking_id,
            "voucher_no": voucher_no,
            "deposit_amount": deposit,
            "method": method,
            "net_before": net_before,
            "net_after": net_after,
            "already": False,
        })
    except Exception as e:
        logger.error("POST /reconciliation/deposits/%s/deduct error: %s", booking_id, e)
        return error_response(str(e), code=500)
