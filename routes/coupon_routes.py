"""PS VIBE API — Coupon Routes."""
from fastapi import APIRouter, Depends, HTTPException, Request
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec, delete_rows as _mysql_rowcount
from helpers import ok, error_response, rate_limit_10pm, rate_limit_5pm
from deps import verify_api_key
import staff_audit
from models import GenericResponse
import logging, random, string
from datetime import datetime
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Coupons'])

# Idempotent CREATE for the coupon redemption audit/idempotency log (Staff App Wave 1).
# UNIQUE(coupon_code, sale_ref) makes a repeated redeem for the same sale a no-op.
_redemptions_ready = False


def _ensure_redemptions_table() -> None:
    global _redemptions_ready
    if _redemptions_ready:
        return
    try:
        _mysql_exec(
            """
            CREATE TABLE IF NOT EXISTS coupon_redemptions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                branch_id INT NOT NULL DEFAULT 1,
                coupon_id INT NOT NULL,
                coupon_code VARCHAR(20) NOT NULL,
                sale_ref VARCHAR(64) NULL,
                minutes_deducted INT NOT NULL DEFAULT 0,
                balance_after INT NOT NULL DEFAULT 0,
                staff_name VARCHAR(100) NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_coupon_sale (coupon_code, sale_ref),
                KEY idx_coupon (coupon_id)
            )
            """
        )
        _redemptions_ready = True
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"coupon_redemptions table init failed: {e}")



