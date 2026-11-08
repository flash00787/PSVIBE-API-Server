"""
PS VIBE Data Digest & Validation Engine
========================================
Systematic data integrity validation for PS VIBE — PS5 Gaming Lounge.

Each check function:
  - Handles its own exceptions
  - Returns structured results
  - Works on empty tables gracefully
  - Accepts optional date filter (YYYY-MM-DD)
"""
import logging
import os
from datetime import datetime, date, timedelta

import mysql.connector

logger = logging.getLogger("psvibe_digest")

# ── MySQL connection config ─────────────────────────────────────────
def _load_secrets_env(path="/etc/psvibe/secrets.env"):
    """Load MYSQL_* env vars from systemd EnvironmentFile if not already set."""
    if os.environ.get("MYSQL_PASSWORD"):
        return  # already set via systemd / environment
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("MYSQL_") and key not in os.environ:
                    os.environ[key] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass  # no secrets file available — rely on env or defaults

_load_secrets_env()

_MYSQL_CFG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "user": os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "psvibe_api"),
}


def _query(sql, params=None):
    """Execute a SELECT query and return all rows as list of dicts."""
    conn = mysql.connector.connect(**_MYSQL_CFG)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        return rows
    finally:
        conn.close()


def _query_one(sql, params=None):
    """Execute a SELECT query and return first row, or None."""
    rows = _query(sql, params)
    return rows[0] if rows else None


def _today_str():
    """Return today's date as YYYY-MM-DD (UTC)."""
    return datetime.utcnow().strftime("%Y-%m-%d")


def _date_filter(table_date_col, date_val):
    """Return (where_clause, params) for optional date filtering."""
    if date_val:
        return f" WHERE {table_date_col} = %s", (date_val,)
    return "", ()


