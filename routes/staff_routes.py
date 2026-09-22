"""PS VIBE API — Staff & Salary Routes."""
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, HTTPException

from auth import get_current_user, require_admin, get_staff_or_dashboard_user


async def require_admin_clean(user: dict = Depends(get_staff_or_dashboard_user)) -> dict:
    """Admin gate that accepts both dashboard + staff JWTs and returns a clean 403.

    Used for payroll-adjacent endpoints so the Staff App can show an
    'owner-only' message instead of a confusing 401.
    """
    if str((user or {}).get("role") or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Owner-only action — ask an admin to do this")
    return user
from mysql_db import query as _mysql_query, query_one as _mysql_query_one, execute as _mysql_execute, delete_rows as _mysql_delete

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Staff"])


@router.get("/dashboard/opex")
async def dashboard_opex_list(
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    user: dict = Depends(get_current_user),
):
    from mysql_db import query as _mq
    from datetime import datetime as _dt, timedelta as _td
    import calendar
    _mmt_now = _dt.utcnow() + _td(hours=6, minutes=30)
    # Default to current month if no date filter provided
    if not date_from and not date_to:
        date_from = _mmt_now.strftime("%Y-%m-01")
        _last_day = calendar.monthrange(_mmt_now.year, _mmt_now.month)[1]
        date_to = _mmt_now.replace(day=_last_day).strftime("%Y-%m-%d")
    sql = "SELECT * FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    if search:
        sql += " AND (category LIKE %s OR description LIKE %s)"
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    sql += " ORDER BY expense_date DESC, created_at DESC LIMIT 200"
    rows = _mq(sql, tuple(params)) if params else _mq(sql)
    return {"success": True, "data": list(rows)}


@router.post("/dashboard/opex")
async def dashboard_opex_add(body: dict = {}, user: dict = Depends(require_admin_clean)):
    """Add an operating expense (owner/admin only — owner 2026-09-20 17:00 "admin = owner")."""
    from mysql_db import execute as _me
    from datetime import datetime, timedelta
    _mmt = timedelta(hours=6, minutes=30)
    _today = (datetime.utcnow() + _mmt).strftime("%Y-%m-%d")
    cat = body.get("category", "").strip()
    desc = body.get("description", "").strip()
    amt = int(body.get("amount", 0))
    pmt = body.get("payment_method", "Cash").strip()
    dt = body.get("expense_date", _today).strip()
    if not cat or amt <= 0:
        return {"success": False, "error": "Category and amount required"}
    _me(
        "INSERT INTO opex (category,description,amount,payment_method,recorded_by,expense_date) VALUES (%s,%s,%s,%s,%s,%s)",
        (cat, desc, amt, pmt, user.get("username", "admin"), dt),
    )
    return {"success": True, "data": {"msg": f"{cat}: {amt:,} Ks recorded"}}


@router.get("/dashboard/opex/summary")
async def dashboard_opex_summary(
    date_from: str = "",
    date_to: str = "",
    user: dict = Depends(get_current_user),
):
    from mysql_db import query as _mq
    from datetime import datetime as _dt, timedelta as _td
    import calendar
    _mmt_now = _dt.utcnow() + _td(hours=6, minutes=30)
    # Default to current month if no date filter provided
    if not date_from and not date_to:
        date_from = _mmt_now.strftime("%Y-%m-01")
        _last_day = calendar.monthrange(_mmt_now.year, _mmt_now.month)[1]
        date_to = _mmt_now.replace(day=_last_day).strftime("%Y-%m-%d")
    sql = "SELECT category, SUM(amount) as total, COUNT(*) as count FROM opex WHERE 1=1"
    params = []
    if date_from:
        sql += " AND expense_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND expense_date <= %s"
        params.append(date_to)
    sql += " GROUP BY category ORDER BY total DESC"
    rows = _mq(sql, tuple(params)) if params else _mq(sql)
    grand_total = sum(r["total"] for r in rows) if rows else 0
    return {"success": True, "data": {"categories": list(rows), "grand_total": grand_total}}


@router.delete("/dashboard/opex/{item_id}")
async def dashboard_opex_delete(item_id: int, user: dict = Depends(get_current_user)):
    from mysql_db import query_one as _mqo
    from mysql_db import execute as _me
    item = _mqo("SELECT id, category, recorded_by FROM opex WHERE id = %s", (item_id,))
    if not item:
        return {"success": False, "error": "Expense not found"}
    _me("DELETE FROM opex WHERE id = %s", (item_id,))
    return {"success": True, "data": {"msg": f"{item['category']}: deleted"}}


# ── Financial Statement: Monthly P&L ──
@router.get("/dashboard/staff")
async def dashboard_get_staff(
    search: str | None = Query(None),
    is_active: str | None = Query(None),
    role: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List staff members with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(staff_name LIKE %s OR role LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like])
        if is_active is not None and is_active.lower() in ("true", "false", "1", "0"):
            where.append("is_active = %s")
            params.append(1 if is_active.lower() in ("true", "1") else 0)
        if role:
            where.append("role = %s")
            params.append(role)

        sql = f"""
            SELECT staff_id, staff_name, email, base_salary, food_allowance, transport_allowance,
                   attendance_bonus, commission_rate_food, role, is_active, last_updated
            FROM staff_records
            WHERE {' AND '.join(where)}
            ORDER BY staff_name ASC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM staff_records WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        staff_list = []
        for r in rows:
            staff_list.append({
                "staff_id": r["staff_id"],
                "staff_name": r.get("staff_name"),
                "email": r.get("email") or "",
                "base_salary": float(r.get("base_salary") or 0),
                "food_allowance": float(r.get("food_allowance") or 0),
                "transport_allowance": float(r.get("transport_allowance") or 0),
                "attendance_bonus": float(r.get("attendance_bonus") or 0),
                "commission_rate_food": float(r.get("commission_rate_food") or 0),
                "role": r.get("role"),
                "is_active": bool(r.get("is_active")),
                "last_updated": str(r["last_updated"]) if r.get("last_updated") else None,
            })
        return {"success": True, "data": staff_list, "total": total}
    except Exception as e:
        logger.error(f"GET /staff error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/staff")
async def dashboard_create_staff(req: dict, user: dict = Depends(get_current_user)):
    """Create a new staff member."""
    try:
        staff_name = req.get("staff_name", "").strip()
        if not staff_name:
            return {"success": False, "error": "staff_name is required"}

        base_salary = req.get("base_salary", 0)
        email = req.get("email", "").strip()
        food_allowance = req.get("food_allowance", 0)
        transport_allowance = req.get("transport_allowance", 0)
        attendance_bonus = req.get("attendance_bonus", 0)
        commission_rate_food = req.get("commission_rate_food", 0)
        role = req.get("role", "")

        new_id = _mysql_execute(
            """INSERT INTO staff_records (staff_name, email, base_salary, food_allowance, transport_allowance,
                   attendance_bonus, commission_rate_food, role, is_active, last_updated)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())""",
            (staff_name, email or None, base_salary, food_allowance, transport_allowance,
             attendance_bonus, commission_rate_food, role),
        )

        return {
            "success": True,
            "data": {
                "staff_id": new_id,
                "staff_name": staff_name,
                "email": email or "",
                "base_salary": base_salary,
                "food_allowance": food_allowance,
                "transport_allowance": transport_allowance,
                "attendance_bonus": attendance_bonus,
                "commission_rate_food": commission_rate_food,
                "role": role,
                "is_active": True,
            },
        }
    except Exception as e:
        logger.error(f"POST /staff error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/dashboard/staff/{staff_id}")
async def dashboard_update_staff(staff_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a staff member."""
    try:
        existing = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not existing:
            return {"success": False, "error": "Staff not found"}

        updates = []
        params = []
        for field in ["staff_name", "email", "base_salary", "food_allowance", "transport_allowance",
                       "attendance_bonus", "commission_rate_food", "role", "is_active"]:
            if field in req:
                updates.append(f"{field} = %s")
                params.append(req[field])

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("last_updated = NOW()")
        params.append(staff_id)
        _mysql_execute(f"UPDATE staff_records SET {', '.join(updates)} WHERE staff_id = %s", tuple(params))

        updated = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        return {
            "success": True,
            "data": {
                "staff_id": updated["staff_id"],
                "staff_name": updated.get("staff_name"),
                "email": updated.get("email") or "",
                "base_salary": float(updated.get("base_salary") or 0),
                "food_allowance": float(updated.get("food_allowance") or 0),
                "transport_allowance": float(updated.get("transport_allowance") or 0),
                "attendance_bonus": float(updated.get("attendance_bonus") or 0),
                "commission_rate_food": float(updated.get("commission_rate_food") or 0),
                "role": updated.get("role"),
                "is_active": bool(updated.get("is_active")),
                "last_updated": str(updated["last_updated"]) if updated.get("last_updated") else None,
            },
        }
    except Exception as e:
        logger.error(f"PUT /staff/{staff_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/dashboard/staff/{staff_id}/deactivate")
