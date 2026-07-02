#!/usr/bin/env python3
"""Patch app.py to add MySQL-first fallback for sales/topup/promotions/card_wallet/receipts endpoints."""

import re
import os
from datetime import datetime

APP_PATH = '/root/psvibe_api_server/app.py'
BACKUP_PATH = f'{APP_PATH}.bak-{datetime.now().strftime("%Y%m%d_%H%M%S")}_mysql_v2'

# Backup
os.system(f'cp {APP_PATH} {BACKUP_PATH}')
print(f"Backup created: {BACKUP_PATH}")

with open(APP_PATH, 'r') as f:
    content = f.read()

patches_applied = []

# ============================================================
# PATCH 1: Add mysql_db import after existing imports
# ============================================================
IMPORT_BLOCK = '''from config import (
    API_TITLE, API_VERSION, API_DESCRIPTION,
    HOST, PORT, DEBUG, API_KEY,
    SHEET_SALES_DAILY, SHEET_SETTING, SHEET_CARD_WALLET,
    SHEET_TOPUP_LOG, SHEET_ATTENDANCE_LOG, SHEET_CONSOLE_BOOKING,
    SHEET_SALARY_ADVANCE, SHEET_GAME_LIBRARY, SHEET_CONSOLE_GAMES,
    MMT_HOURS, MMT_MINUTES,
)'''

MYSQL_IMPORT = '''from config import (
    API_TITLE, API_VERSION, API_DESCRIPTION,
    HOST, PORT, DEBUG, API_KEY,
    SHEET_SALES_DAILY, SHEET_SETTING, SHEET_CARD_WALLET,
    SHEET_TOPUP_LOG, SHEET_ATTENDANCE_LOG, SHEET_CONSOLE_BOOKING,
    SHEET_SALARY_ADVANCE, SHEET_GAME_LIBRARY, SHEET_CONSOLE_GAMES,
    MMT_HOURS, MMT_MINUTES,
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE,
)

# MySQL import for faster queries (fallback-friendly)
try:
    import mysql_db as _mysql_db
    _MYSQL_AVAILABLE = True
    logger.info("MySQL module loaded via mysql_db")
except Exception as _e:
    _MYSQL_AVAILABLE = False
    logger.warning("MySQL not available: %s", _e)'''

if IMPORT_BLOCK in content:
    content = content.replace(IMPORT_BLOCK, MYSQL_IMPORT)
    patches_applied.append("1: mysql_db import + MYSQL config vars")
else:
    print("WARNING: Import block not found!")

# ============================================================
# PATCH 2: Add mysql_fallback helper function after now_mmt/today_str
# ============================================================
HELPER_FUNCTIONS = '''def today_str():
    return now_mmt().strftime("%-m/%-d/%Y")


def _norm_cid(cid: str) -> str:
    return cid.replace(" ", "").upper()'''

MYSQL_HELPERS = '''def today_str():
    return now_mmt().strftime("%-m/%-d/%Y")


# ── MySQL-first fallback helpers ──
def _mysql_available() -> bool:
    """Return True if MySQL module was loaded successfully."""
    return _MYSQL_AVAILABLE


def _mysql_query(sql: str, args: tuple = ()) -> list:
    """Execute a MySQL SELECT query. Returns list of dicts, or [] on failure."""
    global _MYSQL_AVAILABLE
    if not _MYSQL_AVAILABLE:
        return []
    try:
        return _mysql_db.query(sql, args)
    except Exception as e:
        logger.warning("MySQL query failed (will fallback to gspread): %s", e)
        _MYSQL_AVAILABLE = False  # disable for rest of request
        return []


def _mysql_query_one(sql: str, args: tuple = ()) -> dict | None:
    """Execute a MySQL SELECT returning single row, or None."""
    rows = _mysql_query(sql, args)
    return rows[0] if rows else None


def _mysql_execute(sql: str, args: tuple = ()) -> int:
    """Execute a MySQL INSERT/UPDATE/DELETE. Returns rowcount or -1."""
    global _MYSQL_AVAILABLE
    if not _MYSQL_AVAILABLE:
        return -1
    try:
        return _mysql_db.execute(sql, args)
    except Exception as e:
        logger.warning("MySQL execute failed: %s", e)
        return -1


def _norm_cid(cid: str) -> str:
    return cid.replace(" ", "").upper()'''

if HELPER_FUNCTIONS in content:
    content = content.replace(HELPER_FUNCTIONS, MYSQL_HELPERS)
    patches_applied.append("2: MySQL helper functions")
else:
    print("WARNING: Helper functions block not found!")

