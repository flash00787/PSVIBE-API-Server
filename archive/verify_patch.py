import re
with open("/root/psvibe_api_server/app.py") as f:
    lines = f.readlines()
for i, l in enumerate(lines, 1):
    if any(k in l for k in ["_mysql_ok", "Try MySQL", "Fallback", "_mysql_fetch_config", "_mysql_inventory", "_mysql_stock_out", "_mysql_games"]):
        print(f"{i}: {l.rstrip()[:120]}")
