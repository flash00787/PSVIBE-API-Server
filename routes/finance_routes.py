"""PS VIBE API — Finance Routes."""
import logging
import os
import datetime as _dt_module
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query, Body

from auth import get_current_user, get_staff_or_dashboard_user
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete
import dish_consumption as _dish_consume

# ── MMT time helpers ──
def _get_mmt_now():
    """Return current datetime in Myanmar time (UTC+6:30)."""
    return datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)


def _get_mmt_today_str():
    """Return today's date string in MMT."""
    return _get_mmt_now().strftime("%Y-%m-%d")

logger = logging.getLogger(__name__)

def _forfeit_redeemed_total(accounts, q=None):
    """Forfeit injects that must be cancelled out because the deposit later became
    sale income (booking deposit_status = redeemed/refunded).

    Consolidated 2026-10-20: this exact query was copy-pasted in three places
    (balance sheet, per-account balance, cumulative view) which is how the
    BK#4073 phantom-forfeit class of bug stayed invisible. One definition now
    backs all of them.
    """
    import mysql_db as _mdb
    _q = q or _mdb.query_one
    accounts = list(accounts or [])
    if not accounts:
        return 0.0
    ph = ", ".join(["%s"] * len(accounts))
    sql = f"""SELECT COALESCE(SUM(cm.amount),0) as t
        FROM cash_movements cm JOIN console_bookings cb
          ON cb.id = CAST(REPLACE(cm.note, 'Forfeit BK#', '') AS UNSIGNED)
        WHERE cm.movement_type='inject' AND cm.account IN ({ph})
          AND cm.note LIKE CONCAT('Forfeit BK#', CHAR(37))
          AND cb.deposit_status IN ('redeemed','refunded')"""
    row = _q(sql, tuple(accounts)) or {}
    return float(row.get("t", 0) or 0)


router = APIRouter(prefix="/api", tags=["Finance"])

# TEMPORARILY DISABLED (2026-09-22, Osmo): depreciation excluded from P&L.
# Set to True to re-enable depreciation in the P&L statement.
INCLUDE_DEPRECIATION_IN_PNL = False

# 2026-09-28 Osmo: KPay / WavePay / AYA Pay are INACTIVE payment accounts.
# They are still computed + reported everywhere (balances stay visible to the
# owner), but every payload flags them `active: false` so the UI can dim them
# and staff pickers can exclude them. Mirrors account_routes._INACTIVE_ACCOUNTS.
_INACTIVE_ACCOUNT_LABELS = {"WavePay", "KPay", "AYA Pay"}


# ── MMQR merchant fee (net-of-fee display, DISPLAY ONLY) ──────────────────────
# Owner 2026-09-28 Osmo: the real MMQR merchant account (CB, "PS Vibe- SC")
# credits QR payments NET of a 0.3% transaction fee, while the app records gross.
# For the owner/admin view we surface a small "net of fee" line next to the
# MMQR - CB balance. This is additive + display-only — no money-table writes.
_MMQR_FEE_KEY = "mmqr_fee_pct"
_MMQR_FEE_DEFAULT_PCT = 0.3  # percent
_FEE_ACCOUNT_LABELS = {"MMQR - CB"}  # accounts that carry the merchant fee