# ============================================================
# PATCH 3: api_next_voucher - MySQL first
# ============================================================
OLD_NEXT_VOUCHER = '''@app.get("/api/next_voucher", tags=["Sales"])
async def api_next_voucher(auth=Depends(verify_api_key)):
    """Generate next voucher number from Sales_Daily col B."""
    try:
        ws = get_worksheet(SHEET_SALES_DAILY)
        col = ws.col_values(2)
        ids = [v for v in col[1:] if v.upper().startswith("V-")]
        if ids:
            try:
                return ok(f"V-{int(ids[-1].split('-')[1]) + 1:03d}")
            except (IndexError, ValueError):
                pass
        return ok("V-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

NEW_NEXT_VOUCHER = '''@app.get("/api/next_voucher", tags=["Sales"])
async def api_next_voucher(auth=Depends(verify_api_key)):
    """Generate next voucher number (MySQL first, gspread fallback)."""
    try:
        # Try MySQL first
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT voucher_id FROM sales_daily ORDER BY id DESC LIMIT 1"
                )
                if rows and rows[0].get("voucher_id", "").upper().startswith("V-"):
                    vid = rows[0]["voucher_id"]
                    num = int(vid.split("-")[1])
                    return ok(f"V-{num + 1:03d}")
            except Exception as e:
                logger.warning("MySQL next_voucher failed: %s", e)

        # gspread fallback
        ws = get_worksheet(SHEET_SALES_DAILY)
        col = ws.col_values(2)
        ids = [v for v in col[1:] if v.upper().startswith("V-")]
        if ids:
            try:
                return ok(f"V-{int(ids[-1].split('-')[1]) + 1:03d}")
            except (IndexError, ValueError):
                pass
        return ok("V-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if OLD_NEXT_VOUCHER in content:
    content = content.replace(OLD_NEXT_VOUCHER, NEW_NEXT_VOUCHER)
    patches_applied.append("3: api_next_voucher")
else:
    print("WARNING: api_next_voucher block not found!")

# ============================================================
# PATCH 4: api_save_receipt_json - MySQL first
# ============================================================
OLD_SAVE_RECEIPT = '''@app.post("/api/save_receipt_json", tags=["Receipts"])
async def api_save_receipt_json(req: dict, auth=Depends(verify_api_key)):
    """Persist receipt data locally."""
    try:
        voucher_id = req.get("voucher_id", "unknown")
        data = req.get("data", {})
        logger.info("Receipt saved: voucher=%s", voucher_id)
        return ok({"voucher_id": voucher_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

NEW_SAVE_RECEIPT = '''@app.post("/api/save_receipt_json", tags=["Receipts"])
async def api_save_receipt_json(req: dict, auth=Depends(verify_api_key)):
    """Persist receipt data (MySQL first, local file fallback)."""
    import json as _json
    try:
        voucher_id = req.get("voucher_id", "unknown")
        data = req.get("data", {})

        # Try MySQL first
        mysql_saved = False
        if _mysql_available():
            try:
                rc = _mysql_execute(
                    "INSERT INTO receipts (voucher_id, receipt_data) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE receipt_data = VALUES(receipt_data)",
                    (voucher_id, _json.dumps(data, ensure_ascii=False))
                )
                if rc > 0:
                    mysql_saved = True
                    logger.info("Receipt saved to MySQL: voucher=%s", voucher_id)
            except Exception as e:
                logger.warning("MySQL receipt save failed: %s", e)

        # Always also save locally as fallback
        receipt_dir = "/root/psvibe-sales-bot/bot/receipts"
        import os as _os
        _os.makedirs(receipt_dir, exist_ok=True)
        safe_id = voucher_id.replace("/", "-").replace("\\\\", "-")
        receipt_path = _os.path.join(receipt_dir, f"{safe_id}.json")
        with open(receipt_path, "w") as f:
            _json.dump(data, f, ensure_ascii=False)
        logger.info("Receipt saved: voucher=%s mysql=%s", voucher_id, mysql_saved)
        return ok({"voucher_id": voucher_id, "mysql_saved": mysql_saved})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if OLD_SAVE_RECEIPT in content:
    content = content.replace(OLD_SAVE_RECEIPT, NEW_SAVE_RECEIPT)
    patches_applied.append("4: api_save_receipt_json")
else:
    print("WARNING: api_save_receipt_json block not found!")

# ============================================================
# PATCH 5: api_get_receipt_html - MySQL first
# ============================================================
OLD_GET_RECEIPT = '''@app.get("/api/receipt/{voucher_id}", tags=["Receipts"])
async def api_get_receipt_html(voucher_id: str, auth=Depends(verify_api_key)):
    """Render receipt HTML for a given voucher_id."""
    import json
    import os as _os
    
    try:
        # Normalise voucher_id (replace / and \\ with -)
        safe_id = voucher_id.replace("/", "-").replace("\\\\", "-")
        receipt_dir = "/root/psvibe-sales-bot/bot/receipts"
        receipt_path = _os.path.join(receipt_dir, f"{safe_id}.json")
        template_path = "/root/psvibe_api_server/receipt_template.html"
        
        if not _os.path.exists(receipt_path):
            logger.warning("Receipt not found: voucher=%s path=%s", voucher_id, receipt_path)
            return HTMLResponse(
                content=f"<html><body style=\\"font-family:sans-serif;padding:40px;text-align:center\\"><h2>404 - Receipt Not Found</h2><p>Voucher: {voucher_id}</p></body></html>",
                status_code=404
            )
        
        with open(receipt_path, "r") as f:
            receipt_data = json.load(f)
        
        with open(template_path, "r") as f:
            template = f.read()
        
        # Inject receipt data before </head>
        json_str = json.dumps(receipt_data, ensure_ascii=False)
        script_tag = f"<script>window.__RECEIPT_DATA__ = {json_str};</script>"
        injected = template.replace("</head>", script_tag + "\\n</head>")
        
        logger.info("Receipt rendered: voucher=%s", voucher_id)
        return HTMLResponse(content=injected)
    except Exception as e:
        logger.error("Receipt render error: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))'''

NEW_GET_RECEIPT = '''@app.get("/api/receipt/{voucher_id}", tags=["Receipts"])
async def api_get_receipt_html(voucher_id: str, auth=Depends(verify_api_key)):
    """Render receipt HTML for a given voucher_id (MySQL first, local file fallback)."""
    import json as _json
    import os as _os
    
    try:
        # Normalise voucher_id
        safe_id = voucher_id.replace("/", "-").replace("\\\\", "-")
        template_path = "/root/psvibe_api_server/receipt_template.html"
        receipt_data = None
        source = "none"

        # Try MySQL first
        if _mysql_available() and receipt_data is None:
            try:
                row = _mysql_query_one(
                    "SELECT receipt_data FROM receipts WHERE voucher_id = %s",
                    (voucher_id,)
                )
                if row and row.get("receipt_data"):
                    raw = row["receipt_data"]
                    if isinstance(raw, str):
                        receipt_data = _json.loads(raw)
                    else:
                        receipt_data = raw
                    source = "mysql"
                    logger.info("Receipt loaded from MySQL: voucher=%s", voucher_id)
            except Exception as e:
                logger.warning("MySQL receipt fetch failed: %s", e)

        # Fallback: local file
        if receipt_data is None:
            receipt_dir = "/root/psvibe-sales-bot/bot/receipts"
            receipt_path = _os.path.join(receipt_dir, f"{safe_id}.json")
            if not _os.path.exists(receipt_path):
                logger.warning("Receipt not found: voucher=%s", voucher_id)
                return HTMLResponse(
                    content=f"<html><body style=\\"font-family:sans-serif;padding:40px;text-align:center\\"><h2>404 - Receipt Not Found</h2><p>Voucher: {voucher_id}</p></body></html>",
                    status_code=404
                )
            with open(receipt_path, "r") as f:
                receipt_data = _json.load(f)
            source = "local"

        with open(template_path, "r") as f:
            template = f.read()

        json_str = _json.dumps(receipt_data, ensure_ascii=False)
        script_tag = f"<script>window.__RECEIPT_DATA__ = {json_str};</script>"
        injected = template.replace("</head>", script_tag + "\\n</head>")

        logger.info("Receipt rendered: voucher=%s source=%s", voucher_id, source)
        return HTMLResponse(content=injected)
    except Exception as e:
        logger.error("Receipt render error: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))'''

if OLD_GET_RECEIPT in content:
    content = content.replace(OLD_GET_RECEIPT, NEW_GET_RECEIPT)
    patches_applied.append("5: api_get_receipt_html")
else:
    print("WARNING: api_get_receipt_html block not found!")

# ============================================================
# PATCH 6: api_sheets_promotions - MySQL first
# ============================================================
# Find the promotions section - use a unique marker
OLD_PROMO_SECTION = '''# ═══════════════════════════════════════
#  SHEETS — promotions
# ═══════════════════════════════════════
@app.get("/api/sheets/promotions", tags=["Sheets"])
async def api_sheets_promotions(auth=Depends(verify_api_key)):
    """Return active promotions bundled with today's usage count."""
    try:
        today = today_str()
        promos = []
        # Read promotion definitions from Setting!B22:B25 area
        try:
            ws = get_worksheet(SHEET_SETTING)
            for r in range(22, 30):
                name = ws.cell(r, 2).value
                price = ws.cell(r, 3).value
                mins = ws.cell(r, 4).value
                if name and str(name).strip():
                    promos.append({
                        "name": str(name).strip(),
                        "price": int_safe(price),
                        "mins": int_safe(mins),
                        "active": True,
                        "used_today": 0,
                    })
        except Exception as e:
            logger.warning("promotions read error: %s", e)

        # Count usage today
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if d != today:
                    continue
                notes = row[9].strip().lower() if len(row) > 9 else ""
                for p in promos:
                    if p["name"].lower() in notes:
                        p["used_today"] += 1
        except Exception:
            pass

        return ok(promos)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sheets/promotions/all", tags=["Sheets"])
async def api_sheets_promotions_all(auth=Depends(verify_api_key)):
    """Return all promotions (active + historical)."""
    return await api_sheets_promotions(auth)


@app.get("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log(auth=Depends(verify_api_key)):
    """Return promotion usage log."""
    try:
        log = []
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 10:
                    continue
                notes = row[9].strip().lower() if len(row) > 9 else ""
                voucher = row[1].strip() if len(row) > 1 else ""
                d = row[2].strip() if len(row) > 2 else ""
                member = row[3].strip() if len(row) > 3 else ""
                amt = int_safe(row[4]) if len(row) > 4 else 0
                if notes:
                    log.append({
                        "date": d, "voucher": voucher, "member": member,
                        "amount": amt, "promotion": notes,
                    })
        except Exception as e:
            logger.warning("promotions-log error: %s", e)

        return ok({"log": log, "total": len(log)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log_post(req: dict, auth=Depends(verify_api_key)):
    """Log a promotion usage event."""
    try:
        logger.info("PROMO-LOG: member=%s promotion=%s amount=%s", 
                     req.get("member", ""), req.get("promotion", ""), req.get("amount", 0))
        return ok({"logged": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

NEW_PROMO_SECTION = '''# ═══════════════════════════════════════
#  SHEETS — promotions
# ═══════════════════════════════════════
@app.get("/api/sheets/promotions", tags=["Sheets"])
async def api_sheets_promotions(auth=Depends(verify_api_key)):
    """Return active promotions bundled with today's usage count (MySQL first, gspread fallback)."""
    try:
        today = today_str()
        promos = []

        # Try MySQL first
        mysql_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT promo_name, discount_value, status, notes FROM promotions "
                    "WHERE status = 'active' OR status IS NULL ORDER BY id"
                )
                if rows:
                    for r in rows:
                        promos.append({
                            "name": r.get("promo_name", "").strip(),
                            "price": int_safe(r.get("discount_value", 0)),
                            "mins": int_safe(r.get("notes", 0)),
                            "active": True,
                            "used_today": 0,
                        })
                    mysql_ok = True
            except Exception as e:
                logger.warning("MySQL promotions failed: %s", e)

        # gspread fallback
        if not mysql_ok:
            try:
                ws = get_worksheet(SHEET_SETTING)
                for r in range(22, 30):
                    name = ws.cell(r, 2).value
                    price = ws.cell(r, 3).value
                    mins = ws.cell(r, 4).value
                    if name and str(name).strip():
                        promos.append({
                            "name": str(name).strip(),
                            "price": int_safe(price),
                            "mins": int_safe(mins),
                            "active": True,
                            "used_today": 0,
                        })
            except Exception as e:
                logger.warning("promotions read error: %s", e)

        # Count usage today (from MySQL if available, else gspread)
        try:
            if _mysql_available():
                rows = _mysql_query(
                    "SELECT notes FROM sales_daily "
                    "WHERE sale_date = %s AND notes IS NOT NULL AND notes != ''",
                    (today,)
                )
                for r in rows:
                    notes = (r.get("notes", "") or "").lower()
                    for p in promos:
                        if p["name"].lower() in notes:
                            p["used_today"] += 1
            else:
                from sheets_client import get_sales_daily_rows
                sd_raw = get_sales_daily_rows()
                for row in sd_raw[1:]:
                    if len(row) < 5:
                        continue
                    d = row[2].strip() if len(row) > 2 else ""
                    if d != today:
                        continue
                    notes = row[9].strip().lower() if len(row) > 9 else ""
                    for p in promos:
                        if p["name"].lower() in notes:
                            p["used_today"] += 1
        except Exception:
            pass

        return ok(promos)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sheets/promotions/all", tags=["Sheets"])
