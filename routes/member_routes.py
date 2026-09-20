"""PS VIBE API — Member Routes."""
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import JSONResponse
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec
from helpers import _mysql_get_setting, ok, error_response, rate_limit_30pm, rate_limit_120pm, _parse_pm_and_inject
from deps import verify_api_key
from models import GenericResponse
import staff_audit
import logging, random, string, os
from datetime import datetime
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=['Members'])


def _calc_rank(total_spend, master_thresh, immortal_thresh):
    """Compute rank from total_spend vs thresholds (mirrors bot get_member_rank)."""
    try:
        total_spend = int(float(total_spend or 0))
        master_thresh = int(float(master_thresh or 0))
        immortal_thresh = int(float(immortal_thresh or 0))
    except (ValueError, TypeError):
        total_spend = 0
        master_thresh = 300000
        immortal_thresh = 1000000
    if total_spend >= immortal_thresh:
        return "Immortal"
    if total_spend >= master_thresh:
        return "Master"
    return "Warrior"


def _calc_bonus_mins(rank, amount, bonus_table):
    """Compute bonus minutes from amount vs bonus_table (mirrors bot logic).

    bonus_table = list of [threshold, warrior, master, immortal];
    pick the row with the HIGHEST threshold <= amount, then take the member's rank column.
    """
    try:
        amount = int(float(amount or 0))
    except (ValueError, TypeError):
        amount = 0
    if amount <= 0:
        return 0
    # Normalize table rows
    rows = []
    if isinstance(bonus_table, list):
        for row in bonus_table:
            if isinstance(row, list) and len(row) >= 4:
                rows.append([int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)])
            elif isinstance(row, dict):
                rows.append([
                    int(row.get("threshold", 0) or 0),
                    int(row.get("warrior_bonus", 0) or 0),
                    int(row.get("master_bonus", 0) or 0),
                    int(row.get("immortal_bonus", 0) or 0),
                ])
    if not rows:
        return 0
    # Pick row with highest threshold <= amount
    chosen = None
    for row in rows:
        if row[0] <= amount:
            if chosen is None or row[0] > chosen[0]:
                chosen = row
    if chosen is None:
        return 0
    if rank == "Immortal":
        return chosen[3]
    if rank == "Master":
        return chosen[2]
    return chosen[1]


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

