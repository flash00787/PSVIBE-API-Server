"""PS VIBE API — Stock & Inventory Routes."""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import get_current_user, get_staff_or_dashboard_user
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete
import dish_consumption as _dish_consume

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Stock"])


# ═══════════════════════════════════════
#  INVENTORY — CRUD
# ═══════════════════════════════════════

@router.get("/dashboard/inventory")
async def dashboard_get_inventory(
    search: str | None = Query(None),
    category: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_staff_or_dashboard_user),
):
    """List inventory items with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("item_name LIKE %s")
            like = f"%{search}%"
            params.append(like)
        if category:
            where.append("category = %s")
            params.append(category)

        sql = f"""
            SELECT id, item_name, category, quantity, unit_price, reorder_level, last_updated
            FROM inventory
            WHERE {' AND '.join(where)}
            ORDER BY quantity DESC, item_name ASC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM inventory WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "item_name": r["item_name"],
                "category": r.get("category"),
                "quantity": r.get("quantity", 0),
                "unit_price": float(r.get("unit_price") or 0),
                "reorder_level": r.get("reorder_level", 0),
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })
        return {"success": True, "data": items, "total": total}
    except Exception as e:
        logger.error(f"GET /inventory error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/inventory")
async def dashboard_create_inventory(req: dict, user: dict = Depends(get_current_user)):
    """Create a new inventory item."""
    try:
        # 🩹 2026-11-05 (Osmo): strip so "Oishi Prawn " never lands in the DB —
        # trailing spaces broke the Stock-In exact-name match.
        item_name = str(req.get("item_name", "") or "").strip()
        if not item_name:
            return {"success": False, "error": "item_name is required"}

        new_id = _mysql_execute(
            """INSERT INTO inventory (item_name, category, quantity, unit_price, reorder_level, last_updated)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (
                item_name,
                req.get("category", ""),
                req.get("quantity", 0),
                req.get("unit_price", 0),
                req.get("reorder_level", 0),
            ),
        )
        return {
            "success": True,
            "data": {
                "id": new_id,
                "item_name": item_name,
                "category": req.get("category", ""),
                "quantity": req.get("quantity", 0),
                "unit_price": req.get("unit_price", 0),
                "reorder_level": req.get("reorder_level", 0),
            },
        }
    except Exception as e:
        logger.error(f"POST /inventory error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/dashboard/inventory/{item_id}")
async def dashboard_update_inventory(item_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update an inventory item."""
    try:
        existing = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not existing:
            return {"success": False, "error": "Inventory item not found"}

        updates = []
        params = []
        # Track old item_name for stock_in/stock_out sync on rename
        old_name = existing.get("item_name", "")
        for field in ["item_name", "category", "quantity", "unit_price", "reorder_level"]:
            if field in req:
                updates.append(f"{field} = %s")
                _v = req[field]
                if field == "item_name":
                    _v = str(_v or "").strip()
                params.append(_v)

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(item_id)
        _mysql_execute(f"UPDATE inventory SET {', '.join(updates)} WHERE id = %s", tuple(params))

        # Sync stock_in/stock_out on rename
        new_name = req.get("item_name", "")
        if old_name and new_name and old_name != new_name:
            _mysql_execute("UPDATE stock_in SET item_name = %s WHERE item_name = %s", (new_name, old_name))
            _mysql_execute("UPDATE stock_out SET item_name = %s WHERE item_name = %s", (new_name, old_name))
            logger.info("dashboard: renamed '%s' -> '%s' (synced stock_in, stock_out)", old_name, new_name)

        updated = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        return {
            "success": True,
            "data": {
                "id": updated["id"],
                "item_name": updated["item_name"],
                "category": updated.get("category"),
                "quantity": updated.get("quantity", 0),
                "unit_price": float(updated.get("unit_price") or 0),
                "reorder_level": updated.get("reorder_level", 0),
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /inventory/{item_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/dashboard/inventory/{item_id}")
async def dashboard_delete_inventory(item_id: int, user: dict = Depends(get_current_user)):
    """Delete an inventory item."""
    try:
        existing = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not existing:
            return {"success": False, "error": "Inventory item not found"}

        _mysql_delete("DELETE FROM inventory WHERE id = %s", (item_id,))
        return {"success": True, "data": {"deleted": item_id}}
    except Exception as e:
        logger.error(f"DELETE /inventory/{item_id} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  STOCK IN
# ═══════════════════════════════════════

@router.post("/dashboard/stock-in")
async def dashboard_stock_in(req: dict, user: dict = Depends(get_current_user)):
    """Record stock-in for an inventory item."""
    try:
        item_id = req.get("item_id")
        try:
            quantity = round(float(req.get("quantity", 0) or 0), 2)  # 0.9 L etc. (owner Osmo 2026-09-29)
        except (TypeError, ValueError):
            quantity = 0
        unit_cost = req.get("unit_cost", 0)
        source = req.get("source", "")
        receipt_no = req.get("receipt_no", "")
        payment_method = req.get("payment_method", "")
        paid_by = req.get("paid_by", "")
        staff_name = req.get("staff_name", "")

        if not item_id or quantity <= 0:
            return {"success": False, "error": "item_id and positive quantity are required"}

        # Fetch item
        item = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not item:
            return {"success": False, "error": "Inventory item not found"}

        batch_id = "SI-" + uuid.uuid4().hex[:12].upper()

        # Insert into stock_in
        sid = _mysql_execute(
            """INSERT INTO stock_in (batch_id, item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (batch_id, item["item_name"], quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name)
        )
        _dish_consume.ensure_layer_for_stock_in(sid)

        # Update inventory quantity (decimal-safe — owner Osmo 2026-09-29: 0.9 L oil)
        new_qty = round(float(item["quantity"] or 0) + float(quantity), 2)
        _mysql_execute(
            "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
            (new_qty, item_id)
        )

        # Record cash_movements eject for payment
        total_cost_val = quantity * unit_cost
        if payment_method and total_cost_val > 0:
            note = f"Stock In: {item['item_name']} x{quantity}"
            _mysql_execute(
                "INSERT INTO cash_movements (movement_type, account, amount, note, staff_name, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                ("eject", payment_method, total_cost_val, note, staff_name)
            )

        return {
            "success": True,
            "data": {
                "batch_id": batch_id,
                "item_name": item["item_name"],
                "quantity_added": quantity,
                "new_quantity": new_qty,
                "unit_cost": unit_cost,
                "payment_method": payment_method,
                "paid_by": paid_by,
                "staff_name": staff_name,
            }
        }
    except Exception as e:
        logger.error(f"POST /stock-in error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/dashboard/stock-in/{entry_id}")
async def dashboard_update_stock_in(entry_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a stock-in record."""
    try:
        existing = _mysql_query_one("SELECT * FROM stock_in WHERE id = %s", (entry_id,))
        if not existing:
            return {"success": False, "error": "Stock-in record not found"}

        item_name = req.get("item_name", existing.get("item_name"))
        quantity = req.get("quantity", existing.get("quantity"))
        unit_cost = req.get("unit_cost", existing.get("unit_cost"))
        source = req.get("source", existing.get("source"))
        receipt_no = req.get("receipt_no", existing.get("receipt_no"))
        payment_method = req.get("payment_method", existing.get("payment_method"))
        paid_by = req.get("paid_by", existing.get("paid_by"))
        staff_name = req.get("staff_name", existing.get("staff_name"))

        old_qty = round(float(existing.get("quantity") or 0), 2)
        new_qty = round(float(quantity or 0), 2)

        # Reverse old inventory
        old_item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (existing["item_name"],))
        if old_item:
            old_inv_qty = max(0, round(float(old_item["quantity"] or 0) - old_qty, 2))
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (old_inv_qty, old_item["id"])
            )

        # Apply new inventory
        new_item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (item_name,))
        if new_item:
            new_inv_qty = round(float(new_item["quantity"] or 0) + new_qty, 2)
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (new_inv_qty, new_item["id"])
            )

        _mysql_execute(
            """UPDATE stock_in SET item_name=%s, quantity=%s, unit_cost=%s, source=%s,
               receipt_no=%s, payment_method=%s, paid_by=%s, staff_name=%s WHERE id=%s""",
            (item_name, quantity, unit_cost, source, receipt_no, payment_method, paid_by, staff_name, entry_id)
        )

        return {"success": True, "data": {"id": entry_id, "updated": item_name}}
    except Exception as e:
        logger.error(f"PUT /stock-in/{entry_id} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  STOCK OUT
# ═══════════════════════════════════════

@router.post("/dashboard/stock-out")
async def dashboard_stock_out(req: dict, request: Request, user: dict = Depends(get_staff_or_dashboard_user)):
    """Record stock-out (deduction) for an inventory item.

    Staff-allowed (staff JWT or dashboard user). The acting staff name is recorded
    on the stock_out row for the audit trail."""
    try:
        item_id = req.get("item_id")
        try:
            quantity = round(float(req.get("quantity", 0) or 0), 2)  # decimal-safe (owner Osmo 2026-09-29)
        except (TypeError, ValueError):
            quantity = 0
        unit_price = req.get("unit_price", 0)
        notes = req.get("notes", "")
        try:
            import staff_audit
            _actor = staff_audit.identity_from_request(request, req)
        except Exception:
            _actor = ""

        if not item_id or quantity <= 0:
            return {"success": False, "error": "item_id and positive quantity are required"}

        # Fetch item
        item = _mysql_query_one("SELECT * FROM inventory WHERE id = %s", (item_id,))
        if not item:
            return {"success": False, "error": "Inventory item not found"}

        current_qty = round(float(item["quantity"] or 0), 2)
        if quantity > current_qty + 1e-9:
            return {"success": False, "error": f"Not enough stock. Available: {current_qty}"}

        # Insert into stock_out
        total_amount = quantity * unit_price
        sid = _mysql_execute(
            """INSERT INTO stock_out (item_name, quantity, unit_price, total, sale_date, notes, staff_name)
               VALUES (%s, %s, %s, %s, NOW(), %s, %s)""",
            (item["item_name"], quantity, unit_price, total_amount, notes, _actor or None)
        )
        _dish_consume.consume_after_stock_out(item["item_name"], quantity, sid)

        # Update inventory quantity
        new_qty = round(current_qty - float(quantity), 2)
        _mysql_execute(
            "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
            (new_qty, item_id)
        )

        return {
            "success": True,
            "data": {
                "item_name": item["item_name"],
                "quantity_deducted": quantity,
                "new_quantity": new_qty,
                "unit_price": unit_price,
                "total": total_amount,
            }
        }
    except Exception as e:
        logger.error(f"POST /stock-out error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/dashboard/stock-in")
async def dashboard_get_stock_in(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List stock-in records."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(item_name LIKE %s OR batch_id LIKE %s OR source LIKE %s OR staff_name LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])

        sql = f"""
            SELECT id, batch_id, item_name, quantity, unit_cost, source,
                   receipt_no, payment_method, paid_by, staff_name, created_at
            FROM stock_in
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM stock_in WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "batch_id": r.get("batch_id"),
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit_cost": float(r.get("unit_cost") or 0),
                "source": r.get("source"),
                "receipt_no": r.get("receipt_no"),
                "payment_method": r.get("payment_method"),
                "paid_by": r.get("paid_by"),
                "staff_name": r.get("staff_name"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /stock-in error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/dashboard/stock-in/{entry_id}")
async def dashboard_delete_stock_in(entry_id: int, user: dict = Depends(get_current_user)):
    """Delete a stock-in record and reverse inventory quantity."""
    try:
        existing = _mysql_query_one("SELECT * FROM stock_in WHERE id = %s", (entry_id,))
        if not existing:
            return {"success": False, "error": "Stock-in record not found"}

        # Reverse inventory: find item by name and subtract quantity
        item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (existing["item_name"],))
        if item:
            new_qty = max(0, int(item["quantity"] or 0) - int(existing["quantity"] or 0))
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (new_qty, item["id"])
            )

        _mysql_delete("DELETE FROM stock_in WHERE id = %s", (entry_id,))
        return {"success": True, "data": {"deleted": entry_id}}
    except Exception as e:
        logger.error(f"DELETE /stock-in/{entry_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/dashboard/stock-out")
async def dashboard_get_stock_out(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_staff_or_dashboard_user),
):
    """List stock-out records (staff-allowed, read-only)."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(item_name LIKE %s OR staff_name LIKE %s OR notes LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like])

        sql = f"""
            SELECT id, item_name, quantity, unit_price, total, sale_date,
                   staff_name, notes, created_at
            FROM stock_out
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM stock_out WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit_price": float(r.get("unit_price") or 0),
                "total": float(r.get("total") or 0),
                "sale_date": str(r["sale_date"]) if r.get("sale_date") else None,
                "staff_name": r.get("staff_name"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /stock-out error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/food-cart/booking/{booking_id}")
async def api_food_cart_by_booking(booking_id: str, auth=Depends(get_staff_or_dashboard_user)):
    """Fetch food_cart items for a booking (session food order) — staff webapp + dashboard."""
    try:
        rows = _mysql_query(
            "SELECT id, booking_id, item_name, quantity, unit_price, created_at, fulfilled_at "
            "FROM food_cart WHERE booking_id=%s ORDER BY id ASC",
            (booking_id,))
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "booking_id": r.get("booking_id"),
                "item_name": r.get("item_name"),
                "quantity": int(r.get("quantity") or 0),
                "unit_price": float(r.get("unit_price") or 0),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "items": items}
    except Exception as e:
        logger.error(f"GET /food-cart/booking/{booking_id} error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/food-orders/active")
async def api_food_orders_active(auth=Depends(get_staff_or_dashboard_user)):
    """List ACTIVE console sessions with their CURRENT food carts.

    An active session's food_cart = food still to be charged (charging happens at
    session end via the sale voucher). fulfilled_at is only a stock-hold marker
    (always set = created_at shortly after creation), so we include ALL cart rows
    for active sessions rather than filtering on fulfilled_at.
    Used by Mini App '🍔 Food Orders' page (view + edit active food orders).
    """
    try:
        sql = """
            SELECT
                cs.id            AS session_id,
                cs.booking_id    AS booking_id,
                cs.console_id    AS console_id,
                COALESCE(NULLIF(cs.customer_name, ''), NULLIF(cs.member_id, ''), 'Guest') AS customer_name,
                fc.item_name     AS item_name,
                fc.quantity      AS quantity,
                fc.unit_price    AS unit_price
            FROM console_sessions cs
            JOIN food_cart fc ON fc.booking_id = cs.booking_id
            WHERE cs.status = 'active'
            ORDER BY cs.id ASC, fc.id ASC
        """
        rows = _mysql_query(sql)

        sessions = {}
        order = []
        for r in rows:
            sid = r["session_id"]
            key = str(sid)
            if key not in sessions:
                sessions[key] = {
                    "session_id": sid,
                    "booking_id": r.get("booking_id"),
                    "console_id": r.get("console_id"),
                    "customer_name": r.get("customer_name") or "Guest",
                    "items": [],
                    "total": 0,
                }
                order.append(key)
            qty = int(r.get("quantity") or 0)
            price = float(r.get("unit_price") or 0)
            sessions[key]["items"].append({
                "item_name": r.get("item_name"),
                "quantity": qty,
                "unit_price": price,
            })
            sessions[key]["total"] += qty * price

        entries = [sessions[k] for k in order]
        total = len(entries)
        logger.info(f"GET /food-orders/active: returning {total} active session(s) with pending food")
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /food-orders/active error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/dashboard/food-orders")
async def dashboard_get_food_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List food orders linked to console sessions (from food_cart)."""
    try:
        sql = """
            SELECT fc.id, fc.booking_id, fc.item_name, fc.quantity, fc.unit_price,
                   (fc.quantity * fc.unit_price) as line_total,
                   fc.created_at,
                   fc.fulfilled_at,
                   cb.console_id, cb.member_id, cb.status as booking_status,
                   cb.planned_start AS start_time
            FROM food_cart fc
            JOIN console_bookings cb ON CAST(fc.booking_id AS UNSIGNED) = cb.id
            ORDER BY fc.id DESC
            LIMIT %s OFFSET %s
        """
        rows = _mysql_query(sql, (limit, offset))
        count_row = _mysql_query_one("SELECT COUNT(*) as total FROM food_cart")
        total = count_row["total"] if count_row else 0

        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "booking_id": r.get("booking_id"),
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit_price": float(r.get("unit_price") or 0),
                "line_total": float(r.get("line_total") or 0),
                "console_id": r.get("console_id"),
                "member_id": r.get("member_id"),
                "booking_status": r.get("booking_status"),
                "start_time": str(r["start_time"]) if r.get("start_time") else None,
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
                "fulfilled_at": str(r["fulfilled_at"]) if r.get("fulfilled_at") else None,
            })
        logger.info(f"GET /food-orders: returning {len(entries)} entries, total={total}")
        return {"success": True, "data": entries, "total": total}
    except Exception as e:
        logger.error(f"GET /food-orders error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/dashboard/stock-out/{entry_id}")
async def dashboard_delete_stock_out(entry_id: int, user: dict = Depends(get_current_user)):
    """Delete a stock-out record and restore inventory quantity."""
    try:
        existing = _mysql_query_one("SELECT * FROM stock_out WHERE id = %s", (entry_id,))
        if not existing:
            return {"success": False, "error": "Stock-out record not found"}

        # Restore inventory: find item by name and add quantity back
        item = _mysql_query_one("SELECT * FROM inventory WHERE item_name = %s", (existing["item_name"],))
        if item:
            new_qty = int(item["quantity"] or 0) + int(existing["quantity"] or 0)
            _mysql_execute(
                "UPDATE inventory SET quantity = %s, last_updated = NOW() WHERE id = %s",
                (new_qty, item["id"])
            )

        _mysql_delete("DELETE FROM stock_out WHERE id = %s", (entry_id,))
        return {"success": True, "data": {"deleted": entry_id}}
    except Exception as e:
        logger.error(f"DELETE /stock-out/{entry_id} error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  INVENTORY ALERTS
# ═══════════════════════════════════════

@router.get("/dashboard/inventory/alerts")
async def dashboard_inventory_alerts(
    user: dict = Depends(get_staff_or_dashboard_user),
):
    """Get inventory items below reorder level."""
    try:
        rows = _mysql_query("""
            SELECT id, item_name, category, quantity, unit_price, reorder_level, last_updated
            FROM inventory
            WHERE quantity <= reorder_level AND reorder_level > 0
            ORDER BY (reorder_level - quantity) DESC, item_name ASC
        """)

        items = []
        for r in rows:
            shortage = int(r.get("reorder_level", 0)) - int(r.get("quantity", 0))
            items.append({
                "id": r["id"],
                "item_name": r["item_name"],
                "category": r.get("category"),
                "quantity": r.get("quantity", 0),
                "unit_price": float(r.get("unit_price") or 0),
                "reorder_level": r.get("reorder_level", 0),
                "shortage": shortage,
                "status": "critical" if shortage >= int(r.get("reorder_level", 0)) else "low",
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })

        return {"success": True, "data": items, "total": len(items)}
    except Exception as e:
        logger.error(f"GET /inventory/alerts error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/stock/lots")
async def stock_lots(item_name: str | None = Query(None), limit: int = Query(400, ge=1, le=2000),
                     user: dict = Depends(get_staff_or_dashboard_user)):
    """Stock-in history BY LOT — owner 2026-09-29 Osmo: "Stock in history ကို Lot အလိုက်ပြပေးပါ".

    One entry per received lot (``stock_in`` row) carrying the FIFO layer's REMAINING qty
    from ``stock_layers`` (matched on ``stock_in_id``; a lot with no layer row falls back to
    the full received qty).  Also returns the same data grouped per item with totals, so the
    app can show "Lactasoy — 2 lots · received 12 · left 6 · value 18,000 Ks".
    """
    try:
        where = ["1=1"]
        params = []
        if item_name:
            where.append("si.item_name = %s")
            params.append(item_name)
        sql = (
            "SELECT si.id AS stock_in_id, si.batch_id, si.item_name, si.quantity, si.unit_cost, "
            "si.source, si.receipt_no, si.payment_method, si.staff_name, si.created_at, "
            "sl.qty_remaining, sl.purchased_at "
            "FROM stock_in si LEFT JOIN stock_layers sl ON sl.stock_in_id = si.id "
            "WHERE " + " AND ".join(where) + " ORDER BY si.id DESC LIMIT %s"
        )
        params.append(int(limit))
        rows = _mysql_query(sql, tuple(params)) or []

        lots = []
        groups = {}
        for r in rows:
            qty_in = float(r.get("quantity") or 0)
            rem_raw = r.get("qty_remaining")
            rem = float(rem_raw) if rem_raw is not None else qty_in
            unit = float(r.get("unit_cost") or 0)
            lot = {
                "stock_in_id": r.get("stock_in_id"),
                "batch_id": r.get("batch_id"),
                "item_name": r.get("item_name"),
                "qty_in": qty_in,
                "qty_remaining": rem,
                "unit_cost": unit,
                "value_remaining": round(rem * unit, 2),
                "source": r.get("source"),
                "receipt_no": r.get("receipt_no"),
                "payment_method": r.get("payment_method"),
                "staff_name": r.get("staff_name"),
                "received_at": str(r.get("created_at") or r.get("purchased_at") or ""),
            }
            lots.append(lot)
            g = groups.setdefault(lot["item_name"], {
                "item_name": lot["item_name"], "lots": [],
                "qty_in": 0.0, "qty_remaining": 0.0, "value_remaining": 0.0,
            })
            g["lots"].append(lot)
            g["qty_in"] += qty_in
            g["qty_remaining"] += rem
            g["value_remaining"] += lot["value_remaining"]

        by_item = sorted(groups.values(), key=lambda g: str(g["item_name"]))
        for g in by_item:
            g["qty_in"] = round(g["qty_in"], 2)
            g["qty_remaining"] = round(g["qty_remaining"], 2)
            g["value_remaining"] = round(g["value_remaining"], 2)
            g["lot_count"] = len(g["lots"])

        # ── BY VOUCHER (owner 2026-09-29 Osmo: "Voucher အလိုက်") ──
        # A multi-item stock-in writes one row per item sharing a batch prefix
        # ("SI-4FE895B38464-01" … "-08"); single-item stock-ins have no "-NN" suffix.
        # The voucher is therefore the batch id minus a trailing numeric index.
        def _voucher_key(bid):
            b = str(bid or "")
            head, sep, tail = b.rpartition("-")
            return head if (sep and tail.isdigit() and head) else b

        vgroups = {}
        for lot in lots:
            key = _voucher_key(lot.get("batch_id"))
            v = vgroups.setdefault(key, {
                "voucher": key, "received_at": lot.get("received_at"),
                "staff_name": lot.get("staff_name"), "payment_method": lot.get("payment_method"),
                "source": lot.get("source"), "receipt_no": lot.get("receipt_no"),
                "item_count": 0, "total_cost": 0.0, "qty_in": 0.0,
                "qty_remaining": 0.0, "value_remaining": 0.0, "items": [],
            })
            line_total = round(float(lot.get("qty_in") or 0) * float(lot.get("unit_cost") or 0), 2)
            v["items"].append({
                "stock_in_id": lot.get("stock_in_id"),
                "item_name": lot.get("item_name"),
                "batch_id": lot.get("batch_id"),
                "qty_in": lot.get("qty_in"),
                "qty_remaining": lot.get("qty_remaining"),
                "unit_cost": lot.get("unit_cost"),
                "line_total": line_total,
                "value_remaining": lot.get("value_remaining"),
            })
            v["item_count"] += 1
            v["total_cost"] += line_total
            v["qty_in"] += float(lot.get("qty_in") or 0)
            v["qty_remaining"] += float(lot.get("qty_remaining") or 0)
            v["value_remaining"] += float(lot.get("value_remaining") or 0)
            if not v.get("received_at"):
                v["received_at"] = lot.get("received_at")

        by_voucher = sorted(vgroups.values(), key=lambda v: str(v.get("received_at") or ""), reverse=True)
        for v in by_voucher:
            v["total_cost"] = round(v["total_cost"], 2)
            v["qty_in"] = round(v["qty_in"], 2)
            v["qty_remaining"] = round(v["qty_remaining"], 2)
            v["value_remaining"] = round(v["value_remaining"], 2)

        return {"success": True, "data": {"lots": lots, "by_item": by_item,
                                          "by_voucher": by_voucher, "count": len(lots)}}
    except Exception as e:
        logger.error(f"GET /stock/lots error: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  FIX A WRONG STOCK-IN (owner 2026-09-29 Osmo: "Stock in မှာ မှားထည့်မိရင်
#  ဘယ်လိုပြင်ရမလဲ")
#
#  Undo is a FULL reversal — the FIFO layer, the inventory quantity, the payment
#  record (cash_movements) and the stock_in row — so stock and money stay consistent.
#  A lot that has already been (partly) consumed/sold is REFUSED: that stock has left
#  the shelf and reversing it would rewrite COGS/sales history; the app can pass
#  force=1 only after the owner explicitly confirms.
# ─────────────────────────────────────────────────────────────────────────────

def _undo_stock_in_row(row: dict) -> dict:
    """Delete one ``stock_in`` row and reverse everything it caused."""
    sid = row.get("id")
    name = row.get("item_name")
    qty = float(row.get("quantity") or 0)
    unit = float(row.get("unit_cost") or 0)
    total = round(qty * unit, 2)
    batch = str(row.get("batch_id") or "")
    voucher = batch.rpartition("-")[0] if batch.rpartition("-")[2].isdigit() else batch
    out = {"stock_in_id": sid, "item_name": name, "qty": qty, "layers_removed": 0,
           "cash_removed": 0, "inventory_before": None, "inventory_after": None}

    try:
        out["layers_removed"] = int(_mysql_delete("DELETE FROM stock_layers WHERE stock_in_id=%s", (sid,)) or 0)
    except Exception:
        pass

    inv = _mysql_query_one("SELECT id, quantity FROM inventory WHERE item_name=%s", (name,))
    if inv:
        before = float(inv.get("quantity") or 0)
        after = max(0.0, before - qty)
        _mysql_execute("UPDATE inventory SET quantity=%s, last_updated=NOW() WHERE id=%s", (after, inv["id"]))
        out["inventory_before"] = before
        out["inventory_after"] = after

    # Reverse the payment row this stock-in created (single-item note, or the batch note).
    if total > 0:
        qty_txt = str(int(qty)) if float(qty).is_integer() else str(qty)
        for note in ("Stock In: %s x%s" % (name, qty_txt), "Stock In (batch): %s" % voucher):
            try:
                cm = _mysql_query_one(
                    "SELECT id, account FROM cash_movements WHERE movement_type='eject' AND note=%s AND amount=%s "
                    "ORDER BY id DESC LIMIT 1", (note, total))
                if cm:
                    # Audit-safe reversal: keep the original eject and post the opposite
                    # movement, so the payment history stays traceable (Osmo 2026-10-21).
                    _mysql_execute(
                        "INSERT INTO cash_movements (branch_id, movement_type, account, amount, note, staff_name) "
                        "VALUES (1, 'inject', %s, %s, %s, %s)",
                        (cm.get("account") or "Main (ACM)", total,
                         "Stock In deleted (reversal of #%s): %s" % (cm.get("id"), note),
                         row.get("staff_name") or "system"))
                    out["cash_reversed"] = int(total)
                    out["cash_reversed_from"] = int(cm.get("id") or 0)
                    break
            except Exception:
                pass

    _mysql_delete("DELETE FROM stock_in WHERE id=%s", (sid,))
    logger.info("stock-in UNDONE id=%s item=%r qty=%s layers=%s cash=%s",
                sid, name, qty, out["layers_removed"], out["cash_removed"])
    return out


def _lot_usage(row: dict):
    """(received, remaining, used) for a stock_in row, layer-aware."""
    qty_in = float(row.get("quantity") or 0)
    layers = _mysql_query("SELECT qty_remaining FROM stock_layers WHERE stock_in_id=%s", (row.get("id"),)) or []
    rem = sum(float(l.get("qty_remaining") or 0) for l in layers) if layers else qty_in
    return qty_in, rem, max(0.0, qty_in - rem)


@router.delete("/stock/lot/{stock_in_id}")
async def delete_stock_lot(stock_in_id: int, force: int = Query(0),
                           user: dict = Depends(get_staff_or_dashboard_user)):
    """Undo ONE stock-in line (mistake correction). Refuses a (partly) consumed lot."""
    try:
        row = _mysql_query_one("SELECT * FROM stock_in WHERE id=%s", (stock_in_id,))
        if not row:
            raise HTTPException(status_code=404, detail={"success": False, "error": "Stock-in not found", "code": 404})
        qty_in, rem, used = _lot_usage(row)
        if used > 0.001 and not int(force or 0):
            raise HTTPException(status_code=409, detail={
                "success": False, "code": 409, "reason": "partly_used",
                "qty_in": qty_in, "qty_remaining": rem, "qty_used": used, "suggest": "force_undo",
                "error": "'%s' ရဲ့ ဒီ lot က %s လုံး ရောင်း/သုံးပြီးသား (လက်ကျန် %s) — ဖျက်လို့ မရဘူး။ "
                         "ဖျက်ရင် အတိတ် food-cost / ရောင်းမှတ်တမ်း ပျက်မယ်။" % (row.get("item_name"), used, rem),
            })
        res = _undo_stock_in_row(row)
        return {"success": True, "data": res}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DELETE /stock/lot/{stock_in_id} error: {e}")
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e), "code": 500})


