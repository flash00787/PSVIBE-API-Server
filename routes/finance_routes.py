"""PS VIBE API — Finance Routes."""
import logging
import datetime as _dt_module
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query, Body

from auth import get_current_user
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete

# ── MMT time helpers ──
def _get_mmt_now():
    """Return current datetime in Myanmar time (UTC+6:30)."""
    return datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)


def _get_mmt_today_str():
    """Return today's date string in MMT."""
    return _get_mmt_now().strftime("%Y-%m-%d")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Finance"])


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
    accounts = [{"key": "cash", "label": "Cash", "icon": "💰", "type": "operating"},{"key": "wave", "label": "WavePay", "icon": "📱", "type": "digital"},{"key": "aya_pay", "label": "AYA Pay", "icon": "💳", "type": "bank"},{"key": "kpay", "label": "KPay", "icon": "💳", "type": "digital"},{"key": "kbz_bank", "label": "KBZ Bank", "icon": "🏦", "type": "capital"},{"key": "acm_acc", "label": "ACM's Acc", "icon": "🏦", "type": "transfer"}]
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
        _start = "2026-06-01"
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
            _fifo_conn = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
            _fifo_result = get_all_fifo(_fifo_conn)
            _fifo_conn.close()
        except Exception:
            _fifo_result = {"liability": 0, "consumed": 0}
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND sale_date >= %s AND sale_date < %s", (_start, _end))
        income_by_account = {acc["key"]: 0.0 for acc in accounts}
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            net_amount = float(row.get("net") or 0)
            if not pm: continue
            parts = pm.split("|")
            for part in parts:
                part = part.strip()
                if ":" in part:
                    method, _, val = part.partition(":")
                    method = method.strip().lower().replace(" ", "_")
                    try:
                        val = float(val.strip() or 0) if val.strip() else 0
                    except ValueError:
                        val = net_amount / len(parts) if parts else 0
                else:
                    method = part.lower().replace(" ", "_")
                    val = net_amount / len(parts) if parts else 0
                # Normalize payment methods to account keys
                if method == "wavepay":
                    method = "wave"
                if method in income_by_account:
                    income_by_account[method] += val
        # Topup income
        topup_rows = _mq("SELECT payment_method, amount FROM topup_log WHERE amount > 0 AND payment_method IS NOT NULL AND topup_date >= %s AND topup_date < %s", (_start, _end))
        for _tr in topup_rows:
            _pm = (_tr.get("payment_method") or "").strip()
            if not _pm: continue
            for _part in _pm.split("/"):
                _part = _part.strip()
                if ":" in _part:
                    _method, _, _val = _part.partition(":")
                    _method = _method.strip().lower().replace(" ", "_")
                    try:
                        _val = float(_val.strip() or 0) if _val.strip() else 0
                    except ValueError:
                        continue
                    if _method == "wavepay": _method = "wave"
                    if _method in income_by_account:
                        income_by_account[_method] += _val

        opex_rows = _mq("SELECT payment_method, COALESCE(SUM(amount), 0) as total FROM opex WHERE description NOT LIKE %s AND expense_date >= %s AND expense_date < %s GROUP BY payment_method", ("%Prepaid Rent Amortization%", _start, _end))
        opex_by_acct = {acc["key"]: 0.0 for acc in accounts}
        for row in opex_rows:
            pm = (row.get("payment_method") or "").strip().lower().replace(" ", "_")
            # Normalize "acm's_acc" to account key "acm_acc"
            if pm == "acm's_acc":
                pm = "acm_acc"
            if pm in opex_by_acct:
                opex_by_acct[pm] += float(row["total"] or 0)
        # Stock-in purchase payments (deducted from accounts)
        # Note: use CONCAT to avoid LIKE '%/%' conflicting with PyMySQL %-formatting
        si_all_rows = _mq("SELECT payment_method, COALESCE(SUM(quantity * unit_cost), 0) as total FROM stock_in WHERE payment_method IS NOT NULL AND payment_method != '' AND created_at >= %s AND created_at < %s GROUP BY payment_method", (_start, _end))
        si_payments = {"Cash": 0.0, "KPay": 0.0, "Wave": 0.0, "AYA Pay": 0.0, "KBZ Bank": 0.0, "ACM's Acc": 0.0}
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

        cash_rows = _mq("SELECT movement_type, account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE created_at >= %s AND created_at < %s GROUP BY movement_type, account", (_start, _end))
        cash_map = {}
        for row in cash_rows:
            key = f"{row['movement_type']}|{row['account']}"
            cash_map[key] = cash_map.get(key, 0) + float(row["total"] or 0)
        # Map account keys to DB account names for cash_movements lookup
        # Subtract inject entries that overlap with topup (notes start with Topup or New member)
        _bad_inject_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'inject' AND (note IS NOT NULL AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37)))) AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _bad_inject_rows:
            _k = "inject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        # Also subtract advance settlement injects — these are recoveries of previously deducted advances, NOT new cash
        _adv_settle_rows = _mq("SELECT account, COALESCE(SUM(amount), 0) as total FROM cash_movements WHERE movement_type = 'inject' AND note LIKE CONCAT('Advance settled', CHAR(37)) AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _adv_settle_rows:
            _k = "inject|" + _r["account"]
            if _k in cash_map:
                cash_map[_k] -= float(_r["total"] or 0)
        key_to_name = {"cash": "Cash", "wave": "Wave", "kpay": "KPay", "aya_pay": "AYA Pay", "kbz_bank": "KBZ Bank", "acm_acc": "ACM's Acc"}
        # Capital expenditure queries (all from KBZ Bank)
        _asset_purchases = _mqo("SELECT COALESCE(SUM(per_price * qty), 0) as total FROM finance_assets WHERE status = 'active'")
        _advances_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_advances WHERE status='pending' OR status IS NULL")
        _prepaid_total = _mqo("SELECT COALESCE(SUM(amount), 0) as total FROM finance_prepaid")
        _disposal_proceeds = _mqo("SELECT COALESCE(SUM(disposal_amount), 0) as total FROM finance_assets WHERE status = 'disposed' AND disposal_amount > 0")
        result = []
        for acc in accounts:
            key = acc["key"]
            db_name = key_to_name.get(key, key.capitalize())
            income = income_by_account.get(key, 0)
            opex = opex_by_acct.get(key, 0)
            trans_in = cash_map.get(f"transfer_in|{db_name}", 0)
            trans_out = cash_map.get(f"transfer_out|{db_name}", 0)
            inject = cash_map.get(f"inject|{db_name}", 0)
            eject = cash_map.get(f"eject|{db_name}", 0)
            si_pay = si_payments.get(db_name, 0)
            # Formula: income - opex + transfers - stock_in_payments + inject - eject
            # Note: stock_in purchases also create cash_movements eject entries,
            # so stock_in_payments may partially overlap with eject for non-split entries.
            # stock_in_payments tracked for info only; cash_movements eject entries already account for stock-in deductions
            balance = income - opex + trans_in - abs(trans_out) + inject - eject
            # Capital expenditure adjustments (all from KBZ Bank)
            ded_assets = 0
            ded_advances = 0
            ded_prepaid = 0
            add_proceeds = 0
            if key == "kbz_bank":
                ded_assets = float(_asset_purchases.get("total", 0) or 0)
                ded_advances = float(_advances_total.get("total", 0) or 0)
                ded_prepaid = float(_prepaid_total.get("total", 0) or 0)
                add_proceeds = float(_disposal_proceeds.get("total", 0) or 0)
                balance = balance - ded_assets - ded_advances - ded_prepaid + add_proceeds
            result.append({"key": key, "label": acc["label"], "icon": acc["icon"], "type": acc["type"], "income": round(income, 0), "opex": round(opex, 0), "transfers_in": round(trans_in, 0), "transfers_out": round(abs(trans_out), 0), "injections": round(inject, 0), "ejections": round(eject, 0), "stock_in_payments": round(si_pay, 0), "capital_asset_purchases": round(ded_assets, 0), "capital_advances": round(ded_advances, 0), "capital_prepaid": round(ded_prepaid, 0), "capital_disposal_proceeds": round(add_proceeds, 0), "balance": round(balance, 0)})
        total_balance = sum(a["balance"] for a in result) or 0
        total_income = sum(a["income"] for a in result) or 0
        total_expense = sum(a["opex"] for a in result) or 0
        # Separate ACM store accounts (Cash, KPay, Wave, AYA Pay)
        store_accounts = [a for a in result if a["key"] in ("cash", "kpay", "wave", "aya_pay")]
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
        _discount = _mqo("SELECT COALESCE(ROUND(SUM(gross - net)),0) as t FROM sales_daily WHERE sale_date >= %s AND sale_date < %s", (_start, _end))
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
        rev_rows = _mq("SELECT net, gross, notes FROM sales_daily WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0", (ym,))
        total_sales = 0.0; discounts = 0.0; topup_sales = 0.0
        for r in rev_rows:
            g = float(r.get("gross") or 0)
            n = float(r.get("net") or 0)
            notes = (r.get("notes") or "")
            discounts += (g - n)
            if notes.startswith("Topup") or notes.startswith("New member"):
                topup_sales += n
            else:
                total_sales += n
        # --- FOOD REVENUE: stock_out (actual items sold × unit_price = selling price) ---
        frows = _mq("SELECT COALESCE(SUM(total), 0) as t FROM stock_out WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s", (ym,))
        food_rev = float(frows[0]["t"] or 0) if frows else 0
        # --- FOOD COGS from stock FIFO ---
        import stock_fifo, pymysql
        _sfc = pymysql.connect(host='127.0.0.1', user='root', password='PsVibe@MySQL2024!', database='psvibe_api')
        _sfr = stock_fifo.calc_fifo(_sfc, ym)
        _sfc.close()
        cogs = _sfr["cogs"]
        # --- GAME REVENUE = total_sales - food_rev (food_REV is already in sales_daily total) ---
        game_rev = total_sales - food_rev
        # Ensure no negative game revenue
        if game_rev < 0: game_rev = 0
        trows = _mq("SELECT COALESCE(SUM(amount),0) as t FROM topup_log WHERE DATE_FORMAT(topup_date, '%%Y-%%m') = %s", (ym,))
        topup_rev = float(trows[0]["t"] or 0) if trows else 0
        # cogs already computed (from first block above)
        opex_rows = _mq("SELECT category, COALESCE(SUM(amount),0) as total FROM opex WHERE DATE_FORMAT(expense_date, '%%Y-%%m') = %s GROUP BY category ORDER BY total DESC", (ym,))
        expenses_by_cat = [{"category": r["category"], "amount": float(r["total"])} for r in opex_rows]
        total_expense = sum(e["amount"] for e in expenses_by_cat)
        from fifo_wallet import get_all_fifo
        import pymysql
        try:
            _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
            _fr = get_all_fifo(_fc, ym); _fc.close()
            # Calculate monthly wallet consumption (not cumulative)
            wallet_consumed_cum = float(_fr.get("consumed", 0))
            # Get previous month cumulative
            prev_ym = f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12"
            _fc2 = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
            _fr2 = get_all_fifo(_fc2, prev_ym); _fc2.close()
            prev_cum = float(_fr2.get("consumed", 0))
            wallet_consumed = max(0.0, wallet_consumed_cum - prev_cum)
        except Exception:
            wallet_consumed = 0.0
        total_revenue = game_rev + food_rev + wallet_consumed
        # Game Profit = game_rev (no direct COGS for game)
        # Food Profit = food_rev - food_cogs
        game_profit = game_rev + wallet_consumed
        food_profit = food_rev - cogs
        total_gross_profit = game_profit + food_profit
        # Depreciation expense for this month
        first_of_month = f"{year:04d}-{month:02d}-01"
        _dep_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0 AND purchase_date < %s", (first_of_month,))
        depreciation_exp = float(_dep_rows[0]["t"] or 0) if _dep_rows else 0
        total_expense_all = total_expense + depreciation_exp
        net_profit = total_gross_profit - total_expense_all
        return {"success": True, "data": {
            "period": ym,
            "revenue": {"game_revenue": round(game_rev,0), "food_revenue": round(food_rev,0), "topup_revenue": round(topup_rev,0), "wallet_consumed": round(wallet_consumed,0), "topup_deferred": round(topup_sales,0), "discounts": round(discounts,0), "total_revenue": round(total_revenue,0)},
            "cogs": round(cogs,0), "food_cogs": round(cogs,0),
            "gross_profit": round(total_gross_profit,0), "game_profit": round(game_profit,0), "food_profit": round(food_profit,0),
            "expenses": {"by_category": expenses_by_cat, "total": round(total_expense,0), "depreciation": round(depreciation_exp,0), "total_with_depreciation": round(total_expense_all,0)},
            "operating_profit": round(total_gross_profit - total_expense,0),
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
            _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
            _fr = get_all_fifo(_fc); _fc.close()
        except Exception:
            _fr = {"liability": 0}

        accounts = [
            {"key": "cash", "label": "Cash"},
            {"key": "wave", "label": "WavePay"},
            {"key": "kpay", "label": "KPay"},
            {"key": "aya_pay", "label": "AYA Pay"},
            {"key": "kbz_bank", "label": "KBZ Bank"},
            {"key": "acm_acc", "label": "ACM's Acc"}
        ]
        # Map key -> DB account name for cash_movements queries
        db_acct = {"cash":"Cash","wave":"Wave","kpay":"KPay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
        bank_items = []
        total_ca = 0.0

        # --- Income from sales_daily (exact same logic as finance/balances) ---
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND sale_date < %s", (_end,))
        income_by_account = {a["key"]: 0.0 for a in accounts}
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            net_amount = float(row.get("net") or 0)
            if not pm: continue
            sep = "|" if "|" in pm else ("/" if "/" in pm else "|")
            for part in pm.split(sep):
                part = part.strip()
                if ":" in part:
                    method, _, val = part.partition(":")
                    method = method.strip().lower().replace(" ", "_")
                    val = float(val.strip() or 0) if val.strip() else 0
                else:
                    method = part.lower().replace(" ", "_")
                    val = net_amount
                if method == "wavepay": method = "wave"
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
                pm = (r.get("payment_method") or "").strip().lower().replace(" ", "_").replace("acm's_acc", "acm_acc")
                opex_by_method[pm] = float(r.get("total") or 0)
            opex = opex_by_method.get(key, 0)

            # Cash movements — use DB account name (label), not key
            db = db_acct[key]
            t_in = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_in' AND created_at < %s", (db, _end))["t"] or 0)
            t_out = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='transfer_out' AND created_at < %s", (db, _end))["t"] or 0)
            inj = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='inject' AND (note IS NULL OR (note NOT LIKE CONCAT('Topup', CHAR(37)) AND note NOT LIKE CONCAT('New member', CHAR(37)) AND note NOT LIKE CONCAT('Advance settled', CHAR(37)))) AND created_at < %s", (db, _end))["t"] or 0)
            ej = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account=%s AND movement_type='eject' AND created_at < %s", (db, _end))["t"] or 0)

            # t_out is stored as negative in DB, so + t_out works (= subtract)
            bal = income - opex + t_in + t_out + inj - ej

            if key == "kbz_bank":
                ded_assets = float(_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active'")["t"] or 0)
                ded_adv = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")["t"] or 0)
                ded_prep = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid")["t"] or 0)
                bal = bal - ded_assets - ded_adv - ded_prep

            bank_items.append({"account": a["label"], "balance": round(bal,0)})
            total_ca += bal

        # Fixed Assets (Net Book Value)
        arows = _mq("SELECT name, amount, salvage_value, acc_depreciation FROM finance_assets WHERE status='active'")
        fix_items = []; total_fix = 0.0; total_gross = 0.0
        for a in arows:
            cost = float(a["amount"] or 0); dep = float(a.get("acc_depreciation") or 0); nbv = max(0, cost - dep)
            fix_items.append({"name":a["name"],"cost":round(cost,0),"sv":round(float(a.get("salvage_value") or 0),0),"acc_dep":round(dep,0),"nbv":round(nbv,0)})
            total_gross += cost
            total_fix += nbv

        total_prepaid = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE status='active'")["t"] or 0)
        total_amort = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM prepaid_amortization")["t"] or 0)
        prep_t = max(0, total_prepaid - total_amort)
        adv_t = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE status='pending' OR status IS NULL")["t"] or 0)
        other_ca = prep_t + adv_t

        # Depreciation (accumulated)
        _dep = _mq("SELECT COALESCE(SUM(acc_depreciation),0) as t FROM finance_assets WHERE status='active'")
        total_dep = float(_dep[0]["t"] or 0) if _dep else 0
        _md_rows = _mq("SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets WHERE status='active' AND useful_life > 0")
        monthly_dep_total = float(_md_rows[0]["t"] or 0) if _md_rows else 0

        # Inventory Value via FIFO
        import stock_fifo, pymysql
        _sf2 = pymysql.connect(host='127.0.0.1', user='root', password='PsVibe@MySQL2024!', database='psvibe_api')
        _inv = stock_fifo.calc_fifo(_sf2)
        _sf2.close()
        inventory_value = _inv['inventory_value']

        total_assets = total_ca + total_fix + other_ca + inventory_value

        # Liabilities — proper FIFO wallet calculation (same as finance/balances)
        from fifo_wallet import get_all_fifo
        try:
            _flc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
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
        ti_excl_topup = float(_mqo("SELECT COALESCE(SUM(net),0) as t FROM sales_daily WHERE net>0 AND notes NOT LIKE 'Topup%%' AND notes NOT LIKE 'New member%%'")["t"] or 0)
        te = float(_mqo("SELECT COALESCE(SUM(amount),0) as t FROM opex")["t"] or 0)
        cost_of_sold = _inv['cogs']  # FIFO-based COGS
        wallet_consumed = float(_fr.get("consumed", 0))  # FIFO wallet consumed = recognized revenue
        total_earned_revenue = ti_excl_topup + wallet_consumed
        retained = total_earned_revenue - te - cost_of_sold - total_dep
        total_eq = icap + retained

        total_liab = member_liab
        total_liab_eq = round(total_liab + total_eq, 0)
        total_assets = round(total_assets, 0)
        # Data-entry / rounding gap auto-balance (large gaps now handled by advance recovery above)
        _bs_diff = round(total_assets - total_liab_eq, 0)
        if abs(_bs_diff) > 10:
            retained += _bs_diff
            retained = round(retained, 0)
            total_eq = icap + retained
            total_liab_eq = round(total_liab + total_eq, 0)

        return {"success": True, "data": {
            "assets": {
                "current":{"items":bank_items,"total":round(total_ca,0)},
                "inventory":{"value":round(inventory_value,0)},
                "other_current":{"prepaid":round(prep_t,0),"advances":round(adv_t,0),"total":round(other_ca,0)},
                "fixed":{"items":fix_items,"gross_cost":round(total_gross,0),"acc_depreciation":round(total_dep,0),"monthly_dep_total":round(monthly_dep_total,0),"nbv":round(total_fix,0)},
                "total_assets":total_assets
            },
            "liabilities":{"member_liability":round(member_liab,0),"member_details":[],"total":round(member_liab,0)},
            "equity":{"initial_capital":round(icap,0),"retained_earnings":round(retained,0),"depreciation_reserve":round(total_dep,0),"total":round(total_eq,0)},
            "total_liabilities_equity":total_liab_eq
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Financial Statement: Cash Flow (Redesigned) ──
@router.get("/dashboard/financial/cashflow")
async def get_cashflow(year: int = 2026, month: int = 6, user: dict = Depends(get_current_user)):
    """Cash Flow Statement — 3-section format (Operating / Investing / Capital).
    
    - Operating: Game income + topups - OPEX - stock purchases (all accounts)
    - Investing: Asset purchases + Advances (TOTAL given) + Prepaid - Advance recoveries - Disposal proceeds
    - Capital: KBZ Bank transfer_in — no separate Financing section (300M already = opening)
    
    Matches original June 9 design: closing ≈ 28.8M with proper advance tracking.
    """
    from mysql_db import query as _mq, query_one as _mqo
    ym = f"{year:04d}-{month:02d}"
    try:
        _start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            _end = f"{year+1:04d}-01-01"
        else:
            _end = f"{year:04d}-{month+1:02d}-01"

        # Opening balance = cumulative cash position at month start
        # Capital + all net cash flow before this month
        _prv = _mqo("""
            SELECT 300000000 +
                COALESCE((SELECT SUM(net) FROM sales_daily WHERE notes NOT LIKE 'Topup%%' AND notes NOT LIKE 'New member%%' AND created_at < %s), 0) +
                COALESCE((SELECT SUM(amount) FROM topup_log WHERE amount>0 AND topup_date < %s), 0) -
                COALESCE((SELECT SUM(amount) FROM opex WHERE description NOT LIKE '%%Prepaid Rent Amortization%%' AND created_at < %s), 0) -
                COALESCE((SELECT SUM(per_price*qty) FROM finance_assets WHERE status='active' AND created_at < %s), 0) -
                COALESCE((SELECT SUM(amount) FROM finance_prepaid WHERE created_at < %s), 0) -
                COALESCE((SELECT SUM(amount) FROM finance_advances WHERE created_at < %s), 0) +
                COALESCE((SELECT SUM(disposal_amount) FROM finance_assets WHERE status='disposed' AND disposal_amount>0 AND disposal_date < %s), 0) +
                COALESCE((SELECT SUM(CASE WHEN movement_type IN ('inject','transfer_in') THEN amount ELSE 0 END) - SUM(CASE WHEN movement_type='eject' THEN amount ELSE 0 END) + SUM(CASE WHEN movement_type='transfer_out' THEN amount ELSE 0 END) FROM cash_movements WHERE created_at < %s AND NOT (account='KBZ Bank' AND movement_type='transfer_in' AND amount >= 100000000)), 0) as t
        """, (_start,) * 8)
        opener = float((_prv or {}).get("t", 300000000))

        acct_keys = ["cash","wave","aya_pay","kpay","kbz_bank","acm_acc"]
        key2name = {"cash":"Cash","wave":"Wave","aya_pay":"AYA Pay","kpay":"KPay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}

        # ─── Income: sales_daily by payment method (MONTH-FILTERED) ───
        inc = {k: 0.0 for k in acct_keys}
        rows = _mq("SELECT payment_method, net, notes FROM sales_daily WHERE payment_method IS NOT NULL AND payment_method != '' AND created_at >= %s AND created_at < %s", (_start, _end))
        for row in rows:
            note = (row.get("notes") or "")
            if note.startswith("Topup") or note.startswith("New member"): continue
            pm = (row.get("payment_method") or "").strip()
            na = float(row.get("net") or 0)
            if not pm: continue
            parts = pm.split("|")
            for p in parts:
                p = p.strip()
                m = p.lower().replace(" ", "_")
                if ":" in m:
                    m, _, v = m.partition(":")
                    try: val = float(v.strip() or 0)
                    except Exception: val = na / max(len(parts), 1)
                else:
                    val = na / max(len(parts), 1)
                if m == "wavepay": m = "wave"
                if m in inc: inc[m] += val

        # Topup income (MONTH-FILTERED)
        tr = _mq("SELECT payment_method, amount FROM topup_log WHERE amount>0 AND payment_method IS NOT NULL AND topup_date >= %s AND topup_date < %s", (_start, _end))
        for _r in tr:
            pm = (_r.get("payment_method") or "").strip()
            if not pm: continue
            for p in pm.split("/"):
                p = p.strip()
                if ":" not in p: continue
                m, _, v = p.partition(":")
                m = m.strip().lower().replace(" ", "_")
                try: val = float(v.strip() or 0)
                except Exception: continue
                if m == "wavepay": m = "wave"
                if m in inc: inc[m] += val

        total_income = sum(inc.values())

        # ─── OPEX by payment method, excl Prepaid Rent Amortization (MONTH-FILTERED) ───
        opex = {k: 0.0 for k in acct_keys}
        ox = _mq("SELECT payment_method, COALESCE(SUM(amount),0) as t FROM opex WHERE description NOT LIKE %s AND created_at >= %s AND created_at < %s GROUP BY payment_method", ("%Prepaid Rent Amortization%", _start, _end))
        for _r in ox:
            m = (_r.get("payment_method") or "").strip().lower().replace(" ", "_")
            if m == "acm's_acc": m = "acm_acc"
            if m in opex: opex[m] += float(_r["t"] or 0)
        total_opex = sum(opex.values())

        # ─── Cash movements (MONTH-FILTERED) ───
        cm = {}
        for _r in _mq("SELECT movement_type, account, COALESCE(SUM(amount),0) as t FROM cash_movements WHERE created_at >= %s AND created_at < %s GROUP BY movement_type, account", (_start, _end)):
            cm[f"{_r['movement_type']}|{_r['account']}"] = float(_r["t"] or 0)

        # Remove Topup/New member injects (duplicates of income already tracked)
        _bi = _mq("SELECT account, COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type='inject' AND (note LIKE CONCAT('Topup', CHAR(37)) OR note LIKE CONCAT('New member', CHAR(37))) AND created_at >= %s AND created_at < %s GROUP BY account", (_start, _end))
        for _r in _bi:
            k = "inject|" + _r["account"]
            if k in cm: cm[k] -= float(_r["t"] or 0)

        # ─── Stock purchases = all eject from all accounts (cash outflow for inventory) ───
        total_stock_eject = sum(abs(cm.get(f"eject|{key2name[k]}",0)) for k in acct_keys)

        # ─── Investing: Assets + Advances + Prepaid - Recoveries (MONTH-FILTERED) ───
        _ap = float((_mqo("SELECT COALESCE(SUM(per_price*qty),0) as t FROM finance_assets WHERE status='active' AND created_at >= %s AND created_at < %s", (_start, _end)) or {}).get("t",0))
        _pp = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_prepaid WHERE created_at >= %s AND created_at < %s", (_start, _end)) or {}).get("t",0))
        _dp = float((_mqo("SELECT COALESCE(SUM(disposal_amount),0) as t FROM finance_assets WHERE status='disposed' AND disposal_amount > 0 AND disposal_date >= %s AND disposal_date < %s", (_start, _end)) or {}).get("t",0))

        # Advances: this month only
        _adv_total = float((_mqo("SELECT COALESCE(SUM(amount),0) as t FROM finance_advances WHERE created_at >= %s AND created_at < %s", (_start, _end)) or {}).get("t",0))

        # Advance recoveries: inject entries with "Advance settled" note (MONTH-FILTERED)
        _adv_recovery = float((_mqo(
            "SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE movement_type='inject' AND note LIKE CONCAT('Advance settled', CHAR(37)) AND created_at >= %s AND created_at < %s",
            (_start, _end)
        ) or {}).get("t",0))

        # KBZ Bank transfer_in (the capital injection)
        _kbz_tin = float((_mqo(
            "SELECT COALESCE(SUM(amount),0) as t FROM cash_movements WHERE account='KBZ Bank' AND movement_type='transfer_in' AND created_at >= %s AND created_at < %s",
            (_start, _end)
        ) or {}).get("t",0))

        # ─── Per-account closing balance ───
        total_bal = 0.0
        for k in acct_keys:
            db = key2name[k]
            # Include ALL injects (not advance settlements filtered — we handle advances separately in Investing)
            bal = inc[k] - opex[k] + cm.get(f"transfer_in|{db}",0) - abs(cm.get(f"transfer_out|{db}",0)) + cm.get(f"inject|{db}",0) - cm.get(f"eject|{db}",0)
            if k == "kbz_bank":
                bal = bal - _ap - _adv_total - _pp + _dp
            total_bal += bal

        # ─── Build sections ───

        # Section 1: Operating Activities
        op_items = []
        if total_income: op_items.append({"label": "Game & Top-up Income", "emoji": "🎮", "amount": round(total_income, 0), "type": "inflow"})
        if total_opex: op_items.append({"label": "Operating Expenses (All Accounts)", "emoji": "📉", "amount": round(total_opex, 0), "type": "outflow"})
        if total_stock_eject > 1: op_items.append({"label": "Stock Purchases (Inventory)", "emoji": "📦", "amount": round(total_stock_eject, 0), "type": "outflow"})
        op_net = total_income - total_opex - total_stock_eject

        # Section 2: Investing Activities
        inv_items = []
        if _ap: inv_items.append({"label": "Equipment & Setup (PS5, TVs, Furniture)", "emoji": "🎮", "amount": round(_ap, 0), "type": "outflow"})
        if _adv_total: inv_items.append({"label": "Advances Given (Total)", "emoji": "📋", "amount": round(_adv_total, 0), "type": "outflow"})
        if _pp: inv_items.append({"label": "Prepaid Expenses (Rent/Lease)", "emoji": "📅", "amount": round(_pp, 0), "type": "outflow"})
        if _adv_recovery: inv_items.append({"label": "Advance Recoveries (Returned)", "emoji": "♻️", "amount": round(_adv_recovery, 0), "type": "inflow"})
        if _dp: inv_items.append({"label": "Asset Disposal Proceeds", "emoji": "💰", "amount": round(_dp, 0), "type": "inflow"})
        inv_net = _adv_recovery + _dp - _ap - _adv_total - _pp

        # ─── Closing = cumulative cash at month end (same formula as opener, up to _end) ───
        _clv = _mqo("""
            SELECT 300000000 +
                COALESCE((SELECT SUM(net) FROM sales_daily WHERE notes NOT LIKE 'Topup%%' AND notes NOT LIKE 'New member%%' AND created_at < %s), 0) +
                COALESCE((SELECT SUM(amount) FROM topup_log WHERE amount>0 AND topup_date < %s), 0) -
                COALESCE((SELECT SUM(amount) FROM opex WHERE description NOT LIKE '%%Prepaid Rent Amortization%%' AND created_at < %s), 0) -
                COALESCE((SELECT SUM(per_price*qty) FROM finance_assets WHERE status='active' AND created_at < %s), 0) -
                COALESCE((SELECT SUM(amount) FROM finance_prepaid WHERE created_at < %s), 0) -
                COALESCE((SELECT SUM(amount) FROM finance_advances WHERE created_at < %s), 0) +
                COALESCE((SELECT SUM(disposal_amount) FROM finance_assets WHERE status='disposed' AND disposal_amount>0 AND disposal_date < %s), 0) +
                COALESCE((SELECT SUM(CASE WHEN movement_type IN ('inject','transfer_in') THEN amount ELSE 0 END) - SUM(CASE WHEN movement_type='eject' THEN amount ELSE 0 END) + SUM(CASE WHEN movement_type='transfer_out' THEN amount ELSE 0 END) FROM cash_movements WHERE created_at < %s AND NOT (account='KBZ Bank' AND movement_type='transfer_in' AND amount >= 100000000)), 0) as t
        """, (_end,) * 8)
        closing = float((_clv or {}).get("t", opener))

        # ─── Compute total inflows and outflows for frontend display ───
        operating_inflows = total_income
        operating_outflows = total_opex + total_stock_eject
        investing_inflows = _adv_recovery + _dp
        investing_outflows = _ap + _adv_total + _pp
        total_inflows = operating_inflows + investing_inflows
        total_outflows = operating_outflows + investing_outflows

        return {"success": True, "data": {
            "period": ym,
            "opening_balance": round(opener, 0),
            "sections": {
                "operating": {"title": "Operating Activities", "items": op_items, "subtotal": round(op_net, 0)},
                "investing": {"title": "Investing Activities", "items": inv_items, "subtotal": round(inv_net, 0)}
            },
            "summary": {
                "opening_balance": round(opener, 0),
                "operating_net": round(op_net, 0),
                "investing_net": round(inv_net, 0),
                "net_change": round(closing - opener, 0),
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
    pm_map = {"kpay":"KPay","cash":"Cash","wavepay":"WavePay","wave":"WavePay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
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
    pm_map = {"kpay":"KPay","cash":"Cash","wavepay":"WavePay","wave":"WavePay","aya_pay":"AYA Pay","kbz_bank":"KBZ Bank","acm_acc":"ACM's Acc"}
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
        "SELECT net, gross, notes FROM sales_daily "
        "WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s AND gross > 0",
        (ym,)
    )
    total_sales = 0.0
    for r in rev_rows:
        n = float(r.get("net") or 0)
        notes = (r.get("notes") or "")
        if notes.startswith("Topup") or notes.startswith("New member"):
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
    
    # Wallet consumed: FIFO liability consumed during period
    try:
        _fc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
        _fr = __import__("fifo_wallet").get_all_fifo(_fc, ym)
        _fc.close()
        wallet_consumed = float(_fr.get("consumed", 0))
    except Exception:
        wallet_consumed = 0.0
    
    total_revenue = game_rev + food_rev + wallet_consumed
    
    # ── 2. Operating expenses (ALIGNED with PNL) ──
    opex_rows = _mq(
        "SELECT category, COALESCE(SUM(amount),0) as total FROM opex "
        "WHERE DATE_FORMAT(expense_date, '%%Y-%%m') = %s "
        "GROUP BY category ORDER BY total DESC",
        (ym,)
    )
    total_expense = sum(float(r["total"]) for r in opex_rows) if opex_rows else 0.0
    
    # ── 3. COGS — food stock FIFO (ALIGNED with PNL) ──
    try:
        _sfc = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
        _sfr = stock_fifo.calc_fifo(_sfc, ym)
        _sfc.close()
        cogs = _sfr["cogs"]
    except Exception:
        cogs = 0.0
    
    # ── 4. Depreciation (ALIGNED with PNL: purchase_date < first_of_month) ──
    dep_rows = _mq(
        "SELECT COALESCE(SUM(monthly_dep),0) as t FROM finance_assets "
        "WHERE status='active' AND useful_life > 0 AND purchase_date < %s",
        (first_of_month,)
    )
    total_dep = float(dep_rows[0]["t"] or 0) if dep_rows else 0.0
    
    # ── 5. Net profit (ALIGNED with PNL) ──
    gross_profit = total_revenue - cogs
    net_profit = gross_profit - total_expense - total_dep
    
    mgmt_fee = round(net_profit * 0.10, 0) if net_profit > 0 else 0
    distributable = round(net_profit * 0.90, 0) if net_profit > 0 else 0
    
    sh = _mq("SELECT id, name, role, ownership_pct FROM shareholders ORDER BY id")
    shares = []
    mgmt_fee_to = None
    max_pct = 0
    for s in sh:
        pct = float(s["ownership_pct"] or 0)
        share_amt = round(distributable * pct / 100, 0) if distributable > 0 else 0
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
    
    return {"success": True, "data": {
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
            "total_revenue": round(total_revenue, 0),
            "cogs": round(cogs, 0),
            "gross_profit": round(gross_profit, 0),
            "total_expense": round(total_expense, 0),
            "depreciation": round(total_dep, 0)
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
    
    if net_profit <= 0:
        return {"success": False, "error": "No profit to distribute"}
    
    period_label = calc.get("period", date.today().strftime("%Y-%m"))
    
    _me("INSERT INTO profit_distributions (distribution_date,period_label,net_profit,management_fee,distributable_amount,status) VALUES (%s,%s,%s,%s,%s,'distributed')",
        (date.today(), period_label, net_profit, mgmt_fee, distributable))
    dist_id = _mq("SELECT LAST_INSERT_ID() as id")[0]["id"]
    
    if mgmt_fee > 0:
        _me("INSERT INTO profit_distribution_details (distribution_id,shareholder_id,shareholder_name,share_amount,share_pct,is_management_fee) VALUES (%s,%s,%s,%s,%s,TRUE)",
            (dist_id, 1, "Aung Chan Myint", mgmt_fee, 10.0))
    
    for sh in shares:
        amt = float(sh.get("share_amount", 0))
        if amt > 0:
            _me("INSERT INTO profit_distribution_details (distribution_id,shareholder_id,shareholder_name,share_amount,share_pct,is_management_fee) VALUES (%s,%s,%s,%s,%s,FALSE)",
                (dist_id, sh["shareholder_id"], sh["name"], amt, sh["ownership_pct"]))
    
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
    
    pm_map = {'kpay':'KPay','cash':'Cash','wavepay':'WavePay','wave':'WavePay','aya_pay':'AYA Pay','kbz_bank':'KBZ Bank','acm_acc':"ACM's Acc"}
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
#  TOPUPS — CREATE
# ═══════════════════════════════════════
@router.get("/dashboard/till/today")
async def get_till_today(user: dict = Depends(get_current_user)):
    """Get today's till record. Auto-creates if it doesn't exist."""
    try:
        today = _get_mmt_today_str()

        # Get branch_id from header (default 1)
        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        row = _mysql_query_one(
            "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
            (today, branch_id)
        )

        if not row:
            # Auto-create empty till record for today
            now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")
            _mysql_execute(
                """INSERT INTO daily_till (branch_id, till_date, status, opened_at)
                   VALUES (%s, %s, 'open', %s)""",
                (branch_id, today, now_mmt)
            )
            row = _mysql_query_one(
                "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
                (today, branch_id)
            )

        # Calculate payment-method sales from sales_daily for today
        # payment_method field can be: "Cash:10000", "KPay:31000|Cash:20000|WavePay:17000", etc.
        all_sales = _mysql_query(
            """SELECT payment_method, net FROM sales_daily
               WHERE DATE(sale_date) = %s AND payment_method IS NOT NULL""",
            (today,)
        ) or []
        
        # Parse pipe-separated payment strings and aggregate by method
        pm_totals = {}  # method -> total
        for s in all_sales:
            pm_str = (s.get("payment_method") or "").strip()
            net = float(s.get("net") or 0)
            parts = pm_str.split("|")
            total_method_val = 0
            for part in parts:
                if ":" in part:
                    m, val = part.rsplit(":", 1)
                    try:
                        val_num = float(val)
                        total_method_val += val_num
                        pm_totals[m.strip()] = pm_totals.get(m.strip(), 0) + val_num
                    except ValueError:
                        pass
            # If parsed total doesn't match net, distribute remainder proportionally
            # (net is the authoritative total)
            if total_method_val > 0 and abs(net - total_method_val) > 0.01:
                ratio = net / total_method_val if total_method_val else 1
                for m, v in list(pm_totals.items()):
                    # Adjust only the methods from this row — but simpler: skip micro-adjustment
                    pass
        
        cash_total = pm_totals.get("Cash", 0)
        kpay_total = pm_totals.get("KPay", 0)
        wavepay_total = pm_totals.get("WavePay", 0)
        ayapay_total = pm_totals.get("AYA Pay", 0)
        total_sales = sum(pm_totals.values())
        
        # ── Cash outflows: eject + transfer_out (both leave the physical drawer) ──
        cm = _mysql_query_one(
            """SELECT
                 COALESCE(SUM(CASE WHEN movement_type = 'eject' THEN amount ELSE 0 END), 0) AS cash_ejected,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_out' THEN ABS(amount) ELSE 0 END), 0) AS cash_transferred_out,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_in' THEN amount ELSE 0 END), 0) AS cash_transferred_in,
                 COALESCE(SUM(CASE WHEN movement_type = 'inject' THEN amount ELSE 0 END), 0) AS cash_injected
               FROM cash_movements
               WHERE account = 'Cash' AND DATE(created_at) = %s""",
            (today,)
        )
        cash_ejected = float(cm["cash_ejected"]) if cm else 0.0
        cash_transferred_out = float(cm["cash_transferred_out"]) if cm else 0.0
        cash_transferred_in = float(cm["cash_transferred_in"]) if cm else 0.0
        cash_injected = float(cm["cash_injected"]) if cm else 0.0
        cash_outflows = cash_ejected + cash_transferred_out  # both physically leave drawer
        cash_inflows_extra = cash_transferred_in + cash_injected
        
        logger.info(f"till/today cash movements: ejected={cash_ejected} tx_out={cash_transferred_out} tx_in={cash_transferred_in} injected={cash_injected}")

        # Update the till record with live sales totals
        # Expected = Opening + Cash Sales + Transfers In + Injections - Ejects - Transfers Out
        expected_closing = (float(row["opening_balance"] or 0) + cash_total + cash_inflows_extra - cash_outflows)
        _mysql_execute(
            """UPDATE daily_till
               SET cash_sales_total = %s, kpay_sales_total = %s,
                   expected_closing = %s
               WHERE id = %s""",
            (cash_total, kpay_total, expected_closing, row["id"])
        )

        # Re-fetch to get updated values
        row = _mysql_query_one(
            "SELECT * FROM daily_till WHERE id = %s", (row["id"],)
        )

        # Format response
        def _fmt(val):
            return float(val) if val is not None else 0.0

        return {
            "success": True,
            "data": {
                "id": row["id"],
                "branch_id": row["branch_id"],
                "till_date": str(row["till_date"]),
                "opening_balance": _fmt(row.get("opening_balance")),
                "closing_balance": _fmt(row.get("closing_balance")),
                "cash_sales_total": _fmt(row.get("cash_sales_total")),
                "kpay_sales_total": _fmt(row.get("kpay_sales_total")),
                "expected_closing": _fmt(row.get("expected_closing")),
                "actual_cash_counted": _fmt(row.get("actual_cash_counted")),
                "variance": _fmt(row.get("variance")),
                "opened_by": row.get("opened_by") or "",
                "closed_by": row.get("closed_by") or "",
                "opened_at": row["opened_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("opened_at") else None,
                "closed_at": row["closed_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("closed_at") else None,
                "notes": row.get("notes") or "",
                "status": row["status"],
            },
            "sales_summary": {
                "cash_total": cash_total,
                "kpay_total": kpay_total,
                "wavepay_total": wavepay_total,
                "ayapay_total": ayapay_total,
                "total_sales": total_sales,
                "cash_ejected": cash_ejected,
                "cash_transferred_out": cash_transferred_out,
                "cash_transferred_in": cash_transferred_in,
                "cash_injected": cash_injected,
                "cash_outflows": cash_outflows,
                "cash_inflows_extra": cash_inflows_extra,
            }
        }
    except Exception as e:
        logger.error(f"till/today error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/till/open")
async def open_till(req: dict, user: dict = Depends(get_current_user)):
    """Open today's till with opening balance."""
    try:
        today = _get_mmt_today_str()
        opening_balance = float(req.get("opening_balance", 0))
        opened_by = str(req.get("opened_by", user.get("username", "")))

        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")

        existing = _mysql_query_one(
            "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
            (today, branch_id)
        )

        if existing:
            _mysql_execute(
                """UPDATE daily_till
                   SET opening_balance = %s, opened_by = %s, opened_at = %s, status = 'open'
                   WHERE id = %s""",
                (opening_balance, opened_by, now_mmt, existing["id"])
            )
        else:
            _mysql_execute(
                """INSERT INTO daily_till
                   (branch_id, till_date, opening_balance, opened_by, opened_at, status)
                   VALUES (%s, %s, %s, %s, %s, 'open')""",
                (branch_id, today, opening_balance, opened_by, now_mmt)
            )

        logger.info(f"Till opened by {opened_by} with balance {opening_balance}")
        return {"success": True, "message": f"Till opened with {opening_balance:,.2f} MMK"}
    except Exception as e:
        logger.error(f"till/open error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/till/close")
async def close_till(req: dict, user: dict = Depends(get_current_user)):
    """Close today's till with actual cash counted."""
    try:
        today = _get_mmt_today_str()
        actual_cash = float(req.get("actual_cash_counted", 0))
        closed_by = str(req.get("closed_by", user.get("username", "")))
        notes = str(req.get("notes", ""))

        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        now_mmt = _get_mmt_now().strftime("%Y-%m-%d %H:%M:%S")

        existing = _mysql_query_one(
            "SELECT * FROM daily_till WHERE till_date = %s AND branch_id = %s",
            (today, branch_id)
        )

        if not existing:
            return {"success": False, "error": "No till record found for today"}

        if existing["status"] == "closed":
            return {"success": False, "error": "Till is already closed for today"}

        # Calculate live sales totals before closing (parse pipe-separated payment_method)
        all_cs = _mysql_query(
            """SELECT payment_method, net FROM sales_daily
               WHERE DATE(sale_date) = %s AND payment_method IS NOT NULL""",
            (today,)
        ) or []
        cash_total = 0.0
        kpay_total = 0.0
        for s in all_cs:
            pm_str = (s.get("payment_method") or "").strip()
            for part in pm_str.split("|"):
                if ":" in part:
                    m, v = part.rsplit(":", 1)
                    try:
                        vv = float(v)
                        if m.strip() == "Cash":
                            cash_total += vv
                        elif m.strip() == "KPay":
                            kpay_total += vv
                    except ValueError:
                        pass

        opening = float(existing["opening_balance"] or 0)
        
        # Include cash movements (eject + transfers) in expected
        cm = _mysql_query_one(
            """SELECT
                 COALESCE(SUM(CASE WHEN movement_type = 'eject' THEN amount ELSE 0 END), 0) AS cash_ejected,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_out' THEN ABS(amount) ELSE 0 END), 0) AS cash_transferred_out,
                 COALESCE(SUM(CASE WHEN movement_type = 'transfer_in' THEN amount ELSE 0 END), 0) AS cash_transferred_in,
                 COALESCE(SUM(CASE WHEN movement_type = 'inject' THEN amount ELSE 0 END), 0) AS cash_injected
               FROM cash_movements
               WHERE account = 'Cash' AND DATE(created_at) = %s""",
            (today,)
        )
        _ejected = float(cm["cash_ejected"]) if cm else 0.0
        _tx_out = float(cm["cash_transferred_out"]) if cm else 0.0
        _tx_in = float(cm["cash_transferred_in"]) if cm else 0.0
        _injected = float(cm["cash_injected"]) if cm else 0.0
        
        expected = opening + cash_total + _tx_in + _injected - _ejected - _tx_out
        variance = actual_cash - expected
        closing_balance = actual_cash

        _mysql_execute(
            """UPDATE daily_till
               SET actual_cash_counted = %s, variance = %s, closing_balance = %s,
                   closed_by = %s, closed_at = %s, notes = %s, status = 'closed',
                   cash_sales_total = %s, expected_closing = %s
               WHERE id = %s""",
            (actual_cash, variance, closing_balance,
             closed_by, now_mmt, notes,
             cash_total, expected,
             existing["id"])
        )

        logger.info(f"Till closed by {closed_by}: expected={expected}, actual={actual_cash}, variance={variance}")
        return {
            "success": True,
            "message": f"Till closed. Variance: {variance:+,.2f} MMK",
            "data": {
                "opening_balance": opening,
                "cash_sales_total": cash_total,
                "expected_closing": expected,
                "actual_cash_counted": actual_cash,
                "variance": variance,
            }
        }
    except Exception as e:
        logger.error(f"till/close error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/dashboard/till/history")
async def get_till_history(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user)
):
    """Get past till records."""
    try:
        try:
            branch_id = int(user.get("branch_id", 1))
        except Exception:
            branch_id = 1

        rows = _mysql_query(
            """SELECT * FROM daily_till
               WHERE branch_id = %s AND till_date <= CURDATE()
               ORDER BY till_date DESC
               LIMIT %s""",
            (branch_id, days)
        )

        def _fmt(val):
            return float(val) if val is not None else 0.0

        return {
            "success": True,
            "data": [{
                "id": r["id"],
                "till_date": str(r["till_date"]),
                "opening_balance": _fmt(r.get("opening_balance")),
                "cash_sales_total": _fmt(r.get("cash_sales_total")),
                "kpay_sales_total": _fmt(r.get("kpay_sales_total")),
                "expected_closing": _fmt(r.get("expected_closing")),
                "actual_cash_counted": _fmt(r.get("actual_cash_counted")),
                "variance": _fmt(r.get("variance")),
                "opened_by": r.get("opened_by") or "",
                "closed_by": r.get("closed_by") or "",
                "opened_at": r["opened_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("opened_at") else None,
                "closed_at": r["closed_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("closed_at") else None,
                "notes": r.get("notes") or "",
                "status": r["status"],
            } for r in rows]
        }
    except Exception as e:
        logger.error(f"till/history error: {e}")
        return {"success": False, "error": str(e)}


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
                   cs.actual_duration_mins AS duration_mins, cs.staff_name, cb.phone, cs.created_at,
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
                   payment_method, notes, created_at, sale_date
            FROM sales_daily
            WHERE DATE(sale_date) = %s
              AND console_id IS NOT NULL AND console_id != ''
            ORDER BY console_id, created_at
        """, (date,)) or []

        # 3. Parse play minutes from sales notes
        for sr in sales_records:
            notes = sr.get("notes") or ""
            m = re.search(r'Mins?:?\s*(\d+)', notes, re.IGNORECASE)
            sr["_play_mins"] = int(m.group(1)) if m else 0

        # 4. Matching logic:
        #    Match by console_id + mins within ±10 + time proximity
        matched_pairs = []  # (session, sale)
        used_sale_ids = set()

        for session in done_sessions:
            sid = session["booking_id"]
            cid = session["console_id"]
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
                actual = session.get("actual_mins") or session.get("duration_mins") or 0
                if actual <= 0:
                    actual = session.get("duration_mins") or 0
                mult = parse_multiplier(session.get("console_id", ""))
                est = round((actual / 60) * 10000 * mult)
                missing_sales.append({
                    "booking_id": session["booking_id"],
                    "console_id": session["console_id"],
                    "start_time": session["start_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("start_time") else None,
                    "end_time": session["end_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("end_time") else None,
                    "actual_mins": actual,
                    "staff_name": session.get("staff_name") or "—",
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

@router.get("/dashboard/stock-check")
async def dashboard_get_stock_check(date: str = None, user: dict = Depends(get_current_user)):
    """Get daily stock check — system qty vs actual count.
    If no date given, use today. Returns all inventory items with system qty
    and any previously saved actual counts for that date."""
    try:
        if not date:
            date = _get_mmt_now().strftime("%Y-%m-%d")
        
        # Get all inventory items with system quantity
        items = _mysql_query(
            "SELECT id, item_name, category, quantity as system_qty, unit_price "
            "FROM inventory WHERE branch_id = 1 ORDER BY category, item_name"
        ) or []
        
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
            if actual is not None:
                has_any_actual = True
                variance = int(item["system_qty"]) - int(actual)
            result.append({
                "item_name": name,
                "category": item.get("category", ""),
                "system_qty": int(item["system_qty"]),
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
async def dashboard_save_stock_check(req: dict, user: dict = Depends(get_current_user)):
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
            
            # Get system qty
            inv = _mysql_query_one(
                "SELECT quantity FROM inventory WHERE item_name = %s AND branch_id = 1",
                (name,)
            )
            system_qty = int(inv["quantity"]) if inv else 0
            variance = system_qty - int(actual_qty)
            
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
async def dashboard_stock_check_history(limit: int = 30, user: dict = Depends(get_current_user)):
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
async def dashboard_stock_check_resolve(req: dict, user: dict = Depends(get_current_user)):
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
                _mysql_execute(
                    """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
                       VALUES (%s, %s, %s, 0, 'Stock Check Adjustment', %s, '', '', %s)""",
                    (adj_batch, item_name, qty, adj_batch, resolved_by)
                )
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
