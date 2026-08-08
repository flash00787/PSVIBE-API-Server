"""PS VIBE API — reconcile_consoles module."""
#!/usr/bin/env python3
"""Daily console_status ↔ console_sessions reconciliation."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from mysql_db import execute, query

fixed = 0
# Fix: consoles marked Active but no active booking
r = query("""
    SELECT cs.console_id FROM console_status cs
    WHERE cs.status IN ('Active','Occupied')
    AND NOT EXISTS (
        SELECT 1 FROM console_sessions cb 
        WHERE cb.console_id = cs.console_id AND cb.status = 'Active'
    )
""")
for row in (r or []):
    execute("UPDATE console_status SET status='Free', current_member=NULL, current_game=NULL, start_time=NULL WHERE console_id=%s", (row['console_id'],))
    print(f"  🔧 {row['console_id']}: stale Active → Free")
    fixed += 1

# Fix: consoles marked Free but have Active booking (shouldn't happen, but heals)
r2 = query("""
    SELECT DISTINCT cb.console_id FROM console_sessions cb
    WHERE cb.status = 'Active'
    AND EXISTS (
        SELECT 1 FROM console_status cs
        WHERE cs.console_id = cb.console_id AND cs.status = 'Free'
    )
""")
for row in (r2 or []):
    bk = query("SELECT staff_name, customer_name, member_id, game_name FROM console_sessions WHERE console_id=%s AND status='Active' LIMIT 1", (row['console_id'],))
    if bk:
        b = bk[0]
        name = b.get('customer_name') or b.get('staff_name') or b.get('member_id') or ''
        game = b.get('game_name') or ''
        execute("UPDATE console_status SET status='Active', current_member=%s, current_game=%s, start_time=NOW() WHERE console_id=%s", (name, game, row['console_id']))
        print(f"  🔧 {row['console_id']}: Free → Active ({name})")
        fixed += 1

print(f"Reconciliation done: {fixed} fix(es)")