@router.delete("/stock/voucher/{voucher}")
async def delete_stock_voucher(voucher: str, force: int = Query(0),
                               user: dict = Depends(get_staff_or_dashboard_user)):
    """Undo a WHOLE stock-in voucher (every line of one purchase) — mistake correction."""
    try:
        rows = _mysql_query(
            "SELECT * FROM stock_in WHERE batch_id=%s OR batch_id LIKE %s ORDER BY id",
            (voucher, voucher + "-%")) or []
        if not rows:
            raise HTTPException(status_code=404, detail={"success": False, "error": "Voucher not found", "code": 404})
        blocked = []
        for r in rows:
            _qi, _rem, _used = _lot_usage(r)
            if _used > 0.001:
                blocked.append({"item_name": r.get("item_name"), "qty_used": _used, "qty_remaining": _rem})
        if blocked and not int(force or 0):
            raise HTTPException(status_code=409, detail={
                "success": False, "code": 409, "reason": "partly_used", "blocked": blocked, "suggest": "force_undo",
                "error": "ဒီ voucher ထဲက ပစ္စည်း %d မျိုး ရောင်း/သုံးပြီးသား — ဖျက်လို့ မရဘူး။" % len(blocked),
            })
        results = [_undo_stock_in_row(r) for r in rows]
        return {"success": True, "data": {"voucher": voucher, "lines": len(results), "results": results}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DELETE /stock/voucher/{voucher} error: {e}")
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e), "code": 500})


