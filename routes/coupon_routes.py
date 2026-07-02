"""PS VIBE API — Coupon Routes."""
from fastapi import APIRouter, Depends, HTTPException
from mysql_db import query as _mysql_query, execute as _mysql_exec
from helpers import ok, error_response, rate_limit_10pm, rate_limit_5pm
from deps import verify_api_key
from models import GenericResponse
import logging, random, string
from datetime import datetime
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Coupons'])



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
        return error_response(message=str(e))


@router.post("/coupons/redeem", response_model=GenericResponse, tags=["Coupons"])
async def api_coupons_redeem(req: dict, auth=Depends(verify_api_key), _rl=Depends(rate_limit_5pm)):
    try:
        code = req.get("code", "").strip().upper()
        minutes_to_deduct = int(req.get("minutes", 0))
        
        if not code or minutes_to_deduct <= 0:
            return error_response(message="code and minutes required")
        
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
        
        _mysql_exec(
            "UPDATE member_coupons SET balance_minutes=%s, status=%s, redeemed_at=NOW() WHERE id=%s",
            (new_balance, new_status, coupon["id"]))
        
        return ok({
            "remaining_minutes": new_balance,
            "deducted_minutes": minutes_to_deduct,
            "status": new_status
        })
    except Exception as e:
        return error_response(message=str(e))
