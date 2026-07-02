"""PS VIBE API Server — Configuration (type-hinted)"""
import os
from pathlib import Path
from typing import Final

API_TITLE: Final[str] = "PS VIBE Sales API"
API_VERSION: Final[str] = "1.0.0"
API_DESCRIPTION: Final[str] = "REST API for PS VIBE gaming lounge"

HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))
DEBUG: bool = os.environ.get("DEBUG", "false").lower() == "true"

BASE_DIR: Final[Path] = Path(__file__).parent
# All GSheet constants removed (2026-07-01) — migrated to MySQL-only

API_KEY: str = os.environ.get("API_KEY", "")

MMT_HOURS: Final[int] = 6
MMT_MINUTES: Final[int] = 30

# ── MySQL Configuration (optional) ──
MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "")
MYSQL_PORT: int = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER: str = os.environ.get("MYSQL_USER", "")
MYSQL_PASSWORD: str = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE: str = os.environ.get("MYSQL_DATABASE", "psvibe")

# Sync configuration
SYNC_INTERVAL: int = int(os.environ.get("SYNC_INTERVAL", "300"))  # seconds

# Analytics caching TTLs removed (migrated to MySQL-only 2026-07-01)