# ── Unit lookup for the Stock In screen (owner 2026-11-06) ─────────────
@router.get("/stock/units")
async def stock_units(user: dict = Depends(get_staff_or_dashboard_user)):
    """Purchase-unit map for every ingredient — staff readable.

    The Stock In screen needs to show "Buy unit: liter · 1 liter = 1000 ml"
    BEFORE anything is saved, and /api/menu/* is admin-only, so staff got no
    units at all. This is a thin read of the ingredients table.
    """
    try:
        rows = _mysql_query(
            "SELECT name, kind, base_unit, purchase_unit, base_per_purchase "
            "FROM ingredients WHERE branch_id = 1 ORDER BY name"
        ) or []
        items = []
        for r in rows:
            items.append({
                "name": r.get("name"),
                "kind": (r.get("kind") or "").strip().lower(),
                "base_unit": r.get("base_unit") or "",
                "purchase_unit": r.get("purchase_unit") or "",
                "base_per_purchase": float(r.get("base_per_purchase") or 0),
            })
        return {"success": True, "data": {"items": items}, "total": len(items)}
    except Exception as e:
        logger.error(f"GET /stock/units error: {e}")
        return {"success": False, "error": str(e), "data": {"items": []}}



# ── Physical count adjustment (owner 2026-11-06) ────────────────────────
@router.post("/stock/physical-count")
async def stock_physical_count(req: dict, user: dict = Depends(get_staff_or_dashboard_user)):
    """Set an ingredient's on-hand to the physically counted quantity.

    Adjusts ONLY the FIFO ledger (stock_layers): a shortage consumes the oldest
    layers first, a surplus appends an adjustment layer at the last known cost.
    Stock adjustments are admin-only.
    """
    role = str((user or {}).get("role") or "").strip().lower()
    if role != "admin":
        raise HTTPException(status_code=403, detail="Physical count is admin-only.")
    name = str(req.get("item_name") or "").strip()
    try:
        actual = float(req.get("actual_qty"))
    except Exception:
        return {"success": False, "error": "actual_qty must be a number"}
    if not name:
        return {"success": False, "error": "item_name is required"}
    try:
        layers = _mysql_query(
            "SELECT id, qty_remaining, unit_cost FROM stock_layers "
            "WHERE branch_id = 1 AND item_name = %s AND qty_remaining > 0 "
            "ORDER BY purchased_at ASC, id ASC",
            (name,),
        ) or []
        before = sum(float(l.get("qty_remaining") or 0) for l in layers)
        diff = round(actual - before, 4)
        if abs(diff) < 0.0001:
            return {"success": True, "data": {"item_name": name, "before": before, "after": actual, "diff": 0}}
        if diff < 0:
            need = abs(diff)
            for l in layers:
                if need <= 0:
                    break
                have = float(l.get("qty_remaining") or 0)
                take = min(have, need)
                _mysql_execute(
                    "UPDATE stock_layers SET qty_remaining = qty_remaining - %s WHERE id = %s",
                    (take, l["id"]),
                )
                need -= take
        else:
            last = _mysql_query_one(
                "SELECT unit_cost FROM stock_layers WHERE branch_id = 1 AND item_name = %s "
                "ORDER BY purchased_at DESC, id DESC LIMIT 1",
                (name,),
            ) or {}
            _mysql_execute(
                "INSERT INTO stock_layers (branch_id, item_name, qty_in, qty_remaining, unit_cost, purchased_at) "
                "VALUES (1, %s, %s, %s, %s, NOW())",
                (name, diff, diff, float(last.get("unit_cost") or 0)),
            )
        logger.info("physical count %s: before=%s actual=%s diff=%s by %s",
                    name, before, actual, diff, (user or {}).get("username"))
        return {"success": True, "data": {
            "item_name": name, "before": before, "after": actual, "diff": diff,
            "staff": (user or {}).get("username", ""),
        }}
    except Exception as e:
        logger.error(f"POST /stock/physical-count error: {e}")
        return {"success": False, "error": str(e)}



