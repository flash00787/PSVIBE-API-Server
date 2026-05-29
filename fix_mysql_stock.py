#!/usr/bin/env python3
"""
Patch app.py to add MySQL-first, gspread-fallback for:
  1. GET /api/sheets/config    → MySQL: SELECT * FROM settings
  2. GET /api/sheets/inventory → MySQL: SELECT * FROM inventory
  3. GET /api/sheets/stock-today → MySQL: SELECT * FROM stock_out
  4. GET /api/fetch_games      → MySQL: SELECT * FROM games_library
"""

import re
import sys
import os
from datetime import datetime

APP_PATH = "/root/psvibe_api_server/app.py"
BACKUP_SUFFIX = datetime.now().strftime("-%Y%m%d_%H%M%S.bak")

def read_file(path):
    with open(path, 'r') as f:
        return f.read()

def write_file(path, content):
    with open(path, 'w') as f:
        f.write(content)

# 1. Create backup
content = read_file(APP_PATH)
backup_path = APP_PATH + BACKUP_SUFFIX
write_file(backup_path, content)
print(f"Backup created: {backup_path}")

# 2. Add MySQL import after config import
mysql_import = """
from mysql_db import query as mysql_query, query_one as mysql_query_one

# ── MySQL availability check ──
def _mysql_ok():
    try:
        from mysql_db import get_db as _mysql_get_db
        conn = _mysql_get_db()
        if conn and conn.open:
            return True
    except Exception:
        pass
    return False

def _mysql_fetch_config():
    '''Read settings table and build config dict. Returns None if unavailable/empty.'''
    if not _mysql_ok():
        return None
    try:
        rows = mysql_query("SELECT setting_key, setting_value FROM settings")
        if not rows:
            return None
        kv = {}
        for r in rows:
            kv[r['setting_key']] = r.get('setting_value', '')
        
        base_rate = int(kv.get('base_rate', 0) or 0)
        master_thresh = int(kv.get('master_threshold', 0) or 0)
        immortal_thresh = int(kv.get('immortal_threshold', 0) or 0)
        card_price = int(kv.get('new_member_card_price', 0) or 0)
        base_mins = int(kv.get('new_member_base_mins', 0) or 0)
        
        return {
            'base_rate': base_rate,
            'master_threshold': master_thresh,
            'immortal_threshold': immortal_thresh,
            'new_member_card_price': card_price,
            'new_member_base_mins': base_mins,
            'console_multipliers': {},
            'food_prices': {},
            'food_costs': {},
            'bonus_table': [],
        }
    except Exception as e:
        print(f"MySQL config read error: {e}", file=sys.stderr)
        return None

def _mysql_inventory():
    '''Read inventory table. Returns None if unavailable.'''
    if not _mysql_ok():
        return None
    try:
        rows = mysql_query("SELECT item_name, category, quantity, unit_price, reorder_level FROM inventory")
        if not rows:
            return None
        items = []
        total_cost = 0
        categories = {}
        for r in rows:
            name = (r.get('item_name') or '').strip()
            cat = (r.get('category') or 'Uncategorized').strip()
            qty = int(r.get('quantity', 0) or 0)
            cost = float(r.get('unit_price', 0) or 0)
            unit_price = cost  # unit_price column maps to both cost and price
            item_cost = cost * qty
            total_cost += item_cost
            categories[cat] = categories.get(cat, 0) + item_cost
            items.append({
                'name': name, 'category': cat, 'qty': qty,
                'cost': cost, 'price': unit_price, 'total': item_cost,
            })
        return {'items': items, 'categories': categories, 'total_cost': total_cost}
    except Exception as e:
        print(f"MySQL inventory read error: {e}", file=sys.stderr)
        return None

def _mysql_stock_out_today(today_str):
    '''Read stock_out for today. Returns None if unavailable.'''
    if not _mysql_ok():
        return None
    try:
        rows = mysql_query(
            "SELECT item_name, quantity, unit_price, total, sale_date, staff_name, notes "
            "FROM stock_out WHERE DATE(sale_date) = CURDATE()"
        )
        if not rows:
            return {'stock_out': [], 'out_total': 0}
        items = []
        out_total = 0
        for r in rows:
            item = (r.get('item_name') or '').strip()
            qty = int(r.get('quantity', 0) or 0)
            cost = float(r.get('unit_price', 0) or 0)
            total = float(r.get('total', 0) or 0)
            items.append({'item': item, 'qty': qty, 'cost': cost})
            out_total += total
        return {'stock_out': items, 'out_total': out_total}
    except Exception as e:
        print(f"MySQL stock_out read error: {e}", file=sys.stderr)
        return None

def _mysql_games():
    '''Read games_library table. Returns None if unavailable.'''
    if not _mysql_ok():
        return None
    try:
        rows = mysql_query("SELECT game_title, genre, disc_count FROM games_library ORDER BY game_title")
        if not rows:
            return []
        games = []
        for i, r in enumerate(rows, start=1):
            games.append({
                'row': i + 1,
                'title': (r.get('game_title') or '').strip(),
                'platform': '',
                'genre': (r.get('genre') or '').strip(),
                'status': 'Installed',
                'discs': str(r.get('disc_count', 0) or 0),
            })
        return games
    except Exception as e:
        print(f"MySQL games read error: {e}", file=sys.stderr)
        return None
"""