async def api_sheets_promotions_all(auth=Depends(verify_api_key)):
    """Return all promotions (active + historical)."""
    return await api_sheets_promotions(auth)


@app.get("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log(auth=Depends(verify_api_key)):
    """Return promotion usage log (MySQL first, gspread fallback)."""
    try:
        log = []

        # Try MySQL first
        mysql_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT sale_date as date, voucher_id as voucher, member_id as member, "
                    "amount, notes FROM sales_daily "
                    "WHERE notes IS NOT NULL AND notes != '' "
                    "ORDER BY id DESC LIMIT 500"
                )
                for r in rows:
                    log.append({
                        "date": r.get("date", ""),
                        "voucher": r.get("voucher", ""),
                        "member": r.get("member", ""),
                        "amount": int_safe(r.get("amount", 0)),
                        "promotion": (r.get("notes", "") or ""),
                    })
                mysql_ok = True
            except Exception as e:
                logger.warning("MySQL promotions-log failed: %s", e)

        # gspread fallback
        if not mysql_ok:
            try:
                from sheets_client import get_sales_daily_rows
                sd_raw = get_sales_daily_rows()
                for row in sd_raw[1:]:
                    if len(row) < 10:
                        continue
                    notes = row[9].strip().lower() if len(row) > 9 else ""
                    voucher = row[1].strip() if len(row) > 1 else ""
                    d = row[2].strip() if len(row) > 2 else ""
                    member = row[3].strip() if len(row) > 3 else ""
                    amt = int_safe(row[4]) if len(row) > 4 else 0
                    if notes:
                        log.append({
                            "date": d, "voucher": voucher, "member": member,
                            "amount": amt, "promotion": notes,
                        })
            except Exception as e:
                logger.warning("promotions-log error: %s", e)

        return ok({"log": log, "total": len(log)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sheets/promotions-log", tags=["Sheets"])
async def api_sheets_promotions_log_post(req: dict, auth=Depends(verify_api_key)):
    """Log a promotion usage event (MySQL + log)."""
    try:
        logger.info("PROMO-LOG: member=%s promotion=%s amount=%s", 
                     req.get("member", ""), req.get("promotion", ""), req.get("amount", 0))

        # Try MySQL insert
        if _mysql_available():
            try:
                _mysql_execute(
                    "INSERT INTO promotions (promo_name, discount_value, status, notes, start_date) "
                    "VALUES (%s, %s, 'logged', %s, CURDATE())",
                    (
                        req.get("promotion", ""),
                        req.get("amount", 0),
                        f"member={req.get('member', '')}",
                    )
                )
            except Exception as e:
                logger.warning("MySQL promotion log insert failed: %s", e)

        return ok({"logged": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''

if OLD_PROMO_SECTION in content:
    content = content.replace(OLD_PROMO_SECTION, NEW_PROMO_SECTION)
    patches_applied.append("6: promotions endpoints")
else:
    print("WARNING: Promotions section not found!")

# ============================================================
# PATCH 7: api_sheets_report_data - MySQL first for sales & topup
# ============================================================
OLD_REPORT_DATA_SALES = '''        # Sales_Daily
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            members_set = set()
            for row in sd_raw[1:]:
                if len(row) < 6:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if d != today:
                    continue
                amt = int_safe(row[4]) if len(row) > 4 else 0
                payment = row[5].strip().title() if len(row) > 5 else "Unknown"
                member = row[3].strip() if len(row) > 3 else ""
                result["total_sales"] += amt
                result["voucher_count"] += 1
                if payment not in result["payment_breakdown"]:
                    result["payment_breakdown"][payment] = 0
                result["payment_breakdown"][payment] += amt
                if member:
                    members_set.add(member)
            result["members_served"] = len(members_set)
        except Exception as e:
            logger.warning("report-data sales error: %s", e)'''

NEW_REPORT_DATA_SALES = '''        # Sales_Daily (MySQL first, gspread fallback)
        members_set = set()
        mysql_sales_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT member_id, amount, payment_method FROM sales_daily WHERE sale_date = %s",
                    (today,)
                )
                for r in rows:
                    amt = int_safe(r.get("amount", 0))
                    payment = str(r.get("payment_method", "") or "Unknown").strip().title()
                    member = (r.get("member_id", "") or "").strip()
                    result["total_sales"] += amt
                    result["voucher_count"] += 1
                    if payment not in result["payment_breakdown"]:
                        result["payment_breakdown"][payment] = 0
                    result["payment_breakdown"][payment] += amt
                    if member:
                        members_set.add(member)
                mysql_sales_ok = True
            except Exception as e:
                logger.warning("MySQL report-data sales failed: %s", e)

        if not mysql_sales_ok:
            try:
                from sheets_client import get_sales_daily_rows
                sd_raw = get_sales_daily_rows()
                for row in sd_raw[1:]:
                    if len(row) < 6:
                        continue
                    d = row[2].strip() if len(row) > 2 else ""
                    if d != today:
                        continue
                    amt = int_safe(row[4]) if len(row) > 4 else 0
                    payment = row[5].strip().title() if len(row) > 5 else "Unknown"
                    member = row[3].strip() if len(row) > 3 else ""
                    result["total_sales"] += amt
                    result["voucher_count"] += 1
                    if payment not in result["payment_breakdown"]:
                        result["payment_breakdown"][payment] = 0
                    result["payment_breakdown"][payment] += amt
                    if member:
                        members_set.add(member)
            except Exception as e:
                logger.warning("report-data sales error: %s", e)
        result["members_served"] = len(members_set)'''

OLD_REPORT_DATA_TOPUP = '''        # Top-ups
        try:
            from sheets_client import get_topup_log_rows
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 5:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                if d != today:
                    continue
                result["top_ups"]["count"] += 1
                result["top_ups"]["amount"] += int_safe(row[3]) if len(row) > 3 else 0
                result["top_ups"]["mins"] += int_safe(row[4]) if len(row) > 4 else 0
        except Exception as e:
            logger.warning("report-data topup error: %s", e)'''

NEW_REPORT_DATA_TOPUP = '''        # Top-ups (MySQL first, gspread fallback)
        mysql_topup_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT amount, balance_after, balance_before FROM topup_log "
                    "WHERE DATE(topup_date) = %s",
                    (today,)
                )
                for r in rows:
                    result["top_ups"]["count"] += 1
                    result["top_ups"]["amount"] += int_safe(r.get("amount", 0))
                    ba = int_safe(r.get("balance_after", 0))
                    bb = int_safe(r.get("balance_before", 0))
                    result["top_ups"]["mins"] += max(0, ba - bb)
                mysql_topup_ok = True
            except Exception as e:
                logger.warning("MySQL report-data topup failed: %s", e)

        if not mysql_topup_ok:
            try:
                from sheets_client import get_topup_log_rows
                tu_rows = get_topup_log_rows()
                for row in tu_rows[1:]:
                    if len(row) < 5:
                        continue
                    d = row[0].strip() if len(row) > 0 else ""
                    if d != today:
                        continue
                    result["top_ups"]["count"] += 1
                    result["top_ups"]["amount"] += int_safe(row[3]) if len(row) > 3 else 0
                    result["top_ups"]["mins"] += int_safe(row[4]) if len(row) > 4 else 0
            except Exception as e:
                logger.warning("report-data topup error: %s", e)'''

if OLD_REPORT_DATA_SALES in content:
    content = content.replace(OLD_REPORT_DATA_SALES, NEW_REPORT_DATA_SALES)
    patches_applied.append("7a: report-data sales MySQL")
else:
    print("WARNING: report-data sales block not found!")

if OLD_REPORT_DATA_TOPUP in content:
    content = content.replace(OLD_REPORT_DATA_TOPUP, NEW_REPORT_DATA_TOPUP)
    patches_applied.append("7b: report-data topup MySQL")
else:
    print("WARNING: report-data topup block not found!")

# ============================================================
# PATCH 8: api_sheets_pnl - MySQL first for sales & topup
# ============================================================
OLD_PNL_SALES = '''        # Revenue from Sales_Daily
        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if month_slash not in d:
                    continue
                amt = int_safe(row[4]) if len(row) > 4 else 0
                result["revenue"]["console_rental"] += amt
        except Exception as e:
            logger.warning("pnl sales error: %s", e)'''

NEW_PNL_SALES = '''        # Revenue from Sales_Daily (MySQL first, gspread fallback)
        mysql_pnl_sales_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT amount FROM sales_daily WHERE sale_date LIKE %s",
                    (f"%{month_slash}%",)
                )
                for r in rows:
                    result["revenue"]["console_rental"] += int_safe(r.get("amount", 0))
                mysql_pnl_sales_ok = True
            except Exception as e:
                logger.warning("MySQL pnl sales failed: %s", e)

        if not mysql_pnl_sales_ok:
            try:
                from sheets_client import get_sales_daily_rows
                sd_raw = get_sales_daily_rows()
                for row in sd_raw[1:]:
                    if len(row) < 5:
                        continue
                    d = row[2].strip() if len(row) > 2 else ""
                    if month_slash not in d:
                        continue
                    amt = int_safe(row[4]) if len(row) > 4 else 0
                    result["revenue"]["console_rental"] += amt
            except Exception as e:
                logger.warning("pnl sales error: %s", e)'''

OLD_PNL_TOPUP = '''        # TopUp revenue
        try:
            from sheets_client import get_topup_log_rows
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                if month_slash not in d:
                    continue
                amt = int_safe(row[3]) if len(row) > 3 else 0
                result["revenue"]["topup_sales"] += amt
        except Exception as e:
            logger.warning("pnl topup error: %s", e)'''

NEW_PNL_TOPUP = '''        # TopUp revenue (MySQL first, gspread fallback)
        mysql_pnl_topup_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT amount FROM topup_log WHERE DATE_FORMAT(topup_date, '%m/%Y') = %s",
                    (month_slash,)
                )
                for r in rows:
                    result["revenue"]["topup_sales"] += int_safe(r.get("amount", 0))
                mysql_pnl_topup_ok = True
            except Exception as e:
                logger.warning("MySQL pnl topup failed: %s", e)

        if not mysql_pnl_topup_ok:
            try:
                from sheets_client import get_topup_log_rows
                tu_rows = get_topup_log_rows()
                for row in tu_rows[1:]:
                    if len(row) < 4:
                        continue
                    d = row[0].strip() if len(row) > 0 else ""
                    if month_slash not in d:
                        continue
                    amt = int_safe(row[3]) if len(row) > 3 else 0
                    result["revenue"]["topup_sales"] += amt
            except Exception as e:
                logger.warning("pnl topup error: %s", e)'''

if OLD_PNL_SALES in content:
    content = content.replace(OLD_PNL_SALES, NEW_PNL_SALES)
    patches_applied.append("8a: pnl sales MySQL")
else:
    print("WARNING: pnl sales block not found!")

if OLD_PNL_TOPUP in content:
    content = content.replace(OLD_PNL_TOPUP, NEW_PNL_TOPUP)
    patches_applied.append("8b: pnl topup MySQL")
else:
    print("WARNING: pnl topup block not found!")

# ============================================================
# PATCH 9: api_sheets_payment_methods - MySQL first
# ============================================================
OLD_PAYMENT_METHODS = '''        try:
            from sheets_client import get_sales_daily_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 6:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if d != today:
                    continue
                payment = row[5].strip().title() if len(row) > 5 else "Unknown"
                amt = int_safe(row[4]) if len(row) > 4 else 0
                if payment not in methods:
                    methods[payment] = {"count": 0, "amount": 0}
                methods[payment]["count"] += 1
                methods[payment]["amount"] += amt
        except Exception as e:
            logger.warning("payment-methods error: %s", e)'''

NEW_PAYMENT_METHODS = '''        mysql_pm_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT payment_method, amount FROM sales_daily WHERE sale_date = %s",
                    (today,)
                )
                for r in rows:
                    payment = str(r.get("payment_method", "") or "Unknown").strip().title()
                    amt = int_safe(r.get("amount", 0))
                    if payment not in methods:
                        methods[payment] = {"count": 0, "amount": 0}
                    methods[payment]["count"] += 1
                    methods[payment]["amount"] += amt
                mysql_pm_ok = True
            except Exception as e:
                logger.warning("MySQL payment-methods failed: %s", e)

        if not mysql_pm_ok:
            try:
                from sheets_client import get_sales_daily_rows
                sd_raw = get_sales_daily_rows()
                for row in sd_raw[1:]:
                    if len(row) < 6:
                        continue
                    d = row[2].strip() if len(row) > 2 else ""
                    if d != today:
                        continue
                    payment = row[5].strip().title() if len(row) > 5 else "Unknown"
                    amt = int_safe(row[4]) if len(row) > 4 else 0
                    if payment not in methods:
                        methods[payment] = {"count": 0, "amount": 0}
                    methods[payment]["count"] += 1
                    methods[payment]["amount"] += amt
            except Exception as e:
                logger.warning("payment-methods error: %s", e)'''

if OLD_PAYMENT_METHODS in content:
    content = content.replace(OLD_PAYMENT_METHODS, NEW_PAYMENT_METHODS)
    patches_applied.append("9: payment-methods MySQL")
else:
    print("WARNING: payment-methods block not found!")

# ============================================================
# PATCH 10: api_sheets_weekly_report - MySQL first
# ============================================================
OLD_WEEKLY_SALES = '''        try:
            from sheets_client import get_sales_daily_rows, get_topup_log_rows
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                dt = _parse_mm_dd_yyyy(d)
                if dt is None:
                    continue
                dt_mmt = dt.replace(tzinfo=MMT_TZ)
                if dt_mmt < week_start:
                    continue
                day_key = dt.strftime("%Y-%m-%d")
                amt = int_safe(row[4]) if len(row) > 4 else 0
                if day_key not in result["daily_revenue"]:
                    result["daily_revenue"][day_key] = 0
                result["daily_revenue"][day_key] += amt
                result["total_revenue"] += amt
                result["total_vouchers"] += 1
        except Exception as e:
            logger.warning("weekly-report sales error: %s", e)'''

NEW_WEEKLY_SALES = '''        mysql_weekly_sales_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT sale_date, amount FROM sales_daily "
                    "WHERE sale_date >= %s AND sale_date < DATE_ADD(%s, INTERVAL 7 DAY)",
                    (week_start.strftime("%Y-%m-%d"), week_start.strftime("%Y-%m-%d"))
                )
                for r in rows:
                    d = r.get("sale_date", "")
                    dt = _parse_mm_dd_yyyy(str(d)) if d else None
                    if dt is None:
                        continue
                    dt_mmt = dt.replace(tzinfo=MMT_TZ)
                    if dt_mmt < week_start:
                        continue
                    day_key = dt.strftime("%Y-%m-%d")
                    amt = int_safe(r.get("amount", 0))
                    if day_key not in result["daily_revenue"]:
                        result["daily_revenue"][day_key] = 0
                    result["daily_revenue"][day_key] += amt
                    result["total_revenue"] += amt
                    result["total_vouchers"] += 1
                mysql_weekly_sales_ok = True
            except Exception as e:
                logger.warning("MySQL weekly-report sales failed: %s", e)

        if not mysql_weekly_sales_ok:
            try:
                from sheets_client import get_sales_daily_rows, get_topup_log_rows
                import_used = True
                sd_raw = get_sales_daily_rows()
                for row in sd_raw[1:]:
                    if len(row) < 5:
                        continue
                    d = row[2].strip() if len(row) > 2 else ""
                    dt = _parse_mm_dd_yyyy(d)
                    if dt is None:
                        continue
                    dt_mmt = dt.replace(tzinfo=MMT_TZ)
                    if dt_mmt < week_start:
                        continue
                    day_key = dt.strftime("%Y-%m-%d")
                    amt = int_safe(row[4]) if len(row) > 4 else 0
                    if day_key not in result["daily_revenue"]:
                        result["daily_revenue"][day_key] = 0
                    result["daily_revenue"][day_key] += amt
                    result["total_revenue"] += amt
                    result["total_vouchers"] += 1
            except Exception as e:
                logger.warning("weekly-report sales error: %s", e)'''

OLD_WEEKLY_TOPUP = '''        try:
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                dt = _parse_mm_dd_yyyy(d)
                if dt is None:
                    continue
                dt_mmt = dt.replace(tzinfo=MMT_TZ)
                if dt_mmt < week_start:
                    continue
                amt = int_safe(row[3]) if len(row) > 3 else 0
                result["total_topups"] += 1
                result["topup_revenue"] += amt
                result["total_revenue"] += amt
        except Exception as e:
            logger.warning("weekly-report topup error: %s", e)'''

NEW_WEEKLY_TOPUP = '''        mysql_weekly_topup_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT amount FROM topup_log "
                    "WHERE topup_date >= %s AND topup_date < DATE_ADD(%s, INTERVAL 7 DAY)",
                    (week_start.strftime("%Y-%m-%d"), week_start.strftime("%Y-%m-%d"))
                )
                for r in rows:
                    amt = int_safe(r.get("amount", 0))
                    result["total_topups"] += 1
                    result["topup_revenue"] += amt
                    result["total_revenue"] += amt
                mysql_weekly_topup_ok = True
            except Exception as e:
                logger.warning("MySQL weekly-report topup failed: %s", e)

        if not mysql_weekly_topup_ok:
            try:
                # import already done above if fallback reached
                if not (locals().get('import_used')):
                    from sheets_client import get_topup_log_rows
                tu_rows = get_topup_log_rows()
                for row in tu_rows[1:]:
                    if len(row) < 4:
                        continue
                    d = row[0].strip() if len(row) > 0 else ""
                    dt = _parse_mm_dd_yyyy(d)
                    if dt is None:
                        continue
                    dt_mmt = dt.replace(tzinfo=MMT_TZ)
                    if dt_mmt < week_start:
                        continue
                    amt = int_safe(row[3]) if len(row) > 3 else 0
                    result["total_topups"] += 1
                    result["topup_revenue"] += amt
                    result["total_revenue"] += amt
            except Exception as e:
                logger.warning("weekly-report topup error: %s", e)'''

if OLD_WEEKLY_SALES in content:
    content = content.replace(OLD_WEEKLY_SALES, NEW_WEEKLY_SALES)
    patches_applied.append("10a: weekly-report sales MySQL")
else:
    print("WARNING: weekly-report sales block not found!")

if OLD_WEEKLY_TOPUP in content:
    content = content.replace(OLD_WEEKLY_TOPUP, NEW_WEEKLY_TOPUP)
    patches_applied.append("10b: weekly-report topup MySQL")
else:
    print("WARNING: weekly-report topup block not found!")

# ============================================================
# PATCH 11: api_finance_pnl - MySQL first for sales & topup
# ============================================================
OLD_FINANCE_PNL_SALES = '''        from sheets_client import get_sales_daily_rows, get_topup_log_rows
        sd_raw = get_sales_daily_rows()
        for row in sd_raw[1:]:
            if len(row) < 5:
                continue
            d = row[2].strip() if len(row) > 2 else ""
            if month_slash not in d:
                continue
            amt = int_safe(row[4]) if len(row) > 4 else 0
            result["revenue"]["console"] += amt'''

NEW_FINANCE_PNL_SALES = '''        # MySQL first for sales
        mysql_fin_sales_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT amount FROM sales_daily WHERE sale_date LIKE %s",
                    (f"%{month_slash}%",)
                )
                for r in rows:
                    result["revenue"]["console"] += int_safe(r.get("amount", 0))
                mysql_fin_sales_ok = True
            except Exception as e:
                logger.warning("MySQL finance/pnl sales failed: %s", e)

        if not mysql_fin_sales_ok:
            from sheets_client import get_sales_daily_rows, get_topup_log_rows
            import_used_fin = True
            sd_raw = get_sales_daily_rows()
            for row in sd_raw[1:]:
                if len(row) < 5:
                    continue
                d = row[2].strip() if len(row) > 2 else ""
                if month_slash not in d:
                    continue
                amt = int_safe(row[4]) if len(row) > 4 else 0
                result["revenue"]["console"] += amt'''

OLD_FINANCE_PNL_TOPUP = '''        tu_rows = get_topup_log_rows()
        for row in tu_rows[1:]:
            if len(row) < 4:
                continue
            d = row[0].strip() if len(row) > 0 else ""
            if month_slash not in d:
                continue
            amt = int_safe(row[3]) if len(row) > 3 else 0
            result["revenue"]["topup"] += amt
        logger.warning("finance/pnl topup error: %s", e)'''

NEW_FINANCE_PNL_TOPUP = '''        mysql_fin_topup_ok = False
        if _mysql_available():
            try:
                rows = _mysql_query(
                    "SELECT amount FROM topup_log WHERE DATE_FORMAT(topup_date, '%m/%Y') = %s",
                    (month_slash,)
                )
                for r in rows:
                    result["revenue"]["topup"] += int_safe(r.get("amount", 0))
                mysql_fin_topup_ok = True
            except Exception as e:
                logger.warning("MySQL finance/pnl topup failed: %s", e)

        if not mysql_fin_topup_ok:
            if not (locals().get('import_used_fin')):
                from sheets_client import get_topup_log_rows
            tu_rows = get_topup_log_rows()
            for row in tu_rows[1:]:
                if len(row) < 4:
                    continue
                d = row[0].strip() if len(row) > 0 else ""
                if month_slash not in d:
                    continue
                amt = int_safe(row[3]) if len(row) > 3 else 0
                result["revenue"]["topup"] += amt
        logger.warning("finance/pnl topup error: %s", e)'''

OLD_FINANCE_PNL_TOPUP_ERR = '''        except Exception as e:
        logger.warning("finance/pnl topup error: %s", e)'''

NEW_FINANCE_PNL_TOPUP_EXCEPT = '''        except Exception as e:
            logger.warning("finance/pnl topup error: %s", e)'''

if OLD_FINANCE_PNL_SALES in content:
    content = content.replace(OLD_FINANCE_PNL_SALES, NEW_FINANCE_PNL_SALES)
    patches_applied.append("11a: finance/pnl sales MySQL")
else:
    print("WARNING: finance/pnl sales block not found!")

if OLD_FINANCE_PNL_TOPUP in content:
    content = content.replace(OLD_FINANCE_PNL_TOPUP, NEW_FINANCE_PNL_TOPUP)
    patches_applied.append("11b: finance/pnl topup MySQL")
else:
    print("WARNING: finance/pnl topup block not found!")

# Write the patched file
with open(APP_PATH, 'w') as f:
    f.write(content)

print(f"\n=== Patches applied ({len(patches_applied)}):")
for p in patches_applied:
    print(f"  ✓ {p}")
print(f"\nFile written: {APP_PATH}")
print(f"Backup: {BACKUP_PATH}")
print("DONE")