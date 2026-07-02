
import re

with open('/root/psvibe_api_server/app.py', 'r') as f:
    content = f.read()

# Define the old endpoint code
old_endpoint = '''@app.get("/api/sheets/config", tags=["Meta"])
async def api_sheets_config(auth=Depends(verify_api_key)):
    """Return cached config used by the bot (base_rate, thresholds, etc.)."""
    try:
        ws = get_worksheet(SHEET_SETTING)
        base_rate = int_safe(ws.cell(2, 2).value)
        master_thresh = int_safe(ws.cell(3, 13).value)
        immortal_thresh = int_safe(ws.cell(4, 13).value)
        card_price = int_safe(ws.cell(20, 2).value)
        base_mins = int_safe(ws.cell(21, 2).value)

        names = ws.col_values(8)[1:]
        mults = ws.col_values(10)[1:]
        console_multipliers = {}
        for name, mult in zip(names, mults):
            if name.strip():
                try:
                    console_multipliers[name.strip()] = float(float_safe(mult)) or 1.0
                except ValueError:
                    console_multipliers[name.strip()] = 1.0

        food_names = ws.col_values(4)[1:]
        food_prices_raw = ws.col_values(5)[1:]
        food_costs_raw = ws.col_values(6)[1:]
        food_prices = {}
        food_costs = {}
        for n, p, c in zip(food_names, food_prices_raw, food_costs_raw):
            if n.strip():
                food_prices[n.strip()] = int_safe(p)
                food_costs[n.strip()] = int_safe(c) if str(c).strip() else 0

        bonus_rows = ws.get("O2:R5")
        bonus_table = []
        for row in bonus_rows:
            if len(row) >= 4:
                try:
                    bonus_table.append([int_safe(row[0]), int_safe(row[1]),
                                        int_safe(row[2]), int_safe(row[3])])
                except Exception:
                    continue

        return ok({
            "base_rate": base_rate,
            "master_threshold": master_thresh,
            "immortal_threshold": immortal_thresh,
            "new_member_card_price": card_price,
            "new_member_base_mins": base_mins,
            "console_multipliers": console_multipliers,
            "food_prices": food_prices,
            "food_costs": food_costs,
            "bonus_table": bonus_table,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

# Define the new endpoint code
new_endpoint = '''@app.get("/api/sheets/config", tags=["Meta"])
async def api_sheets_config(auth=Depends(verify_api_key)):
    """Return cached config for bot (MySQL primary → cached Sheets → live Sheets).
    NO individual Sheets API calls per request — uses cached all-values."""
    # ── Try MySQL first ──
    mysql_config = _fetch_config_from_mysql()
    if mysql_config is not None:
        return ok(mysql_config)

    # ── Fallback: CACHED Setting sheet (single API call, TTL configurable) ──
    try:
        rows = get_setting_rows()
        if not rows or len(rows) < 22:
            raise ValueError("Setting sheet too small")

        # Parse from cached rows (0-indexed)
        base_rate = int_safe(rows[1][1]) if len(rows[1]) > 1 else 0
        master_thresh = int_safe(rows[2][12]) if len(rows[2]) > 12 else 0
        immortal_thresh = int_safe(rows[3][12]) if len(rows[3]) > 12 else 0
        card_price = int_safe(rows[19][1]) if len(rows[19]) > 1 else 0
        base_mins = int_safe(rows[20][1]) if len(rows[20]) > 1 else 0

        # Console multipliers from col H (index 7) and col J (index 9)
        console_multipliers = {}
        for row in rows[1:]:
            if len(row) > 7 and row[7].strip():
                name = row[7].strip()
                mult_raw = row[9].strip() if len(row) > 9 else ""
                try:
                    console_multipliers[name] = float(float_safe(mult_raw)) or 1.0
                except ValueError:
                    console_multipliers[name] = 1.0

        # Food prices/costs from col D (index 3), col E (index 4), col F (index 5)
        food_prices = {}
        food_costs = {}
        for row in rows[1:]:
            if len(row) > 3 and row[3].strip():
                name = row[3].strip()
                price_raw = row[4] if len(row) > 4 else ""
                cost_raw = row[5] if len(row) > 5 else ""
                food_prices[name] = int_safe(price_raw)
                food_costs[name] = int_safe(cost_raw) if str(cost_raw).strip() else 0

        # Bonus table O2:R5 → rows[1..4], cols[14..17]
        bonus_table = []
        for r_idx in range(1, min(5, len(rows))):
            row = rows[r_idx]
            if len(row) >= 18:
                try:
                    bonus_table.append([
                        int_safe(row[14]), int_safe(row[15]),
                        int_safe(row[16]), int_safe(row[17])
                    ])
                except Exception:
                    continue

        return ok({
            "base_rate": base_rate,
            "master_threshold": master_thresh,
            "immortal_threshold": immortal_thresh,
            "new_member_card_price": card_price,
            "new_member_base_mins": base_mins,
            "console_multipliers": console_multipliers,
            "food_prices": food_prices,
            "food_costs": food_costs,
            "bonus_table": bonus_table,
            "source": "sheets_cache",
        })
    except Exception:
        pass

    # ── Last resort: live Sheets API (individual calls — expensive!) ──
    try:
        ws = get_worksheet(SHEET_SETTING)
        base_rate = int_safe(ws.cell(2, 2).value)
        master_thresh = int_safe(ws.cell(3, 13).value)
        immortal_thresh = int_safe(ws.cell(4, 13).value)
        card_price = int_safe(ws.cell(20, 2).value)
        base_mins = int_safe(ws.cell(21, 2).value)

        names = ws.col_values(8)[1:]
        mults = ws.col_values(10)[1:]
        console_multipliers = {}
        for name, mult in zip(names, mults):
            if name.strip():
                try:
                    console_multipliers[name.strip()] = float(float_safe(mult)) or 1.0
                except ValueError:
                    console_multipliers[name.strip()] = 1.0

        food_names = ws.col_values(4)[1:]
        food_prices_raw = ws.col_values(5)[1:]
        food_costs_raw = ws.col_values(6)[1:]
        food_prices = {}
        food_costs = {}
        for n, p, c in zip(food_names, food_prices_raw, food_costs_raw):
            if n.strip():
                food_prices[n.strip()] = int_safe(p)
                food_costs[n.strip()] = int_safe(c) if str(c).strip() else 0

        bonus_rows = ws.get("O2:R5")
        bonus_table = []
        for row in bonus_rows:
            if len(row) >= 4:
                try:
                    bonus_table.append([int_safe(row[0]), int_safe(row[1]),
                                        int_safe(row[2]), int_safe(row[3])])
                except Exception:
                    continue

        return ok({
            "base_rate": base_rate,
            "master_threshold": master_thresh,
            "immortal_threshold": immortal_thresh,
            "new_member_card_price": card_price,
            "new_member_base_mins": base_mins,
            "console_multipliers": console_multipliers,
            "food_prices": food_prices,
            "food_costs": food_costs,
            "bonus_table": bonus_table,
            "source": "sheets_live",
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if old_endpoint in content:
    content = content.replace(old_endpoint, new_endpoint)
    with open('/root/psvibe_api_server/app.py', 'w') as f:
        f.write(content)
    print('SUCCESS: Replaced api_sheets_config endpoint')
else:
    print('ERROR: Could not find old endpoint code to replace')
    # Try to find what's there
    with open('/root/psvibe_api_server/app.py', 'r') as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if 'api_sheets_config' in line:
            print(f'Found at line {i+1}: {line.strip()}')
            for j in range(max(0,i-2), min(len(lines), i+30)):
                print(f'  {j+1}: {lines[j].rstrip()}')