# Insert after the last initial import block (after config import lines and before logging setup)
import_marker = "from config import ("
# Find where the imports end and logging starts
lines = content.split('\n')
insert_idx = None
for i, line in enumerate(lines):
    if 'logging.basicConfig(' in line and i > 20:
        insert_idx = i
        break

if insert_idx is None:
    print("ERROR: Could not find insertion point for MySQL imports")
    sys.exit(1)

# Insert MySQL import block before logging.basicConfig
new_lines = lines[:insert_idx] + mysql_import.strip().split('\n') + lines[insert_idx:]
content = '\n'.join(new_lines)
print("MySQL imports inserted successfully")

# ============================================================
# 3. Patch /api/sheets/config (formerly line ~1196, shifted)
# ============================================================

old_config_func = '''@app.get("/api/sheets/config", tags=["Meta"])
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

new_config_func = '''@app.get("/api/sheets/config", tags=["Meta"])
async def api_sheets_config(auth=Depends(verify_api_key)):
    """Return cached config used by the bot (MySQL first, gspread fallback)."""
    # ── Try MySQL ──
    try:
        mysql_cfg = _mysql_fetch_config()
        if mysql_cfg is not None and mysql_cfg.get('base_rate'):
            # MySQL returned data — enhance with computed fields from sheets if needed
            # Add console_multipliers, food_prices, food_costs, bonus_table from sheets
            try:
                ws = get_worksheet(SHEET_SETTING)
                names = ws.col_values(8)[1:]
                mults = ws.col_values(10)[1:]
                for name, mult in zip(names, mults):
                    if name.strip():
                        try:
                            mysql_cfg['console_multipliers'][name.strip()] = float(float_safe(mult)) or 1.0
                        except ValueError:
                            mysql_cfg['console_multipliers'][name.strip()] = 1.0
                
                food_names = ws.col_values(4)[1:]
                food_prices_raw = ws.col_values(5)[1:]
                food_costs_raw = ws.col_values(6)[1:]
                for n, p, c in zip(food_names, food_prices_raw, food_costs_raw):
                    if n.strip():
                        mysql_cfg['food_prices'][n.strip()] = int_safe(p)
                        mysql_cfg['food_costs'][n.strip()] = int_safe(c) if str(c).strip() else 0
                
                bonus_rows = ws.get("O2:R5")
                for row in bonus_rows:
                    if len(row) >= 4:
                        try:
                            mysql_cfg['bonus_table'].append([int_safe(row[0]), int_safe(row[1]),
                                                            int_safe(row[2]), int_safe(row[3])])
                        except Exception:
                            continue
            except Exception as e:
                logger.warning("MySQL config: sheets supplement failed: %s", e)
            return ok(mysql_cfg)
    except Exception as e:
        logger.info("MySQL config unavailable, falling back to Google Sheets: %s", e)
    
    # ── Fallback: Google Sheets ──
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

if old_config_func not in content:
    print("ERROR: Could not find old config function")
    sys.exit(1)

content = content.replace(old_config_func, new_config_func)
print("✓ Patched /api/sheets/config")

# ============================================================
# 4. Patch /api/sheets/inventory
# ============================================================

