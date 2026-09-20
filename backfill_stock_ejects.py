#!/usr/bin/env python3
"""
PS VIBE — Backfill Missing Stock Eject Entries
===============================================
Finds gaps between stock_in total values and cash_movements eject entries
(Stock In: notes), and creates missing eject entries to close the gap.

Account-level aggregate gap calculation:
- KBZ Bank: 150,929 Ks (June 6-7 bundled entry accounts for 728,940; rest is loose)
- ACM's Acc: 41,580 Ks
- Cash: 1,200 Ks

SAFETY:
  --dry-run  → Preview only (default)
  --execute  → Actually insert
  --account X → Only process specific account

Run: python3 backfill_stock_ejects.py --dry-run     (preview)
     python3 backfill_stock_ejects.py --execute      (apply)
"""

import pymysql
import sys
import argparse
from datetime import datetime, timezone

DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "root",
    "password": os.environ.get("MYSQL_ROOT_PASSWORD", ""),
    "database": "psvibe_api",
    "charset": "utf8mb4",
}

def get_conn():
    return pymysql.connect(**DB_CONFIG)

def calc_gaps(conn, account_filter=None):
    """Calculate gaps by account between stock_in totals and cash_movements eject totals."""
    sql = """
    SELECT s.pm as account, s.stock_total, COALESCE(e.eject_total, 0) as eject_total,
           s.stock_total - COALESCE(e.eject_total, 0) as gap
    FROM (
      SELECT payment_method as pm, ROUND(SUM(quantity * unit_cost)) as stock_total
      FROM stock_in
      WHERE payment_method IS NOT NULL AND payment_method != ''
      GROUP BY payment_method
    ) s
    LEFT JOIN (
      SELECT account, ROUND(SUM(amount)) as eject_total
      FROM cash_movements
      WHERE movement_type = 'eject' AND note LIKE 'Stock In:%%'
      GROUP BY account
    ) e ON s.pm = e.account
    HAVING gap != 0
    """
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    gaps = {}
    for r in rows:
        acct = r["account"]
        gap = int(r["gap"])
        if account_filter and acct != account_filter:
            continue
        if gap <= 0:
            continue
        gaps[acct] = {
            "account": acct,
            "stock_total": int(r["stock_total"]),
            "eject_total": int(r["eject_total"]),
            "gap": gap,
        }
    return gaps


def backfill(conn, gaps, dry_run=True):
    """Insert one eject entry per account to close the gap."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    inserted = []
    
    for acct, info in gaps.items():
        gap = info["gap"]
        note = f"Stock In: backfill missing eject — {acct} aggregate gap {gap:,} Ks"
        sql = """
        INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at)
        VALUES ('eject', %s, %s, %s, 'backfill', %s)
        """
        
        if dry_run:
            print(f"[DRY-RUN] Would insert: eject | {acct} | {gap:,} Ks | note={note}")
        else:
            with conn.cursor() as cur:
                cur.execute(sql, (acct, gap, note, now))
            inserted.append({"account": acct, "amount": gap, "note": note})
            print(f"[INSERTED] eject | {acct} | {gap:,} Ks")
    
    if inserted:
        total = sum(e["amount"] for e in inserted)
        print(f"\nTotal backfilled: {total:,} Ks across {len(inserted)} accounts")
    
    return inserted


def verify(conn):
    """Post-backfill verification."""
    sql = """
    SELECT s.pm as account, s.stock_total, COALESCE(e.eject_total, 0) as eject_total,
           s.stock_total - COALESCE(e.eject_total, 0) as gap
    FROM (
      SELECT payment_method as pm, ROUND(SUM(quantity * unit_cost)) as stock_total
      FROM stock_in
      WHERE payment_method IS NOT NULL AND payment_method != ''
      GROUP BY payment_method
    ) s
    LEFT JOIN (
      SELECT account, ROUND(SUM(amount)) as eject_total
      FROM cash_movements
      WHERE movement_type = 'eject' AND note LIKE 'Stock In:%%'
      GROUP BY account
    ) e ON s.pm = e.account
    ORDER BY gap DESC
    """
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    
    print("\n═══ Post-Verification ═══")
    all_ok = True
    for r in rows:
        gap = int(r["gap"])
        status = "✅" if gap == 0 else f"⚠️  GAP={gap:,}"
        if gap != 0:
            all_ok = False
        print(f"  {r['account']:15s}  stock={r['stock_total']:>10,}  eject={r['eject_total']:>10,}  gap={gap:>8,}  {status}")
    
    if all_ok:
        print("✅ All accounts balanced!")
    else:
        print("⚠️  Some gaps remain — manual review needed.")
    
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Backfill missing stock-in eject entries")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview mode (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually insert entries")
    parser.add_argument("--account", type=str, default=None,
                        help="Only process specific account (e.g., 'KBZ Bank', 'ACM''s Acc', 'Cash')")
    parser.add_argument("--verify-only", action="store_true",
                        help="Only verify current state, no changes")
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    conn = get_conn()
    try:
        print("═══ PS VIBE Stock Eject Backfill ═══")
        print(f"Mode: {'DRY-RUN (preview)' if dry_run else 'EXECUTE (will insert)'}")
        print(f"Account filter: {args.account or 'ALL'}")
        print()
        
        gaps = calc_gaps(conn, args.account)
        if not gaps:
            print("✅ No gaps found — all accounts balanced!")
            return
        
        print("═══ Gaps Found ═══")
        total_gap = 0
        for acct, info in gaps.items():
            print(f"  {acct:15s}  stock_in={info['stock_total']:>10,}  eject={info['eject_total']:>10,}  GAP={info['gap']:>10,}")
            total_gap += info["gap"]
        print(f"  {'TOTAL':15s}  {'':>10}  {'':>10}  GAP={total_gap:>10,}")
        print()
        
        if args.verify_only:
            return
        
        inserted = backfill(conn, gaps, dry_run=dry_run)
        
        if not dry_run and inserted:
            conn.commit()
            print("✅ Changes committed.")
            
            # Re-verify
            verify(conn)
        elif not dry_run:
            print("No entries to insert.")
            
    except Exception as e:
        conn.rollback()
        print(f"❌ ERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