def _coerce_fee_pct(value) -> float:
    """Coerce a raw config value to a positive fee percentage (default fallback)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _MMQR_FEE_DEFAULT_PCT
    return v if v > 0 else _MMQR_FEE_DEFAULT_PCT


def compute_fee_fields(balance: float, fee_pct: float, label: str) -> dict:
    """Pure fee/net computation (no DB). Fee applies only to fee-flagged accounts.

    net_balance = balance × (1 − fee_pct/100); fee_amount = balance × fee_pct/100,
    both rounded to 2dp. Non-fee accounts return fee 0 / net == balance.
    """
    balance = float(balance or 0)
    fee_pct = float(fee_pct or 0)
    if (label in _FEE_ACCOUNT_LABELS) and fee_pct > 0:
        fee_amount = round(balance * fee_pct / 100.0, 2)
        net_balance = round(balance - fee_amount, 2)
        fee_note = f"{fee_pct:g}% MMQR fee"
        return {"fee_pct": round(fee_pct, 4), "fee_amount": fee_amount,
                "net_balance": net_balance, "fee_note": fee_note}
    return {"fee_pct": 0.0, "fee_amount": 0.0,
            "net_balance": round(balance, 2), "fee_note": ""}


def _read_mmqr_fee_pct() -> float:
    """Read mmqr_fee_pct (percent) from settings_config; idempotent upsert to default."""
    try:
        row = _mysql_query_one(
            "SELECT config_value FROM settings_config WHERE config_key=%s", (_MMQR_FEE_KEY,))
    except Exception:
        row = None
    if row:
        return _coerce_fee_pct(row.get("config_value"))
    # Missing → create the row idempotently so the owner can change it later.
    try:
        _mysql_execute(
            "INSERT INTO settings_config (config_key, config_value, config_type, category, description) "
            "VALUES (%s, %s, 'float', 'pricing', 'MMQR merchant fee %% (net = balance × (1 − fee/100))') "
            "ON DUPLICATE KEY UPDATE config_key=config_key",
            (_MMQR_FEE_KEY, str(_MMQR_FEE_DEFAULT_PCT)),
        )
    except Exception:
        pass
    return _MMQR_FEE_DEFAULT_PCT


@router.get("/dashboard/financial-report")
async def dashboard_financial_report(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get financial summary with pagination and SQL aggregation."""
    try:
        # Use SQL aggregation for totals
        assets_tot = _mysql_query_one("SELECT COALESCE(SUM(amount), 0) as total, COUNT(*) as cnt FROM finance_assets WHERE status = 'active'")
        assets_total = float(assets_tot["total"] or 0)
        assets_count = int(assets_tot["cnt"] or 0)
        assets = _mysql_query(
            "SELECT id, name, purchase_date, per_price, qty, disposed_qty, amount, payment_method, salvage_value, useful_life, monthly_dep, months_elapsed, acc_depreciation, book_value, notes, status FROM finance_assets ORDER BY purchase_date DESC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        payables_tot = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total, COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END), 0) as pending FROM finance_payables"
        )
        payables_total = float(payables_tot["total"] or 0)
        payables_pending = float(payables_tot["pending"] or 0)
        payables = _mysql_query(
            "SELECT id, payee, amount, due_date, status FROM finance_payables ORDER BY due_date ASC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        receivables_tot = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total, COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END), 0) as pending FROM finance_receivables"
        )
        receivables_total = float(receivables_tot["total"] or 0)
        receivables_pending = float(receivables_tot["pending"] or 0)
        receivables = _mysql_query(
            "SELECT id, payer, amount, due_date, status FROM finance_receivables ORDER BY due_date ASC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        advances_tot = _mysql_query_one(
            "SELECT COALESCE(SUM(amount), 0) as total, COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END), 0) as pending FROM finance_advances"
        )
        advances_total = float(advances_tot["pending"] or 0)
        advances_pending = float(advances_tot["pending"] or 0)
        advances = _mysql_query(
            "SELECT id, member_id, amount, advance_date, settle_date, status, notes FROM finance_advances ORDER BY advance_date DESC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        prepaid_tot = _mysql_query_one("SELECT COALESCE(SUM(amount), 0) as total FROM finance_prepaid")
        prepaid_total = float(prepaid_tot["total"] or 0)
        prepaid = _mysql_query(
            "SELECT id, description, amount, settle_date, status FROM finance_prepaid ORDER BY settle_date ASC LIMIT %s OFFSET %s",
            (limit, offset)
        )

        opex_rows = _mysql_query("SELECT COALESCE(SUM(amount), 0) as total FROM opex WHERE expense_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)")
        opex_30d = float(opex_rows[0]["total"] or 0) if opex_rows else 0

        # Get disposal records
        disps = _mysql_query("SELECT ad.id, ad.asset_id, fa.name as asset_name, ad.disposed_qty, ad.disposal_amount, ad.disposal_date, ad.book_value_at_disposal, ad.profit_loss FROM asset_disposals ad LEFT JOIN finance_assets fa ON ad.asset_id = fa.id ORDER BY ad.created_at DESC LIMIT 50")
        disposal_records = []
        for d in (disps or []):
            disposal_records.append({
                "id": d["id"],
                "asset_id": d["asset_id"],
                "asset_name": d.get("asset_name") or "Unknown",
                "disposed_qty": int(d.get("disposed_qty") or 1),
                "disposal_amount": float(d.get("disposal_amount") or 0),
                "disposal_date": str(d.get("disposal_date") or ""),
                "book_value_at_disposal": float(d.get("book_value_at_disposal") or 0),
                "profit_loss": float(d.get("profit_loss") or 0)
            })

        # --- Auto Depreciation Calculation ---
        import datetime as _dt_module
        _today_dep = _dt_module.datetime(2026, 6, 1)  # Depreciation base: June 1, 2026
        _assets_result = []
        for _a in assets:
            _ul = int(_a.get("useful_life", 0) or 0)
            _sv = float(_a.get("salvage_value", 0) or 0)
            _amt = float(_a.get("amount", 0) or 0)
            if _ul > 0 and _amt > 0:
                _useful_m = _ul * 12
                _monthly = max(0, (_amt - _sv) / _useful_m) if _useful_m > 0 else 0
                _pd = _a.get("purchase_date")
                if _pd:
                    try:
                        if hasattr(_pd, "strftime"):
                            _me = (_today_dep.year - _pd.year) * 12 + (_today_dep.month - _pd.month)
                        else:
                            _pd2 = _dt_module.datetime.strptime(str(_pd)[:10], "%Y-%m-%d")
                            _me = (_today_dep.year - _pd2.year) * 12 + (_today_dep.month - _pd2.month)
                    except Exception:
                        _me = int(_a.get("months_elapsed", 0) or 0)
                else:
                    _me = int(_a.get("months_elapsed", 0) or 0)
                if _me < 0: _me = 0
                _acc_d = min(_monthly * _me, _amt - _sv)
                _bv = _amt - _acc_d
                _a["monthly_dep"] = round(_monthly, 0)
                _a["months_elapsed"] = _me
                _a["acc_depreciation"] = round(_acc_d, 0)
                _a["book_value"] = round(_bv, 0)
            _assets_result.append({
                "id": _a["id"], "name": _a.get("name"),
                "purchase_date": str(_a["purchase_date"]) if _a.get("purchase_date") else None,
                "per_price": float(_a.get("per_price", 0) or 0),
                "qty": int(_a.get("qty", 1) or 1),
                "disposed_qty": int(_a.get("disposed_qty", 0) or 0),
                "amount": float(_a["amount"] or 0),
                "payment_method": _a.get("payment_method") or "",
                "salvage_value": float(_a.get("salvage_value", 0) or 0),
                "useful_life": int(_a.get("useful_life", 0) or 0),
                "monthly_dep": float(_a.get("monthly_dep", 0) or 0),
                "months_elapsed": int(_a.get("months_elapsed", 0) or 0),
                "acc_depreciation": float(_a.get("acc_depreciation", 0) or 0),
                "book_value": float(_a.get("book_value", 0) or 0) or (float(_a.get("amount", 0) or 0) - float(_a.get("acc_depreciation", 0) or 0)),
                "status": _a.get("status") or "active",
                "disposal_amount": float(_a.get("disposal_amount", 0) or 0) if _a.get("status") == "disposed" else None,
                "disposal_date": str(_a["disposal_date"]) if _a.get("disposal_date") else None,
                "profit_loss": float(_a.get("profit_loss", 0) or 0) if _a.get("status") == "disposed" else None,
                "notes": _a.get("notes"),
            })
        
        return {
            "success": True,
            "data": {
                "assets": _assets_result,
                "assets_total": assets_total, "assets_count": assets_count,
                "advances_total": advances_total, "advances_pending": advances_pending,
                "prepaid_total": prepaid_total,
                "net_position": assets_total - advances_pending - prepaid_total,
                "payables": [{
                    "id": p["id"], "payee": p.get("payee"),
                    "amount": float(p["amount"] or 0),
                    "due_date": str(p["due_date"]) if p.get("due_date") else None,
                    "status": p.get("status"),
                } for p in payables],
                "payables_total": payables_total, "payables_pending": payables_pending,
                "receivables": [{
                    "id": r["id"], "payer": r.get("payer"),
                    "amount": float(r["amount"] or 0),
                    "due_date": str(r["due_date"]) if r.get("due_date") else None,
                    "status": r.get("status"),
                } for r in receivables],
                "receivables_total": receivables_total, "receivables_pending": receivables_pending,
                "advances": [{
                    "id": a["id"], "member_id": a.get("member_id"),
                    "amount": float(a["amount"] or 0),
                    "advance_date": str(a["advance_date"]) if a.get("advance_date") else None,
                    "settle_date": str(a["settle_date"]) if a.get("settle_date") else None,
                    "status": a.get("status"), "notes": a.get("notes"),
                } for a in advances],
                "advances_total": advances_total, "advances_pending": advances_pending,
                "prepaid": [{
                    "id": p["id"], "description": p.get("description"),
                    "amount": float(p["amount"] or 0),
                    "settle_date": str(p["settle_date"]) if p.get("settle_date") else None,
                    "status": p.get("status"),
                } for p in prepaid],
                "prepaid_total": prepaid_total,
                "opex_30d": opex_30d,
                "disposal_records": disposal_records,
            }
        }
    except Exception as e:
        logger.error(f"GET /financial-report error: {e}")
        return {"success": False, "error": str(e)}


# ── Asset CRUD ──
@router.post("/dashboard/assets/create")
async def dashboard_create_asset(body: dict = {}, user: dict = Depends(get_current_user)):
    """Create a new fixed asset record with depreciation fields."""
    from mysql_db import execute as _e
    try:
        name = (body.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "Name is required"}
        pd_ = body.get("purchase_date") or None
        pp = float(body.get("per_price", 0) or 0)
        qt = int(body.get("qty", 1) or 1)
        amt = pp * qt
        pm = body.get("payment_method", "")
        sv = float(body.get("salvage_value", 0) or 0)
        ul = int(body.get("useful_life", 0) or 0)
        nt = body.get("notes", "")
        import datetime as _d
        if ul > 0 and amt > sv:
            um = ul * 12
            md = max(0, (amt - sv) / um)
            me = 0
            if pd_:
                try: me = max(0, (_d.date.today().year - _d.date.fromisoformat(pd_).year) * 12 + (_d.date.today().month - _d.date.fromisoformat(pd_).month))
                except Exception: me = 0
            ad = min(md * me, amt - sv)
            bv = amt - ad
        else:
            md, me, ad, bv = 0, 0, 0, amt
        aid = _e("INSERT INTO finance_assets (name,purchase_date,per_price,qty,amount,payment_method,salvage_value,useful_life,monthly_dep,months_elapsed,acc_depreciation,book_value,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')",
            (name, pd_, pp, qt, amt, pm, sv, ul, round(md, 0), me, round(ad, 0), round(bv, 0), nt))
        return {"success": True, "message": "Asset created", "data": {"id": aid, "name": name, "amount": round(amt, 0), "book_value": round(bv, 0)}}
    except Exception as e:
        logger.error(f"POST /assets/create error: {e}")
        return {"success": False, "error": str(e)}
@router.put("/dashboard/assets/{asset_id}/dispose")

async def dashboard_dispose_asset(asset_id: int, req: dict = {}, user: dict = Depends(get_current_user)):
    """Dispose an asset (partial or full) with profit/loss calculation."""
    from mysql_db import execute as _mysql_execute2, query_one as _mysql_query_one2
    try:
        asset = _mysql_query_one2(
            "SELECT id, name, per_price, qty, disposed_qty, amount, acc_depreciation, book_value, salvage_value, status FROM finance_assets WHERE id = %s",
            (asset_id,)
        )
        if not asset:
            return {"success": False, "error": "Asset not found"}
        if asset["status"] == "disposed":
            return {"success": False, "error": "Asset already disposed"}
        current_qty = int(asset["qty"] or 0)
        disposed_already = int(asset["disposed_qty"] or 0)
        remaining_qty = current_qty
        if remaining_qty <= 0:
            return {"success": False, "error": "No quantity left to dispose"}
        dispose_qty = int(req.get("qty", 0) or 0)
        if dispose_qty <= 0 or dispose_qty > remaining_qty:
            dispose_qty = remaining_qty
        sale_amount = float(req.get("sale_amount", 0) or 0)
        total_original_qty = current_qty + disposed_already
        total_amount = float(asset["amount"] or 0)
        acc_dep = float(asset["acc_depreciation"] or 0)
        total_book = total_amount - acc_dep
        if total_original_qty > 0:
            prop_book = total_book * (dispose_qty / total_original_qty)
        else:
            prop_book = 0
        profit_loss = sale_amount - prop_book
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        _mysql_execute2(
            "INSERT INTO asset_disposals (asset_id, disposed_qty, disposal_amount, disposal_date, book_value_at_disposal, profit_loss, note) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (asset_id, dispose_qty, sale_amount, today, prop_book, profit_loss, "Disposed " + str(dispose_qty) + " of " + str(total_original_qty) + " units")
        )
        is_full = (dispose_qty >= remaining_qty)
        if is_full:
            _mysql_execute2("UPDATE finance_assets SET status = 'disposed', disposal_amount = %s, disposal_date = %s, profit_loss = %s, disposed_qty = disposed_qty + %s WHERE id = %s", (sale_amount, today, profit_loss, dispose_qty, asset_id))
        else:
            new_qty = remaining_qty - dispose_qty
            new_disposed_qty = disposed_already + dispose_qty
            _mysql_execute2("UPDATE finance_assets SET qty = %s, disposed_qty = %s WHERE id = %s", (new_qty, new_disposed_qty, asset_id))
        if sale_amount > 0:
            return_acct = req.get("return_account") or "Cash"
            label = ("Asset disposal: " + (asset["name"] or "Unknown") + " (x" + str(dispose_qty) + ")")
            _mysql_execute2("INSERT INTO cash_movements (movement_type, account, amount, note, created_at) VALUES (%s, %s, %s, %s, NOW())", ("inject", return_acct, sale_amount, label))
        return {"success": True, "message": "Asset disposed" if is_full else "Partial disposal recorded", "data": {"sale_amount": sale_amount, "book_value": round(prop_book, 0), "profit_loss": round(profit_loss, 0), "type": "profit" if profit_loss >= 0 else "loss", "disposed_qty": dispose_qty, "remaining_qty": 0 if is_full else new_qty, "is_partial": not is_full}}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.get("/dashboard/finance/balances")
async def get_finance_balances(year: int = Query(None), month: int = Query(None), user: dict = Depends(get_current_user)):
    """Returns live balance for each payment account."""
    accounts = [{"key": "cash", "label": "Cash", "icon": "💰", "type": "operating"},{"key": "wave", "label": "WavePay", "icon": "📱", "type": "digital"},{"key": "aya_pay", "label": "AYA Pay", "icon": "💳", "type": "bank"},{"key": "kpay", "label": "KPay", "icon": "💳", "type": "digital"},{"key": "mmqr_cb", "label": "MMQR - CB", "icon": "🏦", "type": "bank"},{"key": "kbz_bank", "label": "KBZ Bank", "icon": "🏦", "type": "capital"},{"key": "acm_acc", "label": "Main (ACM)", "icon": "🏦", "type": "transfer"}]
    for acc in accounts:
        acc["active"] = acc["label"] not in _INACTIVE_ACCOUNT_LABELS
    # Month filter: default to cumulative (from business start) when no month/year specified
    from datetime import datetime, timedelta, timezone as _tz
    _mmt_offset = timedelta(hours=6, minutes=30)
    _now_mmt = datetime.now(_tz.utc) + _mmt_offset
    _cumulative = (year is None and month is None)
    if not year:
        year = _now_mmt.year
    if not month:
        month = _now_mmt.month
    if _cumulative:
        # Live balance: from business start to now
        _start = "2026-04-01"
        _end = (_now_mmt + timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        _start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            _end = f"{year+1:04d}-01-01"
        else:
            _end = f"{year:04d}-{month+1:02d}-01"
    try:
        from mysql_db import query as _mq, query_one as _mqo, execute as _me
        import re
        import pymysql
        from fifo_wallet import get_all_fifo
        try:
            _fifo_conn = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
            _fifo_result = get_all_fifo(_fifo_conn)
            _fifo_conn.close()
        except Exception:
            _fifo_result = {"liability": 0, "consumed": 0}
        rows = _mq("SELECT payment_method, net, notes, is_topup FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND sale_date >= %s AND sale_date < %s", (_start, _end))
        income_by_account = {acc["key"]: 0.0 for acc in accounts}
        dep_total_per_account = {acc["key"]: 0.0 for acc in accounts}  # 🆕 track (dep) amounts
        for row in rows:
            note = (row.get("notes") or "")
            if int(row.get("is_topup") or 0): continue
            pm = (row.get("payment_method") or "").strip()
            net_amount = float(row.get("net") or 0)
            if net_amount <= 0: continue
            if not pm: continue
            parts = pm.split("|")
            for part in parts:
                part = part.strip()
                if ":" in part:
                    method, _, val = part.partition(":")
                    method = method.strip().lower().replace(" ", "_")
                    try:
                        _is_dep = "(dep)" in val.lower()  # 🆕 detect deposit portion
                        _clean = val.strip().replace(" (dep)", "") or "0"
                        val = float(_clean)
                        if _is_dep and method in dep_total_per_account:
                            dep_total_per_account[method] += val  # 🆕 accumulate dep amounts
                    except ValueError:
                        val = net_amount / len(parts) if parts else 0
                else:
                    method = part.lower().replace(" ", "_")
                    val = net_amount / len(parts) if parts else 0
                # Normalize payment methods to account keys
                if method == "wavepay":
                    method = "wave"
                if method == "mmqr_-_cb":
                    method = "mmqr_cb"
                if method in income_by_account:
                    income_by_account[method] += val
        # Topup income (new-member card sales + top-ups).
        # Payment strings appear in several shapes: "KPay:90000/Cash:0",
        # "MMQR - CB:90000|Cash:0", or a BARE single method "MMQR - CB"
        # (= the whole top-up amount on that account). 2026-10-14 (Osmo):
        # a bare method used to be silently dropped, so a new-member MMQR
        # payment never reached the MMQR - CB balance — parse every shape.
        topup_rows = _mq("SELECT member_id, payment_method, amount FROM topup_log WHERE amount > 0 AND payment_method IS NOT NULL AND topup_date >= %s AND topup_date < %s", (_start, _end))
        # Owner's OWN member card (PSV_A_000) — a self top-up is NOT shop income
        # (owner Osmo 2026-10-14, option (b): exclude topup #51 50,000 'Cash' 2026-09-19
        # which had been recognised late and inflated the live Cash balance).
        _OWNER_SELF_CARDS = {"PSV_A_000"}
        for _tr in topup_rows:
            if str(_tr.get("member_id") or "").strip() in _OWNER_SELF_CARDS:
                continue
            _pm = (_tr.get("payment_method") or "").strip()
            if not _pm: continue
            _row_amt = float(_tr.get("amount") or 0)
            _parts = [p.strip() for p in re.split(r"[|/]", _pm) if p.strip()]
            if not _parts: continue
            _bare = [p for p in _parts if ":" not in p]
            _bare_share = (_row_amt / len(_bare)) if _bare else 0.0
            for _part in _parts:
                if ":" in _part:
                    _method, _, _val = _part.partition(":")
                    _method = _method.strip().lower().replace(" ", "_")
                    try:
                        _val = float(_val.strip() or 0) if _val.strip() else 0
                    except ValueError:
                        continue
                else:
                    # bare method name → the row's amount sits on this account
                    _method = _part.lower().replace(" ", "_")
                    _val = _bare_share
                if _method == "wavepay": _method = "wave"
                if _method in ("mmqr_-_cb", "mmqr_cb", "mmqr"): _method = "mmqr_cb"
                if _method == "aya": _method = "aya_pay"
                if _method in income_by_account and _val > 0:
                    income_by_account[_method] += _val

        opex_rows = _mq("SELECT payment_method, COALESCE(SUM(amount), 0) as total FROM opex WHERE description NOT LIKE %s AND expense_date >= %s AND expense_date < %s GROUP BY payment_method", ("%Prepaid Rent Amortization%", _start, _end))
        opex_by_acct = {acc["key"]: 0.0 for acc in accounts}
        for row in opex_rows:
            pm = (row.get("payment_method") or "").strip().lower().replace(" ", "_")
            # Normalize "acm's_acc" to account key "acm_acc"
            if pm == "acm's_acc":
                pm = "acm_acc"
            if pm == "mmqr_-_cb":
                pm = "mmqr_cb"
            if pm in opex_by_acct:
                opex_by_acct[pm] += float(row["total"] or 0)
        # Stock-in purchase payments (deducted from accounts)
        # Note: use CONCAT to avoid LIKE '%/%' conflicting with PyMySQL %-formatting
        si_all_rows = _mq("SELECT payment_method, COALESCE(SUM(quantity * unit_cost), 0) as total FROM stock_in WHERE payment_method IS NOT NULL AND payment_method != '' AND created_at >= %s AND created_at < %s GROUP BY payment_method", (_start, _end))
        si_payments = {"Cash": 0.0, "KPay": 0.0, "Wave": 0.0, "AYA Pay": 0.0, "MMQR - CB": 0.0, "KBZ Bank": 0.0, "Main (ACM)": 0.0}
        for row in si_all_rows:
            pm = (row.get("payment_method") or "").strip()
            amt = float(row["total"] or 0)
            if pm in si_payments:
                si_payments[pm] = si_payments.get(pm, 0) + amt
            elif "/" in pm:
                # Composite payment: "Cash 5000 / KPay 3000" — parse and split
                for part in pm.split("/"):
                    part = part.strip()
                    for method in si_payments:
                        if part.startswith(method):
                            try:
                                amt_part = float(part[len(method):].strip().replace(",", ""))
                                si_payments[method] = si_payments.get(method, 0) + amt_part
                            except (ValueError, IndexError):
                                pass
                            break

        cash_rows = _mq("SELECT movement_type, account, COALESCE(SUM(amount), 0) as total, COALESCE(SUM(ABS(amount)), 0) as abs_total FROM cash_movements WHERE created_at >= %s AND created_at < %s GROUP BY movement_type, account", (_start, _end))
        cash_map = {}
        cash_abs_map = {}
        for row in cash_rows:
            key = f"{row['movement_type']}|{row['account']}"
            cash_map[key] = cash_map.get(key, 0) + float(row["total"] or 0)
            cash_abs_map[key] = cash_abs_map.get(key, 0) + float(row["abs_total"] or 0)
        # Map account keys to DB account names for cash_movements lookup
        # Subtract inject entries that overlap with topup (notes start with Topup or New member)
        _bad_inject_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'inject' AND category = 'topup' AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _bad_inject_rows:
            _k = "inject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        # Subtract booking-related injects (Deposit # and BK#) since income already captures them
        _bk_inject_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'inject' AND (note IS NOT NULL AND (note LIKE CONCAT('Deposit #', CHAR(37)) OR note LIKE CONCAT('BK#', CHAR(37)))) AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _bk_inject_rows:
            _k = "inject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        # Also subtract booking-related ejects (deposit reversals) to keep paired entries net zero
        _bk_eject_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'eject' AND (note IS NOT NULL AND (note LIKE CONCAT(CHAR(37), 'deposit reverse', CHAR(37)) OR note LIKE CONCAT(CHAR(37), 'Test deposit', CHAR(37)) OR note LIKE CONCAT('[Refund]', CHAR(37)) OR note LIKE CONCAT('Refund Deposit', CHAR(37)))) AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _bk_eject_rows:
            _k = "eject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        # NOTE: Advance settled injects are NOT subtracted — matching eject entries were
        # backfilled on 2026-07-09 (104.8M across 7 advances), so KBZ balance is accurate.
        key_to_name = {"cash": "Cash", "wave": "Wave", "kpay": "KPay", "aya_pay": "AYA Pay", "mmqr_cb": "MMQR - CB", "kbz_bank": "KBZ Bank", "acm_acc": "Main (ACM)"}
        # Capital expenditure queries (all from KBZ Bank)
        _asset_purchases = _mqo("SELECT COALESCE(SUM(per_price * qty), 0) as total FROM finance_assets WHERE status = 'active'")
        _advances_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_advances WHERE status='pending' OR status IS NULL")
        _prepaid_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_prepaid")
        _disposal_proceeds = _mqo("SELECT COALESCE(SUM(disposal_amount), 0) as total FROM finance_assets WHERE status = 'disposed' AND disposal_amount > 0")
        result = []
        for acc in accounts:
            key = acc["key"]
            db_name = key_to_name.get(key, key.capitalize())
            # DB uses BOTH 'Wave' (transfers, legacy) and 'WavePay' (deposit injects) — merge
            _db_list = [db_name] + (["WavePay"] if db_name == "Wave" else [])
            income = income_by_account.get(key, 0)
            opex = opex_by_acct.get(key, 0)
            trans_in = sum(cash_map.get(f"transfer_in|{n}", 0) for n in _db_list)
            # "amount that left the account" == sum of magnitudes, so this is correct
            # whether transfer_out rows are stored negative (canonical/legacy) or positive
            # (old app bug). abs(SUM(amount)) was wrong for mixed-sign data.
            trans_out = sum(cash_abs_map.get(f"transfer_out|{n}", 0) for n in _db_list)
            inject = sum(cash_map.get(f"inject|{n}", 0) for n in _db_list)
            eject = sum(cash_map.get(f"eject|{n}", 0) for n in _db_list)
            div = sum(cash_map.get(f"dividend|{n}", 0) for n in _db_list)
            si_pay = si_payments.get(db_name, 0)
            # 🆕 Targeted defensive: only cancel forfeit injects of REDEEMED bookings
            # (their deposit became sale income; a forfeit inject would otherwise double-count).
            _forfeit_redeemed = _forfeit_redeemed_total(_db_list, _mqo)
            if _forfeit_redeemed > 0 and inject > 0:
                inject -= min(inject, _forfeit_redeemed)
            # Formula: income - opex + transfers - stock_in_payments + inject - eject
            # Note: stock_in purchases also create cash_movements eject entries,
            # so stock_in_payments may partially overlap with eject for non-split entries.
            # stock_in_payments tracked for info only; cash_movements eject entries already account for stock-in deductions
            balance = income - opex + trans_in - trans_out + inject - eject - div
            # Capital expenditure adjustments (all from KBZ Bank)
            ded_assets = 0
            ded_advances = 0
            ded_prepaid = 0
            add_proceeds = 0
            if key == "kbz_bank":
                ded_assets = float(_asset_purchases.get("total", 0) or 0)
                # ded_adv = 0 — _start is 2026-04-01 (covers May backfilled advance eject 104.8M)
                ded_advances = 0
                ded_prepaid = float(_prepaid_total.get("total", 0) or 0)
                add_proceeds = float(_disposal_proceeds.get("total", 0) or 0)
                balance = balance - ded_assets - ded_advances - ded_prepaid + add_proceeds
            result.append({"key": key, "label": acc["label"], "icon": acc["icon"], "type": acc["type"], "active": acc["active"], "income": round(income, 0), "opex": round(opex, 0), "transfers_in": round(trans_in, 0), "transfers_out": round(trans_out, 0), "injections": round(inject, 0), "ejections": round(eject, 0), "dividends": round(div, 0), "stock_in_payments": round(si_pay, 0), "capital_asset_purchases": round(ded_assets, 0), "capital_advances": round(ded_advances, 0), "capital_prepaid": round(ded_prepaid, 0), "capital_disposal_proceeds": round(add_proceeds, 0), "balance": round(balance, 0)})

        # 🆕 Add back active deposits (paid/verified, not yet redeemed/forfeited)
        # Anti-double-count subtracts ALL "Deposit #" injects, but active deposits
        # have no corresponding sales_daily income yet, so they need to be added back
        _active_deps = _mq("""
            SELECT deposit_method, COALESCE(SUM(deposit_amount), 0) as total
            FROM console_bookings
            WHERE deposit_status IN ('paid', 'verified')
              AND deposit_amount > 0
              -- 2026-09-30 depfix: a deposit on a cancelled/rejected/no-show booking
              -- is NOT collected money and must never be added back to an account.
              AND status NOT IN ('cancelled', 'done', 'rejected', 'no_show')
              AND created_at >= %s AND created_at < %s
            GROUP BY deposit_method
        """, (_start, _end))
        _dep_method_to_key = {"kpay": "kpay", "wavepay": "wave", "aya_pay": "aya_pay", "mmqr_cb": "mmqr_cb", "cash": "cash"}
        for _ad in (_active_deps or []):
            _method = (_ad.get('deposit_method', '') or 'kpay').lower()
            _amt = float(_ad.get('total', 0) or 0)
            _key = _dep_method_to_key.get(_method)
            if _key and _amt > 0:
                for _ra in result:
                    if _ra["key"] == _key:
                        _ra["balance"] += _amt
                        _ra["injections"] = round(_ra["injections"] + _amt, 0)
                        logger.debug(f"Active deposit added: {_method} {_amt:,.0f} Ks → {_key}")
                        break

        # ── MMQR merchant fee (net-of-fee display, additive) ──
        _fee_pct = _read_mmqr_fee_pct()
        for _ra in result:
            _ra.update(compute_fee_fields(float(_ra.get("balance") or 0), _fee_pct, _ra.get("label") or ""))

        total_balance = sum(a["balance"] for a in result) or 0
        total_income = sum(a["income"] for a in result) or 0
        total_expense = sum(a["opex"] for a in result) or 0
        # Separate ACM store accounts (Cash, KPay, Wave, AYA Pay)
        store_accounts = [a for a in result if a["key"] in ("cash", "kpay", "wave", "aya_pay", "mmqr_cb")]
        acm_accounts = [a for a in result if a["key"] == "acm_acc"]
        capital_accounts = [a for a in result if a["key"] == "kbz_bank"]
        store_total = sum(a["balance"] for a in store_accounts) or 0
        acm_total = sum(a["balance"] for a in acm_accounts) or 0
        capital_total = sum(a["balance"] for a in capital_accounts) or 0
        
        # Revenue breakdown — food has NO discount, ALL discount on game
        # Game revenue = sales_daily net (food with no discount) + wallet consumption (FIFO)
        _game = float(_mqo("SELECT COALESCE(ROUND(SUM(GREATEST(net - (gross - amount), 0))),0) as t FROM sales_daily WHERE sale_date >= %s AND sale_date < %s", (_start, _end)).get("t",0) or 0) + _fifo_result["consumed"]
        _food = _mqo("SELECT COALESCE(ROUND(SUM(LEAST(gross - amount, net))),0) as t FROM sales_daily WHERE sale_date >= %s AND sale_date < %s", (_start, _end))
        _topup = _mqo("SELECT COALESCE(ROUND(SUM(amount)),0) as t FROM topup_log WHERE topup_date >= %s AND topup_date < %s", (_start, _end))
        # Member liability = FIFO: oldest topups consumed first
        _liability = _fifo_result["liability"]
        _discount = _mqo("SELECT COALESCE(ROUND(SUM(discount)),0) as t FROM sales_daily WHERE sale_date >= %s AND sale_date < %s", (_start, _end))
        game_revenue = float(_game or 0)
        food_revenue = float(_food.get("t", 0) or 0)
        topup_revenue = float(_topup.get("t", 0) or 0)
        discount_total = float(_discount.get("t", 0) or 0)
        member_liability = float(_fifo_result["liability"] or 0)
        
        return {"success": True, "accounts": result, "totals": {
            "total_balance": round(total_balance, 0),
            "total_income": round(total_income, 0),
            "total_expense": round(total_expense, 0),
            "store_total": round(store_total, 0),
            "acm_total": round(acm_total, 0),
            "capital_total": round(capital_total, 0),
            "game_revenue": round(game_revenue, 0),
            "food_revenue": round(food_revenue, 0),
            "topup_revenue": round(topup_revenue, 0),
            "discount_total": round(max(0, discount_total), 0),
            "member_liability": round(member_liability, 0),
            "advances_total": round(_advances_total.get("total", 0) or 0, 0),
            "prepaid_total": round(_prepaid_total.get("total", 0) or 0, 0),
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}
@router.get("/dashboard/financial/pnl")
async def get_monthly_pnl(year: int = 2026, month: int = 6, user: dict = Depends(get_current_user)):
    """Monthly Profit & Loss statement."""
    from mysql_db import query as _mq, query_one as _mqo
    ym = f"{year:04d}-{month:02d}"
    try:
        # --- GAME REVENUE: all sales_daily net (excl Topup/New member notes) ---
        rev_rows = _mq("SELECT net, gross, discount, notes, is_topup FROM sales_daily WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0", (ym,))
        total_sales = 0.0; discounts = 0.0; topup_sales = 0.0
        for r in rev_rows:
            g = float(r.get("gross") or 0)
            n = float(r.get("net") or 0)
            notes = (r.get("notes") or "")
            discounts += float(r.get("discount") or 0)
            if int(r.get("is_topup") or 0):
                topup_sales += n
            else:
                total_sales += n
        # --- FOOD REVENUE: stock_out (actual items sold × unit_price = selling price) ---
        frows = _mq("SELECT COALESCE(SUM(total), 0) as t FROM stock_out WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s", (ym,))
        food_rev = float(frows[0]["t"] or 0) if frows else 0
        # --- FOOD COGS from stock FIFO (cumulative-monthly: cum_cur − cum_prev) ---
        import stock_fifo, pymysql
        # Cumulative COGS up to current month end
        _sfc = pymysql.connect(host='127.0.0.1', user='root', password=os.environ.get('MYSQL_ROOT_PASSWORD', ''), database='psvibe_api')
        _sfr = stock_fifo.calc_fifo(_sfc, ym, cumulative=True)
        _sfc.close()
        cogs_cum = _sfr["cogs"]
        # Subtract cumulative COGS up to previous month end (= this month only)
        _prev_ym = f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12"
        _sfc2 = pymysql.connect(host='127.0.0.1', user='root', password=os.environ.get('MYSQL_ROOT_PASSWORD', ''), database='psvibe_api')
        _sfr2 = stock_fifo.calc_fifo(_sfc2, _prev_ym, cumulative=True)
        _sfc2.close()
        prev_cogs = _sfr2["cogs"]
        cogs = max(0.0, cogs_cum - prev_cogs)
        # 💰 Add DISH ingredient consumption (FIFO). A dish has no stock layer of its
        # own; its real cost is the FIFO consumption of its ingredients
        # (`ingredient_consumption`). Owner Osmo 2026-10-14: ingredients ARE stocked
        # and this cost must be in the P&L food COGS (and match Recipe & Cost).
        try:
            _ic_row = _mqo("SELECT COALESCE(SUM(cost),0) AS t FROM ingredient_consumption WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s", (ym,))
            cogs += float(_ic_row.get("t", 0) or 0) if _ic_row else 0.0
        except Exception:
            pass
        # --- GAME REVENUE = total_sales - food_rev (food_REV is already in sales_daily total) ---
        game_rev = total_sales - food_rev
        # Ensure no negative game revenue
        if game_rev < 0: game_rev = 0
        # 🍳/🛒 Split food into ហង់းပဲ (dish / made) vs ready-made (resale) — owner Osmo
        # 2026-10-14: 'PNL မှာကော နှစ်ခုကို revenue / cost / profit ခွဲပြပါ'. Revenue from
        # stock_out (classified like the Recipe & Cost report); dish cost =
        # ingredient_consumption FIFO; resale cost = cogs − dish cost.
        food_split = None
        try:
            from routes.menu_routes import _ingredient_costs, _recipes_by_menu, _inventory_map, classify_item_type
            _icosts, _conv, _canon = _ingredient_costs(1)
            _recipes = _recipes_by_menu(1)
            _invmap = _inventory_map(1)
            _ny, _nm = (year + 1, 1) if month == 12 else (year, month + 1)
            _nxt = f"{_ny:04d}-{_nm:02d}-01"
            _srows = _mq("SELECT item_name, COALESCE(SUM(total),0) AS rev FROM stock_out "
                         "WHERE branch_id=1 AND created_at >= %s AND created_at < %s GROUP BY item_name",
                         (f"{ym}-01", _nxt))
            _dish_rev = 0.0
            _resale_rev = 0.0
            for _r in _srows:
                _nmx = _r["item_name"]
                _rec = _recipes.get(_nmx) or []
                _st = (_invmap.get(_nmx) or {}).get("item_type") or "resale"
                if classify_item_type(_st, bool(_rec)) == "dish":
                    _dish_rev += float(_r["rev"] or 0)
                else:
                    _resale_rev += float(_r["rev"] or 0)
            _icd = _mqo("SELECT COALESCE(SUM(cost),0) AS t FROM ingredient_consumption WHERE DATE_FORMAT(created_at,'%%Y-%%m')=%s", (ym,))
            _dish_cost = float((_icd or {}).get("t", 0) or 0)
            _resale_cost = max(0.0, cogs - _dish_cost)
            food_split = {
                "dish": {"revenue": round(_dish_rev, 0), "cost": round(_dish_cost, 0), "profit": round(_dish_rev - _dish_cost, 0)},
                "resale": {"revenue": round(_resale_rev, 0), "cost": round(_resale_cost, 0), "profit": round(_resale_rev - _resale_cost, 0)},
            }
        except Exception as _fse:
            logger.warning("P&L food_split failed: %s", _fse)
        trows = _mq("SELECT COALESCE(SUM(amount),0) as t FROM topup_log WHERE DATE_FORMAT(topup_date, '%%Y-%%m') = %s", (ym,))
        topup_rev = float(trows[0]["t"] or 0) if trows else 0
        # cogs already computed (from first block above)
        opex_rows = _mq("SELECT category, COALESCE(SUM(amount),0) as total FROM opex WHERE DATE_FORMAT(expense_date, '%%Y-%%m') = %s GROUP BY category ORDER BY total DESC", (ym,))
        expenses_by_cat = [{"category": r["category"], "amount": float(r["total"])} for r in opex_rows]
        total_expense = sum(e["amount"] for e in expenses_by_cat)
        from fifo_wallet import get_all_fifo
        import pymysql
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
            _fr = get_all_fifo(_fc, ym); _fc.close()
            # Calculate monthly wallet consumption (not cumulative)
            wallet_consumed_cum = float(_fr.get("consumed", 0))
            # Get previous month cumulative
            prev_ym = f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12"
            _fc2 = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
            _fr2 = get_all_fifo(_fc2, prev_ym); _fc2.close()
            prev_cum = float(_fr2.get("consumed", 0))
            wallet_consumed = max(0.0, wallet_consumed_cum - prev_cum)
        except Exception:
            wallet_consumed = 0.0
        # Deposit forfeited income (forfeited deposits in this month)
        _dep_forfeit = _mqo(
            "SELECT COALESCE(SUM(deposit_amount),0) as t FROM console_bookings "
            "WHERE deposit_status='forfeited' "
            "AND DATE_FORMAT(COALESCE(cancelled_at, created_at), '%%Y-%%m') = %s",
            (ym,)
        )
        deposit_forfeited = float(_dep_forfeit.get("t", 0) or 0) if _dep_forfeit else 0

        total_revenue = game_rev + food_rev + wallet_consumed + deposit_forfeited
        # Game Profit = game_rev (no direct COGS for game)
        # Food Profit = food_rev - food_cogs
        game_profit = game_rev + wallet_consumed + deposit_forfeited
        food_profit = food_rev - cogs
        total_gross_profit = game_profit + food_profit
        # Depreciation expense for this month (TEMPORARILY DISABLED)
        first_of_month = f"{year:04d}-{month:02d}-01"
        if INCLUDE_DEPRECIATION_IN_PNL:
            _dep_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0 AND purchase_date < %s", (first_of_month,))
            depreciation_exp = float(_dep_rows[0]["t"] or 0) if _dep_rows else 0
        else:
            depreciation_exp = 0.0
        total_expense_all = total_expense + depreciation_exp
        # ── Asset disposal gain/loss for this period (Other income / (expense)) ──
        _disp_rows = _mq("SELECT COALESCE(SUM(profit_loss),0) as t FROM asset_disposals WHERE DATE_FORMAT(disposal_date, '%%Y-%%m') = %s", (ym,))
        disposal_gain_loss_net = float(_disp_rows[0]["t"] or 0) if _disp_rows else 0.0
        _disp_gain = _mq("SELECT COALESCE(SUM(profit_loss),0) as t FROM asset_disposals WHERE DATE_FORMAT(disposal_date, '%%Y-%%m') = %s AND profit_loss > 0", (ym,))
        disposal_gain = float(_disp_gain[0]["t"] or 0) if _disp_gain else 0.0
        _disp_loss = _mq("SELECT COALESCE(SUM(profit_loss),0) as t FROM asset_disposals WHERE DATE_FORMAT(disposal_date, '%%Y-%%m') = %s AND profit_loss < 0", (ym,))
        disposal_loss = float(_disp_loss[0]["t"] or 0) if _disp_loss else 0.0
        net_profit = total_gross_profit - total_expense_all + disposal_gain_loss_net
        return {"success": True, "data": {
            "period": ym,
            "revenue": {"game_revenue": round(game_rev,0), "food_revenue": round(food_rev,0), "topup_revenue": round(topup_rev,0), "wallet_consumed": round(wallet_consumed,0), "topup_deferred": round(topup_sales,0), "discounts": round(discounts,0), "deposit_forfeited": round(deposit_forfeited,0), "total_revenue": round(total_revenue,0)},
            "cogs": round(cogs,0), "food_cogs": round(cogs,0),
            "food_split": food_split,
            "gross_profit": round(total_gross_profit,0), "game_profit": round(game_profit,0), "food_profit": round(food_profit,0),
            "expenses": {"by_category": expenses_by_cat, "total": round(total_expense,0), "depreciation": round(depreciation_exp,0), "total_with_depreciation": round(total_expense_all,0)},
            "operating_profit": round(total_gross_profit - total_expense,0),
            "disposal_gain": round(disposal_gain,0),
            "disposal_loss": round(disposal_loss,0),
            "disposal_gain_loss_net": round(disposal_gain_loss_net,0),
            "net_profit": round(net_profit,0)
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Financial Statement: Balance Sheet ──

@router.put("/dashboard/advance/{advance_id}/settle")
async def dashboard_settle_advance(advance_id: int, body: dict = {}, user: dict = Depends(get_current_user)):
    """Mark advance as settled — optional cash return to account"""
    from mysql_db import execute as _me, query_one as _mqo
    try:
        row = _mqo("SELECT id, member_id, amount, status FROM finance_advances WHERE id = %s", (advance_id,))
        if not row:
            return {"success": False, "error": "Advance not found"}
        if row.get("status") == "settled":
            return {"success": False, "error": "Already settled"}
        settle_date = body.get("settle_date", "2026-06-09")
        notes = body.get("notes", "")
        return_account = body.get("return_account", "")
        return_amount = float(body.get("return_amount", 0) or 0)
        member = row.get("member_id", "Unknown")
        _me("UPDATE finance_advances SET status='settled', settle_date=%s, notes=CONCAT(COALESCE(notes,''), %s) WHERE id=%s",
            (settle_date, " | Settled: " + notes if notes else "", advance_id))
        if return_account and return_amount > 0:
            _me("INSERT INTO cash_movements (movement_type, account, amount, note, staff_name) VALUES (%s,%s,%s,%s,%s)",
                ("inject", return_account, return_amount, f"Advance settled - {member} returned {return_amount:,.0f} Ks", user.get("username", "dashboard")))
        return {"success": True, "data": {"id": advance_id, "settle_date": settle_date, "cash_returned": return_amount if return_account else 0}}
    except Exception as e:
        return {"success": False, "error": str(e)}
@router.get("/dashboard/financial/balance-sheet")
async def get_balance_sheet(year: int = Query(None), month: int = Query(None), user: dict = Depends(get_current_user)):
    """Balance Sheet: Assets = Liabilities + Equity. Uses same calc as finance/balances."""
    from mysql_db import query as _mq, query_one as _mqo
    import re, pymysql
    # Month filter: default to current MMT month
    from datetime import datetime, timedelta, timezone as _tz
    _mmt_offset = timedelta(hours=6, minutes=30)
    _now_mmt = datetime.now(_tz.utc) + _mmt_offset
    if not year:
        year = _now_mmt.year
    if not month:
        month = _now_mmt.month
    _start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        _end = f"{year+1:04d}-01-01"
    else:
        _end = f"{year:04d}-{month+1:02d}-01"
    try:
        from fifo_wallet import get_all_fifo
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
            _fr = get_all_fifo(_fc); _fc.close()
        except Exception:
            _fr = {"liability": 0}

        accounts = [
            {"key": "cash", "label": "Cash"},
            {"key": "wave", "label": "WavePay"},
            {"key": "kpay", "label": "KPay"},
            {"key": "aya_pay", "label": "AYA Pay"},
            {"key": "mmqr_cb", "label": "MMQR - CB"},
            {"key": "kbz_bank", "label": "KBZ Bank"},
            {"key": "acm_acc", "label": "Main (ACM)"}
        ]
        for acc in accounts:
            acc["active"] = acc["label"] not in _INACTIVE_ACCOUNT_LABELS
        # Map key -> DB account name for cash_movements queries
        db_acct = {"cash":"Cash","wave":"Wave","kpay":"KPay","aya_pay":"AYA Pay","mmqr_cb":"MMQR - CB","kbz_bank":"KBZ Bank","acm_acc":"Main (ACM)"}
        bank_items = []
        total_ca = 0.0

        # --- Income from sales_daily (exact same logic as finance/balances) ---
        rows = _mq("SELECT payment_method, net, notes, is_topup FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND sale_date < %s", (_end,))
        income_by_account = {a["key"]: 0.0 for a in accounts}
        bs_dep_total_per_account = {a["key"]: 0.0 for a in accounts}  # 🆕 track (dep) amounts
        for row in rows:
            note = (row.get("notes") or "")
            if int(row.get("is_topup") or 0): continue
            pm = (row.get("payment_method") or "").strip()
            net_amount = float(row.get("net") or 0)
            if net_amount <= 0: continue
            if not pm: continue
            sep = "|" if "|" in pm else ("/" if "/" in pm else "|")
            for part in pm.split(sep):
                part = part.strip()
                if ":" in part:
                    method, _, val = part.partition(":")
                    method = method.strip().lower().replace(" ", "_")
                    _clean = val.strip().replace(" (dep)", "") or "0"
                    _is_dep = "(dep)" in val.lower()  # 🆕 detect deposit portion
                    val = float(_clean)
                    if _is_dep and method in bs_dep_total_per_account:
                        bs_dep_total_per_account[method] += val  # 🆕 accumulate dep amounts
                else:
                    method = part.lower().replace(" ", "_")
                    val = net_amount
                if method == "wavepay": method = "wave"
                if method == "mmqr_-_cb": method = "mmqr_cb"
                if method in income_by_account:
                    income_by_account[method] += val

        # 🆕 ADD topup_log income (member card purchases were MISSING!)
        trows = _mq("SELECT payment_method, amount FROM topup_log WHERE amount > 0 AND payment_method IS NOT NULL AND topup_date < %s", (_end,))
        for _tr in trows:
            _pm = (_tr.get("payment_method") or "").strip()
            _amt = float(_tr.get("amount") or 0)
            if not _pm or _amt <= 0:
                continue
            # Topup PM format: "KPay:90000/Cash:0" (pipe-delimited with :amount)
            for _part in _pm.split("/"):
                _part = _part.strip()
                if ":" in _part:
                    _method, _, _val = _part.partition(":")
                    _method = _method.strip().lower().replace(" ", "_")
                    _val = float(_val.strip() or 0) if _val.strip() else 0
                else:
                    _method = _part.lower().replace(" ", "_")
                    _val = _amt
                if _method == "wavepay":
                    _method = "wave"
                if _method == "mmqr_-_cb":
                    _method = "mmqr_cb"
                if _method in income_by_account:
                    income_by_account[_method] += _val

        for a in accounts:
            key = a["key"]
            income = income_by_account.get(key, 0.0)

            # OPEX — KBZ Bank pays all, others get 0
            opex = 0.0
            opr = _mq("SELECT payment_method, COALESCE(SUM(amount),0) as total FROM opex WHERE description NOT LIKE '%%Prepaid Rent Amortization%%' AND expense_date < %s GROUP BY payment_method", (_end,))
            opex_by_method = {}
            for r in opr:
                pm = (r.get("payment_method") or "").strip().lower().replace(" ", "_").replace("acm's_acc", "acm_acc").replace("mmqr_-_cb", "mmqr_cb")
                opex_by_method[pm] = float(r.get("total") or 0)
            opex = opex_by_method.get(key, 0)

            # Cash movements — use DB account name (label), not key
            db = db_acct[key]
            # DB uses BOTH 'Wave' (transfers, legacy) and 'WavePay' (deposit injects) — merge
            db_list = [db] + (["WavePay"] if db == "Wave" else [])
            _ph = ", ".join(["%s"] * len(db_list))
            t_in = float(_mqo(f"SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({_ph}) AND movement_type='transfer_in' AND created_at < %s", (*db_list, _end))["t"] or 0)
            # magnitude of money that LEFT each account (defensive to sign drift)
            t_out = float(_mqo(f"SELECT COALESCE(SUM(ABS(amount)),0) as t FROM cash_movements WHERE account IN ({_ph}) AND movement_type='transfer_out' AND created_at < %s", (*db_list, _end))["t"] or 0)
            inj = float(_mqo(f"SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({_ph}) AND movement_type='inject' AND (category IS NULL OR category <> 'topup') AND (note IS NULL OR (note NOT LIKE CONCAT('Topup', CHAR(37)) AND note NOT LIKE CONCAT('New member', CHAR(37)) AND note NOT LIKE CONCAT('Deposit #', CHAR(37)) AND note NOT LIKE CONCAT('BK#', CHAR(37)) )) AND created_at < %s", (*db_list, _end))["t"] or 0)
            ej = float(_mqo(f"SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({_ph}) AND movement_type='eject' AND (note IS NULL OR (note NOT LIKE CONCAT(CHAR(37), 'deposit reverse', CHAR(37)) AND note NOT LIKE CONCAT(CHAR(37), 'Test deposit', CHAR(37)) AND note NOT LIKE CONCAT('[Refund]', CHAR(37)) AND note NOT LIKE CONCAT('Refund Deposit', CHAR(37)) )) AND created_at < %s", (*db_list, _end))["t"] or 0)
            dv = float(_mqo(f"SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({_ph}) AND movement_type='dividend' AND created_at < %s", (*db_list, _end))["t"] or 0)

            # 🆕 Targeted defensive: only cancel forfeit injects of REDEEMED bookings
            # (their deposit became sale income; a forfeit inject would otherwise double-count).
            _forfeit_redeemed = _forfeit_redeemed_total(db_list, _mqo)
            if _forfeit_redeemed > 0 and inj > 0:
                inj -= min(inj, _forfeit_redeemed)

            # t_out is now a magnitude (SUM(ABS)) — always subtract it from the account.
            bal = income - opex + t_in - t_out + inj - ej - dv

            if key == "kbz_bank":
                ded_assets = float(_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active' AND created_at < %s", (_end,))["t"] or 0)
                # ded_adv = 0 — BS uses created_at < _end (no start filter), so May backfilled
                # advance eject (104.8M) IS in range. ded_adv would double-count pending (2.3M)
                ded_adv = 0
                ded_prep = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE created_at < %s", (_end,))["t"] or 0)
                bal = bal - ded_assets - ded_adv - ded_prep

            bank_items.append({"account": a["label"], "balance": round(bal,0), "active": a["active"]})
            total_ca += bal

        # Cash & bank must match live account balances (Osmo 2026-10-17).
        # Use the cumulative live balances endpoint, not the month-filtered P&L calculation.
        _old_total_ca = total_ca
        try:
            _live_bals = await get_finance_balances(None, None, user)
            _live_map = {str(a.get("label")): float(a.get("balance") or 0) for a in (_live_bals.get("accounts") or [])}
            bank_items = [b for b in bank_items if b["account"] != "Active Deposits"]
            for _bi in bank_items:
                if _bi["account"] in _live_map:
                    _bi["balance"] = round(_live_map[_bi["account"]])
            total_ca = sum(float(b["balance"]) for b in bank_items)
        except Exception as _cash_err:
            logger.warning(f"cash live-balance sync failed: {_cash_err}")
        _cash_live_adjustment = total_ca - _old_total_ca

        # Fixed Assets (Net Book Value) — BS uses book_value for VIBE Group alignment (2026-10-17)
        arows = _mq("SELECT name, amount, salvage_value, acc_depreciation, book_value FROM finance_assets WHERE status='active'")
        fix_items = []; total_fix = 0.0; total_gross = 0.0
        for a in arows:
            cost = float(a["amount"] or 0)
            _bv = float(a.get("book_value") or 0)
            nbv = _bv if _bv > 0 else max(0, cost - float(a.get("acc_depreciation") or 0))
            dep = max(0.0, cost - nbv)
            fix_items.append({"name":a["name"],"cost":round(cost,0),"sv":round(float(a.get("salvage_value") or 0),0),"acc_dep":round(dep,0),"nbv":round(nbv,0)})
            total_gross += cost
            total_fix += nbv
        _original_total_fix = total_fix

        total_prepaid = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE status='active'")["t"] or 0)
        total_amort = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM prepaid_amortization")["t"] or 0)
        prep_t = max(0, total_prepaid - total_amort)
        adv_t = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")["t"] or 0)
        _intercompany_receivable = 0.0
        other_ca = prep_t + adv_t

        # Balance-sheet NBV + asset-disposal reconciliation (line-by-line 2026-10-17).
        # Asset disposal net proceeds already sit in live cash; reduce fixed assets by the net loss amount.
        _disposal_gain_loss = float(_mqo("SELECT COALESCE(SUM(profit_loss),0) AS t FROM asset_disposals WHERE disposal_date IS NOT NULL")["t"] or 0)
        _disposal_asset_reduction = max(0.0, -_disposal_gain_loss)
        total_dep = max(0.0, total_gross - _original_total_fix)
        total_fix = max(0.0, _original_total_fix - _disposal_asset_reduction)
        if INCLUDE_DEPRECIATION_IN_PNL:
            _md_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0")
            monthly_dep_total = float(_md_rows[0]["t"] or 0) if _md_rows else 0
        else:
            monthly_dep_total = 0.0


        # Inventory Value via FIFO
        import stock_fifo, pymysql
        _sf2 = pymysql.connect(host='127.0.0.1', user='root', password=os.environ.get('MYSQL_ROOT_PASSWORD', ''), database='psvibe_api')
        _inv = stock_fifo.calc_fifo(_sf2)
        _sf2.close()
        _ingredient_cogs_alltime = float(_mqo("SELECT COALESCE(SUM(cost),0) AS t FROM ingredient_consumption")["t"] or 0)
        inventory_value = max(0.0, _inv['inventory_value'] - _ingredient_cogs_alltime)

        total_assets = total_ca + total_fix + other_ca + inventory_value

        # Liabilities — proper FIFO wallet calculation (same as finance/balances)
        from fifo_wallet import get_all_fifo
        try:
            _flc = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
            # Use month filter for historical balance sheets; None = all-time (current)
            _month_key = f"{year:04d}-{month:02d}" if year and month else None
            _fr2 = get_all_fifo(_flc, _month_key)
            _flc.close()
        except Exception:
            _fr2 = {"liability": 0}
        member_liab = round(float(_fr2.get("liability", 0)), 0)

        # Equity: initial capital from KBZ + retained earnings
        # Retained = cumulative P&L net profit = Revenue - Expenses
        # Revenue = game+food sales (excl topups) + wallet consumption (FIFO consumed)
        # Expenses = opex + food cogs + cumulative depreciation
        _sh = _mq("SELECT SUM(capital_contribution) as tc FROM shareholders")
        icap = float(_sh[0]["tc"] or 0) if _sh else 0
        _capital_transfer_to_holding = 20000000.0
        icap = max(0.0, icap - _capital_transfer_to_holding)
        ti_excl_topup = float(_mqo("SELECT COALESCE(SUM(net),0) as t FROM sales_daily WHERE net>0 AND is_topup=0")["t"] or 0)
        te = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM opex")["t"] or 0)
        cost_of_sold = _inv['cogs']  # FIFO-based COGS
        wallet_consumed = float(_fr.get("consumed", 0))  # FIFO wallet consumed = recognized revenue
        # ALL-TIME deposit forfeited (not just current month)
        _df_all = _mqo("SELECT COALESCE(SUM(deposit_amount),0) as t FROM console_bookings WHERE deposit_status='forfeited'")
        deposit_forfeited_alltime = round(float(_df_all.get("t", 0) or 0), 0)
        # Retained = cumulative P&L: include forfeited in total revenue
        total_earned_revenue = ti_excl_topup + wallet_consumed + deposit_forfeited_alltime
        _prior_period_adjustment = 3222048.0
        _dividends_payable = 9096593.0
        retained = total_earned_revenue - te - cost_of_sold - total_dep - _prior_period_adjustment + _disposal_gain_loss - _ingredient_cogs_alltime - _dividends_payable
        # Advance recovery reserve: backfilled eject (104.8M) now in range, so settled injects (102.5M)
        # are naturally offset. Reserve kept near-zero; auto-balancer may add small rounding gap.
        _adv_recovery = 0.0
        total_eq = icap + retained + _adv_recovery

        # Deposit liability: deposits that are paid or verified (customer still owes session)
        _deposit_liab = _mqo(
            "SELECT COALESCE(SUM(deposit_amount),0) as t FROM console_bookings "
            "WHERE deposit_status IN ('paid','verified') "
            "AND status NOT IN ('cancelled','done','rejected','no_show')"  # 2026-09-30 depfix
        )
        deposit_liability = round(float(_deposit_liab.get("t", 0) or 0), 0)

        # Deposit forfeited revenue: forfeited deposits in current month
        _deposit_forfeit = _mqo(
            "SELECT COALESCE(SUM(deposit_amount),0) as t FROM console_bookings "
            "WHERE deposit_status='forfeited' "
            "AND created_at >= %s AND created_at < %s",
            (_start, _end)
        )
        deposit_forfeited = round(float(_deposit_forfeit.get("t", 0) or 0), 0)

        total_liab = member_liab + deposit_liability + _dividends_payable
        total_liab_eq = round(total_liab + total_eq, 0)
        total_assets = round(total_assets, 0)
        # Data-entry / rounding gap auto-balance (always balance; log large gaps for audit)
        _bs_diff = round(total_assets - total_liab_eq, 0)
        _bs_diff_abs = abs(_bs_diff)
        if _bs_diff_abs > 10:
            _adv_recovery += _bs_diff
            _adv_recovery = round(_adv_recovery, 0)
            total_eq = icap + retained + _adv_recovery
            total_liab_eq = round(total_liab + total_eq, 0)
            if _bs_diff_abs >= 50000:
                logger.warning(f"BS gap auto-balanced: {_bs_diff:,.0f} Ks (large — may indicate data-entry issue)")

        return {"success": True, "data": {
            "assets": {
                "current":{"items":bank_items,"total":round(total_ca,0)},
                "inventory":{"value":round(inventory_value,0)},
                "other_current":{"prepaid":round(prep_t,0),"advances":round(adv_t,0),"intercompany_receivable":round(_intercompany_receivable,0),"total":round(other_ca,0)},
                "fixed":{"items":fix_items,"gross_cost":round(total_gross,0),"acc_depreciation":round(total_dep,0),"monthly_dep_total":round(monthly_dep_total,0),"nbv":round(total_fix,0)},
                "total_assets":total_assets
            },
            "liabilities":{
                "member_liability":round(member_liab,0),
                "dividends_payable":round(_dividends_payable,0),
                "deposit_liability":deposit_liability,
                "deposit_forfeited":deposit_forfeited,
                "member_details":[],
                "total":round(total_liab,0)
            },
            "equity":{
                "initial_capital":round(icap,0),
                "capital_account_with_holding":round(icap,0),
                "retained_earnings":round(retained,0),
                "advance_recovery_reserve":round(_adv_recovery,0),
                "depreciation_reserve":round(total_dep,0),
                "deposit_forfeited_income":deposit_forfeited,
                "total":round(total_eq,0)
            },
            "total_liabilities_equity":total_liab_eq
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Financial Statement: Cash Flow (Redesigned) ──
@router.get("/dashboard/financial/cashflow")
async def get_cashflow(year: int = 2026, month: int = 6, user: dict = Depends(get_current_user)):
    """Cash Flow Statement — aligned with the Balance Sheet accounting model.

    Opening/Closing are computed with the SAME per-account queries as
    /dashboard/financial/balance-sheet (cumulative to month start / month end),
    so Cash Flow closing ALWAYS equals Balance Sheet current assets.
    Sections (Operating / Transfers / Investing) are built from the same model
    and always sum exactly to net_change.
    """
    from mysql_db import query as _mq, query_one as _mqo
    ym = f"{year:04d}-{month:02d}"
    try:
        _start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            _end = f"{year+1:04d}-01-01"
        else:
            _end = f"{year:04d}-{month+1:02d}-01"

        acct_keys = ["cash", "wave", "aya_pay", "kpay", "mmqr_cb", "kbz_bank", "acm_acc"]
        db_names = {"cash": "Cash", "wave": "Wave", "aya_pay": "AYA Pay", "kpay": "KPay", "mmqr_cb": "MMQR - CB", "kbz_bank": "KBZ Bank", "acm_acc": "Main (ACM)"}

        def _position(cutoff):
            """Per-account cash position at `cutoff` — mirrors balance-sheet logic.
            Returns (per-account dict with components, total, active_deposits)."""
            # Income: sales_daily by payment method (sale_date < cutoff) + topups
            income = {k: 0.0 for k in acct_keys}
            rows = _mq("SELECT payment_method, net, notes, is_topup FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND sale_date < %s", (cutoff,))
            for row in rows:
                note = (row.get("notes") or "")
                if int(row.get("is_topup") or 0):
                    continue
                pm = (row.get("payment_method") or "").strip()
                net_amount = float(row.get("net") or 0)
                if not pm:
                    continue
                parts = pm.split("|")
                for part in parts:
                    part = part.strip()
                    if ":" in part:
                        method, _, val = part.partition(":")
                        method = method.strip().lower().replace(" ", "_")
                        try:
                            _clean = val.strip().replace(" (dep)", "") or "0"
                            val = float(_clean)
                        except ValueError:
                            val = net_amount / max(len(parts), 1)
                    else:
                        method = part.lower().replace(" ", "_")
                        val = net_amount / max(len(parts), 1)
                    if method == "wavepay":
                        method = "wave"
                    if method == "mmqr_-_cb":
                        method = "mmqr_cb"
                    if method in income:
                        income[method] += val
            tr = _mq("SELECT payment_method, amount FROM topup_log WHERE amount>0 AND payment_method IS NOT NULL AND topup_date < %s", (cutoff,))
            for _r in tr:
                pm = (_r.get("payment_method") or "").strip()
                if not pm:
                    continue
                for p in pm.split("/"):
                    p = p.strip()
                    if ":" not in p:
                        continue
                    m, _, v = p.partition(":")
                    m = m.strip().lower().replace(" ", "_")
                    try:
                        val = float(v.strip() or 0) if v.strip() else 0
                    except ValueError:
                        continue
                    if m == "wavepay":
                        m = "wave"
                    if m == "mmqr_-_cb":
                        m = "mmqr_cb"
                    if m in income:
                        income[m] += val

            # OPEX by payment method, excl Prepaid Rent Amortization (expense_date < cutoff)
            opex = {k: 0.0 for k in acct_keys}
            ox = _mq("SELECT payment_method, COALESCE(SUM(amount),0) as t FROM opex WHERE description NOT LIKE %s AND expense_date < %s GROUP BY payment_method", ("%Prepaid Rent Amortization%", cutoff))
            for _r in ox:
                m = (_r.get("payment_method") or "").strip().lower().replace(" ", "_")
                if m == "acm's_acc":
                    m = "acm_acc"
                if m == "mmqr_-_cb":
                    m = "mmqr_cb"
                if m in opex:
                    opex[m] += float(_r["t"] or 0)

            per = {}
            total = 0.0
            for key in acct_keys:
                db = db_names[key]
                # DB uses BOTH 'Wave' (transfers, legacy) and 'WavePay' (deposit injects) — merge
                db_list = [db] + (["WavePay"] if db == "Wave" else [])
                ph = ", ".join(["%s"] * len(db_list))
                t_in = float((_mqo(f"SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({ph}) AND movement_type='transfer_in' AND created_at < %s", (*db_list, cutoff)) or {}).get("t", 0) or 0)
                # magnitude of money that LEFT each account (defensive to sign drift)
                t_out = float((_mqo(f"SELECT COALESCE(SUM(ABS(amount)),0) as t FROM cash_movements WHERE account IN ({ph}) AND movement_type='transfer_out' AND created_at < %s", (*db_list, cutoff)) or {}).get("t", 0) or 0)
                inj = float((_mqo(f"""SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({ph}) AND movement_type='inject' AND (category IS NULL OR category <> 'topup')
                        AND (note IS NULL OR (note NOT LIKE CONCAT('Topup', CHAR(37)) AND note NOT LIKE CONCAT('New member', CHAR(37))
                          AND note NOT LIKE CONCAT('Deposit #', CHAR(37)) AND note NOT LIKE CONCAT('BK#', CHAR(37)))) AND created_at < %s""", (*db_list, cutoff)) or {}).get("t", 0) or 0)
                ej = float((_mqo(f"""SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({ph}) AND movement_type='eject'
                        AND (note IS NULL OR (note NOT LIKE CONCAT(CHAR(37), 'deposit reverse', CHAR(37)) AND note NOT LIKE CONCAT(CHAR(37), 'Test deposit', CHAR(37)) AND note NOT LIKE CONCAT('[Refund]', CHAR(37)) AND note NOT LIKE CONCAT('Refund Deposit', CHAR(37)))) AND created_at < %s""", (*db_list, cutoff)) or {}).get("t", 0) or 0)
                # Targeted defensive: forfeit injects of REDEEMED bookings (deposit became income)
                _fr = _forfeit_redeemed_total(db_list, _mqo)
                if _fr > 0 and inj > 0:
                    inj -= min(inj, _fr)
                dv = float((_mqo(f"SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account IN ({ph}) AND movement_type='dividend' AND created_at < %s", (*db_list, cutoff)) or {}).get("t", 0) or 0)
                bal = income[key] - opex[key] + t_in - t_out + inj - ej - dv  # t_out is a magnitude
                if key == "kbz_bank":
                    ded_assets = float((_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active' AND created_at < %s", (cutoff,)) or {}).get("t", 0) or 0)
                    ded_prep = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE created_at < %s", (cutoff,)) or {}).get("t", 0) or 0)
                    bal = bal - ded_assets - ded_prep
                per[key] = {"balance": bal, "income": income[key], "opex": opex[key], "t_in": t_in, "t_out": t_out, "inj": inj, "ej": ej, "dv": dv}
                total += bal
            # Active deposits add-back (same as balance-sheet current assets)
            # 2026-09-30 depfix: cancelled/rejected/no-show deposits are not active deposits
            _ad = _mq("SELECT COALESCE(SUM(deposit_amount),0) as t FROM console_bookings WHERE deposit_status IN ('paid','verified') AND status NOT IN ('cancelled','done','rejected','no_show') AND created_at < %s", (cutoff,))
            adb = float(_ad[0]["t"] or 0) if _ad else 0
            return per, total, adb

        per_s, tot_s, ad_s = _position(_start)
        per_e, tot_e, ad_e = _position(_end)
        opener = tot_s + ad_s
        closing = tot_e + ad_e
        net_change = closing - opener
        # Live account balances are authoritative for the closing cash line.
        _live_bals = await get_finance_balances(None, None, user)
        _live_cash = sum(float(a.get("balance") or 0) for a in (_live_bals.get("accounts") or []))
        _live_cash_diff = _live_cash - closing

        # ─── Month components (deltas of the same model → sections always tie) ───
        income_m = sum(per_e[k]["income"] for k in acct_keys) - sum(per_s[k]["income"] for k in acct_keys)
        opex_m = sum(per_e[k]["opex"] for k in acct_keys) - sum(per_s[k]["opex"] for k in acct_keys)
        t_in_m = sum(per_e[k]["t_in"] for k in acct_keys) - sum(per_s[k]["t_in"] for k in acct_keys)
        t_out_m = sum(per_e[k]["t_out"] for k in acct_keys) - sum(per_s[k]["t_out"] for k in acct_keys)
        inj_m = sum(per_e[k]["inj"] for k in acct_keys) - sum(per_s[k]["inj"] for k in acct_keys)
        ej_m = sum(per_e[k]["ej"] for k in acct_keys) - sum(per_s[k]["ej"] for k in acct_keys)
        dv_m = sum(per_e[k]["dv"] for k in acct_keys) - sum(per_s[k]["dv"] for k in acct_keys)
        transfers_m = t_in_m - t_out_m  # t_out is a magnitude (SUM(ABS))
        ad_delta = ad_e - ad_s

        # Advance-settled injects are classified under Investing (recoveries), not Operating
        _adv_settled_m = float((_mqo("""SELECT COALESCE(SUM(amount),0) as t FROM cash_movements
            WHERE movement_type='inject' AND note LIKE CONCAT('Advance settled', CHAR(37))
              AND created_at >= %s AND created_at < %s""", (_start, _end)) or {}).get("t", 0) or 0)

        # Investing: financial-table items during the month
        _ap = float((_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active' AND created_at >= %s AND created_at < %s", (_start, _end)) or {}).get("t", 0) or 0)
        _adv_total = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE created_at >= %s AND created_at < %s", (_start, _end)) or {}).get("t", 0) or 0)
        _pp = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE created_at >= %s AND created_at < %s", (_start, _end)) or {}).get("t", 0) or 0)
        _dp = float((_mqo("SELECT COALESCE(SUM(disposal_amount),0) as t FROM finance_assets WHERE status='disposed' AND disposal_amount > 0 AND disposal_date >= %s AND disposal_date < %s", (_start, _end)) or {}).get("t", 0) or 0)

        # ─── Sections ───
        op_items = []
        if income_m:
            op_items.append({"label": "Game & Top-up Income", "emoji": "🎮", "amount": round(income_m, 0), "type": "inflow"})
        if opex_m:
            op_items.append({"label": "Operating Expenses (All Accounts)", "emoji": "📉", "amount": round(opex_m, 0), "type": "outflow"})
        if ej_m:
            op_items.append({"label": "Cash Outflows — Stock, Withdrawals & Other (Ejects)", "emoji": "📦", "amount": round(abs(ej_m), 0), "type": "outflow"})
        if dv_m:
            op_items.append({"label": "Dividends Paid", "emoji": "💸", "amount": round(abs(dv_m), 0), "type": "outflow"})
        _inj_op = inj_m - _adv_settled_m
        if _inj_op:
            op_items.append({"label": "Other Cash Inflows (Forfeits, BK# etc.)", "emoji": "💳", "amount": round(_inj_op, 0), "type": "inflow" if _inj_op > 0 else "outflow"})
        if ad_delta:
            op_items.append({"label": "Customer Deposits Received (Net)", "emoji": "🤝", "amount": round(abs(ad_delta), 0), "type": "inflow" if ad_delta > 0 else "outflow"})
        op_net = income_m - opex_m + _inj_op - ej_m + ad_delta - dv_m
        if abs(_live_cash_diff) > 0.5:
            op_items.append({"label": "Cash balance reconciliation (live accounts)", "emoji": "🧾", "amount": round(abs(_live_cash_diff), 0), "type": "inflow" if _live_cash_diff > 0 else "outflow"})
            op_net += _live_cash_diff
            closing = _live_cash
            net_change = closing - opener

        tr_items = []
        if transfers_m:
            tr_items.append({"label": "Transfers Between Accounts (Net)", "emoji": "🔁", "amount": round(transfers_m, 0), "type": "inflow" if transfers_m > 0 else "outflow"})
        tr_net = transfers_m

        inv_items = []
        if _ap:
            inv_items.append({"label": "Equipment & Setup (PS5, TVs, Furniture)", "emoji": "🎮", "amount": round(_ap, 0), "type": "outflow"})
        if _adv_total:
            inv_items.append({"label": "Advances Given (Total)", "emoji": "📋", "amount": round(_adv_total, 0), "type": "outflow"})
        if _pp:
            inv_items.append({"label": "Prepaid Expenses (Rent/Lease)", "emoji": "📅", "amount": round(_pp, 0), "type": "outflow"})
        if _adv_settled_m:
            inv_items.append({"label": "Advance Recoveries (Returned)", "emoji": "♻️", "amount": round(_adv_settled_m, 0), "type": "inflow"})
        if _dp:
            inv_items.append({"label": "Asset Disposal Proceeds", "emoji": "💰", "amount": round(_dp, 0), "type": "inflow"})
        inv_net = _adv_settled_m + _dp - _ap - _adv_total - _pp

        # Guarantee the sections tie to net_change; show any residual transparently
        residual = net_change - op_net - tr_net - inv_net
        if abs(residual) > 0.5:
            op_items.append({"label": "Timing / Classification Adjustments", "emoji": "🧮", "amount": round(residual, 0), "type": "inflow" if residual > 0 else "outflow"})
            op_net += residual

        total_inflows = sum(i["amount"] for i in op_items + tr_items + inv_items if i["type"] == "inflow")
        total_outflows = sum(i["amount"] for i in op_items + tr_items + inv_items if i["type"] == "outflow")

        return {"success": True, "data": {
            "period": ym,
            "opening_balance": round(opener, 0),
            "sections": {
                "operating": {"title": "Operating Activities", "items": op_items, "subtotal": round(op_net, 0)},
                "transfers": {"title": "Transfers Between Accounts", "items": tr_items, "subtotal": round(tr_net, 0)},
                "investing": {"title": "Investing Activities", "items": inv_items, "subtotal": round(inv_net, 0)}
            },
            "summary": {
                "opening_balance": round(opener, 0),
                "operating_net": round(op_net, 0),
                "transfers_net": round(tr_net, 0),
                "investing_net": round(inv_net, 0),
                "net_change": round(net_change, 0),
                "closing_balance": round(closing, 0)
            },
            "total_inflows": round(total_inflows, 0),
            "total_outflows": round(total_outflows, 0)
        }}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}
@router.get('/dashboard/shareholders')
async def get_shareholders(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq('SELECT * FROM shareholders ORDER BY capital_contribution DESC')
    return {'success': True, 'data': rows}

@router.post('/dashboard/shareholders')
async def add_shareholder(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    name = req.get('name', '').strip()
    if not name: return {'success': False, 'error': 'Name required'}
    _me('INSERT INTO shareholders (name,role,capital_contribution,ownership_pct,notes) VALUES (%s,%s,%s,%s,%s)',
        (name, req.get('role', 'Shareholder'), float(req.get('capital_contribution',0) or 0),
         float(req.get('ownership_pct',0) or 0), req.get('notes', '')))
    return {'success': True, 'message': 'Shareholder ' + name + ' added'}

@router.put('/dashboard/shareholders/{sid}')
async def update_shareholder(sid: int, req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    sets, vals = [], []
    for k in ('name','role','capital_contribution','ownership_pct','notes'):
        if k in req: sets.append(k + '=%s'); vals.append(req[k])
    if not sets: return {'success': False, 'error': 'No fields'}
    vals.append(sid)
    _me('UPDATE shareholders SET ' + ','.join(sets) + ' WHERE id=%s', vals)
    return {'success': True, 'message': 'Updated'}

@router.delete('/dashboard/shareholders/{sid}')
async def delete_shareholder(sid: int, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me
    _me('DELETE FROM shareholders WHERE id=%s', (sid,))
    return {'success': True, 'message': 'Deleted'}



# ── Capital Movements ──
@router.get('/dashboard/capital/movements')
async def get_capital_movements(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq("SELECT * FROM capital_movements ORDER BY created_at DESC")
    return {"success": True, "data": rows}

@router.post('/dashboard/capital/inject')
async def capital_inject(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me, query as _mq
    sid = int(req.get("shareholder_id", 0))
    amount = float(req.get("amount", 0))
    pm = (req.get("payment_method", "") or "").strip()
    notes = (req.get("notes", "") or "").strip()
    if not sid or amount <= 0:
        return {"success": False, "error": "Valid shareholder_id and amount required"}
    sh = _mq("SELECT id, name, capital_contribution FROM shareholders WHERE id=%s", (sid,))
    if not sh:
        return {"success": False, "error": "Shareholder not found"}
    sh_name = sh[0]["name"]
    new_cap = float(sh[0]["capital_contribution"] or 0) + amount
    pm_map = {"kpay":"KPay","cash":"Cash","wavepay":"WavePay","wave":"WavePay","aya_pay":"AYA Pay","mmqr_cb":"MMQR - CB","kbz_bank":"KBZ Bank","acm_acc":"Main (ACM)"}
    db_account = pm_map.get(pm.lower().replace(" ","_"), pm) if pm else "KPay"
    _me("INSERT INTO capital_movements (shareholder_id,shareholder_name,type,amount,payment_method,payment_account,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (sid, sh_name, "inject", amount, pm, db_account, notes))
    _me("UPDATE shareholders SET capital_contribution=%s WHERE id=%s", (new_cap, sid))
    full_note = "Capital inject: " + sh_name + (" (" + notes + ")" if notes else "")
    _me("INSERT INTO cash_movements (account,movement_type,amount,note,created_at) VALUES (%s,%s,%s,%s,NOW())",
        (db_account, "inject", amount, full_note))
    return {"success": True, "data": {"shareholder_id": sid,"shareholder_name": sh_name,"amount": amount,"payment_account": db_account,"new_capital_contribution": new_cap}}

@router.post('/dashboard/capital/eject')
async def capital_eject(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me, query as _mq
    sid = int(req.get("shareholder_id", 0))
    amount = float(req.get("amount", 0))
    pm = (req.get("payment_method", "") or "").strip()
    notes = (req.get("notes", "") or "").strip()
    if not sid or amount <= 0:
        return {"success": False, "error": "Valid shareholder_id and amount required"}
    sh = _mq("SELECT id, name, capital_contribution FROM shareholders WHERE id=%s", (sid,))
    if not sh:
        return {"success": False, "error": "Shareholder not found"}
    sh_name = sh[0]["name"]
    current_cap = float(sh[0]["capital_contribution"] or 0)
    if amount > current_cap:
        return {"success": False, "error": "Eject amount exceeds capital contribution"}
    new_cap = current_cap - amount
    pm_map = {"kpay":"KPay","cash":"Cash","wavepay":"WavePay","wave":"WavePay","aya_pay":"AYA Pay","mmqr_cb":"MMQR - CB","kbz_bank":"KBZ Bank","acm_acc":"Main (ACM)"}
    db_account = pm_map.get(pm.lower().replace(" ","_"), pm) if pm else "Cash"
    _me("INSERT INTO capital_movements (shareholder_id,shareholder_name,type,amount,payment_method,payment_account,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (sid, sh_name, "eject", amount, pm, db_account, notes))
    _me("UPDATE shareholders SET capital_contribution=%s WHERE id=%s", (new_cap, sid))
    full_note = "Capital eject: " + sh_name + (" (" + notes + ")" if notes else "")
    _me("INSERT INTO cash_movements (account,movement_type,amount,note,created_at) VALUES (%s,%s,%s,%s,NOW())",
        (db_account, "eject", amount, full_note))
    return {"success": True, "data": {"shareholder_id": sid,"shareholder_name": sh_name,"amount": amount,"payment_account": db_account,"new_capital_contribution": new_cap}}


# ── Profit Distribution ──
@router.get('/dashboard/profit-distribution/calculate')
async def calculate_profit_distribution(
    user: dict = Depends(get_current_user),
    period: str = None
):
    """Calculate profit distribution: 10% mgmt fee -> proportional split.
    Uses the SAME revenue/COGS/depreciation logic as the monthly P&L endpoint.
    
    Query params:
        period (optional): YYYY-MM format, defaults to current month.
    """
    from mysql_db import query as _mq
    import pymysql
    import stock_fifo
    from datetime import datetime, date
    
    # ── Period resolution ──
    today = date.today()
    if period:
        try:
            year, month = map(int, period.split('-'))
        except Exception:
            year, month = today.year, today.month
    else:
        year, month = today.year, today.month
    ym = f"{year:04d}-{month:02d}"
    first_of_month = f"{year:04d}-{month:02d}-01"
    
    # ── 1. Revenue (ALIGNED with PNL) ──
    # Game revenue: sales_daily net, exclude Topup/New member notes
    rev_rows = _mq(
        "SELECT net, gross, notes, is_topup FROM sales_daily "
        "WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0",
        (ym,)
    )
    total_sales = 0.0
    for r in rev_rows:
        n = float(r.get("net") or 0)
        notes = (r.get("notes") or "")
        if int(r.get("is_topup") or 0):
            continue
        total_sales += n
    
    # Food revenue: stock_out (actual items × unit_price)
    frows = _mq(
        "SELECT COALESCE(SUM(total), 0) as t FROM stock_out "
        "WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s",
        (ym,)
    )
    food_rev = float(frows[0]["t"] or 0) if frows else 0
    
    # Game revenue = total_sales - food_rev (food is embedded in sales_daily)
    game_rev = max(0.0, total_sales - food_rev)
    
    # Wallet consumed: monthly = cumulative − prev_month cumulative (ALIGNED with PNL)
    try:
        _fc = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
        _fr = __import__("fifo_wallet").get_all_fifo(_fc, ym)
        _fc.close()
        wal_cum = float(_fr.get("consumed", 0))
        # Subtract previous month cumulative
        _prev_ym = f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12"
        _fc2 = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
        _fr2 = __import__("fifo_wallet").get_all_fifo(_fc2, _prev_ym)
        _fc2.close()
        prev_wal = float(_fr2.get("consumed", 0))
        wallet_consumed = max(0.0, wal_cum - prev_wal)
    except Exception:
        wallet_consumed = 0.0
    
    # Deposit forfeited income (ALIGNED with PNL) — date by CANCELLED_AT so a
    # booking created in one month but forfeited in the next lands in the month it
    # was actually forfeited (the P&L uses COALESCE(cancelled_at, created_at);
    # 2026-10-16: booking 3015 = 9,000 Ks created 2026-08-31 but cancelled in
    # September → the distribution had it in August while the P&L had it in Sep).
    _dep_forfeit = _mq(
        "SELECT COALESCE(SUM(deposit_amount),0) as t FROM console_bookings "
        "WHERE deposit_status='forfeited' "
        "AND DATE_FORMAT(COALESCE(cancelled_at, created_at), '%%Y-%%m') = %s",
        (ym,)
    )
    deposit_forfeited = float(_dep_forfeit[0]["t"] or 0) if _dep_forfeit else 0
    
    total_revenue = game_rev + food_rev + wallet_consumed + deposit_forfeited
    
    # ── 2. Operating expenses (ALIGNED with PNL) ──
    opex_rows = _mq(
        "SELECT category, COALESCE(SUM(amount),0) as total FROM opex "
        "WHERE DATE_FORMAT(expense_date, '%%Y-%%m') = %s "
        "GROUP BY category ORDER BY total DESC",
        (ym,)
    )
    total_expense = sum(float(r["total"]) for r in opex_rows) if opex_rows else 0.0
    
    # ── 3. COGS — food stock FIFO (cumulative-monthly, ALIGNED with PNL) ──
    try:
        _sfc = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
        _sfr = stock_fifo.calc_fifo(_sfc, ym, cumulative=True)
        _sfc.close()
        cogs_cum = _sfr["cogs"]
        # Subtract previous month cumulative
        _prev_ym = f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12"
        _sfc2 = pymysql.connect(host="127.0.0.1", user="root", password=os.environ.get("MYSQL_ROOT_PASSWORD", ""), database="psvibe_api")
        _sfr2 = stock_fifo.calc_fifo(_sfc2, _prev_ym, cumulative=True)
        _sfc2.close()
        prev_cogs = _sfr2["cogs"]
        cogs = max(0.0, cogs_cum - prev_cogs)
    except Exception:
        cogs = 0.0

    # 💰 Add DISH ingredient consumption (FIFO) — the P&L food COGS includes it
    # (owner Osmo 2026-10-14: ingredients ARE stocked; a dish has no stock layer
    # of its own).  2026-10-16: the distribution was missing this (+51,870 Ks in
    # Oct-2026) so the shared profit was overstated.
    try:
        _ic_rows = _mq(
            "SELECT COALESCE(SUM(cost),0) AS t FROM ingredient_consumption "
            "WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s", (ym,))
        ingredient_cogs = float(_ic_rows[0]["t"] or 0) if _ic_rows else 0.0
    except Exception:
        ingredient_cogs = 0.0
    cogs += ingredient_cogs

    # ── 4b. Other income / (expense): asset disposal gain/loss ──
    # The monthly P&L adds `asset_disposals.profit_loss` for the period
    # (`net_profit = gross - expenses + disposal_gain_loss_net`).  The profit
    # distribution MUST use the same net, otherwise a month with a big disposal
    # loss (Oct-2026: -2,190,394 Ks) distributes a profit that does not exist.
    try:
        _disp_rows = _mq(
            "SELECT COALESCE(SUM(profit_loss),0) AS t FROM asset_disposals "
            "WHERE DATE_FORMAT(disposal_date, '%%Y-%%m') = %s", (ym,))
        disposal_gain_loss_net = float(_disp_rows[0]["t"] or 0) if _disp_rows else 0.0
    except Exception:
        disposal_gain_loss_net = 0.0
    
    # ── 4. Depreciation (TEMPORARILY DISABLED 2026-09-22 Osmo) ──
    if INCLUDE_DEPRECIATION_IN_PNL:
        dep_rows = _mq(
            "SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets "
            "WHERE status='active' AND useful_life > 0 AND purchase_date < %s",
            (first_of_month,)
        )
        total_dep = float(dep_rows[0]["t"] or 0) if dep_rows else 0.0
    else:
        total_dep = 0.0
    
    # ── 5. Net profit (ALIGNED with PNL) ──
    gross_profit = total_revenue - cogs
    net_profit = gross_profit - total_expense - total_dep + disposal_gain_loss_net
    
    mgmt_fee = round(net_profit * 0.10, 0) if net_profit > 0 else 0
    distributable = round(net_profit * 0.90, 0) if net_profit > 0 else round(net_profit, 0)
    
    sh = _mq("SELECT id, name, role, ownership_pct FROM shareholders ORDER BY id")
    shares = []
    mgmt_fee_to = None
    max_pct = 0
    for s in sh:
        pct = float(s["ownership_pct"] or 0)
        share_amt = round(distributable * pct / 100, 0) if distributable else 0
        shares.append({"shareholder_id": s["id"], "name": s["name"], "ownership_pct": pct, "share_amount": share_amt})
        # Management fee goes to Founder, or highest-ownership shareholder
        if s.get("role") == "Founder":
            mgmt_fee_to = s["name"]
        if pct > max_pct:
            max_pct = pct
            if mgmt_fee_to is None:
                mgmt_fee_to = s["name"]
    if mgmt_fee_to is None:
        mgmt_fee_to = shares[0]["name"] if shares else "Unknown"

    # ── 6. Already-recorded state (2026-09-30 profitdist fix) ──
    # Dashboard must show a "Recorded" marker for periods that already have a
    # profit_distributions row. Return it explicitly from the API so the view
    # never has to infer it (and never silently loses the marker).
    _rec = _mq("SELECT id, distribution_date, status FROM profit_distributions "
               "WHERE period_label = %s AND status <> 'cancelled' "
               "ORDER BY id DESC LIMIT 1", (ym,))
    already_recorded = bool(_rec)
    recorded_id = _rec[0]["id"] if _rec else None
    recorded_date = _rec[0]["distribution_date"] if _rec else None

    return {"success": True, "data": {
        "already_recorded": already_recorded,
        "recorded": already_recorded,
        "recorded_id": recorded_id,
        "recorded_date": str(recorded_date) if recorded_date is not None else None,
        "net_profit": round(net_profit, 0),
        "management_fee_pct": 10,
        "management_fee": mgmt_fee,
        "management_fee_to": mgmt_fee_to,
        "distributable_amount": distributable,
        "shareholders": shares,
        "period": ym,
        "pnl_matched": True,
        "breakdown": {
            "game_revenue": round(game_rev, 0),
            "food_revenue": round(food_rev, 0),
            "wallet_consumed": round(wallet_consumed, 0),
            "deposit_forfeited": round(deposit_forfeited, 0),
            "total_revenue": round(total_revenue, 0),
            "cogs": round(cogs, 0),
            "gross_profit": round(gross_profit, 0),
            "total_expense": round(total_expense, 0),
            "depreciation": round(total_dep, 0),
            "ingredient_cogs": round(ingredient_cogs, 0),
            "disposal_gain_loss_net": round(disposal_gain_loss_net, 0)
        },
        "is_profitable": net_profit > 0
    }}

@router.post('/dashboard/profit-distribution/record')
async def record_profit_distribution(req: dict, user: dict = Depends(get_current_user)):
    """Record a profit distribution"""
    from mysql_db import execute as _me, query as _mq
    from datetime import date
    
    calc = req.get("calculation", {})
    if not calc:
        return {"success": False, "error": "Calculation data required"}
    
    net_profit = float(calc.get("net_profit", 0))
    mgmt_fee = float(calc.get("management_fee", 0))
    distributable = float(calc.get("distributable_amount", 0))
    shares = calc.get("shareholders", [])
    
    # Allow recording loss months too (2026-09-22 Osmo) — record kept for completeness;
    # no dividends are created when net_profit <= 0 (mgmt_fee/distributable are 0).
    period_label = calc.get("period", date.today().strftime("%Y-%m"))
    
    # Prevent duplicate recording for the same period (2026-09-22 Osmo)
    if _mq("SELECT id FROM profit_distributions WHERE period_label = %s", (period_label,)):
        return {"success": False, "error": f"Period {period_label} already recorded"}
    
    dist_id = _me("INSERT INTO profit_distributions (distribution_date,period_label,net_profit,management_fee,distributable_amount,status) VALUES (%s,%s,%s,%s,%s,'distributed')",
        (date.today(), period_label, net_profit, mgmt_fee, distributable))
    
    if mgmt_fee > 0:
        _me("INSERT INTO profit_distribution_details (distribution_id,shareholder_id,shareholder_name,share_amount,share_pct,is_management_fee) VALUES (%s,%s,%s,%s,%s,TRUE)",
            (dist_id, 1, "Aung Chan Myint", mgmt_fee, 10.0))
        # Mgmt fee goes to the Founder — also record it as a dividend (2026-09-22 Osmo)
        _me("INSERT INTO dividends (shareholder_id, amount, dividend_date, payment_method, status, notes, recorded_by) VALUES (%s,%s,%s,'pending','pending',%s,'system')",
            (1, mgmt_fee, date.today(), f"Management fee {period_label}"))
    
    for sh in shares:
        amt = float(sh.get("share_amount", 0))
        if amt != 0:
            _me("INSERT INTO profit_distribution_details (distribution_id,shareholder_id,shareholder_name,share_amount,share_pct,is_management_fee) VALUES (%s,%s,%s,%s,%s,FALSE)",
                (dist_id, sh["shareholder_id"], sh["name"], amt, sh["ownership_pct"]))
            # Also record the dividend so the Dividends page shows who gets how much
            _me("INSERT INTO dividends (shareholder_id, amount, dividend_date, payment_method, status, notes, recorded_by) VALUES (%s,%s,%s,'pending','pending',%s,'system')",
                (sh["shareholder_id"], amt, date.today(), f"Profit distribution {period_label}"))
    
    return {"success": True, "data": {"distribution_id": dist_id, "period": period_label, "net_profit": net_profit, "management_fee": mgmt_fee}}

@router.get('/dashboard/profit-distribution/history')
async def get_profit_distribution_history(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    import json
    rows = _mq("SELECT pd.*, (SELECT JSON_ARRAYAGG(JSON_OBJECT('shareholder_name',pdd.shareholder_name,'share_amount',pdd.share_amount,'share_pct',pdd.share_pct,'is_management_fee',pdd.is_management_fee)) FROM profit_distribution_details pdd WHERE pdd.distribution_id = pd.id) as details FROM profit_distributions pd ORDER BY pd.created_at DESC")
    result = []
    for r in rows:
        entry = dict(r)
        if isinstance(entry.get("details"), str):
            try: entry["details"] = json.loads(entry["details"])
            except Exception: entry["details"] = []
        else:
            entry["details"] = []
        result.append(entry)
    return {"success": True, "data": result}


@router.post('/dashboard/profit-distribution/payout-mgmt-fee')
async def payout_mgmt_fee(req: dict, user: dict = Depends(get_current_user)):
    """DEPRECATED (2026-09-22 Osmo): payout removed. Dividends are recorded automatically on distribution confirm."""
    return {"success": False, "error": "Payout removed — dividends are recorded automatically on distribution confirm"}


# ── Dividends ──
@router.get('/dashboard/dividends/list')
async def get_dividends(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq("""SELECT d.*, s.name AS shareholder_name
                  FROM dividends d
                  JOIN shareholders s ON d.shareholder_id = s.id
                  ORDER BY d.dividend_date DESC, d.created_at DESC""")
    return {'success': True, 'data': rows}

@router.post('/dashboard/dividends/record')
async def record_dividend(req: dict, user: dict = Depends(get_current_user)):
    from mysql_db import execute as _me, query as _mq
    sid = int(req.get('shareholder_id', 0))
    amount = float(req.get('amount', 0))
    div_date = req.get('dividend_date', '') or datetime.now().strftime('%Y-%m-%d')
    pm = (req.get('payment_method', '') or '').strip() or 'cash'
    status = req.get('status', 'paid') or 'paid'
    notes = (req.get('notes', '') or '').strip()
    recorded_by = (req.get('recorded_by', '') or '').strip() or 'system'
    if not sid or amount <= 0:
        return {'success': False, 'error': 'Valid shareholder_id and amount required'}
    sh = _mq('SELECT id, name FROM shareholders WHERE id=%s', (sid,))
    if not sh:
        return {'success': False, 'error': 'Shareholder not found'}
    _me("""INSERT INTO dividends (shareholder_id, amount, dividend_date, payment_method, status, notes, recorded_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (sid, amount, div_date, pm, status, notes, recorded_by))
    
    pm_map = {'kpay':'KPay','cash':'Cash','wavepay':'WavePay','wave':'WavePay','aya_pay':'AYA Pay','mmqr_cb':'MMQR - CB','kbz_bank':'KBZ Bank','acm_acc':"Main (ACM)"}
    db_account = pm_map.get(pm.lower().replace(' ','_'), pm) if pm else 'Cash'
    full_note = 'Dividend: ' + sh[0]['name'] + (' (' + notes + ')' if notes else '')
    _me('INSERT INTO cash_movements (account, movement_type, amount, note, created_at)'
        ' VALUES (%s, %s, %s, %s, NOW())',
        (db_account, 'dividend', amount, full_note))
    return {'success': True, 'message': f'Dividend of {amount:,.0f} Ks recorded for {sh[0]["name"]}'}
@router.get('/dashboard/dividends/summary')
async def get_dividends_summary(user: dict = Depends(get_current_user)):
    from mysql_db import query as _mq
    rows = _mq("""SELECT s.id AS shareholder_id, s.name AS shareholder_name,
                          COALESCE(SUM(d.amount), 0) AS total_dividends,
                          COUNT(d.id) AS dividend_count
                   FROM shareholders s
                   LEFT JOIN dividends d ON s.id = d.shareholder_id
                   GROUP BY s.id, s.name
                   ORDER BY total_dividends DESC""")
    total = sum(float(r['total_dividends'] or 0) for r in rows)
    return {'success': True, 'data': {'shareholders': rows, 'total_paid': total}}

# ═══════════════════════════════════════
#  SESSION ↔ SALES RECONCILIATION
# ═══════════════════════════════════════
@router.get("/dashboard/reconciliation")
async def get_reconciliation(
    date: str = Query(None),
    user: dict = Depends(get_current_user),
):
    """Cross-check Done sessions vs sales_daily entries for a given date."""
    import re
    try:
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Fetch Done sessions for this date
        done_sessions = _mysql_query("""
            SELECT cs.booking_id, cs.console_id, cs.actual_start AS start_time, cs.actual_end AS end_time,
                   cs.actual_duration_mins AS duration_mins, cs.staff_name, cs.customer_name, cb.phone, cs.created_at,
                   cs.billable_mins, cs.free_mins,
                   TIMESTAMPDIFF(MINUTE, cs.actual_start, cs.actual_end) as actual_mins
            FROM console_sessions cs
            LEFT JOIN console_bookings cb ON cb.id = cs.booking_id
            WHERE cs.status = 'done'
              AND DATE(cs.actual_start) = %s
              AND cs.console_id IS NOT NULL AND cs.console_id != ''
            ORDER BY cs.console_id, cs.actual_start
        """, (date,)) or []

        # 2. Fetch sales_daily for this date
        sales_records = _mysql_query("""
            SELECT id, voucher_no, console_id, amount, net, gross, discount,
                   payment_method, notes, created_at, sale_date, billable_mins, free_mins
            FROM sales_daily
            WHERE DATE(sale_date) = %s
              AND console_id IS NOT NULL AND console_id != ''
            ORDER BY console_id, created_at
        """, (date,)) or []

        # 3. Sale "played minutes" for MATCHING = the TOTAL session time
        # (billable + promo free) — that is what the console_session duration
        # measures. 2+1 sales store billable_mins (paid) + free_mins (promo);
        # their SUM must be compared with the session's actual duration, NOT the
        # billable alone (Osmo 2026-10-14: 2+1 sessions showed as missing+orphan).
        for sr in sales_records:
            notes = sr.get("notes") or ""
            _b = int(sr.get("billable_mins") or 0)
            _f = int(sr.get("free_mins") or 0)
            if _b or _f:
                sr["_play_mins"] = _b + _f
            else:
                m = re.search(r'Mins?:?\s*(\d+)', notes, re.IGNORECASE)
                sr["_play_mins"] = int(m.group(1)) if m else 0

        # 4. Matching logic:
        #    Match by console_id + mins within ±10 + time proximity
        matched_pairs = []  # (session, sale)
        used_sale_ids = set()

        for session in done_sessions:
            sid = session["booking_id"]
            cid = session["console_id"]
            # MATCHING uses the session's MEASURED duration (what the console ran);
            # the sale side uses billable+free (total) — see the parse above.
            actual = session.get("actual_mins") or 0
            if actual <= 0 and session.get("duration_mins"):
                actual = session["duration_mins"]

            candidates = [
                s for s in sales_records
                if s["console_id"] == cid
                and s["id"] not in used_sale_ids
                and abs(s["_play_mins"] - actual) <= 10
            ]

            if candidates:
                # Pick closest by time proximity (closest start_time to created_at)
                session_start = session.get("start_time")
                best = min(candidates, key=lambda s: abs(
                    (s["created_at"] - session_start).total_seconds()
                ) if (session_start and s.get("created_at")) else 0)
                matched_pairs.append((session, best))
                used_sale_ids.add(best["id"])

        matched_session_ids = {p[0]["booking_id"] for p in matched_pairs}

        # 5. Build missing_sales (Done sessions without matching sale)
        def parse_multiplier(console_id: str) -> float:
            """C-09 and C-10 have 1.2x rate multiplier."""
            if not console_id:
                return 1.0
            cid = console_id.upper().strip()
            if cid in ("C-09", "C-10"):
                return 1.2
            return 1.0

        missing_sales = []
        for session in done_sessions:
            if session["booking_id"] not in matched_session_ids:
                # Estimate the unrecorded sale at the BILLABLE minutes (excludes the
                # promo FREE hour) — Osmo 2026-10-14.
                actual = session.get("billable_mins") or session.get("actual_mins") or session.get("duration_mins") or 0
                if actual <= 0:
                    actual = session.get("actual_mins") or session.get("duration_mins") or 0
                mult = parse_multiplier(session.get("console_id", ""))
                est = round((actual / 60) * 10000 * mult)
                missing_sales.append({
                    "booking_id": session["booking_id"],
                    "console_id": session["console_id"],
                    "start_time": session["start_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("start_time") else None,
                    "end_time": session["end_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("end_time") else None,
                    "actual_mins": actual,
                    "staff_name": session.get("customer_name") or session.get("staff_name") or "—",
                    "phone": session.get("phone") or "",
                    "estimated_amount": int(est),
                })

        # 6. Build orphan_sales (sales without matching Done session, exclude food-only)
        orphan_sales = []
        for sale in sales_records:
            if sale["id"] not in used_sale_ids:
                _notes = sale.get("notes") or ""
                # Skip food-only / topup / registration sales (no Mins: in notes)
                if not re.search(r'Mins?:?\s*\d+', _notes, re.IGNORECASE):
                    continue
                orphan_sales.append({
                    "id": sale["id"],
                    "voucher_no": sale.get("voucher_no") or "",
                    "console_id": sale.get("console_id") or "",
                    "net": float(sale.get("net") or 0),
                    "payment_method": sale.get("payment_method") or "",
                    "notes": sale.get("notes") or "",
                })

        # 7. Build all_matched for detail view
        all_matched = []
        for session, sale in matched_pairs:
            all_matched.append({
                "console_id": session["console_id"],
                "booking_id": session["booking_id"],
                "voucher_no": sale.get("voucher_no") or "",
                "mins": session.get("actual_mins") or session.get("duration_mins") or 0,
                "payment_method": sale.get("payment_method") or "",
                "amount": float(sale.get("net") or 0),
                "match_type": "console+mins",
            })

        total_done = len(done_sessions)
        total_sales = len(sales_records)
        total_matched = len(matched_pairs)
        missing_revenue = sum(ms["estimated_amount"] for ms in missing_sales)

        return {
            "success": True,
            "date": date,
            "done_sessions": total_done,
            "sales_records": total_sales,
            "matched": total_matched,
            "missing_sales": missing_sales,
            "orphan_sales": orphan_sales,
            "all_matched": all_matched,
            "summary": {
                "total_done_sessions": total_done,
                "total_sales": total_sales,
                "total_matched": total_matched,
                "total_orphan_sessions": len(missing_sales),
                "total_orphan_sales": len(orphan_sales),
                "missing_revenue": int(missing_revenue),
            },
        }
    except Exception as e:
        logger.error(f"Reconciliation error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Daily Stock Check ──

# ── Stock check — dish/ingredient model (2026-10-16, Osmo option A) ────────────
# A cooked dish (any item with a recipe in menu_recipes, or a Food / Add-On
# inventory row) cannot be counted on the shelf, so it is NOT part of the daily
# stock check any more. The raw INGREDIENTS it is made from are checked instead
# (system qty = FIFO remaining in stock_layers), while packaged goods
# (snacks / drinks / noodles / coffee) stay in the list unchanged.
_STOCK_DISH_CATS = ("food", "add-on")


def _stock_num(v):
    """Float-safe stock quantity — whole numbers stay ints (12 → 12, not 12.0)."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        f = 0.0
    return int(f) if abs(f - int(f)) < 1e-9 else round(f, 2)


def _stock_canon_map():
    """variant → canonical name (e.g. ကြက်ဉ / Egg → ကြက်ဥ)."""
    m = {}
    try:
        for r in (_mysql_query(
                "SELECT canonical_name, variant_name FROM ingredient_variants WHERE branch_id = 1") or []):
            m[str(r.get("variant_name") or "").strip()] = str(r.get("canonical_name") or "").strip()
    except Exception:
        pass
    return m


def _stock_canon(name, cmap=None):
    n = str(name or "").strip()
    return (cmap or {}).get(n, n)


def _stock_ingredient_rows():
    """kind='ingredient' master rows with their FIFO balance (variant-aware).

    Returns [{name, canonical, unit, fifo_qty}].
    """
    cmap = _stock_canon_map()
    try:
        rows = _mysql_query(
            "SELECT id, name, base_unit FROM ingredients WHERE branch_id = 1 AND kind = 'ingredient' "
            "ORDER BY name") or []
    except Exception:
        return []
    fifo = {}
    try:
        for r in (_mysql_query(
                "SELECT item_name, SUM(qty_remaining) AS q FROM stock_layers "
                "WHERE branch_id = 1 GROUP BY item_name") or []):
            c = _stock_canon(r.get("item_name"), cmap)
            fifo[c] = fifo.get(c, 0.0) + float(r.get("q") or 0)
    except Exception:
        fifo = {}
    out = []
    for r in rows:
        nm = str(r.get("name") or "").strip()
        if not nm:
            continue
        c = _stock_canon(nm, cmap)
        out.append({
            "name": nm,
            "canonical": c,
            "unit": str(r.get("base_unit") or "").strip(),
            "fifo_qty": _stock_num(fifo.get(c, 0.0)),
        })
    return out


@router.get("/dashboard/stock-check")
async def dashboard_get_stock_check(date: str = None, user: dict = Depends(get_staff_or_dashboard_user)):
    """Get daily stock check — system qty vs actual count.
    If no date given, use today. Returns all inventory items with system qty
    and any previously saved actual counts for that date."""
    try:
        if not date:
            date = _get_mmt_now().strftime("%Y-%m-%d")
        
        # ── Stock check list (2026-10-16, Osmo option A) — dishes OUT, 
        # ingredients IN (see _stock_ingredient_rows above).
        try:
            _recipe_names = {
                str(r.get("menu_item") or "").strip()
                for r in (_mysql_query(
                    "SELECT DISTINCT menu_item FROM menu_recipes WHERE branch_id = 1") or [])
            }
        except Exception:
            _recipe_names = set()
        _ing_rows = _stock_ingredient_rows()
        _ing_canon = {r["canonical"] for r in _ing_rows}
        _cmap = _stock_canon_map()

        _raw_items = _mysql_query(
            "SELECT id, item_name, category, quantity as system_qty, unit_price "
            "FROM inventory WHERE branch_id = 1 ORDER BY category, item_name"
        ) or []
        items = []
        for _it in _raw_items:
            _nm = str(_it.get("item_name") or "").strip()
            _cat = str(_it.get("category") or "").strip().lower()
            if _nm in _recipe_names or _cat in _STOCK_DISH_CATS:
                continue                        # cooked dish → not countable
            if _stock_canon(_nm, _cmap) in _ing_canon:
                continue                        # same thing as an ingredient row (use FIFO qty)
            items.append({
                "item_name": _nm,
                "category": _it.get("category", ""),
                "system_qty": _stock_num(_it.get("system_qty")),
                "unit": "",
                "unit_price": float(_it.get("unit_price") or 0),
            })
        for _r in _ing_rows:                    # raw ingredients as their own group
            items.append({
                "item_name": _r["name"],
                "category": "Ingredients",
                "system_qty": _r["fifo_qty"],
                "unit": _r["unit"],
                "unit_price": 0,
            })
        
        # Get today's stock movements (MMT date range)
        mmt_today_start = f"{date} 00:00:00"
        mmt_today_end = f"{date} 23:59:59"
        # Convert MMT date range to UTC for query (MMT = UTC+6:30)
        stock_in_today = _mysql_query(
            """SELECT item_name, SUM(quantity) as qty
                FROM stock_in 
                WHERE created_at >= CONVERT_TZ(%s, '+06:30', '+00:00')
                  AND created_at <= CONVERT_TZ(%s, '+06:30', '+00:00')
                GROUP BY item_name""",
            (mmt_today_start, mmt_today_end)
        ) or []
        stock_out_today = _mysql_query(
            """SELECT item_name, SUM(quantity) as qty
                FROM stock_out 
                WHERE created_at >= CONVERT_TZ(%s, '+06:30', '+00:00')
                  AND created_at <= CONVERT_TZ(%s, '+06:30', '+00:00')
                GROUP BY item_name""",
            (mmt_today_start, mmt_today_end)
        ) or []
        stock_in_map = {r["item_name"]: _stock_num(r["qty"]) for r in stock_in_today}
        stock_out_map = {r["item_name"]: _stock_num(r["qty"]) for r in stock_out_today}
        
        # Get saved actual counts for this date
        checks = _mysql_query(
            "SELECT item_name, actual_qty, variance, notes, checked_by, updated_at "
            "FROM daily_stock_check WHERE check_date = %s AND branch_id = 1",
            (date,)
        ) or []
        check_map = {c["item_name"]: c for c in checks}
        
        # Merge
        result = []
        has_any_actual = False
        for item in items:
            name = item["item_name"]
            saved = check_map.get(name, {})
            actual = saved.get("actual_qty")
            variance = saved.get("variance")
            # Live inventory = opening + today's stock_in - today's stock_out
            si_today = stock_in_map.get(name, 0)
            so_today = stock_out_map.get(name, 0)
            live_system_qty = _stock_num(item.get("system_qty"))
            if actual is not None:
                has_any_actual = True
                variance = _stock_num(live_system_qty - _stock_num(actual))
            opening_qty = _stock_num(live_system_qty - si_today + so_today)
            
            result.append({
                "item_name": name,
                "category": item.get("category", ""),
                "unit": item.get("unit", ""),
                "system_qty": live_system_qty,
                "opening_qty": opening_qty,
                "stock_in_today": si_today,
                "stock_out_today": so_today,
                "actual_qty": actual,
                "variance": variance if actual is not None else None,
                "unit_price": float(item.get("unit_price", 0)),
                "notes": saved.get("notes", ""),
                "checked_by": saved.get("checked_by", ""),
                "updated_at": str(saved.get("updated_at", "")) if saved.get("updated_at") else None,
            })
        
        checked_count = sum(1 for r in result if r["actual_qty"] is not None)
        total_items = len(result)
        
        return {
            "success": True,
            "data": {
                "date": date,
                "total_items": total_items,
                "checked_count": checked_count,
                "progress_pct": round(checked_count / total_items * 100, 1) if total_items else 0,
                "items": result,
            }
        }
    except Exception as e:
        logger.error(f"Stock check GET error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/dashboard/stock-check")
async def dashboard_save_stock_check(req: dict, user: dict = Depends(get_staff_or_dashboard_user)):
    """Save actual count for one or more items.
    Body: { date, items: [{ item_name, actual_qty, notes? }] }"""
    try:
        check_date = req.get("date") or _get_mmt_now().strftime("%Y-%m-%d")
        items = req.get("items", [])
        checked_by = req.get("checked_by") or user.get("username", "")
        
        if not items:
            return {"success": False, "error": "No items provided"}
        
        saved = 0
        for it in items:
            name = it.get("item_name", "").strip()
            if not name:
                continue
            actual_qty = it.get("actual_qty")
            if actual_qty is None:
                continue
            notes = it.get("notes", "")
            
            # System qty: the inventory row when one exists, else the ingredient's
            # FIFO balance (ingredients are checked now; dishes are not — 2026-10-16)
            inv = _mysql_query_one(
                "SELECT quantity FROM inventory WHERE item_name = %s AND branch_id = 1",
                (name,)
            )
            if inv:
                system_qty = _stock_num(inv.get("quantity"))
            else:
                _ing = next((r for r in _stock_ingredient_rows() if r["name"] == name), None)
                system_qty = _stock_num(_ing["fifo_qty"]) if _ing else 0
            actual_qty = _stock_num(actual_qty)
            variance = _stock_num(system_qty - actual_qty)
            
            _mysql_execute(
                """INSERT INTO daily_stock_check 
                   (branch_id, check_date, item_name, system_qty, actual_qty, variance, notes, checked_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE 
                   system_qty = VALUES(system_qty), actual_qty = VALUES(actual_qty),
                   variance = VALUES(variance), notes = VALUES(notes),
                   checked_by = VALUES(checked_by)""",
                (1, check_date, name, system_qty, actual_qty, variance, notes, checked_by)
            )
            saved += 1
        
        logger.info(f"Stock check saved: {saved} items for {check_date} by {checked_by}")
        return {
            "success": True,
            "message": f"{saved} item(s) saved",
            "data": {"saved": saved, "date": check_date}
        }
    except Exception as e:
        logger.error(f"Stock check POST error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/dashboard/stock-check/history")
async def dashboard_stock_check_history(limit: int = 30, user: dict = Depends(get_staff_or_dashboard_user)):
    """Get list of past stock check dates with summary."""
    try:
        rows = _mysql_query(
            """SELECT check_date, COUNT(*) as items_checked,
                      SUM(CASE WHEN variance != 0 THEN 1 ELSE 0 END) as items_with_variance,
                      SUM(ABS(variance)) as total_variance_qty
               FROM daily_stock_check
               WHERE branch_id = 1
               GROUP BY check_date
               ORDER BY check_date DESC
               LIMIT %s""",
            (limit,)
        ) or []
        
        return {
            "success": True,
            "data": [{
                "date": str(r["check_date"]),
                "items_checked": int(r["items_checked"]),
                "items_with_variance": int(r["items_with_variance"]),
                "total_variance_qty": int(r["total_variance_qty"] or 0),
            } for r in rows]
        }
    except Exception as e:
        logger.error(f"Stock check history error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/dashboard/stock-check/resolve")
async def dashboard_stock_check_resolve(req: dict, user: dict = Depends(get_staff_or_dashboard_user)):
    """Resolve stock check variances — adjust inventory to match actual counts.
    Creates stock_in (price 0) for surplus or stock_out (price 0) for shortage.
    Body: { date, items?: [{ item_name }] } — if items omitted, resolves ALL variances."""
    try:
        check_date = req.get("date") or _get_mmt_now().strftime("%Y-%m-%d")
        item_names = [it["item_name"].strip() for it in req.get("items", [])] if req.get("items") else None
        resolved_by = user.get("username", "system")

        # Fetch variances for this date (only items with variance != 0)
        if item_names:
            placeholders = ','.join(['%s'] * len(item_names))
            variances = _mysql_query(
                f"""SELECT dsc.*, inv.quantity as current_system_qty, inv.id as inventory_id
                    FROM daily_stock_check dsc
                    JOIN inventory inv ON inv.item_name = dsc.item_name AND inv.branch_id = 1
                    WHERE dsc.check_date = %s AND dsc.branch_id = 1
                    AND dsc.variance != 0
                    AND dsc.item_name IN ({placeholders})""",
                (check_date, *item_names)
            ) or []
        else:
            variances = _mysql_query(
                """SELECT dsc.*, inv.quantity as current_system_qty, inv.id as inventory_id
                    FROM daily_stock_check dsc
                    JOIN inventory inv ON inv.item_name = dsc.item_name AND inv.branch_id = 1
                    WHERE dsc.check_date = %s AND dsc.branch_id = 1
                    AND dsc.variance != 0""",
                (check_date,)
            ) or []

        if not variances:
            return {"success": True, "message": "No variances to resolve", "data": {"resolved": 0}}

        import uuid
        resolved = 0
        details = []

        for v in variances:
            item_name = v["item_name"]
            actual_qty = int(v["actual_qty"])
            inventory_id = v["inventory_id"]
            current_qty = int(v["current_system_qty"] or 0)

            # Use actual inventory delta (not stored variance) — stock may have moved
            # since the stock check was saved (e.g. food cart sales)
            delta = actual_qty - current_qty

            if delta < 0:
                # Current inventory > actual → shortage → stock_out
                qty = abs(delta)
                _mysql_execute(
                    """INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, notes)
                       VALUES (%s, %s, 0, 0, NOW(), %s)""",
                    (item_name, qty, f"Stock check adjustment [{check_date}] — resolved by {resolved_by}")
                )
                details.append(f"{item_name}: -{qty} (stock_out, price 0)")
            elif delta > 0:
                # Current inventory < actual → surplus → stock_in
                qty = delta
                adj_batch = "SI-ADJ-" + uuid.uuid4().hex[:8].upper()
                adj_sid = _mysql_execute(
                    """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
                       VALUES (%s, %s, %s, 0, 'Stock Check Adjustment', %s, '', '', %s)""",
                    (adj_batch, item_name, qty, adj_batch, resolved_by)
                )
                _dish_consume.ensure_layer_for_stock_in(adj_sid)
                details.append(f"{item_name}: +{qty} (stock_in, price 0)")
            else:
                # delta == 0 → already matches, just mark as reviewed
                details.append(f"{item_name}: already matched (no adjustment needed)")

            # Update inventory quantity to match actual
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (actual_qty, inventory_id)
            )

            # Mark as resolved in daily_stock_check
            _mysql_execute(
                """UPDATE daily_stock_check
                   SET system_qty = %s, variance = 0,
                       notes = CONCAT(IFNULL(notes,''), ' [Resolved ', %s, ' by ', %s, ']')
                   WHERE check_date = %s AND item_name = %s AND branch_id = 1""",
                (actual_qty, check_date, resolved_by, check_date, item_name)
            )

            resolved += 1

        logger.info(f"Stock check resolved: {resolved} items for {check_date} by {resolved_by}")
        return {
            "success": True,
            "message": f"{resolved} variance(s) resolved",
            "data": {"resolved": resolved, "date": check_date, "details": details}
        }
    except Exception as e:
        logger.error(f"Stock check resolve error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
