#!/usr/bin/env python3
"""PS VIBE Daily Summary Report."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from mysql_db import query, execute
from datetime import date

today = date.today()

# Today's bookings
bks = query("""
    SELECT status, COUNT(*) as cnt, COALESCE(SUM(duration_mins),0) as total_mins
    FROM console_booking WHERE booking_date = %s
    GROUP BY status
""", (today,))

# Sales
sales = query("""
    SELECT COALESCE(SUM(net),0) as total_sales,
           COUNT(*) as transactions
    FROM sales_daily WHERE DATE(sale_date) = %s
""", (today,))

# Active/occupied
consoles = query("SELECT status, COUNT(*) as cnt FROM console_status WHERE status != 'Free' GROUP BY status")

lines = [f"📊 <b>PS VIBE Daily Summary — {today}</b>", "━━━━━━━━━━━━━━━━━━"]

# Bookings
lines.append("\n📅 <b>Bookings:</b>")
for b in (bks or []):
    lines.append(f"  {b['status']}: {b['cnt']} | {b['total_mins']}min")

# Sales  
if sales:
    s = sales[0]
    lines.append(f"\n💰 <b>Sales:</b> {s['total_sales']:,} Ks ({s['transactions']} txns)")

# Consoles
if consoles:
    lines.append(f"\n🕹️ <b>Consoles Active:</b>")
    for c in consoles:
        lines.append(f"  {c['status']}: {c['cnt']}")

print("\n".join(lines))
