
#!/usr/bin/env python3
"""One-time sync: populate MySQL settings table from Google Sheets."""
import sys, os, json
sys.path.insert(0, '/root/psvibe_api_server')
from config import SERVICE_ACCOUNT_FILE, SHEETS_SCOPES, SHEET_ID, SHEET_SETTING
import gspread
import pymysql

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "psvibe_user",
    "password": "PsVibe@2026_Rotated!",
    "database": "psvibe_api",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor
}

def int_safe(val):
    if val is None: return 0
    try:
        s = str(val).replace(",", "").strip()
        return int(float(s)) if s else 0
    except: return 0

def float_safe(val):
    if val is None: return 0.0
    try:
        s = str(val).replace(",", "").strip()
        return float(s) if s else 0.0
    except: return 0.0

print("Connecting to Google Sheets...")
gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES)
wb = gc.open_by_key(SHEET_ID)
ws = wb.worksheet(SHEET_SETTING)
rows = ws.get_all_values()
print(f"Got {len(rows)} rows from Setting sheet")

# Parse values
base_rate = int_safe(rows[1][1]) if len(rows) > 1 and len(rows[1]) > 1 else 0
master_thresh = int_safe(rows[2][12]) if len(rows) > 2 and len(rows[2]) > 12 else 0
immortal_thresh = int_safe(rows[3][12]) if len(rows) > 3 and len(rows[3]) > 12 else 0
card_price = int_safe(rows[19][1]) if len(rows) > 19 and len(rows[19]) > 1 else 0
base_mins = int_safe(rows[20][1]) if len(rows) > 20 and len(rows[20]) > 1 else 0

# Console multipliers
console_multipliers = {}
for row in rows[1:]:
    if len(row) > 7 and row[7].strip():
        name = row[7].strip()
        mult_raw = row[9].strip() if len(row) > 9 else ""
        try:
            console_multipliers[name] = float(float_safe(mult_raw)) or 1.0
        except: console_multipliers[name] = 1.0

# Food prices/costs
food_prices = {}
food_costs = {}
for row in rows[1:]:
    if len(row) > 3 and row[3].strip():
        name = row[3].strip()
        food_prices[name] = int_safe(row[4]) if len(row) > 4 else 0
        food_costs[name] = int_safe(row[5]) if len(row) > 5 else 0

# Bonus table
bonus_table = []
for r_idx in range(1, min(5, len(rows))):
    row = rows[r_idx]
    if len(row) >= 18:
        try:
            bonus_table.append([int_safe(row[14]), int_safe(row[15]), int_safe(row[16]), int_safe(row[17])])
        except: continue

print(f"base_rate={base_rate}, master_thresh={master_thresh}, immortal_thresh={immortal_thresh}")
print(f"consoles: {len(console_multipliers)}, foods: {len(food_prices)}, bonus rows: {len(bonus_table)}")

# Connect to MySQL
conn = pymysql.connect(**DB_CONFIG)
try:
    with conn.cursor() as cur:
        # Upsert settings
        settings_data = {
            'base_rate': str(base_rate),
            'master_threshold': str(master_thresh),
            'immortal_threshold': str(immortal_thresh),
            'new_member_card_price': str(card_price),
            'new_member_base_mins': str(base_mins),
            'console_multipliers': json.dumps(console_multipliers),
            'food_prices': json.dumps(food_prices),
            'food_costs': json.dumps(food_costs),
            'bonus_table': json.dumps(bonus_table),
        }
        for key, value in settings_data.items():
            cur.execute(
                "INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE setting_value = %s",
                (key, value, value)
            )
        conn.commit()
        print(f"Inserted/updated {len(settings_data)} settings rows")
        
        # Verify
        cur.execute("SELECT COUNT(*) as cnt FROM settings")
        row = cur.fetchone()
        print(f"Total settings rows: {row['cnt']}")
finally:
    conn.close()

print("DONE: MySQL settings table populated")

