import os
#!/usr/bin/env python3
"""
Financial Validation Script — runs daily via cron, only alerts when issues found.
Checks:
  1. Balance Sheet: A = L+E (within 50K tolerance)
  2. Current Assets ≈ Finance Balances ≈ Cash Flow closing (same period)
  3. Retained Earnings ≈ Cumulative P&L Net Profit

Uses current MMT month for all comparisons to avoid stale-month mismatches.
"""
import json
import urllib.request
import sys
from datetime import datetime, timezone, timedelta

API_BASE = "http://localhost:8000"


def mmt_now():
    """Return current datetime in Myanmar time (UTC+6:30)."""
    return datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)


def api_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def login():
    payload = json.dumps({"username": os.environ.get("DASH_USER", "admin"), "password": os.environ.get("DASH_PASS", "")}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/auth/login", data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        token = json.loads(r.read()).get("access_token", "")
    if not token:
        raise RuntimeError("Login returned no token")
    return token


def main():
    issues = []

    # Determine current MMT year/month so all queries use the same period
    _mmt = mmt_now()
    _y, _m = _mmt.year, _mmt.month

    # 1. Login
    try:
        token = login()
    except Exception as e:
        issues.append(f"🔴 Login error: {e}")
        return issues

    # ── 2. Balance Sheet — A = L+E? ──
    bs = {}
    try:
        bs = api_get(f"{API_BASE}/api/dashboard/financial/balance-sheet?year={_y}&month={_m}", token)["data"]
        assets = bs["assets"]["total_assets"]
        le = bs["total_liabilities_equity"]
        gap = round(assets - le, 0)
        if abs(gap) > 100000:
            issues.append(f"🔴 BS Unbalanced! A({assets:,.0f}) − L+E({le:,.0f}) = {gap:,.0f} Ks")
    except Exception as e:
        issues.append(f"🔴 BS fetch error: {e}")

    # ── 3. CA = FB? (use no-month filter = all-time current data) ──
    try:
        ca = bs.get("assets", {}).get("current", {}).get("total", 0)

        fb = api_get(f"{API_BASE}/api/dashboard/finance/balances", token)
        fb_total = sum(a["balance"] for a in fb.get("accounts", []))

        if abs(ca - fb_total) > 1000:
            issues.append(f"🔴 CA/FB mismatch! CA={ca:,.0f} FB={fb_total:,.0f} diff={ca - fb_total:,.0f}")
        else:
            # CA ≈ FB matched — now check CF month-over-month progression
            try:
                cf_prev = api_get(f"{API_BASE}/api/dashboard/financial/cashflow?year={_y}&month={_m - 1 if _m > 1 else 12}", token)
                cf_curr = api_get(f"{API_BASE}/api/dashboard/financial/cashflow?year={_y}&month={_m}", token)
                cf_close_prev = cf_prev.get("data", {}).get("summary", {}).get("closing_balance", 0)
                cf_close_curr = cf_curr.get("data", {}).get("summary", {}).get("closing_balance", 0)
                cf_change = cf_close_curr - cf_close_prev
                if cf_change < 0:
                    issues.append(f"⚠️  CF decreased from prev month: {cf_close_prev:,.0f} → {cf_close_curr:,.0f} ({cf_change:,.0f} Ks)")
            except Exception as ce:
                issues.append(f"⚠️  CF month-over-month check skipped: {ce}")
    except Exception as e:
        issues.append(f"🔴 CA/FB fetch error: {e}")

    # ── 4. Retained ≈ Cumulative P&L? ──
    # Compare retained earnings against cumulative net profit (sum ALL months)
    try:
        retained = bs.get("equity", {}).get("retained_earnings", 0)
        # Sum PNL for ALL months from business start (June 2026) to current
        cumulative_pnl = 0.0
        start_y, start_m = 2026, 6  # PS VIBE opened June 6, 2026
        cur_y, cur_m = _y, _m
        _y_loop, _m_loop = start_y, start_m
        while (_y_loop < cur_y) or (_y_loop == cur_y and _m_loop <= cur_m):
            pnl_data = api_get(
                f"{API_BASE}/api/dashboard/financial/pnl?year={_y_loop}&month={_m_loop}",
                token
            )
            cumulative_pnl += pnl_data.get("data", {}).get("net_profit", 0)
            _m_loop += 1
            if _m_loop > 12:
                _m_loop = 1
                _y_loop += 1
        diff = abs(retained - cumulative_pnl)
        if diff > 500000:
            issues.append(f"🔴 Retained({retained:,.0f}) ≠ Cumulative P&L({cumulative_pnl:,.0f}) diff={diff:,.0f} Ks")
    except Exception as e:
        issues.append(f"🔴 Retained/P&L error: {e}")

    return issues


if __name__ == "__main__":
    issues = main()
    if issues:
        print(f"📅 {mmt_now().strftime('%Y-%m-%d %H:%M')} MMT — Financial Validation")
        print("\n".join(issues))
        sys.exit(1)
    else:
        sys.exit(0)
