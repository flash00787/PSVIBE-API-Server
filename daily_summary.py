#!/usr/bin/env python3
"""PS VIBE Daily Summary Report."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Load env vars from secrets file (needed for cron/non-systemd runs)
_secrets_file = '/etc/psvibe/secrets.env'
if os.path.exists(_secrets_file):
    with open(_secrets_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()
from mysql_db import query, execute
from datetime import date

today = date.today()

# Today's bookings
bks = query("""
    SELECT status, COUNT(*) as cnt, COALESCE(SUM(planned_duration_mins),0) as total_mins
    FROM console_bookings WHERE booking_date = %s
    GROUP BY status
""", (today,))

# Sales
sales = query("""
    SELECT COALESCE(SUM(CASE WHEN notes NOT LIKE 'Topup%%' AND notes NOT LIKE 'New member%%' THEN net ELSE 0 END),0) as total_sales,
           COALESCE(SUM(CASE WHEN notes LIKE 'Topup%%' OR notes LIKE 'New member%%' THEN net ELSE 0 END),0) as topup_sales,
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
    lines.append(f"\n💰 <b>Game Sales:</b> {s['total_sales']:,} Ks ({s['transactions']} txns)")
    lines.append(f"💳 <b>Top-up:</b> {s['topup_sales']:,} Ks")
    lines.append(f"📈 <b>Total Collected:</b> {s['total_sales'] + s['topup_sales']:,} Ks")

# Consoles
if consoles:
    lines.append(f"\n🕹️ <b>Consoles Active:</b>")
    for c in consoles:
        lines.append(f"  {c['status']}: {c['cnt']}")

print("\n".join(lines))
