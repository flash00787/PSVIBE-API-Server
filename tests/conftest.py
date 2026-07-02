"""PS VIBE API — Test fixtures."""
import pytest
import os
import sys

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test env vars before importing app
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-pytest-123456789")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_USER", "psvibe_user")
os.environ.setdefault("MYSQL_PASSWORD", "PsVibe@2026_Rotated!")
os.environ.setdefault("MYSQL_DATABASE", "psvibe_api")
os.environ.setdefault("API_SECRET_KEY", "test-api-key-pytest")
os.environ.setdefault("BOT_TOKEN", "12345:test")
os.environ.setdefault("CUSTOMER_BOT_TOKEN", "12345:test")


@pytest.fixture
def app():
    """Create FastAPI test app."""
    from app import app as fastapi_app
    return fastapi_app


@pytest.fixture
def client(app):
    """Create test client."""
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Return headers with valid API key."""
    return {"x-api-key": "test-api-key-pytest"}
