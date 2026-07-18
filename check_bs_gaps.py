#!/usr/bin/env python3
"""
PS VIBE — Balance Sheet Gap Check (Stock Eject Audit)
=======================================================
Compares stock_in payments by method vs cash_movements eject (Stock In: notes).
Reports any gaps by account. Can be called daily or on-demand.

Usage:
  python3 check_bs_gaps.py              # Full audit
  python3 check_bs_gaps.py --json       # JSON output
  python3 check_bs_gaps.py --account "KBZ Bank"  # Single account
  python3 check_bs_gaps.py --alert      # Exit 1 if gaps > 0 (for cron/monitoring)
"""

import pymysql
import sys
import json
import argparse
from datetime import datetime, timezone

DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "root",
    "password": "PsVibe@MySQL2024!",
    "database": "psvibe_api",
    "charset": "utf8mb4",
}


def check_gaps(account_filter=None):
    """Check all accounts (or one) for stock-in vs eject gaps."""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # Stock-in totals by payment method
            cur.execute("""
                SELECT payment_method as account, ROUND(SUM(quantity * unit_cost)) as stock_total, COUNT(*) as item_count
                FROM stock_in
                WHERE payment_method IS NOT NULL AND payment_method != ''
                GROUP BY payment_method
            """)
            stock_rows = {r["account"]: r for r in cur.fetchall()}

            # Eject totals for Stock In notes
            cur.execute("""
                SELECT account, ROUND(SUM(amount)) as eject_total, COUNT(*) as eject_count
                FROM cash_movements
                WHERE movement_type = 'eject' AND note LIKE 'Stock In:%%'
                GROUP BY account
            """)
            eject_rows = {r["account"]: r for r in cur.fetchall()}

        results = []
        all_accounts = set(list(stock_rows.keys()) + list(eject_rows.keys()))

        for acct in sorted(all_accounts):
            if account_filter and acct != account_filter:
                continue
            stock = stock_rows.get(acct, {"stock_total": 0, "item_count": 0})
            eject = eject_rows.get(acct, {"eject_total": 0, "eject_count": 0})
            gap = stock["stock_total"] - eject["eject_total"]
            results.append({
                "account": acct,
                "stock_total": int(stock["stock_total"]),
                "eject_total": int(eject["eject_total"]),
                "stock_items": int(stock["item_count"]),
                "eject_entries": int(eject["eject_count"]),
                "gap": int(gap),
                "status": "BALANCED" if gap == 0 else "GAP",
            })

        return results
    finally:
        conn.close()


def print_report(results, json_out=False):
    total_gap = sum(r["gap"] for r in results)

    if json_out:
        print(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_gap": total_gap,
            "accounts": results,
        }, indent=2, ensure_ascii=False))
        return

    print("═══ PS VIBE Stock Eject Gap Audit ═══")
    print(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()
    print(f"{'Account':<18} {'Stock-In':>10} {'Eject':>10} {'Gap':>10} {'Stock#':>6} {'Eject#':>6}  Status")
    print("-" * 90)
    for r in results:
        status_icon = "✅" if r["gap"] == 0 else "⚠️ "
        print(f"{r['account']:<18} {r['stock_total']:>10,} {r['eject_total']:>10,} {r['gap']:>10,} {r['stock_items']:>6} {r['eject_entries']:>6}  {status_icon} {r['status']}")
    print("-" * 90)
    print(f"{'TOTAL':<18} {'':>10} {'':>10} {total_gap:>10,}")
    print()

    if total_gap == 0:
        print("✅ All accounts balanced — no stock eject gaps.")
    elif total_gap < 1000:
        print(f"⚠️  Minor gap: {total_gap:,} Ks (likely rounding)")
    elif total_gap < 50000:
        print(f"⚠️  Moderate gap: {total_gap:,} Ks — review recommended")
    else:
        print(f"🚨 LARGE GAP: {total_gap:,} Ks — immediate attention required!")
        print("   Run: python3 backfill_stock_ejects.py --dry-run")
        print("   Then: python3 backfill_stock_ejects.py --execute")


def main():
    parser = argparse.ArgumentParser(description="PS VIBE Stock Eject Gap Audit")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--account", type=str, default=None, help="Single account to check")
    parser.add_argument("--alert", action="store_true", help="Exit 1 if gaps > 0 (for monitoring)")
    args = parser.parse_args()

    try:
        results = check_gaps(args.account)
    except Exception as e:
        print(f"❌ Database error: {e}", file=sys.stderr)
        sys.exit(2)

    print_report(results, json_out=args.json)

    if args.alert:
        total_gap = sum(r["gap"] for r in results)
        if total_gap != 0:
            sys.exit(1)
        else:
            sys.exit(0)


if __name__ == "__main__":
    main()
