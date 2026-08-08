"""
Stock FIFO module for PS VIBE API.
Calculates COGS and inventory value using FIFO method.
"""
import pymysql
from collections import OrderedDict
from decimal import Decimal


def get_stock_in_batches(conn, year_month=None):
    """Get all stock_in batches ordered by item_name, created_at ASC.
    If year_month (YYYY-MM) provided, only include stock purchased by month-end."""
    cur = conn.cursor(pymysql.cursors.DictCursor)
    if year_month:
        from datetime import datetime, timedelta
        ym_year, ym_month = map(int, year_month.split("-"))
        # Calculate last day of month: first day of next month minus 1 day
        if ym_month == 12:
            next_month_first = datetime(ym_year + 1, 1, 1)
        else:
            next_month_first = datetime(ym_year, ym_month + 1, 1)
        # Use < next_first to include ALL records up to month end (inclusive)
        cut_off = next_month_first.strftime("%Y-%m-%d")
        cur.execute("""
            SELECT id, item_name, quantity, unit_cost, created_at
            FROM stock_in
            WHERE created_at < %s
            ORDER BY item_name, created_at ASC, id ASC
        """, (cut_off,))
    else:
        cur.execute("""
            SELECT id, item_name, quantity, unit_cost, created_at
            FROM stock_in
            ORDER BY item_name, created_at ASC, id ASC
        """)
    rows = cur.fetchall()
    cur.close()
    return rows


def get_stock_out_items(conn, year_month=None, cumulative=False):
    """Get all stock_out transactions, summed by item_name.
    If year_month (YYYY-MM) provided and cumulative=False, filter to that month only.
    If cumulative=True, filter to stock_out on or before end of that month."""
    cur = conn.cursor(pymysql.cursors.DictCursor)
    if year_month:
        if cumulative:
            from datetime import datetime, timedelta
            ym_year, ym_month = map(int, year_month.split("-"))
            if ym_month == 12:
                next_first = datetime(ym_year + 1, 1, 1)
            else:
                next_first = datetime(ym_year, ym_month + 1, 1)
            cut_off = next_first.strftime("%Y-%m-%d")
            # Use < next_first to include ALL records up to month end (including same day)
            cur.execute("""
                SELECT item_name, SUM(quantity) as total_qty
                FROM stock_out
                WHERE created_at < %s
                GROUP BY item_name
            """, (cut_off,))
        else:
            cur.execute("""
                SELECT item_name, SUM(quantity) as total_qty
                FROM stock_out
                WHERE DATE_FORMAT(created_at, '%%Y-%%m') = %s
                GROUP BY item_name
            """, (year_month,))
    else:
        cur.execute("""
            SELECT item_name, SUM(quantity) as total_qty
            FROM stock_out
            GROUP BY item_name
        """)
    rows = cur.fetchall()
    cur.close()
    return rows


def calc_fifo(conn, year_month=None, cumulative=False):
    """
    Calculate COGS and remaining inventory value using FIFO.
    
    Args:
        conn: MySQL connection
        year_month: If provided (e.g. "2026-06"), calculate COGS for that month only.
                    Stock-in filtered to purchases <= last day of month.
                    Stock-out filtered to that month (or up to that month if cumulative=True).
                    If None, calculates lifetime COGS.
        cumulative: If True, include all stock_out up to end of given month (not just that month).
    
    Returns:
        dict: { "cogs": float, "inventory_value": float, "details": [...] }
    """
    from decimal import Decimal
    
    # Build FIFO queue: item_name -> list of (qty, unit_cost) ordered by date
    batches = get_stock_in_batches(conn, year_month)
    fifo = {}
    for b in batches:
        name = b["item_name"].strip()
        qty = int(b["quantity"])
        cost = float(b["unit_cost"])
        if name not in fifo:
            fifo[name] = []
        fifo[name].append({"qty": qty, "cost": cost})
    
    sold_items = get_stock_out_items(conn, year_month, cumulative=cumulative)
    
    total_cogs = 0.0
    details = []
    
    for s in sold_items:
        name = s["item_name"].strip()
        qty_to_sell = int(s["total_qty"])
        
        if name not in fifo:
            # No stock_in record for this item — use selling price as COGS
            cogs = qty_to_sell * 0
            total_cogs += cogs
            details.append({"item": name, "sold_qty": qty_to_sell, "cogs": cogs, "note": "no_stock_in"})
            continue
        
        item_batches = fifo[name]
        batch_idx = 0
        remaining_to_sell = qty_to_sell
        item_cogs = 0.0
        
        while remaining_to_sell > 0 and batch_idx < len(item_batches):
            batch = item_batches[batch_idx]
            consume = min(remaining_to_sell, batch["qty"])
            item_cogs += consume * batch["cost"]
            batch["qty"] -= consume
            remaining_to_sell -= consume
            if batch["qty"] <= 0:
                batch_idx += 1
        
        total_cogs += item_cogs
        details.append({"item": name, "sold_qty": qty_to_sell, "cogs": round(item_cogs, 0), "note": "fifo" if remaining_to_sell == 0 else "partial"})
    
    # Calculate remaining inventory value
    inventory_value = 0.0
    for name, batches in fifo.items():
        for b in batches:
            if b["qty"] > 0:
                inventory_value += b["qty"] * b["cost"]
    
    return {
        "cogs": round(total_cogs, 0),
        "inventory_value": round(inventory_value, 0),
        "details": details
    }


if __name__ == "__main__":
    conn = pymysql.connect(host="127.0.0.1", user="root", password="PsVibe@MySQL2024!", database="psvibe_api")
    result = calc_fifo(conn)
    conn.close()
    print(f"COGS: {result['cogs']} Ks")
    print(f"Inventory Value: {result['inventory_value']} Ks")
    print(f"Items sold: {len(result['details'])}")
    for d in result['details']:
        print(f"  {d['item']}: {d['sold_qty']} × {d['cogs']} Ks ({d['note']})")