@router.post("/coupons/generate", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_generate(req: dict, auth=Depends(verify_api_key)):
    try:
        member_id = req.get("member_id", "")
        session_minutes = int(req.get("session_minutes", 0))
        session_id = req.get("session_id", 0)
        
        if not member_id or session_minutes <= 0:
            return error_response(message="member_id and session_minutes required")
        
        # Check active promotion
        rows = _mysql_query("SELECT id, coupon_expiry_date FROM promotions WHERE promo_type='cashback_coupon' AND start_date <= CURDATE() AND end_date >= CURDATE() LIMIT 1")
        if not rows:
            return ok({"coupon": None, "message": "No active promotion"})
        
        promo = rows[0]
        promo_id = promo["id"]
        expiry_date = str(promo["coupon_expiry_date"]) + " 23:59:59"
        
        # Generate unique code: CB + 6 random alphanumeric chars
        code = "CB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        # Check uniqueness
        existing = _mysql_query("SELECT id FROM member_coupons WHERE coupon_code=%s", (code,))
        retries = 0
        while existing and retries < 5:
            code = "CB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            existing = _mysql_query("SELECT id FROM member_coupons WHERE coupon_code=%s", (code,))
            retries += 1
        
        bk_id = _mysql_exec(
            "INSERT INTO member_coupons (coupon_code, member_id, original_minutes, balance_minutes, issued_date, expiry_date, status, promo_id, source_session_id) VALUES (%s, %s, %s, %s, NOW(), %s, 'active', %s, %s)",
            (code, member_id, session_minutes, session_minutes, expiry_date, promo_id, session_id)
        )
        
        return ok({"coupon": {
            "id": bk_id,
            "code": code,
            "member_id": member_id,
            "minutes": session_minutes,
            "expiry": expiry_date
        }})
    except Exception as e:
        return error_response(message=str(e))


@router.get("/coupons/list", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_list(member_id: str, auth=Depends(verify_api_key)):
    try:
        rows = _mysql_query(
            "SELECT id, coupon_code, original_minutes, balance_minutes, issued_date, expiry_date, status FROM member_coupons WHERE member_id=%s ORDER BY issued_date DESC",
            (member_id,))
        coupons = []
        for r in rows:
            coupons.append({
                "id": r["id"],
                "code": r["coupon_code"],
                "original_minutes": r["original_minutes"],
                "balance_minutes": r["balance_minutes"],
                "issued_date": str(r["issued_date"]),
                "expiry_date": str(r["expiry_date"]),
                "status": r["status"]
            })
        return ok({"coupons": coupons})
    except Exception as e:
        return error_response(message=str(e))


@router.post("/coupons/validate", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_validate(req: dict, auth=Depends(verify_api_key), _rl=Depends(rate_limit_10pm)):
    try:
        code = req.get("code", "").strip().upper()
        if not code:
            return error_response(message="coupon code required")
        
        rows = _mysql_query(
            "SELECT id, coupon_code, member_id, original_minutes, balance_minutes, expiry_date, status FROM member_coupons WHERE coupon_code=%s",
            (code,))
        if not rows:
            return error_response(message="Coupon not found")
        
        coupon = rows[0]
        if coupon["status"] != "active":
            return error_response(message=f"Coupon status: {coupon['status']}")
        if coupon["expiry_date"] and coupon["expiry_date"] < datetime.now():
            return error_response(message="Coupon has expired")
        if coupon["balance_minutes"] <= 0:
            return error_response(message="Coupon has no remaining balance")
        
        return ok({"coupon": {
            "id": coupon["id"],
            "code": coupon["coupon_code"],
            "member_id": coupon["member_id"],
            "balance_minutes": coupon["balance_minutes"],
            "expiry_date": str(coupon["expiry_date"])
        }})
    except Exception as e:
        logger.error("POST /coupons/validate error: %s", e)
        return error_response(message="Could not validate coupon. Please try again.")


@router.post("/coupons/redeem", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_redeem(req: dict, request: Request, auth=Depends(verify_api_key), _rl=Depends(rate_limit_5pm)):
    """Redeem coupon minutes. Idempotent per sale when ``sale_ref``/``voucher_no`` is
    supplied (Staff App): a retry for the same sale never double-deducts. The acting
    staff (JWT) is stored on every redemption for the audit trail."""
    try:
        code = str(req.get("code", "")).strip().upper()
        try:
            minutes_to_deduct = int(req.get("minutes", 0))
        except (ValueError, TypeError):
            return error_response(message="minutes must be a whole number")

        if not code or minutes_to_deduct <= 0:
            return error_response(message="code and minutes required")

        # Per-sale idempotency key (Staff App) + audit identity.
        sale_ref = str(req.get("sale_ref") or req.get("voucher_no") or req.get("voucher_id")
                       or req.get("booking_id") or "").strip() or None
        staff_name = staff_audit.identity_from_request(request, req)
        _ensure_redemptions_table()

        # Fast path: this sale already redeemed this coupon -> replay stored result.
        if sale_ref:
            prev = _mysql_query_one(
                "SELECT minutes_deducted, balance_after FROM coupon_redemptions "
                "WHERE coupon_code=%s AND sale_ref=%s", (code, sale_ref))
            if prev:
                bal = int(prev["balance_after"] or 0)
                return ok({"remaining_minutes": bal, "deducted_minutes": int(prev["minutes_deducted"] or 0),
                           "status": "used" if bal <= 0 else "active", "idempotent": True})

        rows = _mysql_query("SELECT id, balance_minutes, status, expiry_date FROM member_coupons WHERE coupon_code=%s", (code,))
        if not rows:
            return error_response(message="Coupon not found")

        coupon = rows[0]
        if coupon["status"] != "active":
            return error_response(message=f"Coupon status: {coupon['status']}")
        if coupon["balance_minutes"] < minutes_to_deduct:
            return error_response(message=f"Insufficient balance. Available: {coupon['balance_minutes']} mins")
        if coupon["expiry_date"] and coupon["expiry_date"] < datetime.now():
            return error_response(message="Coupon has expired")

        new_balance = coupon["balance_minutes"] - minutes_to_deduct
        new_status = "used" if new_balance <= 0 else "active"

        # Claim the idempotency key FIRST (atomic vs a concurrent duplicate for the same sale).
        redemption_id = None
        if sale_ref:
            try:
                redemption_id = _mysql_exec(
                    "INSERT INTO coupon_redemptions (coupon_id, coupon_code, sale_ref, minutes_deducted, balance_after, staff_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (coupon["id"], code, sale_ref, minutes_to_deduct, new_balance, staff_name or None))
            except Exception:
                prev = _mysql_query_one(
                    "SELECT minutes_deducted, balance_after FROM coupon_redemptions "
                    "WHERE coupon_code=%s AND sale_ref=%s", (code, sale_ref))
                if prev:
                    bal = int(prev["balance_after"] or 0)
                    return ok({"remaining_minutes": bal, "deducted_minutes": int(prev["minutes_deducted"] or 0),
                               "status": "used" if bal <= 0 else "active", "idempotent": True})
                return error_response(message="Coupon redeem conflict — please retry")

        # Guarded deduct: only if still active and still has enough balance.
        affected = _mysql_rowcount(
            "UPDATE member_coupons SET balance_minutes=balance_minutes-%s, "
            "status=CASE WHEN balance_minutes-%s<=0 THEN 'used' ELSE 'active' END, redeemed_at=NOW() "
            "WHERE id=%s AND status='active' AND balance_minutes>=%s",
            (minutes_to_deduct, minutes_to_deduct, coupon["id"], minutes_to_deduct))
        if not affected:
            if redemption_id:
                _mysql_exec("DELETE FROM coupon_redemptions WHERE id=%s", (redemption_id,))
            return error_response(message="Coupon changed — please retry")

        return ok({
            "remaining_minutes": new_balance,
            "deducted_minutes": minutes_to_deduct,
            "status": new_status,
            "idempotent": False,
        })
    except Exception as e:
        logger.error("POST /coupons/redeem error: %s", e)
        return error_response(message="Coupon redeem failed. Please try again.")
