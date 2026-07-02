"""PS VIBE API — Webhook Routes."""
from fastapi import APIRouter, Depends, HTTPException
from helpers import ok, error_response
from deps import verify_webhook_secret
from booking_reminder import add_booking_reminder, cancel_booking_reminder
from models import GenericResponse
import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=['Webhooks'])

# ═══════════════════════════════════════
@router.post("/webhook/booking-reminder", response_model=GenericResponse, tags=["Webhooks"], summary="Receive booking data for 30-min advance reminder")
async def webhook_booking_reminder(req: dict, _auth=Depends(verify_webhook_secret)):
    """Webhook called by Sales Bot when a booking is confirmed.
    Schedules a 30-min-before reminder to STAFF_NOTIFY_CHAT via Telegram."""
    try:
        result = add_booking_reminder(req)
        if result == "ok":
            return ok({"message": "reminder scheduled"})
        return ok({"message": result})
    except Exception as e:
        logger.error(f"webhook/booking-reminder: {e}")
        return error_response(message=str(e))

@router.post("/webhook/booking-reminder/cancel", response_model=GenericResponse, tags=["Webhooks"], summary="Cancel a pending booking reminder")
async def webhook_booking_reminder_cancel(req: dict, _auth=Depends(verify_webhook_secret)):
    """Called when a booking is cancelled/rejected — removes any pending reminder."""
    try:
        bk_id = str(req.get("bk_id") or req.get("booking_id", ""))
        if not bk_id:
            return ok({"message": "missing bk_id"})
        cancelled = cancel_booking_reminder(bk_id)
        return ok({"message": "cancelled" if cancelled else "none pending"})
    except Exception as e:
        logger.error(f"webhook/booking-reminder/cancel: {e}")
        return error_response(message=str(e))


# ═══════════════════════════════════════
#  fetch_members
# ═══════════════════════════════════════
def _calc_member_liability_kyat(member_id):
    """Calculate FIFO-based liability in kyat for a member."""
    try:
        from fifo_wallet import fifo_calc
        tu = _mysql_query(
            "SELECT amount, mins_added FROM topup_log WHERE member_id=%s ORDER BY topup_date ASC",
            (member_id,)
        ) or []
        topups = [{"amount": float(t["amount"] or 0), "mins_added": float(t["mins_added"] or 0)} for t in tu]
        bw = _mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        bal = float(bw["balance_mins"]) if bw and bw.get("balance_mins") else 0
        fifo = fifo_calc(topups, bal)
        return int(fifo["liability"])
    except Exception:
        return 0  # non-critical: liability calc fallback