# ── Unified stock alerts (owner 2026-11-06) ────────────────────────────
@router.get("/alerts/stock")
async def stock_alerts(user: dict = Depends(get_staff_or_dashboard_user)):
    """One alert feed for BOTH apps: inventory low/out + ingredients out.

    The old /dashboard/inventory/alerts only looked at reorder_level, and only
    3 of 69 items have one — so it was almost always empty. This also counts
    zero-stock rows and ingredient FIFO layers so the badge means something.
    """
    try:
        items = []
        inv = _mysql_query(
            "SELECT item_name, category, quantity, COALESCE(reorder_level,0) AS reorder_level, unit_price "
            "FROM inventory WHERE branch_id = 1 "
            "AND COALESCE(item_type, '') <> 'dish' "
            "AND COALESCE(is_active, 1) = 1 "
            "AND (quantity <= 0 OR (reorder_level > 0 AND quantity <= reorder_level)) "
            "ORDER BY (quantity <= 0) DESC, item_name"
        ) or []
        for r in inv:
            qty = float(r.get("quantity") or 0)
            lvl = float(r.get("reorder_level") or 0)
            items.append({
                "kind": "inventory",
                "name": r.get("item_name"),
                "category": r.get("category") or "",
                "qty": qty,
                "unit": "pcs",
                "reorder_level": lvl,
                "status": "out" if qty <= 0 else "low",
                "shortage": max(0.0, lvl - qty) if lvl > 0 else 0.0,
            })
        try:
            # Ingredients: ONLY the ingredient master (kind=ingredient), canonicalised
            # through ingredient_variants, never a product or a dish (owner 2026-11-06:
            # "inactive item တွေပါပြနေတယ် / count မမှန်ဘူး").
            master = _mysql_query(
                "SELECT name FROM ingredients WHERE branch_id = 1 "
                "AND COALESCE(kind, 'ingredient') = 'ingredient'"
            ) or []
            variants = {}
            for v in (_mysql_query(
                "SELECT canonical_name, variant_name FROM ingredient_variants WHERE branch_id = 1"
            ) or []):
                variants[(v.get("variant_name") or "").strip()] = (v.get("canonical_name") or "").strip()
            layers = _mysql_query(
                "SELECT item_name, SUM(qty_remaining) AS qty, SUM(qty_in) AS bought "
                "FROM stock_layers WHERE branch_id = 1 GROUP BY item_name"
            ) or []
            agg = {}
            for l in layers:
                raw = (l.get("item_name") or "").strip()
                canon = variants.get(raw, raw)
                slot = agg.setdefault(canon, {"qty": 0.0, "bought": 0.0})
                slot["qty"] += float(l.get("qty") or 0)
                slot["bought"] += float(l.get("bought") or 0)
            inv_names = {(r.get("item_name") or "").strip() for r in (_mysql_query(
                "SELECT item_name FROM inventory WHERE branch_id = 1"
            ) or [])}
            seen_ing = set()
            for m in master:
                name = (m.get("name") or "").strip()
                if not name or name in inv_names:
                    continue                      # sellable product → not an ingredient alert
                canon = variants.get(name, name)
                if canon in seen_ing:
                    continue
                seen_ing.add(canon)
                slot = agg.get(canon) or agg.get(name) or {"qty": 0.0, "bought": 0.0}
                qty = slot["qty"]
                bought = slot["bought"]
                if qty <= 0:
                    status = "out"
                elif bought > 0 and qty <= bought * 0.25:
                    status = "low"
                else:
                    continue
                items.append({
                    "kind": "ingredient", "name": name, "category": "Ingredients",
                    "qty": round(qty, 2), "unit": "", "reorder_level": 0,
                    "status": status, "shortage": 0.0, "purchased": round(bought, 2),
                })
        except Exception:
            pass
        products = [i for i in items if i.get("kind") == "inventory"]
        ingredients = [i for i in items if i.get("kind") == "ingredient"]
        return {"success": True, "data": {
            "items": items,
            "count": len(items),
            "out": sum(1 for i in items if i["status"] == "out"),
            "product_count": len(products),
            "ingredient_count": len(ingredients),
            "product_out": sum(1 for i in products if i["status"] == "out"),
            "ingredient_out": sum(1 for i in ingredients if i["status"] == "out"),
        }}
    except Exception as e:
        logger.error(f"GET /alerts/stock error: {e}")
        return {"success": False, "error": str(e), "data": {"items": [], "count": 0, "out": 0}}

