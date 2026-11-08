"""PS VIBE API — Single source of truth for sale money maths (Staff App Wave 2).

Both ``POST /api/promotions/calc`` (preview) and ``POST /api/sales/record`` (commit)
call :func:`compute_totals` so the discount / net the app previews is the exact
discount / net that gets charged.  A second helper family owns the *deposit
redemption* commit path so a verified booking deposit can be applied to a sale
**exactly once** (idempotent on a stable sale ref), never via a parallel/double path.

Design notes
------------
* ``compute_totals`` is a PURE function — it reads promotions + coupons but never
  writes.  Mirrors the sale-bot promo engine (``bot/handlers/discount.py``).
* Deposit redemption is committed through :func:`commit_deposit_redemption`,
  which is guarded by the booking's ``deposit_status='verified'`` transition
  (single winner) *and* by the ``deposit_redemptions`` audit table keyed on
  ``(booking_id, sale_ref)`` (idempotent replay).
"""
import logging
from datetime import datetime, date, time as _dtime, timedelta, timezone
from typing import Any, Dict, Optional

from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_exec

# MMT = UTC+06:30 (Asia/Yangon)
_MMT = timezone(timedelta(hours=6, minutes=30))

logger = logging.getLogger("psvibe_api.sale_money")

_DEP_LABELS = {"kpay": "KPay", "wavepay": "WavePay", "aya_pay": "AYA Pay", "cash": "Cash"}

_redemptions_ready = False


# ────────────────────────────────────────────────────────────────────────────
#  Types helpers
# ────────────────────────────────────────────────────────────────────────────
def _to_int(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_naive_mmt(start_mmt):
    """Normalise a session start to a naive datetime in MMT wall-clock.

    Accepts an aware/naive datetime, a date, or an ISO-ish string.  DB
    ``console_sessions.actual_start`` is stored as MMT wall-clock (naive), so a
    naive input is returned unchanged.
    """
    if start_mmt is None:
        return None
    if isinstance(start_mmt, datetime):
        dt = start_mmt
    elif isinstance(start_mmt, date):
        dt = datetime(start_mmt.year, start_mmt.month, start_mmt.day)
    elif isinstance(start_mmt, str):
        s = start_mmt.strip()
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_MMT).replace(tzinfo=None)
    return dt


