#!/usr/bin/env python3
"""
Financial Validation Script — runs daily, only alerts when something is wrong.
Checks: A = L+E, CA = FB = CF, Retained ≈ P&L cumulative
"""
import json
import urllib.request
import os
import sys

API_BASE = "http://localhost:8000"
TOKEN_URL = f"{API_BASE}/auth/login"
BS_URL = f"{API_BASE}/api/dashboard/financial/balance-sheet"
CF_URL = f"{API_BASE}/api/dashboard/financial/cashflow?year=2026&month=6"
PNL_URL = f"{API_BASE}/api/dashboard/financial/pnl?year=2026&month=6"
FB_URL = f"{API_BASE}/api/dashboard/finance/balances"

def api_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def main():
    issues = []

    # 1. Login
    try:
        login_payload = json.dumps({"username": "admin", "password": "admin123"}).encode()
        req = urllib.request.Request(TOKEN_URL, data=login_payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            token = json.loads(r.read()).get("access_token", "")
        if not token:
            issues.append("🔴 Auth failed — cannot run validation")
            return issues
    except Exception as e:
        issues.append(f"🔴 Login error: {e}")
        return issues

    # 2. Balance Sheet — A = L+E?
    try:
        bs = api_get(BS_URL, token)["data"]
        assets = bs["assets"]["total_assets"]
        le = bs["total_liabilities_equity"]
        gap = round(assets - le, 0)
        if abs(gap) > 50000:
            issues.append(f"🔴 BS Unbalanced! A({assets:,.0f}) − L+E({le:,.0f}) = {gap:,.0f} Ks")
    except Exception as e:
        issues.append(f"🔴 BS fetch error: {e}")

    # 3. CA = FB = CF?
    try:
        ca = bs["assets"]["current"]["total"]
        fb = api_get(FB_URL, token)
        fb_total = sum(a["balance"] for a in fb["accounts"])
        cf = api_get(CF_URL, token)["data"]["summary"]["closing_balance"]
        if abs(ca - fb_total) > 1000 or abs(ca - cf) > 1000:
            issues.append(f"🔴 CA/FB/CF mismatch! CA={ca:,.0f} FB={fb_total:,.0f} CF={cf:,.0f}")
    except Exception as e:
        issues.append(f"🔴 CA/FB/CF fetch error: {e}")

    # 4. Retained ≈ P&L cumulative?
    try:
        retained = bs["equity"]["retained_earnings"]
        pnl = api_get(PNL_URL, token)["data"]["net_profit"]
        # Estimate cumulative: P&L_this_month + retained_last_month ≈ retained_this
        # Since business just started, retained should ≈ P&L net profit (within dep range)
        diff = abs(retained - pnl)
        if diff > 500000:  # >5L difference is suspicious
            issues.append(f"🔴 Retained({retained:,.0f}) vs P&L({pnl:,.0f}) diff={diff:,.0f} Ks")
    except Exception as e:
        issues.append(f"🔴 Retained/P&L error: {e}")

    return issues

if __name__ == "__main__":
    issues = main()
    if issues:
        # Only print output when there are issues
        print("\n".join(issues))
        sys.exit(1)
    else:
        # Silent when all good
        sys.exit(0)
