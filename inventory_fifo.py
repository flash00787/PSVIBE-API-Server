import mysql.connector
import json
import uuid

DB_CONFIG = {
    'host': '127.0.0.1',
    'user': 'psvibe_user',
    'password': 'PsVibe@2026_Rotated!',
    'database': 'psvibe_api'
}

def _conn():
    return mysql.connector.connect(**DB_CONFIG)

def get_fifo_stock(item_name=None):
    conn = _conn()
    cur = conn.cursor(dictionary=True)
    if item_name:
        cur.execute('SELECT i.item_name, COALESCE(si.qty,0) as stock_in_qty, COALESCE(so.qty,0) as stock_out_qty FROM (SELECT DISTINCT item_name FROM inventory) i LEFT JOIN (SELECT item_name, SUM(quantity) as qty FROM stock_in GROUP BY item_name) si ON i.item_name=si.item_name LEFT JOIN (SELECT item_name, SUM(quantity) as qty FROM stock_out GROUP BY item_name) so ON i.item_name=so.item_name WHERE i.item_name=%s', (item_name,))
    else:
        cur.execute('SELECT i.item_name, COALESCE(si.qty,0) as stock_in_qty, COALESCE(so.qty,0) as stock_out_qty FROM (SELECT DISTINCT item_name FROM inventory) i LEFT JOIN (SELECT item_name, SUM(quantity) as qty FROM stock_in GROUP BY item_name) si ON i.item_name=si.item_name LEFT JOIN (SELECT item_name, SUM(quantity) as qty FROM stock_out GROUP BY item_name) so ON i.item_name=so.item_name')
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        avail = r['stock_in_qty'] - r['stock_out_qty']
        result.append({'item_name': r['item_name'], 'quantity': max(avail, 0), 'stock_in_qty': r['stock_in_qty'], 'stock_out_qty': r['stock_out_qty']})
    return result

def compute_fifo_cost(item_name, qty_to_sell):
    conn = _conn()
    cur = conn.cursor(dictionary=True)

    # Get total already sold (existing stock_out)
    cur.execute('SELECT COALESCE(SUM(quantity), 0) as total_out FROM stock_out WHERE item_name=%s', (item_name,))
    already_sold = int(cur.fetchone()['total_out'] or 0)

    # Get all batches FIFO order
    cur.execute('SELECT id, batch_id, quantity, unit_cost FROM stock_in WHERE item_name=%s AND quantity>0 ORDER BY created_at ASC', (item_name,))
    batches = cur.fetchall()
    conn.close()

    # First, simulate depletion from existing stock-outs
    remaining_prev = already_sold
    available_batches = []
    for b in batches:
        b_qty = int(b['quantity'])
        take_prev = min(remaining_prev, b_qty)
        remaining_avail = b_qty - take_prev
        remaining_prev -= take_prev
        if remaining_avail > 0:
            available_batches.append({'batch_id': b['batch_id'], 'quantity': remaining_avail, 'unit_cost': float(b['unit_cost'])})

    # Now allocate the new sale from remaining available batches
    remaining = qty_to_sell
    total_cost = 0.0
    consumed = []
    for b in available_batches:
        take = min(remaining, b['quantity'])
        if take <= 0:
            continue
        total_cost += take * b['unit_cost']
        consumed.append({'batch_id': b['batch_id'], 'qty_taken': take, 'unit_cost': b['unit_cost']})
        remaining -= take
        if remaining <= 0:
            break

    return {'total_cost': round(total_cost, 2), 'qty_sold': qty_to_sell, 'consumed': consumed, 'shortfall': max(remaining, 0)}

def get_fifo_valuation(item_name=None):
    conn = _conn()
    cur = conn.cursor(dictionary=True)
    cur.execute('SELECT DISTINCT item_name FROM inventory')
    all_items = [r['item_name'] for r in cur.fetchall()]
    if item_name:
        all_items = [item_name]
    result = {'items': [], 'total_inventory_value': 0.0}
    for item in all_items:
        cur.execute('SELECT COALESCE(SUM(quantity), 0) as total_out FROM stock_out WHERE item_name=%s', (item,))
        total_out = int(cur.fetchone()['total_out'] or 0)
        cur.execute('SELECT quantity, unit_cost FROM stock_in WHERE item_name=%s AND quantity>0 ORDER BY created_at ASC', (item,))
        batches = cur.fetchall()
        remaining_out = total_out
        fifo_value = 0.0
        available_qty = 0
        for b in batches:
            b_qty = int(b['quantity'])
            b_cost = float(b['unit_cost'])
            take_out = min(remaining_out, b_qty)
            remaining_qty = b_qty - take_out
            remaining_out -= take_out
            if remaining_qty > 0:
                available_qty += remaining_qty
                fifo_value += remaining_qty * b_cost
        item_data = {'item_name': item, 'quantity': available_qty, 'fifo_value': round(fifo_value, 2)}
        result['items'].append(item_data)
        result['total_inventory_value'] += round(fifo_value, 2)
    conn.close()
    result['total_inventory_value'] = round(result['total_inventory_value'], 2)
    return result

def get_batches(item_name):
    conn = _conn()
    cur = conn.cursor(dictionary=True)
    cur.execute('SELECT id, batch_id, item_name, quantity, unit_cost, total_cost, source, receipt_no, created_at FROM stock_in WHERE item_name=%s ORDER BY created_at ASC', (item_name,))
    batches = cur.fetchall()
    conn.close()
    result = []
    for b in batches:
        result.append({'id': b['id'], 'batch_id': b['batch_id'], 'item_name': b['item_name'], 'quantity': b['quantity'], 'unit_cost': float(b['unit_cost']), 'total_cost': float(b['total_cost']), 'source': b['source'] or '', 'receipt_no': b['receipt_no'] or '', 'created_at': b['created_at'].isoformat() if b['created_at'] else None})
    return result

def add_stock_in(item_name, quantity, unit_cost, source='', receipt_no='', payment_method='', paid_by='', staff_name=''):
    batch_id = 'SI-' + uuid.uuid4().hex[:12].upper()
    conn = _conn()
    cur = conn.cursor(dictionary=True)
    cur.execute('INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)', (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name))
    conn.commit()
    batch_id_ret = cur.lastrowid
    conn.close()
    return {'batch_id': batch_id, 'id': batch_id_ret, 'item_name': item_name, 'quantity': quantity, 'unit_cost': unit_cost}

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'test':
            print(json.dumps(get_fifo_stock(), indent=2, default=str))
            print(json.dumps(get_fifo_valuation(), indent=2, default=str))
        elif cmd == 'stock':
            name = sys.argv[2] if len(sys.argv) > 2 else None
            print(json.dumps(get_fifo_stock(name), indent=2, default=str))
        elif cmd == 'cogs':
            name, qty = sys.argv[2], int(sys.argv[3])
            print(json.dumps(compute_fifo_cost(name, qty), indent=2, default=str))
        elif cmd == 'batches':
            name = sys.argv[2]
            print(json.dumps(get_batches(name), indent=2, default=str))
