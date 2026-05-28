"""PS VIBE BI Dashboard — Analytics Engine

Reads from Google Sheets (Sales_Daily, TopUp_Log, Card_Wallet, Console_Booking,
Attendance_Log) and computes aggregate KPIs for the BI dashboard.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sheets_client import (
    get_worksheet, get_member_rows, get_booking_rows,
    int_safe, float_safe,
)
from config import (
    SHEET_SALES_DAILY, SHEET_TOPUP_LOG, SHEET_CARD_WALLET,
    SHEET_CONSOLE_BOOKING, SHEET_ATTENDANCE_LOG, SHEET_SETTING,
    MMT_HOURS, MMT_MINUTES,
)

logger = logging.getLogger(__name__)

MMT = timezone(timedelta(hours=MMT_HOURS, minutes=MMT_MINUTES))


def now_mmt() -> datetime:
    return datetime.now(MMT)


def today_str() -> str:
    return now_mmt().strftime("%-m/%-d/%Y")


def _parse_date(val: str) -> Optional[datetime]:
    """Parse dates like '5/28/2026' or '5/28/2026 14:30' into MMT datetime."""
    if not val or not val.strip():
        return None
    val = val.strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(val, fmt)
            return dt.replace(tzinfo=MMT)
        except ValueError:
            continue
    return None


def _week_range(offset_weeks: int = 0) -> Tuple[datetime, datetime]:
    """Return (start, end) of the (current - offset_weeks) week in MMT.
    offset_weeks=0 → this week (Mon–Sun), 1 → last week, etc.
    """
    today = now_mmt()
    weekday = today.weekday()  # Monday=0
    start = today - timedelta(days=weekday + 7 * offset_weeks)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return start, end


def _month_range(offset_months: int = 0) -> Tuple[datetime, datetime]:
    """Return (start, end) of the (current - offset_months) month in MMT."""
    today = now_mmt()
    year, month = today.year, today.month
    for _ in range(offset_months):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    start = datetime(year, month, 1, tzinfo=MMT)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=MMT)
    else:
        end = datetime(year, month + 1, 1, tzinfo=MMT)
    return start, end


# ═══════════════════════════════════════════════════════════════
#  DAILY SALES
# ═══════════════════════════════════════════════════════════════

def get_daily_sales(date_str: Optional[str] = None) -> Dict[str, Any]:
    """Return today's sales KPIs from Sales_Daily.

    Sales_Daily columns (best-effort discovery):
      A=Row#, B=VoucherID (V-xxx), C=Date, D=MemberID, E=TotalAmount,
      F=PaymentMethod, G=Staff, H=ConsoleID, I=Duration(min), J=Notes, …

    Returns:
        dict with total_sales, voucher_count, average_ticket, by_payment, …
    """
    target = date_str or today_str()
    from sheets_client import get_sales_daily_rows
    rows = get_sales_daily_rows()
    if len(rows) < 2:
        return _empty_sales()

    # Detect columns from header
    header = [c.strip().lower() for c in rows[0]] if rows else []
    col_map = _map_columns(header, {
        "date": ["date", "date (mm/dd/yyyy)"],
        "voucher": ["voucher id", "voucher", "voucher_no"],
        "member": ["member", "member id", "customer"],
        "amount": ["total amount", "amount", "total", "grand total", "total (ks)"],
        "payment": ["payment method", "payment", "method"],
        "staff": ["staff", "staff name", "attendant"],
        "console": ["console", "console id"],
        "duration": ["duration", "duration (min)", "play time", "mins"],
    })

    # Also fallback: assume standard column positions
    sales = []
    total_amount = 0
    voucher_count = 0
    payment_counts: Dict[str, int] = defaultdict(int)
    payment_amounts: Dict[str, float] = defaultdict(float)
    hourly_sales: Dict[int, float] = defaultdict(float)

    for row in rows[1:]:
        if not row or len(row) < 2:
            continue
        # Date match
        row_date = _get_col(row, col_map.get("date"), 2)  # fallback col C
        # Normalize date formats
        rdn = row_date.strip()
        # Try both MM/DD/YYYY and M/D/YYYY
        if rdn != target:
            # Try normalizing
            parts = rdn.replace("/", " ").replace("-", " ").split()
            tparts = target.replace("/", " ").replace("-", " ").split()
            if len(parts) >= 3 and len(tparts) >= 3:
                try:
                    if (int(parts[0]) == int(tparts[0]) and
                        int(parts[1]) == int(tparts[1]) and
                        int(parts[2]) == int(tparts[2])):
                        pass  # match
                    else:
                        continue
                except (ValueError, IndexError):
                    continue
            else:
                continue

        voucher = _get_col(row, col_map.get("voucher"), 1)
        amount = int_safe(_get_col(row, col_map.get("amount"), 4))
        payment = _get_col(row, col_map.get("payment"), 5).strip().title()
        if not payment:
            payment = "Unknown"

        if amount <= 0 and voucher.strip():
            amount = 0  # still count as sale attempt, keep voucher count

        sales.append({
            "voucher": voucher,
            "member": _get_col(row, col_map.get("member"), 3),
            "amount": amount,
            "payment": payment,
            "staff": _get_col(row, col_map.get("staff"), 6),
            "console": _get_col(row, col_map.get("console"), 7),
            "duration": int_safe(_get_col(row, col_map.get("duration"), 8)),
        })
        total_amount += amount
        voucher_count += 1
        payment_counts[payment] += 1
        payment_amounts[payment] += amount

    avg_ticket = round(total_amount / voucher_count, 2) if voucher_count > 0 else 0

    return {
        "date": target,
        "total_sales_ks": total_amount,
        "voucher_count": voucher_count,
        "average_ticket_ks": avg_ticket,
        "by_payment": {k: {"count": payment_counts[k], "amount": payment_amounts[k]}
                       for k in payment_counts},
        "top_sales": sorted(sales, key=lambda s: s["amount"], reverse=True)[:5],
    }


def _empty_sales() -> Dict[str, Any]:
    return {
        "date": today_str(),
        "total_sales_ks": 0,
        "voucher_count": 0,
        "average_ticket_ks": 0,
        "by_payment": {},
        "top_sales": [],
    }


# ═══════════════════════════════════════════════════════════════
#  TOP-UP TRENDS
# ═══════════════════════════════════════════════════════════════

def get_topup_trends(days: int = 30) -> Dict[str, Any]:
    """Return top-up analytics from TopUp_Log.

    TopUp_Log columns:
      A=Date, B=MemberID, C=TopUpType/Package, D=Amount(Ks), E=Mins

    Returns daily, weekly aggregates + all-time effective rate.
    """
    from sheets_client import get_topup_log_rows
    rows = get_topup_log_rows()
    if len(rows) < 2:
        return _empty_topups()

    cutoff = now_mmt() - timedelta(days=days)
    daily: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0, "mins": 0})
    member_totals: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0, "mins": 0})
    all_total_ks = 0
    all_total_mins = 0
    all_count = 0

    for row in rows[1:]:
        if len(row) < 5:
            continue
        date_val = _get_col(row, None, 0).strip()
        if not date_val:
            continue
        dt = _parse_date(date_val)
        if dt is None:
            continue

        member = _get_col(row, None, 1).strip()
        ks = int_safe(_get_col(row, None, 3))
        mins = int_safe(_get_col(row, None, 4))
        if ks <= 0 or mins <= 0:
            continue

        all_total_ks += ks
        all_total_mins += mins
        all_count += 1

        if member:
            member_totals[member]["count"] += 1
            member_totals[member]["amount"] += ks
            member_totals[member]["mins"] += mins

        if dt >= cutoff:
            day_key = dt.strftime("%Y-%m-%d")
            daily[day_key]["count"] += 1
            daily[day_key]["amount"] += ks
            daily[day_key]["mins"] += mins

    # Daily series sorted
    daily_series = [
        {"date": k, **v, "rate": round(v["amount"] / v["mins"], 2) if v["mins"] > 0 else 0}
        for k, v in sorted(daily.items())
    ]

    # Weekly aggregates
    weekly = _aggregate_weekly(daily_series)

    # Top members
    top_members = sorted(member_totals.items(),
                         key=lambda x: x[1]["amount"], reverse=True)[:10]
    top_members_list = [
        {"member_id": m[0], **m[1],
         "rate": round(m[1]["amount"] / m[1]["mins"], 2) if m[1]["mins"] > 0 else 0}
        for m in top_members
    ]

    all_time_rate = round(all_total_ks / all_total_mins, 4) if all_total_mins > 0 else 0

    return {
        "period_days": days,
        "total_topups": all_count,
        "total_amount_ks": all_total_ks,
        "total_mins": all_total_mins,
        "all_time_effective_rate": all_time_rate,
        "daily_series": daily_series,
        "weekly_aggregates": weekly,
        "top_members": top_members_list,
    }


def _empty_topups() -> Dict[str, Any]:
    return {
        "period_days": 30,
        "total_topups": 0,
        "total_amount_ks": 0,
        "total_mins": 0,
        "all_time_effective_rate": 0,
        "daily_series": [],
        "weekly_aggregates": [],
        "top_members": [],
    }


# ═══════════════════════════════════════════════════════════════
#  MEMBER ACTIVITY
# ═══════════════════════════════════════════════════════════════

def get_member_activity() -> Dict[str, Any]:
    """Return member activity stats from Card_Wallet + Console_Booking + TopUp_Log.

    Card_Wallet columns:
      A=Row, B=MemberID, C=Name, D=Phone, E=JoinDate, F=NetSpend,
      G=Tier, H=WalletMins, I=ThisMonth, J=LastMonth, K=?, L=EffectiveRate,
      M=?, N=ReferralCode
    """
    members = get_member_rows()
    today = today_str()

    total_members = 0
    tier_counts: Dict[str, int] = defaultdict(int)
    total_wallet_mins = 0
    total_spend = 0
    tiers_dist: List[Dict] = []

    for row in members[1:]:
        if len(row) < 2:
            continue
        mid = row[1].strip()
        if not mid:
            continue
        total_members += 1
        tier = row[6].strip() if len(row) > 6 and row[6].strip() else "Warrior"
        tier_counts[tier] += 1
        mins = int_safe(row[7]) if len(row) > 7 else 0
        total_wallet_mins += mins
        spend = int_safe(row[5]) if len(row) > 5 else 0
        total_spend += spend

    # Active today (from Console_Booking)
    active_today = 0
    try:
        bk_rows = get_booking_rows()
        active_members_set = set()
        for row in bk_rows[1:]:
            if len(row) < 7:
                continue
            bk_date = row[1].strip() if len(row) > 1 else ""
            bk_status = row[6].strip() if len(row) > 6 else ""
            member = row[3].strip() if len(row) > 3 else ""
            if bk_date == today and bk_status in ("Active", "Scheduled") and member:
                active_members_set.add(member)
        active_today = len(active_members_set)
    except Exception as e:
        logger.warning("Could not determine active-today count: %s", e)

    # Recent top-ups (last 7 days) to count active members
    recent_topup_members = 0
    try:
        from sheets_client import get_topup_log_rows
        topup_rows = get_topup_log_rows()
        cutoff = now_mmt() - timedelta(days=7)
        tu_members = set()
        for row in topup_rows[1:]:
            if len(row) < 5:
                continue
            dt = _parse_date(_get_col(row, None, 0))
            if dt and dt >= cutoff:
                member = _get_col(row, None, 1).strip()
                if member:
                    tu_members.add(member)
        recent_topup_members = len(tu_members)
    except Exception as e:
        logger.warning("Could not count recent top-up members: %s", e)

    # Compute tier distribution
    for tier, count in sorted(tier_counts.items()):
        tiers_dist.append({
            "tier": tier,
            "count": count,
            "pct": round(count / total_members * 100, 1) if total_members > 0 else 0,
        })

    return {
        "total_members": total_members,
        "active_today": active_today,
        "active_last_7d": recent_topup_members,
        "total_wallet_mins": total_wallet_mins,
        "total_spend_ks": total_spend,
        "avg_spend_per_member": round(total_spend / total_members, 2) if total_members > 0 else 0,
        "tier_distribution": tiers_dist,
    }


# ═══════════════════════════════════════════════════════════════
#  CONSOLE USAGE
# ═══════════════════════════════════════════════════════════════

def get_console_usage(days: int = 30) -> Dict[str, Any]:
    """Return console usage stats from Console_Booking + Setting.

    Console_Booking columns:
      A=BookingID, B=Date, C=ConsoleID, D=Member, E=StartTime,
      F=EndTime, G=Status, H=Staff, I=Notes
    """
    bk_rows = get_booking_rows()
    cutoff = now_mmt() - timedelta(days=days)

    console_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total_bookings": 0, "active_bookings": 0, "completed_bookings": 0,
        "cancelled_bookings": 0, "total_hours": 0.0, "unique_members": set(),
        "daily_bookings": defaultdict(int),
    })
    daily_totals: Dict[str, int] = defaultdict(int)
    total_bookings_all = 0
    active_now = 0

    for row in bk_rows[1:]:
        if len(row) < 7:
            continue
        bk_id = row[0].strip()
        bk_date = row[1].strip()
        console_id = row[2].strip()
        member = row[3].strip() if len(row) > 3 else ""
        start_t = row[4].strip() if len(row) > 4 else ""
        end_t = row[5].strip() if len(row) > 5 else ""
        status = row[6].strip() if len(row) > 6 else ""

        if not console_id:
            continue

        dt = _parse_date(bk_date)
        cs = console_stats[console_id]
        cs["total_bookings"] += 1
        total_bookings_all += 1

        if status == "Active":
            cs["active_bookings"] += 1
            active_now += 1
        elif status == "Done":
            cs["completed_bookings"] += 1
        elif status == "Cancelled":
            cs["cancelled_bookings"] += 1

        if member:
            cs["unique_members"].add(member)

        # Calculate hours from start/end time
        hours = _calc_hours(start_t, end_t)
        cs["total_hours"] += hours

        if dt and dt >= cutoff:
            day_key = dt.strftime("%Y-%m-%d")
            cs["daily_bookings"][day_key] += 1
            daily_totals[day_key] += 1

    # Also get console configs from Setting
    try:
        from sheets_client import get_setting_rows
        setting_rows = get_setting_rows()
        names = [r[7] if len(r) > 7 else '' for r in setting_rows[1:]]
        types = [r[8] if len(r) > 8 else '' for r in setting_rows[1:]]
        for i, name in enumerate(names):
            name = name.strip()
            if name and name not in console_stats:
                console_stats[name] = {
                    "total_bookings": 0, "active_bookings": 0, "completed_bookings": 0,
                    "cancelled_bookings": 0, "total_hours": 0.0, "unique_members": set(),
                    "daily_bookings": defaultdict(int),
                }
            if name:
                ctype = types[i].strip() if i < len(types) else ""
                console_stats[name]["type"] = ctype
    except Exception as e:
        logger.warning("Could not fetch console configs: %s", e)

    # Format results
    consoles = []
    for cid, stats in sorted(console_stats.items()):
        daily = [{"date": k, "bookings": v}
                 for k, v in sorted(stats["daily_bookings"].items())]
        consoles.append({
            "console_id": cid,
            "type": stats.get("type", ""),
            "total_bookings": stats["total_bookings"],
            "active_bookings": stats["active_bookings"],
            "completed_bookings": stats["completed_bookings"],
            "cancelled_bookings": stats["cancelled_bookings"],
            "total_hours": round(stats["total_hours"], 1),
            "unique_members": len(stats["unique_members"]),
            "daily_series": daily,
        })

    daily_series = [{"date": k, "total_bookings": v}
                    for k, v in sorted(daily_totals.items())]

    # Utilization rate: avg bookings per console per day
    num_consoles = len(consoles) or 1
    util_rate = round(total_bookings_all / (num_consoles * days), 2)

    return {
        "period_days": days,
        "total_consoles": len(consoles),
        "total_bookings": total_bookings_all,
        "active_now": active_now,
        "avg_bookings_per_console_day": util_rate,
        "daily_series": daily_series,
        "consoles": consoles,
    }


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD SUMMARY (all KPIs)
# ═══════════════════════════════════════════════════════════════

def get_dashboard_summary() -> Dict[str, Any]:
    """Return a consolidated dashboard with top-level KPIs."""
    sales = get_daily_sales()
    members = get_member_activity()
    consoles = get_console_usage(days=1)  # today only for dashboard
    topups = get_topup_trends(days=7)

    # Get base rate
    base_rate = 0
    try:
        from sheets_client import get_setting_rows
        setting_rows = get_setting_rows()
        base_rate = int_safe(setting_rows[1][1]) if len(setting_rows) > 1 and len(setting_rows[1]) > 1 else 0
    except Exception:
        pass

    return {
        "generated_at": now_mmt().isoformat(),
        "summary": {
            "today_sales_ks": sales["total_sales_ks"],
            "today_vouchers": sales["voucher_count"],
            "today_avg_ticket_ks": sales["average_ticket_ks"],
            "active_members_today": members["active_today"],
            "total_members": members["total_members"],
            "active_consoles": consoles["active_now"],
            "total_consoles": consoles["total_consoles"],
            "week_topup_ks": topups["total_amount_ks"],
            "week_topup_count": topups["total_topups"],
            "base_rate_ks_hr": base_rate,
        },
        "daily_sales": sales,
        "member_activity": members,
        "console_usage_today": consoles,
        "topup_trends_7d": topups,
    }


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_col(row: List[str], col_idx: Optional[int], fallback: int) -> str:
    """Get column value by index or fallback."""
    idx = col_idx if col_idx is not None else fallback
    if idx is not None and idx < len(row):
        return row[idx]
    return ""


def _map_columns(header: List[str], aliases: Dict[str, List[str]]) -> Dict[str, Optional[int]]:
    """Map column names to indices using alias matching."""
    result: Dict[str, Optional[int]] = {}
    for key, names in aliases.items():
        result[key] = None
        for name in names:
            for i, h in enumerate(header):
                if h.strip().lower() == name:
                    result[key] = i
                    break
            if result[key] is not None:
                break
    return result


def _calc_hours(start: str, end: str) -> float:
    """Calculate hours between HH:MM time strings."""
    if not start or not end:
        return 0.0
    try:
        parts_s = start.strip().split(":")
        parts_e = end.strip().split(":")
        sh, sm = int(parts_s[0]), int(parts_s[1]) if len(parts_s) > 1 else 0
        eh, em = int(parts_e[0]), int(parts_e[1]) if len(parts_e) > 1 else 0
        mins = (eh * 60 + em) - (sh * 60 + sm)
        return max(0, mins / 60.0)
    except (ValueError, IndexError):
        return 0.0


def _aggregate_weekly(daily: List[Dict]) -> List[Dict]:
    """Aggregate daily series into weekly buckets."""
    weeks: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"amount": 0, "mins": 0, "count": 0})
    for day in daily:
        try:
            dt = datetime.strptime(day["date"], "%Y-%m-%d")
            iso = dt.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            weeks[wk]["amount"] += day.get("amount", 0)
            weeks[wk]["mins"] += day.get("mins", 0)
            weeks[wk]["count"] += day.get("count", 0)
        except (ValueError, KeyError):
            continue
    return [
        {"week": k, **v, "rate": round(v["amount"] / v["mins"], 2) if v["mins"] > 0 else 0}
        for k, v in sorted(weeks.items())
    ]