@router.get("/fetch_members", response_model=GenericResponse, tags=["Members"], summary="Fetch all members [MySQL]")
async def api_fetch_members(limit: int = 100, offset: int = 0, auth=Depends(verify_api_key), _rl=Depends(rate_limit_120pm)):
    """Fetch all members with name, phone, tier, and wallet balance (liability).
    Supports pagination via limit/offset query params (Fix #28). Default: limit=100, offset=0."""
    try:
        # Get total count for pagination metadata
        total = _mysql_query_one("SELECT COUNT(*) as cnt FROM member_wallets WHERE member_id IS NOT NULL AND member_id != ''")
        total_count = total["cnt"] if total else 0

        rows = _mysql_query(
            "SELECT member_id, member_name, phone, balance_mins, tier, total_spend, lifetime_spend "
            "FROM member_wallets WHERE member_id IS NOT NULL AND member_id != '' "
            "ORDER BY member_id LIMIT %s OFFSET %s",
            (limit, offset)
        )
        result = []
        for r in rows:
            result.append({
                "id": r["member_id"],
                "name": r.get("member_name") or r["member_id"],
                "phone": r.get("phone") or "",
                "tier": r.get("tier") or "",
                "liability_mins": int(r.get("balance_mins") or 0),
                "liability_kyat": _calc_member_liability_kyat(r["member_id"]),
                "wallet_balance": int(r.get("balance_mins") or 0)
            })
        return ok({
            "members": result,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total_count
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_data
# ═══════════════════════════════════════
@router.get("/fetch_member_data/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch member data [MySQL]")
async def api_fetch_member_data(member_id: str, auth=Depends(verify_api_key)):
    """Fetch consolidated member data (name, phone, rank, wallet) from MySQL."""
    try:
        row = _mysql_query_one(
            "SELECT member_id, member_name, phone, balance_mins, tier, total_spend, referral_code FROM member_wallets WHERE member_id=%s",
            (member_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Member not found")
        net_spend = int(row.get("total_spend", 0) or 0)
        # Compute rank from spend using thresholds
        try:
            mt = int(_mysql_get_setting("master_threshold", "300000"))
            it = int(_mysql_get_setting("immortal_threshold", "1000000"))
        except Exception:
            mt, it = 300000, 1000000
        if net_spend >= it:
            rank_raw = "Immortal"
        elif net_spend >= mt:
            rank_raw = "Master"
        else:
            rank_raw = "Warrior"
        return ok({
            "name": row.get("member_name", "-") or "-",
            "phone": row.get("phone", "-") or "-",
            "email": "",
            "net_spend": net_spend,
            "rank_raw": rank_raw,
            "wallet_mins": row.get("balance_mins"),
            "referral_code": row.get("referral_code", "") or "",
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_wallet_mins
# ═══════════════════════════════════════
@router.get("/fetch_wallet_mins/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch wallet minutes [MySQL]")
async def api_fetch_wallet_mins(member_id: str, auth=Depends(verify_api_key)):
    """Fetch wallet balance from MySQL."""
    try:
        rows = _mysql_query(
            "SELECT balance_mins FROM member_wallets WHERE member_id=%s",
            (member_id,))
        if not rows:
            return ok(0)
        return ok(rows[0].get("balance_mins", 0) or 0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_balance_mins (alias live read)
# ═══════════════════════════════════════
@router.get("/fetch_balance_mins/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch wallet balance live [MySQL]")
async def api_fetch_balance_mins(member_id: str, auth=Depends(verify_api_key)):
    """Fetch wallet balance in mins (live from MySQL)."""
    try:
        rows = _mysql_query("SELECT balance_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        if rows:
            return ok(rows[0]["balance_mins"] or 0)
        # Consistent with fetch_wallet_mins: return ok(0) for missing members instead
        # of 404 so bot-side callers always receive an int (avoids dict>=int TypeError).
        return ok(0)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_member_tier
# ═══════════════════════════════════════

#  wallet_deduct (after sale)
@router.post("/wallet/deduct", response_model=GenericResponse, tags=["Members"], summary="Deduct wallet balance after sale [MySQL]")
async def api_wallet_deduct(req: dict, auth=Depends(verify_api_key)):
    """Deduct wallet balance_mins after a gaming session sale."""
    try:
        member_id = req.get("member_id", "")
        deduct_mins = int(req.get("deduct_mins", 0))
        total_mins = int(req.get("total_mins", 0))

        if not member_id or deduct_mins <= 0:
            return error_response(message="member_id and deduct_mins > 0 required")

        rows = _mysql_query(
            "SELECT balance_mins, total_spend, total_bought_mins FROM member_wallets WHERE member_id=%s",
            (member_id,))
        if not rows:
            return error_response(message=f"Member {member_id} not found in wallet")

        cur = rows[0]
        old_bal = cur.get("balance_mins", 0) or 0
        new_bal = max(0, old_bal - deduct_mins)
        new_spend = (cur.get("total_spend", 0) or 0) + int(total_mins)

        _mysql_exec(
            "UPDATE member_wallets SET balance_mins=%s, total_spend=%s, last_updated=NOW() WHERE member_id=%s",
            (new_bal, new_spend, member_id))

        return ok({
            "success": True,
            "member_id": member_id,
            "balance_before": old_bal,
            "balance_after": new_bal,
            "deducted": deduct_mins,
            "total_spend": new_spend
        })
    except Exception as e:
        return error_response(message=str(e))

@router.get("/fetch_member_tier/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch member tier [MySQL]")
async def api_fetch_member_tier(member_id: str, auth=Depends(verify_api_key)):
    """Fetch member tier from MySQL member_wallets."""
    try:
        rows = _mysql_query("SELECT tier, total_spend FROM member_wallets WHERE member_id=%s", (member_id,))
        r = rows[0] if rows else {"tier": "Warrior", "total_spend": 0}
        tier = (r.get("tier") or "Warrior") if r else "Warrior"
        return ok(tier)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_staff / fetch_staff_names
# ═══════════════════════════════════════

# ═══════════════════════════════════════
@router.get("/fetch_member_effective_rate/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch member effective rate [MySQL]")
async def api_fetch_member_effective_rate(member_id: str, auth=Depends(verify_api_key)):
    """Fetch member effective rate from MySQL member_wallets."""
    try:
        rows = _mysql_query("SELECT effective_rate FROM member_wallets WHERE member_id=%s", (member_id,))
        rate = float(rows[0]["effective_rate"]) if rows and rows[0]["effective_rate"] else 1.0
        return ok({"member_id": member_id, "effective_rate": rate})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/update_member_effective_rate", response_model=GenericResponse, tags=["Members"], summary="Update member effective rate [MySQL]")
async def api_update_member_effective_rate(req: dict, auth=Depends(verify_api_key)):
    """Update member effective rate in MySQL."""
    try:
        mid = req.get("member_id", "")
        rate = req.get("effective_rate", 1.0)
        _mysql_exec("UPDATE member_wallets SET effective_rate=%s WHERE member_id=%s", (rate, mid))
        return ok({"member_id": mid, "effective_rate": rate, "updated": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  build_member_rate_dict
# ═══════════════════════════════════════
@router.get("/build_member_rate_dict", response_model=GenericResponse, tags=["Members"], summary="Build member rate dict [MySQL]")
async def api_build_member_rate_dict(auth=Depends(verify_api_key)):
    """Build dict of member_id to effective rate from MySQL."""
    try:
        rows = _mysql_query("SELECT member_id, effective_rate FROM member_wallets WHERE effective_rate IS NOT NULL")
        rate_dict = {r["member_id"]: float(r["effective_rate"] or 1.0) for r in rows}
        return ok({"rate_dict": rate_dict, "count": len(rate_dict)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════
#  fetch_base_salaries
# ═══════════════════════════════════════

# ═══════════════════════════════════════
@router.get("/fetch_referral_code/{member_id}", response_model=GenericResponse, tags=["Members"], summary="Fetch referral code [MySQL]")
async def api_fetch_referral_code(member_id: str, auth=Depends(verify_api_key)):
    """Fetch referral code from MySQL."""
    try:
        rows = _mysql_query("SELECT referral_code FROM member_wallets WHERE member_id=%s", (member_id,))
        code = rows[0]["referral_code"] if rows and rows[0]["referral_code"] else ""
        return ok({"member_id": member_id, "referral_code": code})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save_referral_code", response_model=GenericResponse, tags=["Members"], summary="Save referral code [MySQL]")
async def api_save_referral_code(req: dict, auth=Depends(verify_api_key)):
    """Save referral code to MySQL. Checks uniqueness before saving."""
    try:
        mid = req.get("member_id", "")
        code = req.get("referral_code", "") or req.get("code", "") or req.get("referralCode", "")
        
        # If code is not empty, check uniqueness
        if code:
            dup = _mysql_query(
                "SELECT member_id FROM member_wallets WHERE referral_code=%s AND member_id!=%s",
                (code, mid),
            )
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"Code already assigned to {dup[0]['member_id']}",
                )
        
        _mysql_exec("UPDATE member_wallets SET referral_code=%s WHERE member_id=%s", (code, mid))
        return ok({"member_id": mid, "referral_code": code})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@router.post("/topup/log", response_model=GenericResponse, tags=["Topup"], summary="Log a top-up transaction and update wallet")
async def api_topup_log(req: dict, auth=Depends(verify_api_key)) -> JSONResponse:
    """Log a top-up transaction into topup_log table + update member wallet."""
    try:
        member_id = req.get("member_id", "")
        amount = req.get("amount", 0)
        amount = float(amount or 0)
        mins_added = req.get("mins_added", 0)
        staff = req.get("staff_name") or req.get("staff", "")
        client_ref = req.get("client_ref") or None

        # ── Payment method resolution ────────────────────────────────
        # Prefer direct payment_method split string; else build from kpay/cash/aya fields.
        pm = req.get("payment_method")
        if not pm:
            kpay = req.get("kpay", 0)
            cash = req.get("cash", 0)
            aya = req.get("aya", 0)
            pm = f"Kpay:{kpay}/Cash:{cash}/AYA:{aya}"

        # ── Auto-calc base + bonus if mins_added missing ─────────────
        base_mins = None
        bonus_mins = None
        tier = None
        if not mins_added and amount > 0:
            try:
                base_rate = _mysql_get_setting("base_rate", 0)
                bonus_table = _mysql_get_setting("bonus_table", [])
                master_thresh = int(_mysql_get_setting("master_threshold", "300000"))
                immortal_thresh = int(_mysql_get_setting("immortal_threshold", "1000000"))
                cur = _mysql_query_one(
                    "SELECT total_spend FROM member_wallets WHERE member_id=%s",
                    (member_id,)
                )
                total_spend = (cur["total_spend"] if cur else 0) or 0
                tier = _calc_rank(total_spend, master_thresh, immortal_thresh)
                base_rate = int(float(base_rate or 0))
                if base_rate > 0:
                    base_mins = round((amount * 60) / base_rate)
                else:
                    base_mins = int(amount)
                bonus_mins = _calc_bonus_mins(tier, amount, bonus_table)
                mins_added = base_mins + bonus_mins
                logger.info(
                    "Topup auto-calc: member=%s amount=%s rank=%s base=%s bonus=%s mins=%s",
                    member_id, amount, tier, base_mins, bonus_mins, mins_added,
                )
            except Exception as _e:
                logger.warning("Topup auto-calc failed (%s) — falling back to amount", _e)
                base_mins = int(amount)
                bonus_mins = 0
                tier = None
                mins_added = int(amount)
        else:
            mins_added = int(mins_added or 0)

        logger.info("Topup: member=%s amount=%s mins=%s", member_id, amount, mins_added)

        # ── Idempotency guard (client_ref) ────────────────────────────────
        if client_ref:
            existing = _mysql_query_one(
                "SELECT balance_mins_after FROM topup_log WHERE client_ref=%s",
                (client_ref,)
            )
            if existing:
                cur_bal = _mysql_query_one(
                    "SELECT balance_mins FROM member_wallets WHERE member_id=%s",
                    (member_id,)
                )
                return ok({"success": True, "duplicate": True,
                          "balance_mins": (cur_bal["balance_mins"] if cur_bal else 0)})

        current = _mysql_query_one(
            "SELECT balance_mins, total_bought_mins, total_spend FROM member_wallets WHERE member_id=%s",
            (member_id,))
        bal_before = current["balance_mins"] if current else 0
        bal_after = bal_before + mins_added
        bought = (current["total_bought_mins"] if current else 0) + mins_added
        new_spend = (float(current["total_spend"]) if current and current.get("total_spend") is not None else 0.0) + amount

        _mysql_exec(
            "INSERT INTO topup_log (member_id, amount, mins_added, topup_date, staff_name, payment_method, balance_before, balance_after, balance_mins_before, balance_mins_after, client_ref) VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s)",
            (member_id, amount, mins_added, staff, pm, amount, amount, bal_before, bal_after, client_ref))

        _mysql_exec(
            "INSERT INTO member_wallets (member_id, balance_mins, total_bought_mins, total_spend) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE balance_mins=%s, total_bought_mins=%s, total_spend=%s, last_updated=NOW()",
            (member_id, bal_after, bought, new_spend, bal_after, bought, new_spend))

        # 🆕 Record cash_movements for top-up income
        if float(amount) > 0:
            try:
                note = f"Topup {member_id} + {mins_added} mins"
                if base_mins is not None:
                    note = f"Topup {mins_added} mins (base {base_mins} + bonus {bonus_mins})"
                _parse_pm_and_inject(amount, pm, note, staff, category='topup')
            except Exception as _e:
                logger.warning("Topup cash_movements failed: %s", _e)
        # ✅ Also create sales_daily record for topup
        if float(amount) > 0:
            try:
                note = f"Topup {mins_added} mins"
                if base_mins is not None:
                    note = f"Topup {mins_added} mins (base {base_mins} + bonus {bonus_mins})"
                _mysql_exec(
                    "INSERT INTO sales_daily (sale_date, member_id, amount, gross, net, payment_method, notes, staff_name, customer_name, is_topup) "
                    "VALUES (CURDATE(), %s, %s, %s, %s, %s, %s, %s, %s, 1)",
                    (member_id, amount, amount, amount, pm, note, staff, staff))
            except Exception as _e:
                logger.warning("Topup sales_daily failed: %s", _e)

        resp = {"success": True, "balance_mins": bal_after, "total_spend": new_spend, "mins_added": mins_added}
        if base_mins is not None:
            resp["base_mins"] = base_mins
            resp["bonus_mins"] = bonus_mins
            resp["tier"] = tier
        return ok(resp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
#  MUTATION — receipt (bot push)
# ================================================================

@router.post("/members/register", response_model=GenericResponse, tags=["Members"], summary="Register a new member into MySQL")
async def api_member_register(req: dict, request: Request, auth=Depends(verify_api_key)):
    """Register a new member. Writes to member_wallets and topup_log.

    Gift / free card is PIN-gated: an authenticated staff/dashboard user must also
    present the owner PIN (``STOCK_PIN``, the same secret the sale bot uses for
    ``BTN_NM_GIFT``). Without the correct PIN the request is rejected with 403.
    The acting staff (JWT) is recorded for audit.
    """
    try:
        member_id = req.get("member_id", "")
        name = req.get("name", "")
        phone = req.get("phone", "")
        staff = req.get("staff", "")
        email = req.get("email", "")
        initial_mins = req.get("initial_mins", 0)
        amount = req.get("amount", 0)
        kpay = req.get("kpay", 0)
        cash = req.get("cash", 0)
        mins_added = req.get("mins_added", initial_mins)
        referral_code = req.get("referral_code", "")
        # Gift / free card request (PIN-gated).
        _is_gift_req = bool(req.get("is_gift") or req.get("gift") or req.get("free_card"))

        if not staff:
            try:
                staff = staff_audit.identity_from_request(request, req)
            except Exception:
                staff = ""

        # Input validation — never create an empty/garbage member row (Staff App Wave 1).
        if not str(member_id or "").strip():
            return error_response(message="member_id is required — call /api/next_member_id first")
        if not str(name or "").strip():
            return error_response(message="member name is required")

        # ── Gift / free card PIN gate ─────────────────────────────────────
        # Decision (Wave 2): the owner PIN is the authorisation — any authenticated
        # staff OR dashboard user who presents the correct STOCK_PIN may issue a gift
        # card (mirrors the sale-bot behaviour). Missing/wrong PIN -> 403.
        if _is_gift_req:
            _pin_expected = (os.environ.get("STOCK_PIN") or "").strip()
            _pin_given = str(req.get("pin") or req.get("gift_pin") or "").strip()
            if not _pin_expected:
                return error_response(message="Gift card PIN is not configured on the server", code=500)
            if not _pin_given or _pin_given != _pin_expected:
                raise HTTPException(status_code=403, detail="Invalid owner PIN — gift/free card not allowed")
            # A gift card has no cash amount; minutes are granted free.
            amount = 0
            kpay = 0
            cash = 0

        logger.info("Registering member: %s (%s) mins=%s gift=%s staff=%s", member_id, name, initial_mins, _is_gift_req, staff)

        # Upsert into member_wallets
        _mysql_exec(
            "INSERT INTO member_wallets (member_id, member_name, phone, email, balance_mins, total_bought_mins, tier, total_spend, reg_staff) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE member_name=VALUES(member_name), phone=VALUES(phone), email=COALESCE(VALUES(email), email)",
            (member_id, name, phone, (email or None), initial_mins, mins_added, "Warrior", 0, (staff or None)))

        # 🆕 Record cash_movements for member registration income
        if float(amount) > 0:
            try:
                _parse_pm_and_inject(amount, f"KPay:{kpay}/Cash:{cash}", f"New member {member_id} + {mins_added} mins", staff, category='topup')
            except Exception as _e:
                logger.warning("Reg cash_movements failed: %s", _e)
        # ✅ Also create sales_daily record for registration
        if float(amount) > 0:
            try:
                _mysql_exec(
                    "INSERT INTO sales_daily (sale_date, member_id, amount, gross, net, payment_method, notes, staff_name, customer_name, is_topup) "
                    "VALUES (CURDATE(), %s, %s, %s, %s, %s, %s, %s, %s, 1)",
                    (member_id, amount, amount, amount, f"KPay:{kpay}/Cash:{cash}", f"New member {member_id} + {mins_added} mins", staff, staff))
            except Exception as _e:
                logger.warning("Reg sales_daily failed: %s", _e)

        # Log into topup_log
        if mins_added > 0:
            _pm = "Gift" if _is_gift_req else f"KPay:{kpay}/Cash:{cash}"
            _notes = f"Gift/Free card issued by {staff}" if _is_gift_req else None
            _mysql_exec(
                "INSERT INTO topup_log (member_id, amount, mins_added, topup_date, staff_name, payment_method, balance_mins_before, balance_mins_after, notes) "
                "VALUES (%s,%s,%s,NOW(),%s,%s,0,%s,%s)",
                (member_id, amount, mins_added, staff, _pm, initial_mins, _notes))

        logger.info("Member registered: %s gift=%s", member_id, _is_gift_req)
        return ok({"status": "ok", "member_id": member_id})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#  MUTATION — save_receipt_json
# ═══════════════════════════════════════



@router.post("/member/wallet/update", response_model=GenericResponse, tags=["Members"], summary="Update member wallet balance [MySQL]")
async def api_member_wallet_update(req: dict, auth=Depends(verify_api_key)):
    """Update member wallet balance_mins, total_spend, and optional bonus_mins."""
    try:
        member_id = req.get("member_id", "")
        balance_mins = req.get("balance_mins")
        total_spend = req.get("total_spend")
        bonus_mins = req.get("bonus_mins")
        deduct_mins = req.get("deduct_mins")

        if not member_id:
            return error_response(message="member_id required")

        # Build dynamic UPDATE
        updates = []
        params = []

        if balance_mins is not None:
            updates.append("balance_mins=%s")
            params.append(int(balance_mins))
        if deduct_mins is not None:
            updates.append("balance_mins = balance_mins - %s")
            params.append(int(deduct_mins))
        if total_spend is not None:
            updates.append("total_spend=%s")
            params.append(float(total_spend))
        if bonus_mins is not None:
            # Bonus mins: add to total_bought_mins and balance_mins
            updates.append("total_bought_mins = total_bought_mins + %s")
            updates.append("balance_mins = balance_mins + %s")
            params.append(int(bonus_mins))
            params.append(int(bonus_mins))

        if updates:
            updates.append("last_updated=NOW()")
            params.append(member_id)
            _mysql_exec(
                f"UPDATE member_wallets SET {', '.join(updates)} WHERE member_id=%s",
                tuple(params))

        # Read back current state
        row = _mysql_query_one(
            "SELECT balance_mins, total_spend, total_bought_mins FROM member_wallets WHERE member_id=%s",
            (member_id,))

        return ok({
            "member_id": member_id,
            "balance_mins": row["balance_mins"] if row else 0,
            "total_spend": row["total_spend"] if row else 0,
            "total_bought_mins": row["total_bought_mins"] if row else 0,
            "success": True
        })
    except Exception as e:
        logger.error(f"api_member_wallet_update: {e}")
        return error_response(str(e))


# ── OPEX / Operating Expenses ─────────────────────────────────────────────────

@router.post("/member/wallet/update", response_model=GenericResponse, tags=["Members"], summary="Update member wallet (deduct mins / add bonus)")
async def api_wallet_update(req: dict, auth=Depends(verify_api_key)):
    """Deduct or add bonus minutes to a member's wallet."""
    try:
        member_id = req.get("member_id", "")
        deduct_mins = int(req.get("deduct_mins", 0))
        bonus_mins = int(req.get("bonus_mins", 0))
        total_mins = int(req.get("total_mins", 0))

        if not member_id:
            return ok({"success": False, "error": "member_id required"})

        # Get current wallet
        cur = _mysql_query_one("SELECT balance_mins FROM member_wallets WHERE member_id=%s", (member_id,))
        if not cur:
            return ok({"success": False, "error": f"Member {member_id} not found"})

        current_bal = int(cur["balance_mins"] or 0)
        new_bal = current_bal

        if deduct_mins > 0:
            new_bal = max(0, current_bal - deduct_mins)
            _mysql_exec("UPDATE member_wallets SET balance_mins=%s, last_updated=NOW() WHERE member_id=%s", (new_bal, member_id))
            logger.info("Wallet deduct: %s -%d mins (bal: %d→%d)", member_id, deduct_mins, current_bal, new_bal)
        elif bonus_mins > 0:
            new_bal = current_bal + bonus_mins
            _mysql_exec("UPDATE member_wallets SET balance_mins=%s, last_updated=NOW() WHERE member_id=%s", (new_bal, member_id))
            logger.info("Wallet bonus: %s +%d mins (bal: %d→%d)", member_id, bonus_mins, current_bal, new_bal)

        return ok({
            "success": True,
            "member_id": member_id,
            "balance_before": current_bal,
            "deduct_mins": deduct_mins,
            "bonus_mins": bonus_mins,
            "balance_after": new_bal,
        })
    except Exception as e:
        logger.exception("wallet/update error")
        raise HTTPException(status_code=500, detail=str(e))



# ── n8n Reverse Proxy (HTTP middleware, path preserved) ──
async def n8n_middleware(request, call_next):
    if request.url.path.startswith("/n8n") or request.url.path == "/favicon.ico":
        import httpx as _httpx
        from starlette.responses import StreamingResponse as _StreamingResponse

        # Rewrite n8n HTML: prefix asset paths with /n8n/ so they get
        # proxied back to n8n through the /n8n/ middleware handler
        # (without this, assets at /assets/* conflict with dashboard)
        if request.url.path == "/n8n/" or request.url.path == "/n8n":
            target = f"http://localhost:5678{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            body = await request.body()
            try:
                async with _httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.request(
                        method=request.method,
                        url=target,
                        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                        content=body or None,
                    )
                    # Read full HTML body
                    html = b""
                    async for chunk in resp.aiter_bytes():
                        html += chunk
                    # Rewrite asset paths: /assets/ -> /n8n/assets/ etc.
                    text = html.decode("utf-8", errors="replace")
                    text = text.replace('src="/assets/', 'src="/n8n/assets/')
                    text = text.replace('href="/assets/', 'href="/n8n/assets/')
                    text = text.replace('src="/static/', 'src="/n8n/static/')
                    text = text.replace('href="/static/', 'href="/n8n/static/')
                    text = text.replace('href="/favicon.ico"', 'href="/n8n/favicon.ico"')
                    text = text.replace("window.BASE_PATH = '/';", "window.BASE_PATH = '/n8n/',")
                    # Strip headers that cause garbled output (gzip already decoded)
                    clean_headers = {k: v for k, v in resp.headers.items()
                                     if k.lower() not in ("content-encoding", "content-length",
                                                          "transfer-encoding", "content-security-policy",
                                                          "strict-transport-security")}
                    # Prevent Cloudflare from auto-minifying/transforming n8n HTML
                    clean_headers["Cache-Control"] = "no-cache, no-store, must-revalidate, proxy-revalidate, no-transform"
                    clean_headers["Cloudflare-No-Transform"] = "1" 
                    return _StreamingResponse(
                        [text.encode("utf-8")],
                        status_code=resp.status_code,
                        headers=clean_headers,
                    )
            except Exception as e:
                from starlette.responses import JSONResponse
                return JSONResponse({"success": False, "error": f"n8n proxy: {e}"}, status_code=502)

        target = f"http://localhost:5678{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        body = await request.body()
        try:
            async with _httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                    content=body or None,
                )
                return _StreamingResponse(
                    resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("content-encoding", "content-length",
                                                  "transfer-encoding", "content-security-policy",
                                                  "strict-transport-security")},
                )
        except Exception as e:
            from starlette.responses import JSONResponse
            return JSONResponse({"success": False, "error": f"n8n proxy: {e}"}, status_code=502)

    return await call_next(request)


# n8n assets proxy — handles /n8n/assets/, /n8n/static/, /n8n/favicon.ico
# (called when n8n's rewritten HTML references these paths)
async def n8n_assets_middleware(request, call_next):
    if request.url.path.startswith("/n8n/assets/") or request.url.path.startswith("/n8n/static/") or request.url.path == "/n8n/favicon.ico":
        import httpx as _httpx
        from starlette.responses import StreamingResponse as _StreamingResponse

        # Override base-path.js to use /n8n/ prefix for API calls
        if request.url.path == "/n8n/static/base-path.js":
            from starlette.responses import Response as _N8nResp
            return _N8nResp(content="window.BASE_PATH = '/n8n/';", media_type="text/javascript")

        # Strip /n8n prefix, forward remainder to n8n
        internal_path = request.url.path[4:]  # /n8n/assets/... -> /assets/...
        target = f"http://localhost:5678{internal_path}"
        if request.url.query:
            target += f"?{request.url.query}"
        body = await request.body()
        try:
            async with _httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                    content=body or None,
                )
                return _StreamingResponse(
                    resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("content-encoding", "content-length",
                                                  "transfer-encoding", "content-security-policy",
                                                  "strict-transport-security")},
                )
        except Exception as e:
            from starlette.responses import JSONResponse
            return JSONResponse({"success": False, "error": f"n8n assets proxy: {e}"}, status_code=502)

    return await call_next(request)