old_inventory_func = '''@app.get("/api/sheets/inventory", tags=["Sheets"])
async def api_sheets_inventory(auth=Depends(verify_api_key)):
    """Return inventory data from Inventory sheet."""
    try:
        ws = get_worksheet("Inventory")
        rows = ws.get_all_values()
        if len(rows) < 2:
            return ok({"items": [], "categories": {}, "total_cost": 0})
        items = []
        total_cost = 0
        categories = {}
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            name = row[0].strip() if len(row) > 0 else ""
            cat = row[1].strip() if len(row) > 1 else "Uncategorized"
            qty = int_safe(row[2]) if len(row) > 2 else 0
            cost = int_safe(row[3]) if len(row) > 3 else 0
            price = int_safe(row[4]) if len(row) > 4 else 0
            item_cost = cost * qty
            total_cost += item_cost
            if cat not in categories:
                categories[cat] = 0
            categories[cat] += item_cost
            items.append({
                "name": name, "category": cat, "qty": qty,
                "cost": cost, "price": price, "total": item_cost,
            })
        return ok({"items": items, "categories": categories, "total_cost": total_cost})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

new_inventory_func = '''@app.get("/api/sheets/inventory", tags=["Sheets"])
async def api_sheets_inventory(auth=Depends(verify_api_key)):
    """Return inventory data (MySQL first, gspread fallback)."""
    # ── Try MySQL ──
    try:
        mysql_data = _mysql_inventory()
        if mysql_data is not None and mysql_data.get('items') is not None:
            return ok(mysql_data)
    except Exception as e:
        logger.info("MySQL inventory unavailable, falling back to Google Sheets: %s", e)
    
    # ── Fallback: Google Sheets ──
    try:
        ws = get_worksheet("Inventory")
        rows = ws.get_all_values()
        if len(rows) < 2:
            return ok({"items": [], "categories": {}, "total_cost": 0})
        items = []
        total_cost = 0
        categories = {}
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            name = row[0].strip() if len(row) > 0 else ""
            cat = row[1].strip() if len(row) > 1 else "Uncategorized"
            qty = int_safe(row[2]) if len(row) > 2 else 0
            cost = int_safe(row[3]) if len(row) > 3 else 0
            price = int_safe(row[4]) if len(row) > 4 else 0
            item_cost = cost * qty
            total_cost += item_cost
            if cat not in categories:
                categories[cat] = 0
            categories[cat] += item_cost
            items.append({
                "name": name, "category": cat, "qty": qty,
                "cost": cost, "price": price, "total": item_cost,
            })
        return ok({"items": items, "categories": categories, "total_cost": total_cost})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if old_inventory_func not in content:
    print("ERROR: Could not find old inventory function")
    sys.exit(1)

content = content.replace(old_inventory_func, new_inventory_func)
print("✓ Patched /api/sheets/inventory")

# ============================================================
# 5. Patch /api/sheets/stock-today
# ============================================================

