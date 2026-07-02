"""PS VIBE API — Promotion Routes."""
from fastapi import APIRouter, Depends
from mysql_db import query as _mysql_query, execute as _mysql_exec
from deps import verify_api_key

router = APIRouter(prefix="/api", tags=["Promotions"])


@router.get("/promotions")
async def get_promotions(active_only: bool = False, auth=Depends(verify_api_key)):
    """List all promotions."""
    rows = _mysql_query("SELECT * FROM promotions ORDER BY id DESC")
    return {"success": True, "promotions": rows}


@router.get("/promotions/log")
async def get_promotions_log(limit: int = 50, auth=Depends(verify_api_key)):
    """Get promotion usage log."""
    rows = _mysql_query(
        "SELECT * FROM promotions_log ORDER BY id DESC LIMIT %s", (limit,)
    )
    return {"success": True, "log": rows}


@router.post("/promotions/log")
async def add_promotion_log(data: dict, auth=Depends(verify_api_key)):
    """Record promotion usage."""
    _mysql_exec(
        "INSERT INTO promotions_log (voucher_no, promo_id, promo_title, member_id, "
        "console_id, gross_total, discount_amt, net_total, staff_name, promo_date) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            data.get("voucher_no", ""), data.get("promo_id", ""),
            data.get("promo_title", ""), data.get("member_id", ""),
            data.get("console_id", ""), data.get("gross_total", 0),
            data.get("discount_amt", 0), data.get("net_total", 0),
            data.get("staff_name", ""), data.get("promo_date", ""),
        ),
    )
    return {"success": True, "message": "Logged"}