# ═══════════════════════════════════════════════════════════════════
#  CHECK 1: Wallet Balance Integrity
# ═══════════════════════════════════════════════════════════════════
def check_wallet_balance_integrity(date=None):
    """
    Verify member_wallets consistency:
      - balance_mins should equal total_bought_mins - total_used_mins
      - balance_mins must be >= 0 (no negative balances)

    Returns: {"status": "pass"|"warning"|"critical", "issues": [...], "count": N}
    """
    logger.info("check_wallet_balance_integrity(date=%s)", date)
    issues = []
    try:
        rows = _query(
            "SELECT member_id, member_name, balance_mins, total_bought_mins, total_used_mins "
            "FROM member_wallets WHERE member_id IS NOT NULL AND member_id != ''"
        )
        for r in rows:
            mid = r.get("member_id", "?")
            bal = int(r.get("balance_mins") or 0)
            bought = int(r.get("total_bought_mins") or 0)
            used = int(r.get("total_used_mins") or 0)

            # Check 1a: balance = bought - used
            expected = bought - used
            diff = abs(bal - expected)
            if diff > 5:
                issues.append({
                    "type": "balance_mismatch",
                    "member_id": mid,
                    "member_name": r.get("member_name", ""),
                    "balance_mins": bal,
                    "expected": expected,
                    "diff": bal - expected,
                    "detail": f"Member {mid}: balance={bal}, expected {bought}-{used}={expected} (diff={bal - expected})"
                })

            # Check 1b: no negative balances
            if bal < 0:
                issues.append({
                    "type": "negative_balance",
                    "member_id": mid,
                    "member_name": r.get("member_name", ""),
                    "balance_mins": bal,
                    "detail": f"Member {mid}: negative balance {bal}"
                })

        status = "critical" if any(i["type"] == "balance_mismatch" for i in issues) else (
            "warning" if issues else "pass"
        )
        logger.info("wallet_balance_integrity: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_wallet_balance_integrity error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK 2: Topup Chain Integrity
# ═══════════════════════════════════════════════════════════════════
def check_topup_chain_integrity(date=None):
    """
    Verify topup_log consistency:
      - For each member's topups ordered by date:
        balance_mins_after of prev topup ≈ balance_mins_before of next topup (±5 mins)

    Returns: {"status": ..., "issues": [...], "count": N}
    """
    logger.info("check_topup_chain_integrity(date=%s)", date)
    issues = []
    try:
        member_ids = _query(
            "SELECT DISTINCT member_id FROM topup_log WHERE member_id IS NOT NULL AND member_id != ''"
        )
        for mrow in member_ids:
            mid = mrow["member_id"]
            topups = _query(
                "SELECT id, topup_date, amount, mins_added, balance_mins_before, balance_mins_after "
                "FROM topup_log WHERE member_id=%s ORDER BY topup_date ASC, id ASC",
                (mid,)
            )
            if len(topups) < 2:
                continue

            for i in range(1, len(topups)):
                prev_after = topups[i - 1].get("balance_mins_after")
                curr_before = topups[i].get("balance_mins_before")

                if prev_after is None or curr_before is None:
                    # One of them is NULL — skip (may be legacy data)
                    continue

                prev_after = int(prev_after or 0)
                curr_before = int(curr_before or 0)
                diff = abs(prev_after - curr_before)

                if diff > 5:
                    issues.append({
                        "type": "chain_break",
                        "member_id": mid,
                        "prev_topup_id": topups[i - 1]["id"],
                        "curr_topup_id": topups[i]["id"],
                        "prev_after": prev_after,
                        "curr_before": curr_before,
                        "diff": diff,
                        "detail": (
                            f"Member {mid}: topup#{topups[i-1]['id']} after={prev_after} → "
                            f"topup#{topups[i]['id']} before={curr_before} (diff={diff})"
                        )
                    })

        status = "critical" if len(issues) > 0 else "pass"
        logger.info("topup_chain_integrity: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_topup_chain_integrity error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK 3: Sales vs Bookings Cross-Reference
# ═══════════════════════════════════════════════════════════════════
def check_sales_vs_bookings(date=None):
    """
    Cross-reference sales_daily with console_bookings:
      - Each completed/cancelled booking should have a corresponding sales_daily record
      - Each sales_daily record with a console_id should have a corresponding booking
      - Flag orphans on both sides

    Returns: {"status": ..., "issues": [...], "count": N}
    """
    logger.info("check_sales_vs_bookings(date=%s)", date)
    issues = []
    try:
        # ── Bookings with no matching sales ──
        bk_sql = (
            "SELECT cb.id, cb.console_id, cb.member_id, cb.booking_date, "
            "COALESCE(cs.actual_start, cb.planned_start) AS start_time, cb.status "
            "FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE cb.status IN ('Done', 'Active', 'cancelled') "
            "  AND cb.console_id IS NOT NULL AND cb.console_id != ''"
        )
        bk_params = ()
        if date:
            bk_sql += " AND booking_date = %s"
            bk_params = (date,)
        bookings = _query(bk_sql, bk_params)

        # ── Sales with console_id ──
        sl_sql = "SELECT id, voucher_no, console_id, member_id, sale_date FROM sales_daily WHERE console_id IS NOT NULL AND console_id != ''"
        sl_params = ()
        if date:
            sl_sql += " AND sale_date = %s"
            sl_params = (date,)
        sales = _query(sl_sql, sl_params)

        # Build lookup sets
        # Sales: (console_id, member_id, sale_date) — fuzzy match
        sales_set = set()
        for s in sales:
            sd = str(s.get("sale_date", ""))[:10]
            cid = (s.get("console_id") or "").replace(" ", "").upper()
            mid = (s.get("member_id") or "").strip()
            sales_set.add((cid, mid, sd))

        # Booking IDs found in sales (by console+date)
        for bk in bookings:
            bd = str(bk.get("booking_date", ""))[:10]
            cid_bk = (bk.get("console_id") or "").replace(" ", "").upper()
            mid_bk = (bk.get("member_id") or "").strip()
            if (cid_bk, mid_bk, bd) not in sales_set:
                # Try without member_id (fuzzy: just console + date)
                fuzzy = any(
                    sc == cid_bk and sd == bd
                    for (sc, sm, sd) in sales_set
                )
                if not fuzzy:
                    issues.append({
                        "type": "booking_without_sale",
                        "booking_id": bk["id"],
                        "console_id": bk.get("console_id", ""),
                        "member_id": bk.get("member_id", ""),
                        "booking_date": bd,
                        "booking_status": bk.get("status", ""),
                        "detail": f"Booking#{bk['id']} ({bk.get('status')}) on {bd} has no matching sales_daily record"
                    })

        # ── Sales with no matching booking ──
        bk_set = set()
        for bk in bookings:
            bd = str(bk.get("booking_date", ""))[:10]
            cid_bk = (bk.get("console_id") or "").replace(" ", "").upper()
            mid_bk = (bk.get("member_id") or "").strip()
            bk_set.add((cid_bk, mid_bk, bd))

        for s in sales:
            sd = str(s.get("sale_date", ""))[:10]
            cid_s = (s.get("console_id") or "").replace(" ", "").upper()
            mid_s = (s.get("member_id") or "").strip()
            if (cid_s, mid_s, sd) not in bk_set:
                fuzzy = any(
                    bc == cid_s and bd == sd
                    for (bc, bm, bd) in bk_set
                )
                if not fuzzy:
                    issues.append({
                        "type": "sale_without_booking",
                        "sale_id": s["id"],
                        "voucher_no": s.get("voucher_no", ""),
                        "console_id": s.get("console_id", ""),
                        "member_id": s.get("member_id", ""),
                        "sale_date": sd,
                        "detail": f"Sale#{s['id']} (voucher {s.get('voucher_no')}) on {sd} has no matching booking"
                    })

        status = "critical" if len(issues) > 0 else "pass"
        logger.info("sales_vs_bookings: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_sales_vs_bookings error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK 4: Inventory Consistency
# ═══════════════════════════════════════════════════════════════════
def check_inventory_consistency(date=None):
    """
    Verify: inventory.quantity = SUM(stock_in.quantity) - SUM(stock_out.quantity)
    Flag mismatches > 0.

    Returns: {"status": ..., "issues": [...], "count": N}
    """
    logger.info("check_inventory_consistency(date=%s)", date)
    issues = []
    try:
        items = _query("SELECT item_name, quantity FROM inventory ORDER BY item_name")
        for item in items:
            item_name = item["item_name"]
            inv_qty = int(item.get("quantity") or 0)

            # Sum stock_in for this item
            si = _query_one(
                "SELECT COALESCE(SUM(quantity), 0) AS total_in FROM stock_in WHERE item_name = %s",
                (item_name,)
            )
            total_in = int(si["total_in"]) if si else 0

            # Sum stock_out for this item
            so = _query_one(
                "SELECT COALESCE(SUM(quantity), 0) AS total_out FROM stock_out WHERE item_name = %s",
                (item_name,)
            )
            total_out = int(so["total_out"]) if so else 0

            expected = total_in - total_out
            diff = inv_qty - expected

            if diff != 0:
                issues.append({
                    "type": "inventory_mismatch",
                    "item_name": item_name,
                    "inventory_quantity": inv_qty,
                    "stock_in_total": total_in,
                    "stock_out_total": total_out,
                    "expected_quantity": expected,
                    "drift": diff,
                    "detail": f"{item_name}: inventory={inv_qty}, FIFO in({total_in})-out({total_out})={expected} (drift={diff})"
                })

        status = "critical" if len(issues) > 0 else "pass"
        logger.info("inventory_consistency: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_inventory_consistency error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK 5: Receipts vs Sales
# ═══════════════════════════════════════════════════════════════════
def check_receipts_vs_sales(date=None):
    """
    Verify receipts match sales:
      - Count receipts vs sales_daily records per day
      - Flag receipt_no values in one but not the other

    Returns: {"status": ..., "issues": [...], "count": N}
    """
    logger.info("check_receipts_vs_sales(date=%s)", date)
    issues = []
    try:
        # Count receipts per day
        rec_sql = (
            "SELECT DATE(receipt_date) as dt, COUNT(*) as cnt "
            "FROM receipts GROUP BY DATE(receipt_date)"
        )
        rec_params = ()
        rec_rows = _query(rec_sql, rec_params)

        # Count sales per day
        sal_sql = (
            "SELECT sale_date as dt, COUNT(*) as cnt "
            "FROM sales_daily GROUP BY sale_date"
        )
        sal_params = ()
        sal_rows = _query(sal_sql, sal_params)

        rec_map = {str(r["dt"]): r["cnt"] for r in rec_rows}
        sal_map = {str(s["dt"]): s["cnt"] for s in sal_rows}

        all_dates = set(rec_map.keys()) | set(sal_map.keys())
        for d in sorted(all_dates):
            if date and d != date:
                continue
            rcnt = rec_map.get(d, 0)
            scnt = sal_map.get(d, 0)
            if rcnt != scnt:
                issues.append({
                    "type": "receipt_sale_count_mismatch",
                    "date": d,
                    "receipt_count": rcnt,
                    "sale_count": scnt,
                    "diff": rcnt - scnt,
                    "detail": f"{d}: receipts={rcnt}, sales={scnt} (diff={rcnt - scnt})"
                })

        # Also check receipt_no cross-reference (receipt_no in receipts not in sales_daily voucher_no)
        if not date:
            # Only do full cross-ref without date filter (it would be meaningless with one day)
            rec_nos = _query("SELECT DISTINCT receipt_no FROM receipts WHERE receipt_no IS NOT NULL AND receipt_no != ''")
            sal_nos = _query("SELECT DISTINCT voucher_no FROM sales_daily WHERE voucher_no IS NOT NULL AND voucher_no != ''")
            rec_set = {r["receipt_no"] for r in rec_nos}
            sal_set = {s["voucher_no"] for s in sal_nos}

            orphan_recs = rec_set - sal_set
            orphan_sales = sal_set - rec_set

            for rn in sorted(orphan_recs):
                issues.append({
                    "type": "receipt_without_sale",
                    "receipt_no": rn,
                    "detail": f"Receipt {rn} exists in receipts but not in sales_daily"
                })
            for sn in sorted(orphan_sales):
                issues.append({
                    "type": "sale_without_receipt",
                    "voucher_no": sn,
                    "detail": f"Voucher {sn} exists in sales_daily but not in receipts"
                })

        status = "warning" if len(issues) > 0 else "pass"
        logger.info("receipts_vs_sales: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_receipts_vs_sales error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK 6: Negative or Zero Sales
# ═══════════════════════════════════════════════════════════════════
def check_negative_or_zero_sales(date=None):
    """
    Flag suspicious records:
      - sales_daily where amount <= 0 or net <= 0
      - topup_log where amount <= 0

    Returns: {"status": ..., "issues": [...], "count": N}
    """
    logger.info("check_negative_or_zero_sales(date=%s)", date)
    issues = []
    try:
        # ── Check sales_daily ──
        sl_where, sl_params = _date_filter("sale_date", date)
        sl_sql = f"SELECT id, voucher_no, sale_date, console_id, member_id, amount, gross, discount, net FROM sales_daily{sl_where}"
        sales = _query(sl_sql, sl_params)

        for s in sales:
            amt = float(s.get("amount") or 0)
            net = float(s.get("net") or 0)

            if amt <= 0:
                issues.append({
                    "type": "zero_or_negative_amount",
                    "source": "sales_daily",
                    "sale_id": s["id"],
                    "voucher_no": s.get("voucher_no", ""),
                    "date": str(s.get("sale_date", "")),
                    "amount": amt,
                    "detail": f"Sale#{s['id']} voucher {s.get('voucher_no')}: amount={amt}"
                })
            if net <= 0 and amt > 0:
                # Only flag if net is suspicious but amount is positive (could be fully discounted)
                issues.append({
                    "type": "zero_or_negative_net",
                    "source": "sales_daily",
                    "sale_id": s["id"],
                    "voucher_no": s.get("voucher_no", ""),
                    "date": str(s.get("sale_date", "")),
                    "gross": float(s.get("gross") or 0),
                    "discount": float(s.get("discount") or 0),
                    "net": net,
                    "detail": f"Sale#{s['id']}: net={net} (gross={s.get('gross')}, discount={s.get('discount')})"
                })

        # ── Check topup_log ──
        tu_where, tu_params = (" WHERE topup_date >= %s", (date,)) if date else ("", ())
        tu_sql = f"SELECT id, member_id, amount, mins_added, topup_date FROM topup_log{tu_where}"
        topups = _query(tu_sql, tu_params)

        for t in topups:
            amt = float(t.get("amount") or 0)
            if amt <= 0:
                issues.append({
                    "type": "zero_or_negative_topup",
                    "source": "topup_log",
                    "topup_id": t["id"],
                    "member_id": t.get("member_id", ""),
                    "date": str(t.get("topup_date", "")),
                    "amount": amt,
                    "detail": f"Topup#{t['id']} member {t.get('member_id')}: amount={amt}"
                })

        status = "critical" if len(issues) > 0 else "pass"
        logger.info("negative_or_zero_sales: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_negative_or_zero_sales error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK 7: Booking Status Integrity
# ═══════════════════════════════════════════════════════════════════
def check_booking_status_integrity():
    """
    Verify booking consistency:
      - No bookings with end_time < start_time
      - No overlapping bookings on same console
      - No bookings with status='Active' but end_time is in the past (stuck active)

    Returns: {"status": ..., "issues": [...], "count": N}
    """
    logger.info("check_booking_status_integrity()")
    issues = []
    try:
        # ── Check end_time < start_time ──
        bad_times = _query(
            "SELECT id, console_id, member_id, COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_end, cb.planned_end) AS end_time, cb.status FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE COALESCE(cs.actual_start, cb.planned_start) IS NOT NULL "
            "  AND COALESCE(cs.actual_end, cb.planned_end) IS NOT NULL "
            "  AND COALESCE(cs.actual_end, cb.planned_end) < COALESCE(cs.actual_start, cb.planned_start)"
        )
        for b in bad_times:
            issues.append({
                "type": "end_before_start",
                "booking_id": b["id"],
                "console_id": b.get("console_id", ""),
                "start_time": str(b.get("start_time", "")),
                "end_time": str(b.get("end_time", "")),
                "status": b.get("status", ""),
                "detail": f"Booking#{b['id']}: end_time ({b.get('end_time')}) < start_time ({b.get('start_time')})"
            })

        # ── Check overlapping bookings on same console ──
        consoles = _query("SELECT DISTINCT console_id FROM console_bookings WHERE console_id IS NOT NULL AND console_id != ''")
        for crow in consoles:
            cid = crow["console_id"]
            bookings = _query(
                "SELECT id, COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
                "COALESCE(cs.actual_end, cb.planned_end) AS end_time, cb.status FROM console_bookings cb "
                "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
                "WHERE cb.console_id = %s "
                "  AND COALESCE(cs.actual_start, cb.planned_start) IS NOT NULL "
                "  AND COALESCE(cs.actual_end, cb.planned_end) IS NOT NULL "
                "  AND cb.status NOT IN ('cancelled', 'rejected') "
                "ORDER BY COALESCE(cs.actual_start, cb.planned_start) ASC",
                (cid,)
            )
            for i in range(len(bookings)):
                for j in range(i + 1, len(bookings)):
                    a_start = bookings[i]["start_time"]
                    a_end = bookings[i]["end_time"]
                    b_start = bookings[j]["start_time"]
                    b_end = bookings[j]["end_time"]

                    # Overlap: a_start < b_end AND b_start < a_end
                    if a_start < b_end and b_start < a_end:
                        issues.append({
                            "type": "overlapping_booking",
                            "console_id": cid,
                            "booking_a": bookings[i]["id"],
                            "booking_b": bookings[j]["id"],
                            "a_start": str(a_start),
                            "a_end": str(a_end),
                            "b_start": str(b_start),
                            "b_end": str(b_end),
                            "detail": (
                                f"Console {cid}: Booking#{bookings[i]['id']} [{a_start}→{a_end}] "
                                f"overlaps Booking#{bookings[j]['id']} [{b_start}→{b_end}]"
                            )
                        })

        # ── Check Active bookings stuck in the past ──
        now = datetime.utcnow()
        stuck = _query(
            "SELECT cb.id, cb.console_id, cb.member_id, "
            "COALESCE(cs.actual_start, cb.planned_start) AS start_time, "
            "COALESCE(cs.actual_end, cb.planned_end) AS end_time, "
            "cb.status, COALESCE(cs.actual_duration_mins, cb.planned_duration_mins) AS duration_mins "
            "FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE cb.status = 'Active' AND COALESCE(cs.actual_end, cb.planned_end) IS NOT NULL "
            "  AND COALESCE(cs.actual_end, cb.planned_end) < NOW()"
        )
        for b in stuck:
            issues.append({
                "type": "stuck_active_booking",
                "booking_id": b["id"],
                "console_id": b.get("console_id", ""),
                "member_id": b.get("member_id", ""),
                "end_time": str(b.get("end_time", "")),
                "duration_mins": b.get("duration_mins", 0),
                "detail": f"Booking#{b['id']} is status='Active' but end_time ({b.get('end_time')}) is in the past"
            })

        status = "critical" if len(issues) > 0 else "pass"
        logger.info("booking_status_integrity: %s (%d issues)", status, len(issues))
        return {"status": status, "issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error("check_booking_status_integrity error: %s", e, exc_info=True)
        return {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}


# ═══════════════════════════════════════════════════════════════════
#  CHECK ALL
# ═══════════════════════════════════════════════════════════════════
_CHECK_FUNCTIONS = {
    "wallet_balance": check_wallet_balance_integrity,
    "topup_chain": check_topup_chain_integrity,
    "sales_vs_bookings": check_sales_vs_bookings,
    "inventory": check_inventory_consistency,
    "receipts_vs_sales": check_receipts_vs_sales,
    "negative_sales": check_negative_or_zero_sales,
    "booking_status": check_booking_status_integrity,
}


def run_all_checks(date=None):
    """
    Run all data integrity checks and return a combined report.

    Returns:
    {
        "timestamp": "ISO timestamp",
        "date": "YYYY-MM-DD",
        "overall_status": "pass" | "warning" | "critical",
        "total_issues": N,
        "checks": {
            "wallet_balance": {"status": "...", "issues": [...], "count": N},
            ...
        }
    }
    """
    target_date = date or _today_str()
    logger.info("run_all_checks(date=%s)", target_date)
    timestamp = datetime.utcnow().isoformat() + "Z"

    checks = {}
    total_issues = 0
    has_critical = False
    has_warning = False

    for name, func in _CHECK_FUNCTIONS.items():
        try:
            result = func(date=date) if name != "booking_status" else func()
        except Exception as e:
            result = {"status": "error", "issues": [{"type": "exception", "detail": str(e)}], "count": 1}
            logger.error("Check '%s' crashed: %s", name, e, exc_info=True)

        checks[name] = result
        total_issues += result.get("count", 0)
        if result.get("status") == "critical":
            has_critical = True
        elif result.get("status") == "warning":
            has_warning = True

    if has_critical:
        overall_status = "critical"
    elif has_warning or total_issues > 0:
        overall_status = "warning"
    else:
        overall_status = "pass"

    report = {
        "timestamp": timestamp,
        "date": target_date,
        "overall_status": overall_status,
        "total_issues": total_issues,
        "checks": checks,
    }

    # Persist to history
    _save_digest_history(target_date, report, kind="check")

    return report


# ═══════════════════════════════════════════════════════════════════
#  GENERATE DAILY DIGEST (Human-Readable Business Summary)
# ═══════════════════════════════════════════════════════════════════
def generate_daily_digest(date=None):
    """
    Generate a HUMAN-READABLE daily business digest.

    Returns:
    {
        "date": "YYYY-MM-DD",
        "summary": { ... },
        "alerts": [...],
        "recommendations": [...]
    }
    """
    target_date = date or _today_str()
    logger.info("generate_daily_digest(date=%s)", target_date)
    alerts = []
    recommendations = []
    summary = {}

    try:
        # ── Total Sales ──
        sl = _query_one(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(gross),0) as gross, COALESCE(SUM(net),0) as net "
            "FROM sales_daily WHERE sale_date = %s",
            (target_date,)
        )
        summary["total_sales"] = {
            "count": int(sl["cnt"]) if sl else 0,
            "gross": float(sl["gross"]) if sl else 0,
            "net": float(sl["net"]) if sl else 0,
        }

        # ── Total Topups ──
        tu = _query_one(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as amount, COALESCE(SUM(mins_added),0) as mins_added "
            "FROM topup_log WHERE DATE(topup_date) = %s",
            (target_date,)
        )
        summary["total_topups"] = {
            "count": int(tu["cnt"]) if tu else 0,
            "amount": float(tu["amount"]) if tu else 0,
            "mins_added": int(tu["mins_added"]) if tu else 0,
        }

        # ── Active vs Completed Sessions ──
        active = _query_one(
            "SELECT COUNT(*) as cnt FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE cs.status = 'active' AND cb.booking_date = %s",
            (target_date,)
        )
        completed = _query_one(
            "SELECT COUNT(*) as cnt FROM console_sessions "
            "WHERE status = 'done' AND DATE(actual_start) = %s",
            (target_date,)
        )
        summary["active_sessions"] = int(active["cnt"]) if active else 0
        summary["completed_sessions"] = int(completed["cnt"]) if completed else 0

        # ── Food Sales ──
        food = _query_one(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as total "
            "FROM stock_out WHERE DATE(sale_date) = %s AND COALESCE(staff_name,'') <> 'recipe'",
            (target_date,)
        )
        summary["food_sales"] = {
            "count": int(food["cnt"]) if food else 0,
            "total": float(food["total"]) if food else 0,
        }

        # ── New Members ──
        new_mem = _query_one(
            "SELECT COUNT(*) as cnt FROM member_wallets WHERE DATE(join_date) = %s",
            (target_date,)
        )
        summary["new_members"] = int(new_mem["cnt"]) if new_mem else 0

        # ── Top Consoles by hours ──
        top_consoles = _query(
            "SELECT cb.console_id, COALESCE(SUM(cs.actual_duration_mins), SUM(cb.planned_duration_mins), 0) as total_mins "
            "FROM console_bookings cb "
            "LEFT JOIN console_sessions cs ON cs.booking_id=cb.id "
            "WHERE cb.booking_date = %s AND cb.status IN ('Done', 'Active') "
            "  AND cb.console_id IS NOT NULL AND cb.console_id != '' "
            "GROUP BY cb.console_id ORDER BY total_mins DESC LIMIT 5",
            (target_date,)
        )
        summary["top_consoles"] = [
            {"console_id": r["console_id"], "hours": round(int(r["total_mins"] or 0) / 60, 1)}
            for r in top_consoles
        ]

        # ── Top Games by sessions ──
        top_games = _query(
            "SELECT game_name, COUNT(*) as sessions "
            "FROM console_bookings "
            "WHERE booking_date = %s AND game_name IS NOT NULL AND game_name != '' "
            "GROUP BY game_name ORDER BY sessions DESC LIMIT 5",
            (target_date,)
        )
        summary["top_games"] = [
            {"game": r["game_name"], "sessions": r["sessions"]}
            for r in top_games
        ]

        # ── Revenue Breakdown by payment_method ──
        pm = _query(
            "SELECT payment_method, COUNT(*) as cnt, COALESCE(SUM(net), 0) as total "
            "FROM sales_daily WHERE sale_date = %s AND payment_method IS NOT NULL "
            "GROUP BY payment_method ORDER BY total DESC",
            (target_date,)
        )
        summary["revenue_by_payment"] = [
            {"method": r["payment_method"], "count": r["cnt"], "total": float(r["total"])}
            for r in pm
        ]

        # ── Generate Alerts ──
        if summary["total_sales"]["count"] == 0 and summary["total_topups"]["count"] == 0:
            alerts.append("⚠️ No sales or topups recorded today. Verify if business is open.")
        if summary["active_sessions"] > 0:
            alerts.append(f"🔴 {summary['active_sessions']} active session(s) still running — check if any are stuck.")
        if summary["total_sales"]["net"] <= 0 and summary["total_sales"]["count"] > 0:
            alerts.append("⚠️ Sales recorded but net revenue is zero or negative — check discounts.")

        # ── Generate Recommendations ──
        if summary["total_sales"]["count"] == 0:
            recommendations.append("Run the integrity checks to verify no missing records.")
        if summary["food_sales"]["count"] == 0:
            recommendations.append("No food sales today — check if food sales are being recorded properly.")
        if summary["top_consoles"]:
            top = summary["top_consoles"][0]
            if top["hours"] < 2:
                recommendations.append(f"Top console ({top['console_id']}) only had {top['hours']}h — low utilization day.")

        # Persist to history
        digest_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "date": target_date,
            "summary": summary,
            "alerts": alerts,
            "recommendations": recommendations,
        }
        _save_digest_history(target_date, digest_data, kind="daily")

        return digest_data

    except Exception as e:
        logger.error("generate_daily_digest error: %s", e, exc_info=True)
        return {
            "date": target_date,
            "summary": {},
            "alerts": [f"❌ Error generating digest: {str(e)}"],
            "recommendations": ["Check server logs for details."],
            "error": str(e),
        }


# ═══════════════════════════════════════════════════════════════════
#  HISTORY PERSISTENCE
# ═══════════════════════════════════════════════════════════════════
import json as _json
import os as _os

_HISTORY_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "digest_history")


def _ensure_history_dir():
    """Ensure the digest_history directory exists."""
    _os.makedirs(_HISTORY_DIR, exist_ok=True)


def _save_digest_history(target_date, data, kind="check"):
    """Save digest/check result to JSON history file."""
    try:
        _ensure_history_dir()
        filename = f"{kind}_{target_date}.json"
        filepath = _os.path.join(_HISTORY_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        logger.info("Saved digest history: %s", filepath)
        return filepath
    except Exception as e:
        logger.error("Failed to save digest history: %s", e)
        return None


def load_digest_history(target_date, kind="check"):
    """Load a saved digest/check result from JSON history."""
    try:
        filename = f"{kind}_{target_date}.json"
        filepath = _os.path.join(_HISTORY_DIR, filename)
        if not _os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        logger.error("Failed to load digest history: %s", e)
        return None


def list_digest_history(days=7):
    """List available digest history files for the last N days."""
    try:
        _ensure_history_dir()
        cutoff = datetime.utcnow() - timedelta(days=days)
        results = []
        for fname in sorted(_os.listdir(_HISTORY_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            fpath = _os.path.join(_HISTORY_DIR, fname)
            try:
                mtime = datetime.fromtimestamp(_os.path.getmtime(fpath))
                if mtime >= cutoff:
                    # Parse kind and date from filename: kind_YYYY-MM-DD.json
                    base = fname.replace(".json", "")
                    parts = base.split("_", 1)
                    kind = parts[0] if len(parts) > 0 else "unknown"
                    file_date = parts[1] if len(parts) > 1 else ""
                    results.append({
                        "filename": fname,
                        "kind": kind,
                        "date": file_date,
                        "generated_at": mtime.isoformat(),
                    })
            except Exception:
                continue
        return results
    except Exception as e:
        logger.error("Failed to list digest history: %s", e)
        return []