old_stock_today = '''@app.get("/api/sheets/stock-today", tags=["Sheets"])
async def api_sheets_stock_today(auth=Depends(verify_api_key)):
    """Return today's stock movement summary from Stock_In / Stock_Out."""
    try:
        today = today_str()
        result = {"date": today, "stock_in": [], "stock_out": [], "in_total": 0, "out_total": 0}

        # Stock In
        try:
            si = get_worksheet(SHEET_STOCK_IN)
            si_rows = si.get_all_values()
            for row in si_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if row[0] else ""
                if d != today:
                    continue
                item = row[1].strip() if len(row) > 1 else ""
                qty = int_safe(row[2]) if len(row) > 2 else 0
                cost = int_safe(row[3]) if len(row) > 3 else 0
                result["stock_in"].append({"item": item, "qty": qty, "cost": cost})
                result["in_total"] += cost * qty
        except Exception as e:
            logger.warning("Stock_In read error: %s", e)

        # Stock Out
        try:
            so = get_worksheet(SHEET_STOCK_OUT)
            so_rows = so.get_all_values()
            for row in so_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if row[0] else ""
                if d != today:
                    continue
                item = row[1].strip() if len(row) > 1 else ""
                qty = int_safe(row[2]) if len(row) > 2 else 0
                cost = int_safe(row[3]) if len(row) > 3 else 0
                result["stock_out"].append({"item": item, "qty": qty, "cost": cost})
                result["out_total"] += cost * qty
        except Exception as e:
            logger.warning("Stock_Out read error: %s", e)

        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

new_stock_today = '''@app.get("/api/sheets/stock-today", tags=["Sheets"])
async def api_sheets_stock_today(auth=Depends(verify_api_key)):
    """Return today's stock movement summary (MySQL first, gspread fallback)."""
    try:
        today = today_str()
        result = {"date": today, "stock_in": [], "stock_out": [], "in_total": 0, "out_total": 0}

        # ── Stock Out: Try MySQL ──
        stock_out_from_mysql = False
        try:
            mysql_so = _mysql_stock_out_today(today)
            if mysql_so is not None:
                result["stock_out"] = mysql_so.get("stock_out", [])
                result["out_total"] = mysql_so.get("out_total", 0)
                stock_out_from_mysql = True
        except Exception as e:
            logger.info("MySQL stock_out unavailable, falling back to Google Sheets: %s", e)
        
        if not stock_out_from_mysql:
            try:
                so = get_worksheet(SHEET_STOCK_OUT)
                so_rows = so.get_all_values()
                for row in so_rows[1:]:
                    if len(row) < 4:
                        continue
                    d = row[0].strip() if row[0] else ""
                    if d != today:
                        continue
                    item = row[1].strip() if len(row) > 1 else ""
                    qty = int_safe(row[2]) if len(row) > 2 else 0
                    cost = int_safe(row[3]) if len(row) > 3 else 0
                    result["stock_out"].append({"item": item, "qty": qty, "cost": cost})
                    result["out_total"] += cost * qty
            except Exception as e:
                logger.warning("Stock_Out read error: %s", e)

        # ── Stock In: Google Sheets only (no MySQL stock_in table) ──
        try:
            si = get_worksheet(SHEET_STOCK_IN)
            si_rows = si.get_all_values()
            for row in si_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if row[0] else ""
                if d != today:
                    continue
                item = row[1].strip() if len(row) > 1 else ""
                qty = int_safe(row[2]) if len(row) > 2 else 0
                cost = int_safe(row[3]) if len(row) > 3 else 0
                result["stock_in"].append({"item": item, "qty": qty, "cost": cost})
                result["in_total"] += cost * qty
        except Exception as e:
            logger.warning("Stock_In read error: %s", e)

        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if old_stock_today not in content:
    print("ERROR: Could not find old stock-today function")
    sys.exit(1)

content = content.replace(old_stock_today, new_stock_today)
print("✓ Patched /api/sheets/stock-today")

# ============================================================
# 6. Patch /api/fetch_games
# ============================================================

old_games_func = '''@app.get("/api/fetch_games", tags=["Games"])
async def api_fetch_games(auth=Depends(verify_api_key)):
    """Return all games from Game_Library sheet (cached 10 min)."""
    try:
        rows = get_game_rows()
        if len(rows) < 2:
            return ok([])
        games = []
        for i, row in enumerate(rows[1:], start=2):
            if not row or not row[1].strip():
                continue
            games.append({
                "row": i,
                "title": row[1].strip() if len(row) > 1 else "",
                "platform": row[2].strip() if len(row) > 2 else "",
                "genre": row[3].strip() if len(row) > 3 else "",
                "status": row[4].strip() if len(row) > 4 else "",
                "discs": row[5].strip() if len(row) > 5 else "",
            })
        return ok(games)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

new_games_func = '''@app.get("/api/fetch_games", tags=["Games"])
async def api_fetch_games(auth=Depends(verify_api_key)):
    """Return all games (MySQL first, gspread fallback)."""
    # ── Try MySQL ──
    try:
        mysql_games = _mysql_games()
        if mysql_games is not None:
            return ok(mysql_games)
    except Exception as e:
        logger.info("MySQL games unavailable, falling back to Google Sheets: %s", e)
    
    # ── Fallback: Google Sheets ──
    try:
        rows = get_game_rows()
        if len(rows) < 2:
            return ok([])
        games = []
        for i, row in enumerate(rows[1:], start=2):
            if not row or not row[1].strip():
                continue
            games.append({
                "row": i,
                "title": row[1].strip() if len(row) > 1 else "",
                "platform": row[2].strip() if len(row) > 2 else "",
                "genre": row[3].strip() if len(row) > 3 else "",
                "status": row[4].strip() if len(row) > 4 else "",
                "discs": row[5].strip() if len(row) > 5 else "",
            })
        return ok(games)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if old_games_func not in content:
    print("ERROR: Could not find old fetch_games function")
    sys.exit(1)

content = content.replace(old_games_func, new_games_func)
print("✓ Patched /api/fetch_games")

# ============================================================
# 7. Write back
# ============================================================

write_file(APP_PATH, content)
print(f"✓ Written modified app.py ({len(content)} chars)")
print("DONE — All 4 endpoints patched with MySQL-first, gspread-fallback.")
