#!/usr/bin/env python3
"""
Backfill missing stock_out entries from food_cart items.
Food cart items that were served (fulfilled) but never created stock_out entries.

Run: python3 /root/psvibe_api_server/backfill_foodcart_stockout.py [--dry-run]
"""

import pymysql
import os
import sys
from datetime import datetime, timezone, timedelta

# ─────────────────────────── DB Config ───────────────────────────
DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "psvibe_user"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "psvibe_api"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

DRY_RUN = "--dry-run" in sys.argv
MMT = timezone(timedelta(hours=6, minutes=30))


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def find_missing(conn):
    """Find food_cart items without matching stock_out (time-window join)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT fc.id, fc.booking_id, fc.item_name, fc.quantity, fc.unit_price,
               fc.created_at, fc.fulfilled_at
        FROM food_cart fc
        WHERE NOT EXISTS (
            SELECT 1 FROM stock_out so
            WHERE so.item_name = fc.item_name
              AND so.quantity = fc.quantity
              AND so.sale_date >= DATE_SUB(fc.created_at, INTERVAL 1 HOUR)
              AND so.sale_date <= DATE_ADD(fc.created_at, INTERVAL 1 HOUR)
        )
        ORDER BY fc.created_at DESC
    """)
    return cur.fetchall()


def backfill(conn, items):
    """Insert stock_out + update inventory for each missing item."""
    cur = conn.cursor()
    stats = {"inserted": 0, "skipped": 0, "inventory_updated": 0, "total_amount": 0}

    for item in items:
        fc_id = item["id"]
        booking_id = item["booking_id"]
        item_name = item["item_name"]
        quantity = int(item["quantity"])
        unit_price = float(item["unit_price"])
        total = quantity * unit_price
        sale_date = item["created_at"]  # Use food_cart creation time

        stats["total_amount"] += total

        if DRY_RUN:
            print(f"  [DRY-RUN] Would insert: bk=#{booking_id} {item_name} x{quantity} @{unit_price} = {total} Ks (fc_id={fc_id})")
            stats["inserted"] += 1
            continue

        try:
            # INSERT stock_out
            cur.execute(
                """INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, staff_name, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    item_name, quantity, unit_price, total,
                    sale_date, "backfill",
                    f"Backfill for booking #{booking_id} (fc_id={fc_id})"
                ),
            )
            stock_out_id = cur.lastrowid
            stats["inserted"] += 1

            # UPDATE inventory: decrement
            cur.execute(
                """UPDATE inventory SET quantity = GREATEST(0, quantity - %s)
                   WHERE item_name = %s AND quantity IS NOT NULL""",
                (quantity, item_name),
            )
            if cur.rowcount > 0:
                stats["inventory_updated"] += 1

            print(f"  ✅ stock_out #{stock_out_id}: bk=#{booking_id} {item_name} x{quantity} @{unit_price} = {total} Ks")

        except pymysql.err.IntegrityError as ie:
            print(f"  ⚠️ Duplicate/IntegrityError for fc_id={fc_id}: {ie}")
            stats["skipped"] += 1
        except Exception as e:
            print(f"  ❌ Error on fc_id={fc_id}: {e}")
            stats["skipped"] += 1

    return stats


def main():
    print("=" * 70)
    print("  FOOD CART → STOCK OUT BACKFILL")
    print("=" * 70)
    if DRY_RUN:
        print("  ⚠️  DRY-RUN MODE — no changes will be made\n")

    conn = get_conn()
    try:
        conn.begin()
        items = find_missing(conn)
        print(f"\n  📋 Found {len(items)} food_cart items missing stock_out entries")

        if not items:
            print("  Nothing to backfill. Exiting.")
            conn.commit()
            return

        # Show summary by booking
        bk_map = {}
        for i in items:
            bk = i["booking_id"]
            bk_map[bk] = bk_map.get(bk, 0) + 1
        print(f"  📊 Spanning {len(bk_map)} unique bookings")

        # Recent vs old breakdown
        recent = [i for i in items if i["created_at"] and i["created_at"] >= datetime(2026, 7, 15)]
        old = [i for i in items if i["created_at"] and i["created_at"] < datetime(2026, 7, 15)]
        recent_amt = sum(int(i["quantity"]) * float(i["unit_price"]) for i in recent)
        old_amt = sum(int(i["quantity"]) * float(i["unit_price"]) for i in old)
        print(f"  📅 Recent (Jul 15+): {len(recent)} items = {recent_amt:,.0f} Ks")
        print(f"  📅 Older: {len(old)} items = {old_amt:,.0f} Ks")
        print()

        stats = backfill(conn, items)

        print(f"\n{'─' * 60}")
        print(f"  📊 BACKFILL SUMMARY")
        print(f"{'─' * 60}")
        print(f"  Stock-out entries inserted: {stats['inserted']}")
        print(f"  Skipped (errors/dupes):      {stats['skipped']}")
        print(f"  Inventory rows updated:      {stats['inventory_updated']}")
        print(f"  Total amount backfilled:     {stats['total_amount']:,.0f} Ks")
        print(f"{'─' * 60}")

        if not DRY_RUN:
            conn.commit()
            print("\n  ✅ Transaction COMMITTED.")
        else:
            conn.rollback()
            print("\n  🔄 DRY-RUN: rolled back all changes.")

    except Exception as e:
        conn.rollback()
        print(f"\n  ❌ FAILED: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
