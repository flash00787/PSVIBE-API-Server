"""Authentication tests."""
def test_dashboard_auth_required(client):
    resp = client.get("/api/dashboard/stats")
    assert resp.status_code in (401, 403)

def test_dashboard_bad_key_rejected(client):
    """Dashboard stats rejects bad API key."""
    resp = client.get("/api/dashboard/stats", headers={"x-api-key": "wrong-key"})
    assert resp.status_code in (401, 403)
