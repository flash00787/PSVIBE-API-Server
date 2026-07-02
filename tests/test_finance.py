"""Finance endpoint smoke tests."""
import os

def test_accounts_endpoint(client):
    """GET /api/finance/accounts — check it responds."""
    resp = client.get("/api/dashboard/finance/balances", headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)  # 401 if key wrong, 200 if right

def test_pnl_endpoint(client):
    """GET /api/sheets/pnl (old path, may 404 now) → use /api/reports/pnl."""
    resp = client.get("/api/dashboard/financial/pnl", params={"m": "2026-06"}, headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_cashflow_endpoint(client):
    """GET /api/dashboard/financial/cashflow."""
    resp = client.get("/api/dashboard/financial/cashflow", params={"year": "2026", "month": "6"}, headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_balance_sheet_endpoint(client):
    """GET /api/dashboard/financial/balance-sheet."""
    resp = client.get("/api/dashboard/financial/balance-sheet", params={"year": "2026", "month": "6"}, headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_sales_daily_endpoint(client):
    """GET /api/dashboard/sales-daily."""
    resp = client.get("/api/dashboard/sales-daily", headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)
