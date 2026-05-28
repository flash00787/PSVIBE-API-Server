"""PS VIBE API Server — Configuration"""
import os
from pathlib import Path

API_TITLE = "PS VIBE Sales API"
API_VERSION = "1.0.0"
API_DESCRIPTION = "REST API for PS VIBE gaming lounge"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

BASE_DIR = Path(__file__).parent
SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", str(BASE_DIR / "service_account.json")
)
SHEET_ID = os.environ.get("SHEET_ID", "")

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SHEET_SALES_DAILY      = "Sales_Daily"
SHEET_SETTING           = "Setting"
SHEET_CARD_WALLET       = "Card_Wallet"
SHEET_STOCK_OUT         = "Stock_Out"
SHEET_STOCK_IN          = "Stock_In"
SHEET_TOPUP_LOG         = "TopUp_Log"
SHEET_INVENTORY         = "Inventory"
SHEET_ATTENDANCE_LOG    = "Attendance_Log"
SHEET_CONSOLE_BOOKING   = "Console_Booking"
SHEET_SALARY_ADVANCE    = "Salary_Advance"
SHEET_GAME_LIBRARY      = "Game_Library"
SHEET_CONSOLE_GAMES     = "Console_Games"

CACHE_TTL_CONFIG     = 300
CACHE_TTL_MEMBERS    = 180
CACHE_TTL_BOOKINGS   = 30
CACHE_TTL_GAMES      = 600
CACHE_TTL_CONSOLE_GAMES = 300

API_KEY = os.environ.get("API_KEY", "")

MMT_HOURS = 6
MMT_MINUTES = 30

# ── MySQL Configuration (optional) ──
MYSQL_HOST = os.environ.get("MYSQL_HOST", "")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "psvibe")

# Sync configuration
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "300"))  # seconds

# Analytics caching TTLs (seconds)
CACHE_TTL_SALES_DAILY = 60
CACHE_TTL_TOPUP_LOG = 60
CACHE_TTL_SETTING = 120
