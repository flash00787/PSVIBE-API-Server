import json, os, sys

# Read console data from GSheet Setting tab
import gspread
gc = gspread.service_account('/root/psvibe-sales-bot/service_account.json')
sh = gc.open_by_key('1VFNvhdcYVlVrr5TS49n2peIZa3U6y_AI-Mfo7q7gVsA')
setting = sh.worksheet('Setting')

names = setting.col_values(8)[1:]   # H:H console names
types = setting.col_values(9)[1:]   # I:I types
mults = setting.col_values(10)[1:]  # J:J multipliers

# Build console_multipliers dict
console_multipliers = {}
for i, n in enumerate(names):
    if n.strip():
        try:
            m = float(str(mults[i] if i < len(mults) else '1').replace(',', '').strip()) or 1.0
        except (ValueError, IndexError):
            m = 1.0
        console_multipliers[n.strip()] = m

print(f"Read {len(console_multipliers)} consoles from GSheet:")
for k, v in console_multipliers.items():
    print(f"  {k}: {v}")

# Now write to MySQL settings_config
from config import DB_CONFIG
import mysql.connector
db = mysql.connector.connect(**DB_CONFIG)
cur = db.cursor()

# Upsert console_multipliers into settings_config
cur.execute("SELECT id FROM settings_config WHERE setting_key = 'console_multipliers'")
existing = cur.fetchone()

if existing:
    cur.execute("UPDATE settings_config SET setting_value = %s, updated_at = NOW() WHERE setting_key = 'console_multipliers'",
                (json.dumps(console_multipliers),))
    print("Updated console_multipliers in settings_config")
else:
    cur.execute("INSERT INTO settings_config (setting_key, setting_value, updated_at) VALUES ('console_multipliers', %s, NOW())",
                (json.dumps(console_multipliers),))
    print("Inserted console_multipliers into settings_config")

db.commit()
cur.close()
db.close()
print("Done!")