def _to_time(v):
    """Normalise a TIME value (pymysql returns timedelta) to datetime.time."""
    if v is None:
        return None
    if isinstance(v, _dtime):
        return v
    if isinstance(v, timedelta):
        total = int(v.total_seconds()) % 86400
        return _dtime(total // 3600, (total % 3600) // 60, total % 60)
    if isinstance(v, datetime):
        return v.time()
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                continue
        return None
    return None


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()[:10]
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ────────────────────────────────────────────────────────────────────────────
#  Weekday Happy Hour "2+1" duration promotion (owner Osmo, 2026-10-08)
# ────────────────────────────────────────────────────────────────────────────
def billable_minutes(T, charge=120, cycle=180):
    """Billable minutes for the 2+1 tier on total playtime ``T``.

    ``billable(T) = charge * floor(T/cycle) + min(T mod cycle, charge)``.
    The customer pays for ``billable`` minutes; ``free = T - billable``.
    Pure function — no I/O.

    120→120 · 180→120 · 181→121 · 240→180 · 300→240 · 360→240 · 420→300 · 480→360
    """
    T = _to_int(T)
    charge = _to_int(charge) or 120
    cycle = _to_int(cycle) or 180
    if T <= 0 or charge <= 0 or cycle <= 0:
        return 0
    return charge * (T // cycle) + min(T % cycle, charge)


def duration_promo_applies(start_mmt, promo_row, ref_now=None):
    """Predicate: does the duration_2plus1 promo apply to a session start?

    Conditions (all must hold):
      * ``promo_row`` present and ``status == 'active'``
      * ``applies_auto`` truthy (auto-bind only — never a manual picker pick)
      * weekday bit set (``weekday_mask``, Mon=bit1 … Sun=bit7; Mon–Fri = 62)
      * ``time_window_start <= start.time() < time_window_end`` (upper EXCLUSIVE)
      * ``start_date <= start.date() <= end_date`` (promo period, either NULL = open)

    ``start_mmt`` is the session start (MMT). Pure — no I/O.
    """
    if not promo_row:
        return False
    if str(promo_row.get("status") or "").strip().lower() != "active":
        return False
    if not int(promo_row.get("applies_auto") or 0):
        return False
    dt = _to_naive_mmt(start_mmt)
    if dt is None:
        return False

    mask = int(promo_row.get("weekday_mask") or 0)
    if mask:
        bit = 1 << dt.isoweekday()  # Mon=1 → 2 … Sun=7 → 128
        if not (mask & bit):
            return False

    t = dt.time()
    tw_start = _to_time(promo_row.get("time_window_start"))
    tw_end = _to_time(promo_row.get("time_window_end"))
    if tw_start is not None and t < tw_start:
        return False
    if tw_end is not None and t >= tw_end:
        return False

    d = dt.date()
    sd = _to_date(promo_row.get("start_date"))
    ed = _to_date(promo_row.get("end_date"))
    if sd is not None and d < sd:
        return False
    if ed is not None and d > ed:
        return False
    return True


def compute_duration_2plus1(session_start, session_duration, promo_row):
    """Return the 2+1 re-pricing dict, or None when the promo does not apply.

    ``{promo_id, title, total_mins, billable_mins, free_mins, cash_mins, wallet_mins}``
    where ``cash_mins``/``wallet_mins`` default to a fully-cash re-pricing (the
    checkout hook overrides them once it knows the member wallet balance).
    """
    if not promo_row or not duration_promo_applies(session_start, promo_row):
        return None
    T = _to_int(session_duration)
    if T <= 0:
        return None
    charge = _to_int(promo_row.get("tier_charge_mins")) or 120
    cycle = _to_int(promo_row.get("tier_cycle_mins")) or 180
    billable = billable_minutes(T, charge, cycle)
    free = max(0, T - billable)
    title = str(promo_row.get("promo_name") or promo_row.get("name") or "2+1 Happy Hour").strip()
    return {
        "promo_id": promo_row.get("id"),
        "title": title,
        "total_mins": T,
        "billable_mins": billable,
        "free_mins": free,
        "cash_mins": billable,
        "wallet_mins": 0,
    }


def price_duration_2plus1(billable_mins, base_rate, multiplier, wallet_balance=None):
    """Price a "2+1" checkout **exactly** like a normal session of ``billable_mins``.

    The promo only changes the MINUTES (billable vs free) — it NEVER changes the
    rate.  So a C-09 (multiplier 1.2) 3h session (billable 2h) costs
    2 × 12,000 = 24,000 Ks, not 20,000.

    ``wallet_balance`` is the member's wallet balance in SCALED minutes
    (``None`` for walk-in/guest).  The wallet is deducted in SCALED minutes;
    any uncovered billable minutes fall back to cash at the same scaled rate.

    Pure function — no I/O.

    Returns::
        {
            "cash_ks": int,          # cash amount for the cash portion (Ks)
            "wallet_mins": int,      # SCALED minutes deducted from the wallet
            "cash_mins": float,      # UNSCALED billable minutes paid in cash
            "scaled_cash_mins": int, # SCALED cash minutes (cash_mins × multiplier)
        }
    """
    B = int(round(billable_mins))
    rate = float(base_rate) or 0.0
    mult = float(multiplier) or 1.0
    scaled_billable = round(B * mult)
    if wallet_balance is None:
        wallet_mins = 0
        cash_mins = float(B)
    else:
        bal = int(round(float(wallet_balance)))
        wallet_mins = min(scaled_billable, bal) if bal > 0 else 0
        cash_mins = max(0.0, B - (wallet_mins / mult))
    cash_ks = round(cash_mins * rate * mult / 60)
    return {
        "cash_ks": int(cash_ks),
        "wallet_mins": int(wallet_mins),
        "cash_mins": cash_mins,
        "scaled_cash_mins": int(round(cash_mins * mult)),
    }


def load_active_duration_promo():
    """Load the singleton active ``duration_2plus1`` promo (in date window).

    Auto-apply only — the caller re-checks ``applies_auto`` via
    :func:`duration_promo_applies`. Returns None when none is active.
    """
    try:
        return _mysql_query_one(
            "SELECT * FROM promotions WHERE promo_type='duration_2plus1' AND status='active' "
            "AND (start_date IS NULL OR start_date <= CURDATE()) "
            "AND (end_date IS NULL OR end_date >= CURDATE()) "
            "ORDER BY id DESC LIMIT 1"
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("load_active_duration_promo failed: %s", e)
        return None


# ────────────────────────────────────────────────────────────────────────────
#  Money engine (promo + coupon + FOC + manual discount)
# ────────────────────────────────────────────────────────────────────────────
def compute_totals(req: Dict[str, Any]) -> Dict[str, Any]:
    """Compute gross / cash_payable / discounts / net_total for a cart.

    Body keys (all optional except an amount): ``game_amt``, ``food_total``,
    ``gross_total``, ``is_member``, ``wallet_mins``, ``effective_cost_mins``,
    ``base_rate``, ``multiplier``, ``promo_id``, ``coupon_code``,
    ``manual_discount``, ``food_items``:[{name,qty,subtotal}], ``foc_item``.
    """
    game_amt = _to_int(req.get("game_amt"))
    food_total = _to_int(req.get("food_total"))
    gross_total_in = _to_int(req.get("gross_total"))
    is_member = bool(req.get("is_member"))
    wallet_mins = _to_int(req.get("wallet_mins"))
    eff_mins = _to_int(req.get("effective_cost_mins") or req.get("remin_mins"))
    base_rate = _to_float(req.get("base_rate"), 500.0) or 500.0
    multiplier = _to_float(req.get("multiplier"), 1.0) or 1.0
    manual_discount = _to_int(req.get("manual_discount"))
    food_items = req.get("food_items") or []

    # ── Duration 2+1 (Weekday Happy Hour) — re-pricing, NOT a Ks discount ──
    # The checkout hook computes billable/free minutes and passes them here via
    # req["duration_2plus1"]; the amount then derives from BILLABLE minutes only
    # (free minutes are on the house). total_discount stays 0 for this promo.
    d2p = req.get("duration_2plus1") or {}
    d2p_billable = 0
    d2p_free = 0
    d2p_total = 0
    d2p_title = ""
    if isinstance(d2p, dict) and _to_int(d2p.get("billable_mins")) > 0:
        d2p_total = _to_int(d2p.get("total_mins"))
        d2p_billable = _to_int(d2p.get("billable_mins"))
        d2p_free = _to_int(d2p.get("free_mins"))
        d2p_title = str(d2p.get("title") or "").strip()
        # cash portion = minutes billed in cash (B for walk-in, 0 if wallet covers,
        # shortfall when wallet is insufficient); wallet portion = billable minutes
        # covered by the member wallet (SCALED minutes, matching effective_cost_mins).
        # The promo only changes the MINUTES, never the rate — so the console
        # multiplier MUST be applied to the UNSCALED cash minutes here.
        d2p_cash_mins = float(d2p.get("cash_mins")) if d2p.get("cash_mins") is not None else float(d2p_billable)
        d2p_wallet_mins = _to_int(d2p.get("wallet_mins"))
        game_amt = _to_int(d2p_cash_mins * base_rate * multiplier / 60)
        eff_mins = d2p_wallet_mins if d2p_wallet_mins > 0 else 0

    # ── wallet vs cash session (mirror discount.py) ──────────────────────
    is_wallet_session = bool(is_member and wallet_mins > 0)
    wallet_game_value = 0
    if is_wallet_session:
        if eff_mins > 0:
            if req.get("effective_cost_mins"):
                wallet_game_value = _to_int(eff_mins * base_rate / 60)
            else:
                wallet_game_value = _to_int(eff_mins * base_rate * multiplier / 60)
        gross = wallet_game_value + food_total
        cash_payable = food_total
    else:
        # ── 2+1 duration promo: gross MUST derive from the re-priced game_amt ──
        # When duration_2plus1 applies, game_amt was re-priced above from BILLABLE
        # minutes only. The client (cartForCalc) sends the STALE pre-promo
        # `gross_total` (e.g. 42,833) on the /promotions/calc preview path, so
        # trusting `gross_total_in > 0` would ignore the promo and make the preview
        # disagree with the commit (which overwrites gross_total server-side).
        # Force gross = re-priced game + food so preview == commit by construction.
        if d2p_billable > 0:
            gross = game_amt + food_total
        else:
            gross = gross_total_in if gross_total_in > 0 else (game_amt + food_total)
        cash_payable = gross

    breakdown = []
    promo_discount = 0
    coupon_discount = 0
    foc_discount = 0
    bonus_mins = 0
    promo_title = ""
    coupon_code = ""
    promo_id = str(req.get("promo_id") or "").strip()

    # ── promotion ────────────────────────────────────────────────────────
    if promo_id:
        row = None
        if promo_id.isdigit():
            row = _mysql_query_one("SELECT * FROM promotions WHERE id=%s", (int(promo_id),))
        if row:
            _is_d2p = (row.get("promo_type") == "duration_2plus1") or (row.get("discount_type") == "duration_2plus1")
            ptype = "duration_2plus1" if _is_d2p else (row.get("discount_type") or row.get("promo_type") or "general")
            pct = _to_float(row.get("discount_value"))
            target = row.get("discount_target") or "overall"
            promo_title = row.get("promo_name") or row.get("name") or f"Promo #{promo_id}"
            if ptype == "duration_2plus1":
                # Weekday Happy Hour 2+1 — re-priced by the duration_2plus1 block above
                # (billable vs free minutes). No separate discount line here.
                pass
            elif ptype == "bonus_mins":
                bonus_mins = _to_int(req.get("bonus_mins") or pct)
                breakdown.append({"label": f"Promo {promo_title}: +{bonus_mins} bonus mins", "amount": 0})
            elif ptype in ("discount", "cashback") and pct:
                if is_wallet_session and target in ("game_only", "overall"):
                    bonus_mins = _to_int(eff_mins * pct / 100) or _to_int(60 * pct / 100)
                    breakdown.append({"label": f"Promo {promo_title}: {pct:g}% -> +{bonus_mins} bonus mins", "amount": 0})
                else:
                    base = game_amt if target == "game_only" else food_total if target == "food_only" else gross
                    promo_discount = _to_int(base * pct / 100)
                    breakdown.append({"label": f"Promo {promo_title}: {pct:g}% off {target}", "amount": -promo_discount})
            else:
                foc_item = str(req.get("foc_item") or "").strip()
                if foc_item and isinstance(food_items, list):
                    for fi in food_items:
                        if isinstance(fi, dict) and str(fi.get("name") or "").strip() == foc_item:
                            foc_discount = _to_int(fi.get("subtotal"))
                            break
                if foc_discount > gross:
                    foc_discount = gross
                if foc_discount:
                    breakdown.append({"label": f"Promo {promo_title}: FOC {foc_item}", "amount": -foc_discount})
                else:
                    breakdown.append({"label": f"Promo {promo_title} (no discount applied)", "amount": 0})
        else:
            breakdown.append({"label": f"Promo #{promo_id} not found/active", "amount": 0})

    # ── coupon ───────────────────────────────────────────────────────────
    coupon_code = str(req.get("coupon_code") or "").strip().upper()
    coupon_valid = False
    coupon_id = None
    coupon_minutes_available = 0
    coupon_minutes_used = 0
    if coupon_code:
        c = _mysql_query_one(
            "SELECT id, coupon_code, balance_minutes, status, expiry_date FROM member_coupons WHERE coupon_code=%s",
            (coupon_code,))
        if not c:
            breakdown.append({"label": f"Coupon {coupon_code}: not found", "amount": 0})
        elif c.get("status") != "active":
            breakdown.append({"label": f"Coupon {coupon_code}: {c.get('status')}", "amount": 0})
        elif _to_int(c.get("balance_minutes")) <= 0:
            breakdown.append({"label": f"Coupon {coupon_code}: no minutes left", "amount": 0})
        else:
            coupon_valid = True
            coupon_id = c.get("id")
            coupon_minutes_available = _to_int(c.get("balance_minutes"))
            # Cap the discount at the coupon's available minutes; use request-supplied
            # `coupon_minutes` when the cashier only wants to burn part of the coupon.
            coupon_minutes = _to_int(req.get("coupon_minutes")) or coupon_minutes_available
            coupon_minutes = max(0, min(coupon_minutes, coupon_minutes_available))
            coupon_minutes_used = coupon_minutes
            coupon_discount = _to_int(coupon_minutes * base_rate / 60)
            breakdown.append({"label": f"Coupon {coupon_code}: {coupon_minutes} mins", "amount": -coupon_discount})

    if manual_discount:
        breakdown.append({"label": "Manual discount", "amount": -manual_discount})

    # ── Duration 2+1 visible breakdown line (re-pricing, amount stays 0) ──
    if d2p_billable > 0:
        _d2p_label = d2p_title or (promo_title or "2+1 Happy Hour")
        breakdown.append({
            "label": f"Promo {_d2p_label}: 2+1 — billable {d2p_billable}m / free {d2p_free}m",
            "amount": 0,
        })

    total_discount = promo_discount + coupon_discount + foc_discount + manual_discount
    if total_discount > cash_payable:
        total_discount = cash_payable
    net_total = max(0, cash_payable - total_discount)

    return {
        "is_wallet_session": is_wallet_session,
        "game_amt": game_amt,
        "food_total": food_total,
        "wallet_game_value": wallet_game_value,
        "gross_total": gross,
        "cash_payable": cash_payable,
        "promo_id": promo_id or None,
        "promo_title": promo_title or None,
        "coupon_code": coupon_code or None,
        "coupon_id": coupon_id,
        "coupon_valid": coupon_valid,
        "coupon_minutes": coupon_minutes_available,
        "coupon_minutes_used": coupon_minutes_used,
        "promo_discount": promo_discount,
        "coupon_discount": coupon_discount,
        "foc_discount": foc_discount,
        "manual_discount": manual_discount,
        "total_discount": total_discount,
        "bonus_mins": bonus_mins,
        "net_total": net_total,
        "breakdown": breakdown,
        "duration_2plus1": ({
            "promo_id": d2p.get("promo_id") if isinstance(d2p, dict) else None,
            "title": d2p_title or None,
            "total_mins": d2p_total,
            "billable_mins": d2p_billable,
            "free_mins": d2p_free,
        } if d2p_billable > 0 else None),
        "billable_mins": d2p_billable or None,
        "free_mins": d2p_free or None,
    }


# ────────────────────────────────────────────────────────────────────────────
#  Deposit redemption (commit path — idempotent on a stable sale ref)
# ────────────────────────────────────────────────────────────────────────────
def _ensure_redemptions_table() -> None:
    global _redemptions_ready
    if _redemptions_ready:
        return
    try:
        _mysql_exec(
            """
            CREATE TABLE IF NOT EXISTS deposit_redemptions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                branch_id INT NOT NULL DEFAULT 1,
                booking_id INT NOT NULL,
                sale_ref VARCHAR(64) NULL,
                voucher_no VARCHAR(50) NULL,
                amount INT NOT NULL DEFAULT 0,
                method VARCHAR(20) NULL,
                staff_name VARCHAR(100) NULL,
                source VARCHAR(16) NOT NULL DEFAULT 'sale',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_dep_sale (booking_id, sale_ref),
                KEY idx_booking (booking_id)
            )
            """
        )
        _redemptions_ready = True
    except Exception as e:  # pragma: no cover - defensive
        logger.error("deposit_redemptions table init failed: %s", e)


# ────────────────────────────────────────────────────────────────────────────
#  Coupon redemption (shared commit path — idempotent on sale ref)
# ────────────────────────────────────────────────────────────────────────────
_coupon_ready = False


def _ensure_coupon_redemptions_table() -> None:
    global _coupon_ready
    if _coupon_ready:
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
        _coupon_ready = True
    except Exception as e:  # pragma: no cover - defensive
        logger.error("coupon_redemptions table init failed: %s", e)


def redeem_coupon(code: str, minutes: int, sale_ref: Optional[str] = None,
                  staff_name: str = "", branch_id: int = 1) -> Dict[str, Any]:
    """Redeem ``minutes`` off a coupon, idempotent per ``sale_ref``.

    Same semantics/table as ``POST /api/coupons/redeem`` so a sale that also
    redeems the coupon in the money path never double-deducts.
    Returns ``{ok, remaining_minutes, deducted_minutes, status, idempotent, error}``.
    """
    out = {"ok": False, "remaining_minutes": 0, "deducted_minutes": 0,
           "status": "", "idempotent": False, "error": None}
    code = (code or "").strip().upper()
    minutes = _to_int(minutes)
    if not code or minutes <= 0:
        out["error"] = "code and minutes required"
        return out
    _ensure_coupon_redemptions_table()
    sale_ref = (str(sale_ref).strip() or None) if sale_ref else None
    try:
        # Replay path
        if sale_ref:
            prev = _mysql_query_one(
                "SELECT minutes_deducted, balance_after FROM coupon_redemptions "
                "WHERE coupon_code=%s AND sale_ref=%s", (code, sale_ref))
            if prev:
                bal = int(prev["balance_after"] or 0)
                out.update({"ok": True, "remaining_minutes": bal,
                            "deducted_minutes": int(prev["minutes_deducted"] or 0),
                            "status": "used" if bal <= 0 else "active", "idempotent": True})
                return out
        rows = _mysql_query(
            "SELECT id, balance_minutes, status, expiry_date FROM member_coupons WHERE coupon_code=%s",
            (code,))
        if not rows:
            out["error"] = "Coupon not found"
            return out
        coupon = rows[0]
        if coupon["status"] != "active":
            out["error"] = f"Coupon status: {coupon['status']}"
            return out
        if int(coupon["balance_minutes"] or 0) < minutes:
            out["error"] = f"Insufficient balance. Available: {coupon['balance_minutes']} mins"
            return out
        try:
            from datetime import datetime as _dt
            if coupon.get("expiry_date") and coupon["expiry_date"] < _dt.now():
                out["error"] = "Coupon has expired"
                return out
        except Exception:
            pass

        new_balance = int(coupon["balance_minutes"] or 0) - minutes
        new_status = "used" if new_balance <= 0 else "active"

        redemption_id = None
        if sale_ref:
            try:
                redemption_id = _mysql_exec(
                    "INSERT INTO coupon_redemptions (coupon_id, coupon_code, sale_ref, minutes_deducted, balance_after, staff_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (coupon["id"], code, sale_ref, minutes, new_balance, staff_name or None))
            except Exception:
                prev = _mysql_query_one(
                    "SELECT minutes_deducted, balance_after FROM coupon_redemptions "
                    "WHERE coupon_code=%s AND sale_ref=%s", (code, sale_ref))
                if prev:
                    bal = int(prev["balance_after"] or 0)
                    out.update({"ok": True, "remaining_minutes": bal,
                                "deducted_minutes": int(prev["minutes_deducted"] or 0),
                                "status": "used" if bal <= 0 else "active", "idempotent": True})
                    return out
                out["error"] = "Coupon redeem conflict — please retry"
                return out

        from mysql_db import delete_rows as _rowcount
        affected = _rowcount(
            "UPDATE member_coupons SET balance_minutes=balance_minutes-%s, "
            "status=CASE WHEN balance_minutes-%s<=0 THEN 'used' ELSE 'active' END, redeemed_at=NOW() "
            "WHERE id=%s AND status='active' AND balance_minutes>=%s",
            (minutes, minutes, coupon["id"], minutes))
        if not affected:
            if redemption_id:
                _mysql_exec("DELETE FROM coupon_redemptions WHERE id=%s", (redemption_id,))
            out["error"] = "Coupon changed — please retry"
            return out

        out.update({"ok": True, "remaining_minutes": new_balance,
                    "deducted_minutes": minutes, "status": new_status, "idempotent": False})
        return out
    except Exception as e:
        logger.error("redeem_coupon error: %s", e)
        out["error"] = "Coupon redeem failed. Please try again."
        return out


def deposit_already_in_payment_method(pm: str) -> bool:
    """True when the caller already folded the deposit into ``payment_method``.

    The sale bot pre-redeems the deposit client-side and appends ``"KPay:5000 (dep)"``
    to the payment string; in that case the server must NOT deduct again.
    """
    return "(dep" in (pm or "").lower()


def get_verified_deposit(booking_id: Any) -> Dict[str, Any]:
    """Return ``{amount, method, status, ref, ref_type}`` for a booking (0 when none).

    2026-09-30 depfix: a deposit on a cancelled / rejected / no-show booking is NOT
    redeemable — it is reported as ``status='none'`` with amount 0 so no caller can
    fold uncollected money into a sale, balance or liability.
    """
    empty = {"amount": 0, "method": "", "status": "none", "ref": "", "ref_type": "", "booking_id": booking_id}
    try:
        row = _mysql_query_one(
            "SELECT status, deposit_status, deposit_amount, deposit_method, deposit_ref, deposit_ref_type "
            "FROM console_bookings WHERE id=%s",
            (booking_id,),
        )
        if not row:
            return empty
        _bk_status = str(row.get("status") or "").lower()
        if _bk_status in ("cancelled", "rejected", "no_show"):
            logger.info(
                "get_verified_deposit BK#%s: booking status=%s -> deposit not redeemable",
                booking_id, _bk_status,
            )
            return empty
        return {
            "booking_id": booking_id,
            "amount": int(row.get("deposit_amount") or 0),
            "method": (row.get("deposit_method") or "").lower(),
            "status": row.get("deposit_status") or "none",
            "ref": row.get("deposit_ref") or "",
            "ref_type": row.get("deposit_ref_type") or "",
        }
    except Exception as e:
        logger.warning("get_verified_deposit BK#%s failed: %s", booking_id, e)
        return empty


def commit_deposit_redemption(
    booking_id: Any,
    sale_ref: Optional[str],
    voucher_no: str = "",
    staff_name: str = "",
    source: str = "sale",
) -> Dict[str, Any]:
    """Redeem a verified deposit for a booking exactly once.

    Returns ``{redeemed, already, amount, method, reason}``.
    * ``redeemed=True``  — this call performed the redemption.
    * ``already=True``   — a prior redemption exists for this booking/sale (no-op).
    * ``amount``         — the deposit value that was (or had been) applied.
    """
    out = {"redeemed": False, "already": False, "amount": 0, "method": "", "reason": ""}
    try:
        dep = get_verified_deposit(booking_id)
        out["method"] = dep.get("method") or ""

        # Already redeemed / forfeited / refunded → nothing to do.
        # 2026-09-26 (owner): also accept a SUBMITTED-but-unverified deposit ('paid')
        # — a booking-bound session end must deduct it (staff see it on the voucher).
        if dep.get("status") not in ("verified", "paid") or dep.get("amount", 0) <= 0:
            out["amount"] = dep.get("amount", 0) if dep.get("status") == "redeemed" else 0
            out["already"] = dep.get("status") == "redeemed"
            out["reason"] = f"deposit_status={dep.get('status')}"
            return out

        amount = int(dep["amount"])
        method = dep.get("method") or "kpay"

        # 🩹 2026-10-27 (BK#4447): explicit idempotency guard — if a redemption row
        # already exists for this booking (ANY sale_ref), never redeem a second time.
        # (The atomic status transition below is the single-winner, but a stale
        # 'verified' status + an existing audit row must still short-circuit.)
        try:
            _existing = _mysql_query_one(
                "SELECT id FROM deposit_redemptions WHERE booking_id=%s LIMIT 1",
                (booking_id,),
            )
        except Exception:
            _existing = None
        if _existing:
            out["already"] = True
            out["amount"] = amount
            out["reason"] = "already-redeemed (existing redemption row)"
            return out

        # Atomic single-winner transition (guarded on the verified status).
        affected = 0
        try:
            from mysql_db import delete_rows as _rowcount
            affected = _rowcount(
                "UPDATE console_bookings SET deposit_status='redeemed', deposit_redeemed_at=NOW() "
                "WHERE id=%s AND deposit_status IN ('verified','paid') AND deposit_amount>0 "
                "AND status NOT IN ('cancelled','rejected','no_show')",
                (booking_id,),
            )
        except Exception as e:
            logger.warning("deposit redeem transition BK#%s failed: %s", booking_id, e)

        if not affected:
            out["already"] = True
            out["amount"] = amount
            out["reason"] = "already-redeemed (race)"
            return out

        # Audit row (idempotency key = booking_id + sale_ref).
        _ensure_redemptions_table()
        try:
            _mysql_exec(
                "INSERT INTO deposit_redemptions (booking_id, sale_ref, voucher_no, amount, method, staff_name, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE amount=VALUES(amount)",
                (booking_id, sale_ref, voucher_no or None, amount, method, staff_name or None, source),
            )
        except Exception as e:
            logger.warning("deposit_redemptions insert BK#%s failed: %s", booking_id, e)

        out.update({"redeemed": True, "amount": amount, "reason": "ok"})
        logger.info(
            "Deposit redeemed BK#%s amount=%s method=%s sale_ref=%s staff=%s source=%s",
            booking_id, amount, method, sale_ref, staff_name, source,
        )
        return out
    except Exception as e:
        logger.error("commit_deposit_redemption BK#%s error: %s", booking_id, e)
        out["reason"] = f"error: {e}"
        return out
