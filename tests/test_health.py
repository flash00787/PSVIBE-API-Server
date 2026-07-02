"""Health check + smoke tests."""
def test_root_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200

def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"

def test_mysql_health(client, auth_headers):
    resp = client.get("/api/mysql/health", headers=auth_headers)
    assert resp.status_code == 200

def test_config_returns_ok(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)

def test_config_with_auth(client, auth_headers):
    resp = client.get("/api/config", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)

def test_consoles_with_auth(client, auth_headers):
    resp = client.get("/api/consoles", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("success") == True
    assert "consoles" in data

def test_bookings_endpoint(client):
    resp = client.get("/api/bookings")
    assert resp.status_code in (200, 401)
