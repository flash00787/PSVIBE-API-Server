"""
PS VIBE Data Digest — FastAPI Routes
=====================================
API endpoints for running data integrity checks and generating daily digests.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Query, HTTPException

from digest_engine import (
    run_all_checks,
    generate_daily_digest,
    load_digest_history,
    list_digest_history,
    _CHECK_FUNCTIONS,
)

logger = logging.getLogger("psvibe_digest")

router = APIRouter(prefix="/api/digest", tags=["Data Digest"])


@router.get("/check")
async def digest_check(date: str = Query(None, description="Date filter YYYY-MM-DD (default: today)")):
    """
    Run all data integrity checks.

    Returns a combined report with status per check category:
      - wallet_balance: balance_mins = bought - used
      - topup_chain: sequential balance consistency
      - sales_vs_bookings: cross-reference sales with bookings
      - inventory: stock_in - stock_out = inventory.quantity
      - receipts_vs_sales: receipt count matches sales count
      - negative_sales: suspicious zero/negative amounts
      - booking_status: no overlaps, no stuck sessions, no time inversions
    """
    try:
        report = run_all_checks(date=date)
        return {"success": True, "data": report}
    except Exception as e:
        logger.error("digest_check error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily")
async def digest_daily(date: str = Query(None, description="Date filter YYYY-MM-DD (default: today)")):
    """
    Generate daily business digest.

    Returns:
      - Total sales (count, gross, net)
      - Total topups (count, amount, mins_added)
      - Active/completed sessions
      - Food sales
      - New members
      - Top consoles by hours
      - Top games by sessions
      - Revenue by payment method
      - Alerts and recommendations
    """
    try:
        digest = generate_daily_digest(date=date)
        return {"success": True, "data": digest}
    except Exception as e:
        logger.error("digest_daily error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check/{check_name}")
async def digest_single_check(
    check_name: str,
    date: str = Query(None, description="Date filter YYYY-MM-DD (optional, not all checks use it)")
):
    """
    Run a single data integrity check by name.

    Available checks:
      - wallet_balance
      - topup_chain
      - sales_vs_bookings
      - inventory
      - receipts_vs_sales
      - negative_sales
      - booking_status
    """
    if check_name not in _CHECK_FUNCTIONS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown check '{check_name}'. Available: {list(_CHECK_FUNCTIONS.keys())}"
        )

    try:
        func = _CHECK_FUNCTIONS[check_name]
        # booking_status doesn't accept date parameter
        if check_name == "booking_status":
            result = func()
        else:
            result = func(date=date)
        return {"success": True, "data": {"check": check_name, "result": result}}
    except Exception as e:
        logger.error("digest_single_check(%s) error: %s", check_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def digest_history(days: int = Query(7, description="Number of days to look back (default: 7)")):
    """
    Get digest history for the last N days.

    Returns list of available check/daily history files with metadata.
    """
    try:
        files = list_digest_history(days=days)
        return {"success": True, "data": {"days": days, "files": files, "count": len(files)}}
    except Exception as e:
        logger.error("digest_history error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{kind}/{date}")
async def digest_history_detail(
    kind: str,
    date: str,
):
    """
    Load a specific digest history file by kind and date.

    kind: 'check' or 'daily'
    date: YYYY-MM-DD
    """
    if kind not in ("check", "daily"):
        raise HTTPException(status_code=400, detail="kind must be 'check' or 'daily'")

    try:
        data = load_digest_history(date, kind=kind)
        if data is None:
            raise HTTPException(status_code=404, detail=f"No {kind} history found for {date}")
        return {"success": True, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("digest_history_detail error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
