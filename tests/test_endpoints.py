"""New clean endpoint smoke tests."""
import os

def test_promotions_endpoint(client):
    resp = client.get("/api/promotions", headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_promotions_log_endpoint(client):
    resp = client.get("/api/promotions/log", headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_reports_daily_endpoint(client):
    resp = client.get("/api/dashboard/sales-daily", headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_staff_breakdown_endpoint(client):
    resp = client.get("/api/dashboard/staff", headers={"x-api-key": "test"})
    assert resp.status_code in (200, 401)

def test_new_endpoints_not_404(client):
    """Verify new endpoints don't 404."""
    endpoints = [
        "/api/config",
        "/api/consoles",
        "/api/dashboard/sales-daily",
        "/api/dashboard/financial/pnl?year=2026&month=6",
        "/api/promotions",
        "/api/promotions/log",
        "/api/dashboard/staff",
    ]
    for ep in endpoints:
        resp = client.get(ep, headers={"x-api-key": "test"})
        assert resp.status_code != 404, f"{ep} returned 404!"