async def dashboard_deactivate_staff(staff_id: int, user: dict = Depends(get_current_user)):
    """Soft-deactivate a staff member (set is_active=0)."""
    try:
        existing = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not existing:
            return {"success": False, "error": "Staff not found"}

        _mysql_execute(
            "UPDATE staff_records SET is_active = 0, last_updated = NOW() WHERE staff_id = %s",
            (staff_id,)
        )
        return {
            "success": True,
            "data": {
                "staff_id": staff_id,
                "staff_name": existing.get("staff_name"),
                "is_active": False,
                "message": "Staff deactivated",
            },
        }
    except Exception as e:
        logger.error(f"PUT /staff/{staff_id}/deactivate error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════ Staff Leave ═══════════
@router.get("/dashboard/staff/{staff_id}/leave")
async def dashboard_get_staff_leave(staff_id: int, month: str | None = Query(None), user: dict = Depends(get_current_user)):
    """Get leave days for a staff member (optionally filtered by month)."""
    try:
        where = "staff_id = %s"
        params = [staff_id]
        if month and len(month) == 7:
            where += " AND DATE_FORMAT(leave_date, '%%Y-%%m') = %s"
            params.append(month)
        rows = _mysql_query(f"SELECT * FROM staff_leave WHERE {where} ORDER BY leave_date DESC", tuple(params))
        total_days = len(rows)
        return {"success": True, "data": [{
            "id": r["id"], "staff_id": r["staff_id"],
            "leave_date": str(r["leave_date"]), "reason": r.get("reason") or ""
        } for r in rows], "total": total_days, "free_limit": 2,
           "bonus_at_risk": total_days > 2}
    except Exception as e:
        logger.error(f"GET staff leave error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/dashboard/salary/leaves")
async def dashboard_get_all_leaves(month: str | None = Query(None), user: dict = Depends(get_current_user)):
    """Get all leave records with staff names (optionally filtered by month)."""
    try:
        where = "1=1"
        params = []
        if month and len(month) == 7:
            where += " AND DATE_FORMAT(l.leave_date, '%%Y-%%m') = %s"
            params.append(month)
        rows = _mysql_query(f"""
            SELECT l.id, l.staff_id, s.staff_name, l.leave_date, l.reason, l.created_at
            FROM staff_leave l JOIN staff_records s ON l.staff_id = s.staff_id
            WHERE {where} ORDER BY l.leave_date DESC
        """, tuple(params))
        return {"success": True, "data": [{
            "id": r["id"], "staff_id": r["staff_id"],
            "staff_name": r["staff_name"],
            "leave_date": str(r["leave_date"]),
            "reason": r.get("reason") or ""
        } for r in rows]}
    except Exception as e:
        logger.error(f"GET salary/leaves error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/staff/{staff_id}/leave")
async def dashboard_add_staff_leave(staff_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Record a leave day for a staff member."""
    try:
        leave_date = req.get("leave_date", "")
        reason = req.get("reason", "")
        if not leave_date:
            return {"success": False, "error": "leave_date required"}
        
        existing = _mysql_query_one(
            "SELECT id FROM staff_leave WHERE staff_id = %s AND leave_date = %s",
            (staff_id, leave_date)
        )
        if existing:
            return {"success": False, "error": "Leave already recorded for this date"}
        
        new_id = _mysql_execute(
            "INSERT INTO staff_leave (staff_id, leave_date, reason) VALUES (%s, %s, %s)",
            (staff_id, leave_date, reason)
        )
        return {"success": True, "data": {"id": new_id, "staff_id": staff_id, "leave_date": leave_date, "reason": reason}}
    except Exception as e:
        logger.error(f"POST staff leave error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/dashboard/staff/{staff_id}/leave/{leave_id}")
async def dashboard_remove_staff_leave(staff_id: int, leave_id: int, user: dict = Depends(get_current_user)):
    """Remove a leave record."""
    try:
        _mysql_execute("DELETE FROM staff_leave WHERE id = %s AND staff_id = %s", (leave_id, staff_id))
        return {"success": True, "message": "Leave removed"}
    except Exception as e:
        logger.error(f"DELETE staff leave error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/dashboard/staff/{staff_id}/attendance")
async def dashboard_get_staff_attendance(
    staff_id: int,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get attendance history for a specific staff member."""
    try:
        existing = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not existing:
            return {"success": False, "error": "Staff not found"}

        where = ["al.staff_id = %s"]
        params = [staff_id]
        if date_from:
            where.append("al.date >= %s")
            params.append(date_from)
        if date_to:
            where.append("al.date <= %s")
            params.append(date_to)

        sql = f"""
            SELECT al.id, al.staff_id, al.check_in, al.check_out, al.hours_worked,
                   al.hourly_rate, al.daily_pay, al.date, al.status, al.notes, al.created_at,
                   sr.staff_name
            FROM attendance_log al
            JOIN staff_records sr ON al.staff_id = sr.staff_id
            WHERE {' AND '.join(where)}
            ORDER BY al.date DESC, al.check_in DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM attendance_log al WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        attendance = []
        for r in rows:
            attendance.append({
                "id": r["id"],
                "staff_id": r.get("staff_id"),
                "staff_name": r.get("staff_name"),
                "check_in": str(r["check_in"]) if r.get("check_in") else None,
                "check_out": str(r["check_out"]) if r.get("check_out") else None,
                "hours_worked": float(r.get("hours_worked") or 0),
                "hourly_rate": float(r.get("hourly_rate") or 0),
                "daily_pay": float(r.get("daily_pay") or 0),
                "date": str(r["date"]) if r.get("date") else None,
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": attendance, "total": total}
    except Exception as e:
        logger.error(f"GET /staff/{staff_id}/attendance error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  ATTENDANCE — LOG
# ═══════════════════════════════════════
@router.get("/dashboard/attendance")
async def dashboard_get_attendance(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    staff_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_staff_or_dashboard_user),
):
    """List attendance records with optional filters (staff-allowed, read-only)."""
    try:
        where = ["1=1"]
        params = []
        if date_from:
            where.append("al.date >= %s")
            params.append(date_from)
        if date_to:
            where.append("al.date <= %s")
            params.append(date_to)
        if staff_id:
            where.append("al.staff_id = %s")
            params.append(staff_id)

        sql = f"""
            SELECT al.id, al.staff_id, al.check_in, al.check_out, al.hours_worked,
                   al.hourly_rate, al.daily_pay, al.date, al.status, al.notes, al.created_at,
                   sr.staff_name
            FROM attendance_log al
            LEFT JOIN staff_records sr ON al.staff_id = sr.staff_id
            WHERE {' AND '.join(where)}
            ORDER BY al.date DESC, al.check_in DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM attendance_log al WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        attendance = []
        for r in rows:
            attendance.append({
                "id": r["id"],
                "staff_id": r.get("staff_id"),
                "staff_name": r.get("staff_name"),
                "check_in": str(r["check_in"]) if r.get("check_in") else None,
                "check_out": str(r["check_out"]) if r.get("check_out") else None,
                "hours_worked": float(r.get("hours_worked") or 0),
                "hourly_rate": float(r.get("hourly_rate") or 0),
                "daily_pay": float(r.get("daily_pay") or 0),
                "date": str(r["date"]) if r.get("date") else None,
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": attendance, "total": total}
    except Exception as e:
        logger.error(f"GET /attendance error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/attendance/check-in")
async def dashboard_attendance_check_in(req: dict, user: dict = Depends(get_staff_or_dashboard_user)):
    """Check in a staff member — create attendance log entry."""
    try:
        staff_id = req.get("staff_id")
        notes = req.get("notes", "")

        if not staff_id:
            return {"success": False, "error": "staff_id is required"}

        # Get staff record for hourly_rate
        staff = _mysql_query_one("SELECT * FROM staff_records WHERE staff_id = %s", (staff_id,))
        if not staff:
            return {"success": False, "error": "Staff not found"}

        if not staff.get("is_active"):
            return {"success": False, "error": "Staff is inactive"}

        hourly_rate = float(staff.get("hourly_rate") or 0)

        # Check if already checked in today without checkout
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = _mysql_query_one(
            "SELECT id FROM attendance_log WHERE staff_id = %s AND date = %s AND status = 'checked_in'",
            (staff_id, today)
        )
        if existing:
            return {"success": False, "error": "Staff already checked in today. Check out first."}

        new_id = _mysql_execute(
            """INSERT INTO attendance_log
               (staff_id, check_in, hourly_rate, date, status, notes, created_at)
               VALUES (%s, NOW(), %s, %s, 'checked_in', %s, NOW())""",
            (staff_id, hourly_rate, today, notes),
        )

        return {
            "success": True,
            "data": {
                "id": new_id,
                "staff_id": staff_id,
                "staff_name": staff.get("staff_name"),
                "date": today,
                "status": "checked_in",
                "hourly_rate": hourly_rate,
                "message": f"{staff.get('staff_name')} checked in",
            },
        }
    except Exception as e:
        logger.error(f"POST /attendance/check-in error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/attendance/check-out")
async def dashboard_attendance_check_out(req: dict, user: dict = Depends(get_staff_or_dashboard_user)):
    """Check out a staff member — update attendance log with hours and pay."""
    try:
        staff_id = req.get("staff_id")
        notes = req.get("notes", "")

        if not staff_id:
            return {"success": False, "error": "staff_id is required"}

        # Find the open check-in for today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        record = _mysql_query_one(
            "SELECT * FROM attendance_log WHERE staff_id = %s AND date = %s AND status = 'checked_in' ORDER BY id DESC LIMIT 1",
            (staff_id, today)
        )
        if not record:
            return {"success": False, "error": "No open check-in found for today"}

        # Calculate hours worked
        hours_worked = 0
        daily_pay = 0
        try:
            check_in_time = record["check_in"]
            if check_in_time:
                now = datetime.now(timezone.utc)
                if hasattr(check_in_time, 'timestamp'):
                    delta = now - check_in_time
                else:
                    ci_str = str(check_in_time)
                    ci_dt = datetime.fromisoformat(ci_str.replace('Z', '+00:00'))
                    delta = now.replace(tzinfo=None) - ci_dt.replace(tzinfo=None)
                hours_worked = round(delta.total_seconds() / 3600.0, 2)
                hourly_rate = float(record.get("hourly_rate") or 0)
                daily_pay = round(hours_worked * hourly_rate, 2)
        except Exception as calc_err:
            logger.warning(f"Hours calculation fallback: {calc_err}")
            hours_worked = 0
            daily_pay = 0

        # Update the record
        _mysql_execute(
            """UPDATE attendance_log
               SET check_out = NOW(), hours_worked = %s, daily_pay = %s, status = 'checked_out', notes = CONCAT(IFNULL(notes,''), ' | ', %s)
               WHERE id = %s""",
            (hours_worked, daily_pay, notes, record["id"]),
        )

        staff = _mysql_query_one("SELECT staff_name FROM staff_records WHERE staff_id = %s", (staff_id,))

        return {
            "success": True,
            "data": {
                "id": record["id"],
                "staff_id": staff_id,
                "staff_name": staff.get("staff_name") if staff else "Unknown",
                "date": today,
                "hours_worked": hours_worked,
                "daily_pay": daily_pay,
                "status": "checked_out",
                "message": f"Checked out. {hours_worked}h worked, {daily_pay} pay",
            },
        }
    except Exception as e:
        logger.error(f"POST /attendance/check-out error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  SALARY & PAYROLL
# ═══════════════════════════════════════
@router.get("/dashboard/salary")
async def dashboard_get_salary(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    staff_name: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List salary/payroll records with optional filters."""
    try:
        where = ["1=1"]
        params = []
        if date_from:
            where.append("pay_date >= %s")
            params.append(date_from)
        if date_to:
            where.append("pay_date <= %s")
            params.append(date_to)
        if staff_name:
            where.append("staff_name LIKE %s")
            params.append(f"%{staff_name}%")
        if status:
            where.append("status = %s")
            params.append(status)

        sql = f"""
            SELECT sp.id, sp.staff_name, sp.base_salary, sp.bonus, sp.deductions, sp.net_salary,
                   sp.pay_period, sp.pay_date, sp.status, sp.notes, sp.created_at,
                   COALESCE(sr.food_allowance, 0) AS food_allowance,
                   COALESCE(sr.transport_allowance, 0) AS transport_allowance
            FROM salary_payroll sp
            LEFT JOIN staff_records sr ON sp.staff_name = sr.staff_name
            WHERE {' AND '.join(where)}
            ORDER BY sp.pay_date DESC, sp.id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM salary_payroll WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        import re as _re
        records = []
        for r in rows:
            # Extract attendance_bonus from notes: e.g. "att_bonus=50,000"
            notes_text = r.get("notes") or ""
            att_match = _re.search(r'att_bonus=([0-9,]+)', notes_text)
            attendance_bonus = float(att_match.group(1).replace(",","")) if att_match else 0.0
            
            records.append({
                "id": r["id"],
                "staff_name": r.get("staff_name"),
                "base_salary": float(r.get("base_salary") or 0),
                "food_allowance": float(r.get("food_allowance") or 0),
                "transport_allowance": float(r.get("transport_allowance") or 0),
                "attendance_bonus": attendance_bonus,
                "bonus": float(r.get("bonus") or 0),
                "deductions": float(r.get("deductions") or 0),
                "net_salary": float(r.get("net_salary") or 0),
                "pay_period": r.get("pay_period"),
                "pay_date": str(r["pay_date"]) if r.get("pay_date") else None,
                "status": r.get("status"),
                "notes": notes_text,
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": records, "total": total}
    except Exception as e:
        logger.error(f"GET /salary error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/salary")
async def dashboard_create_salary(req: dict, user: dict = Depends(get_current_user)):
    """Record a salary payment. Creates OPEX entry when status=paid."""
    try:
        staff_name = req.get("staff_name", "").strip()
        if not staff_name:
            return {"success": False, "error": "staff_name is required"}

        pay_period = req.get("pay_period", "")
        
        # Look up auto-generated payroll for amount
        gen_row = _mysql_query_one(
            "SELECT net_salary, notes FROM salary_payroll WHERE staff_name = %s AND pay_period = %s",
            (staff_name, pay_period)
        )
        if gen_row:
            net_salary = float(gen_row["net_salary"] or 0)
            notes = gen_row.get("notes") or ""
        else:
            # Manual: use provided amounts
            base_salary = req.get("base_salary", 0)
            bonus = req.get("bonus", 0)
            deductions = req.get("deductions", 0)
            net_salary = float(base_salary) + float(bonus) - float(deductions)
            notes = req.get("notes", "[manual entry]")

        pay_date = req.get("pay_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        status = req.get("status", "pending")
        new_id = _mysql_execute(
            """INSERT INTO salary_payroll
               (staff_name, base_salary, bonus, deductions, net_salary, pay_period, pay_date, status, payment_method, notes, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (staff_name, 0, net_salary, 0, net_salary, pay_period, pay_date, status, payment_method, notes),
        )

        # If marked as paid, create OPEX entry
        if status == "paid":
            _mysql_execute(
                """INSERT INTO opex (category, description, amount, expense_date, payment_method, staff_name, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
                ("Staff Salary", f"{staff_name} - {pay_period}", net_salary, pay_date, payment_method, staff_name),
            )

        return {
            "success": True,
            "data": {
                "id": new_id, "staff_name": staff_name,
                "net_salary": net_salary, "pay_period": pay_period,
                "pay_date": pay_date, "status": status,
                "payment_method": payment_method, "notes": notes,
            },
        }
    except Exception as e:
        logger.error(f"POST /salary error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/dashboard/salary/{payroll_id}")
async def dashboard_update_salary(payroll_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a payroll entry (e.g. mark as paid). Auto-settles unpaid advances when paid."""
    try:
        existing = _mysql_query_one("SELECT * FROM salary_payroll WHERE id = %s", (payroll_id,))
        if not existing:
            return {"success": False, "error": "Payroll entry not found"}

        status = req.get("status", existing.get("status"))
        pay_date = req.get("pay_date", existing.get("pay_date"))
        payment_method = req.get("payment_method", existing.get("payment_method") or "Cash")
        notes = req.get("notes", existing.get("notes") or "")

        _mysql_execute(
            "UPDATE salary_payroll SET status=%s, pay_date=%s, payment_method=%s, notes=%s WHERE id=%s",
            (status, pay_date, payment_method, notes, payroll_id)
        )

        # Auto-settle unpaid advances for this staff when marking as paid
        settled = 0
        if status == "paid" and existing.get("status") != "paid":
            staff_name = existing.get("staff_name", "")
            if staff_name:
                settled = _mysql_execute(
                    "UPDATE salary_advance SET repayment_status='paid' WHERE staff_name=%s AND (repayment_status != 'paid' OR repayment_status IS NULL)",
                    (staff_name,)
                )

        return {"success": True, "message": f"Payroll #{payroll_id} updated to {status}, {settled} advance(s) settled"}
    except Exception as e:
        logger.error(f"PUT /salary/{payroll_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/salary/generate")
async def dashboard_generate_salary(req: dict, user: dict = Depends(get_current_user)):
    """Auto-generate payroll for a given month.
    Net Pay = base_salary + transport_allowance + attendance_bonus
            + commission_food + game_target_bonus
            + food_allowance_val - unpaid_advances
    Game bonus: SHOP-WIDE tier by total play hours (non-stacking, highest qualifying).
    All staff get the same game bonus if shop target is met."""
    try:
        month = req.get("month", "").strip()
        if not month or len(month) != 7:
            return {"success": False, "error": "month required (YYYY-MM)"}

        staff_ids = req.get("staff_ids", None)

        where = "WHERE is_active = 1"
        extra = ()
        if staff_ids:
            ids = tuple(staff_ids) if isinstance(staff_ids, list) else (staff_ids,)
            placeholders = ",".join(["%s"] * len(ids))
            where += f" AND staff_id IN ({placeholders})"
            extra = ids

        staff_rows = _mysql_query(f"SELECT * FROM staff_records {where} ORDER BY staff_name", extra)

        # Read game bonus tiers from config
        tier_cfg = _mysql_query_one(
            "SELECT config_value FROM settings_config WHERE config_key = 'game_bonus_tiers'"
        )
        import json as _json
        tiers = _json.loads(tier_cfg["config_value"]) if tier_cfg else {}

        # ── SHOP-WIDE total play hours (game sessions only — exclude topup/new-member mirror rows) ──
        shop_row = _mysql_query_one(
            "SELECT COALESCE(SUM(CAST(REGEXP_REPLACE(IFNULL(notes,''), '.*Mins?:? *([0-9]+).*', '$1') AS UNSIGNED)), 0) AS total_mins "
            "FROM sales_daily WHERE DATE_FORMAT(sale_date, '%%Y-%%m') = %s "
            "AND is_topup = 0",
            (month,)
        )
        shop_total_mins = int(shop_row["total_mins"] or 0)
        shop_total_hrs = round(shop_total_mins / 60.0, 1)

        # ── Member Card Commission (shop-wide, split equally) ──
        # Count new member registrations (non-gift): balance_mins_before=0 + amount>0
        card_total_row = _mysql_query_one(
            "SELECT COUNT(*) AS card_count FROM topup_log "
            "WHERE balance_mins_before = 0 AND amount > 0 AND LEFT(topup_date, 7) = %s",
            (month,)
        )
        total_cards = int(card_total_row["card_count"] or 0) if card_total_row else 0
        CARD_COMMISSION_PER_CARD = 1000  # Ks per card
        shop_per_staff_card_com = total_cards * CARD_COMMISSION_PER_CARD

        # ── Food Profit (same as PNL): stock_out revenue − FIFO COGS ──
        food_rev_row = _mysql_query_one(
            "SELECT COALESCE(SUM(total), 0) AS t FROM stock_out WHERE DATE_FORMAT(sale_date, '%%Y-%%m') = %s",
            (month,)
        )
        food_revenue = float(food_rev_row["t"] or 0)
        try:
            import stock_fifo, pymysql
            _sfc = pymysql.connect(host='127.0.0.1', user='root', password=os.environ.get('MYSQL_ROOT_PASSWORD', ''), database='psvibe_api')
            _sfr = stock_fifo.calc_fifo(_sfc, month)
            _sfc.close()
            food_cogs = _sfr["cogs"]
        except Exception:
            food_cogs = 0
        total_food_profit = food_revenue - food_cogs

        # Determine shop-wide game bonus (non-stacking, highest qualifying tier)
        game_bonus = 0
        qualified_tier = None
        if tiers:
            hrs_thresholds = sorted([(int(k), int(v)) for k, v in tiers.items()], key=lambda x: x[0], reverse=True)
            for threshold_hrs, bonus_amt in hrs_thresholds:
                if shop_total_hrs >= threshold_hrs:
                    game_bonus = bonus_amt
                    qualified_tier = f"{threshold_hrs}hr"
                    break

        generated = []
        for s in staff_rows:
            sname = s["staff_name"]
            sid = s["staff_id"]
            base = float(s.get("base_salary") or 0)
            food_allowance_val = float(s.get("food_allowance") or 0)
            transport = float(s.get("transport_allowance") or 0)
            attendance_bonus = float(s.get("attendance_bonus") or 0)
            cr_food = float(s.get("commission_rate_food") or 0)

            # Leave policy:
            #   - ANY leave day → forfeit attendance bonus
            #   - >2 leave days → 15,000 Ks per extra day deducted
            leave_row = _mysql_query_one(
                "SELECT COUNT(*) AS lc FROM staff_leave WHERE staff_id = %s AND DATE_FORMAT(leave_date, '%%Y-%%m') = %s",
                (sid, month)
            )
            leave_days = int(leave_row["lc"] or 0)
            actual_att_bonus = 0 if leave_days > 0 else attendance_bonus
            extra_leave = max(0, leave_days - 2)
            leave_penalty = extra_leave * 15000

            # Food commission — SHOP-WIDE: staff rate × total shop food profit
            commission_food = round(total_food_profit * cr_food, 0)

            # Member card commission — shop-wide, split equally among all staff
            commission_card = shop_per_staff_card_com

            # Unpaid advances
            adv_row = _mysql_query_one(
                "SELECT COALESCE(SUM(amount), 0) AS ta FROM salary_advance "
                "WHERE staff_name = %s AND repayment_status != 'paid'", (sname,)
            )
            unpaid_adv = int(adv_row["ta"] or 0)

            deductions = unpaid_adv + leave_penalty
            additions = transport + food_allowance_val + actual_att_bonus + commission_food + game_bonus + commission_card
            net_pay = round(base + additions - deductions, 0)

            total_commission = commission_food + game_bonus + commission_card

            exist = _mysql_query_one(
                "SELECT id FROM salary_payroll WHERE staff_name = %s AND pay_period = %s",
                (sname, month)
            )
            card_note = f", card_com={commission_card:,.0f}({total_cards}cards×{CARD_COMMISSION_PER_CARD})" if total_cards > 0 else ""
            bonus_note = f"[auto: food_com={commission_food:,.0f}({cr_food*100:.0f}% of profit={total_food_profit:,.0f}(rev{food_revenue:,.0f}-cogs{food_cogs:,.0f})), game_bonus={game_bonus:,.0f}@{qualified_tier or 'none'}(shop={shop_total_hrs}hr){card_note}, transport={transport:,.0f}, att_bonus={actual_att_bonus:,.0f}(leave={leave_days}d, forfeit)" if leave_days > 0 else f"[auto: food_com={commission_food:,.0f}({cr_food*100:.0f}% of profit={total_food_profit:,.0f}(rev{food_revenue:,.0f}-cogs{food_cogs:,.0f})), game_bonus={game_bonus:,.0f}@{qualified_tier or 'none'}(shop={shop_total_hrs}hr){card_note}, transport={transport:,.0f}, att_bonus={actual_att_bonus:,.0f}(leave=0d)"
            if extra_leave > 0:
                bonus_note = bonus_note[:-1] + f", penalty={leave_penalty}(extra={extra_leave}d×15K)]"
            if exist:
                _mysql_execute(
                    "UPDATE salary_payroll SET base_salary=%s, bonus=%s, deductions=%s, net_salary=%s, status='pending', notes=%s WHERE id=%s",
                    (base, total_commission, deductions, net_pay, bonus_note, exist["id"])
                )
                rid = exist["id"]
            else:
                rid = _mysql_execute(
                    "INSERT INTO salary_payroll (staff_name, base_salary, bonus, deductions, net_salary, pay_period, pay_date, status, notes, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, CURDATE(), 'pending', %s, NOW())",
                    (sname, base, total_commission, deductions, net_pay, month, bonus_note)
                )

            generated.append({
                "id": rid,
                "staff_name": sname,
                "staff_id": sid,
                "base_salary": base,
                "food_allowance": food_allowance_val,
                "transport_allowance": transport,
                "attendance_bonus": actual_att_bonus,
                "leave_days": leave_days,
                "leave_penalty": leave_penalty,
                "commission_food": commission_food,
                "commission_card": commission_card,
                "total_cards": total_cards,
                "card_commission_rate": CARD_COMMISSION_PER_CARD,
                "game_bonus": game_bonus,
                "total_food_profit": total_food_profit, "food_revenue": food_revenue, "food_cogs": food_cogs,
                "shop_total_hrs": shop_total_hrs,
                "qualified_tier": qualified_tier,
                "unpaid_advances": unpaid_adv,
                "deductions": deductions,
                "net_salary": net_pay,
                "pay_period": month,
                "commission_rate_food": cr_food,
            })

        return {"success": True, "data": generated, "count": len(generated), "shop_total_hrs": shop_total_hrs, "game_bonus": game_bonus, "qualified_tier": qualified_tier, "total_food_profit": total_food_profit, "food_revenue": food_revenue, "food_cogs": food_cogs}
    except Exception as e:
        logger.error(f"POST /salary/generate error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/dashboard/salary/advances")
async def dashboard_get_salary_advances(
    staff_name: str | None = Query(None),
    repayment_status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_admin_clean),
):
    """List salary advances (owner-only — clean 403 for staff)."""
    try:
        where = ["1=1"]
        params = []
        if staff_name:
            where.append("staff_name LIKE %s")
            params.append(f"%{staff_name}%")
        if repayment_status:
            where.append("repayment_status = %s")
            params.append(repayment_status)

        sql = f"""
            SELECT id, staff_name, amount, advance_date, repayment_status, notes, created_at
            FROM salary_advance
            WHERE {' AND '.join(where)}
            ORDER BY advance_date DESC, id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = _mysql_query(sql, tuple(params))

        count_row = _mysql_query_one(
            f"SELECT COUNT(*) as total FROM salary_advance WHERE {' AND '.join(where)}",
            tuple(params[:-2])
        )
        total = count_row["total"] if count_row else 0

        advances = []
        for r in rows:
            advances.append({
                "id": r["id"],
                "staff_name": r.get("staff_name"),
                "amount": float(r.get("amount") or 0),
                "advance_date": str(r["advance_date"]) if r.get("advance_date") else None,
                "repayment_status": r.get("repayment_status"),
                "notes": r.get("notes"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"success": True, "data": advances, "total": total}
    except Exception as e:
        logger.error(f"GET /salary/advances error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/dashboard/salary/advances")
async def dashboard_create_salary_advance(req: dict, user: dict = Depends(require_admin_clean)):
    """Record a salary advance. Creates OPEX entry automatically.

    ADMIN-ONLY (Staff App Wave 1): payroll-adjacent money movement stays with the
    owner/dashboard. The acting admin is stored as OPEX ``recorded_by`` for audit.
    """
    try:
        staff_name = req.get("staff_name", "").strip()
        amount = req.get("amount", 0)
        if not staff_name:
            return {"success": False, "error": "staff_name is required"}
        if float(amount) <= 0:
            return {"success": False, "error": "amount must be positive"}

        advance_date = req.get("advance_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        repayment_status = req.get("repayment_status", "unpaid")
        notes = req.get("notes") or req.get("reason", "")
        payment_method = req.get("payment_method", "Cash")
        new_id = _mysql_execute(
            """INSERT INTO salary_advance
               (staff_name, amount, advance_date, repayment_status, notes, payment_method, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
            (staff_name, amount, advance_date, repayment_status, notes, payment_method),
        )

        # Auto-create OPEX entry for staff salary advance (record acting admin for audit)
        _recorded_by = user.get("username") or user.get("name") or "admin"
        _mysql_execute(
            """INSERT INTO opex (category, description, amount, expense_date, payment_method, staff_name, recorded_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
            ("Staff Salary", f"Advance: {staff_name}", amount, advance_date, payment_method, staff_name, _recorded_by),
        )

        return {
            "success": True,
            "data": {
                "id": new_id,
                "staff_name": staff_name,
                "amount": amount,
                "advance_date": advance_date,
                "repayment_status": repayment_status,
                "notes": notes,
            },
        }
    except Exception as e:
        logger.error(f"POST /salary/advances error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/dashboard/salary/advances/{advance_id}")
async def dashboard_delete_salary_advance(advance_id: int, user: dict = Depends(require_admin_clean)):
    """Delete a salary advance record."""
    try:
        _mysql_execute("DELETE FROM salary_advance WHERE id = %s", (advance_id,))
        return {"success": True, "message": "Advance deleted"}
    except Exception as e:
        logger.error(f"DELETE /salary/advances error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════
#  CONSOLE — DETAILED CRUD
# ═══════════════════════════════════════
