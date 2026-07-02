"""PS VIBE API — Named constants for magic numbers and configuration values."""
from datetime import timedelta

# ── Booking & Session Timing ─────────────────────────────────────────
BOOKING_OVERLAP_WINDOW_MINUTES = 30
CANCELLATION_WINDOW_HOURS = 2
NO_SHOW_GRACE_MINUTES = 15
SESSION_REMINDER_OFFSET_MINUTES = 5
MAX_REMIND_LOOP_FIRES = 10
EXTEND_DEFAULT_MINUTES = 30

# ── Rate Limits ─────────────────────────────────────────────────────
RATE_LIMIT_10PM = timedelta(minutes=1)   # 10 per minute
RATE_LIMIT_30PM = timedelta(seconds=2)   # 30 per minute
RATE_LIMIT_5PM = timedelta(seconds=12)   # 5 per minute
RATE_LIMIT_20PH = timedelta(seconds=180) # 20 per hour

# ── API & Network ───────────────────────────────────────────────────
API_TIMEOUT_SECONDS = 5
TELEGRAM_TIMEOUT_SECONDS = 5
SYNC_INTERVAL_DEFAULT_SECONDS = 300

# ── Cache TTLs (seconds) ────────────────────────────────────────────
CACHE_TTL_CONFIG = 300
CACHE_TTL_MEMBERS = 180
CACHE_TTL_BOOKINGS = 30
CACHE_TTL_GAMES = 600
CACHE_TTL_CONSOLE_GAMES = 300
CACHE_TTL_SALES_DAILY = 60
CACHE_TTL_TOPUP_LOG = 60
CACHE_TTL_SETTING = 120

# ── Wallet & Membership ─────────────────────────────────────────────
MEMBER_LIABILITY_DEFAULT = 999
DEFAULT_BASE_RATE = 3000
DEFAULT_WALLET_BALANCE = 0
MIN_TOPUP_AMOUNT = 1000

# ── Booking ─────────────────────────────────────────────────────────
MAX_BOOKING_DURATION_MINUTES = 360  # 6 hours
MIN_BOOKING_DURATION_MINUTES = 30
DEFAULT_BOOKING_DURATION = 60
MAX_DAILY_BOOKINGS_PER_MEMBER = 3
BOOKING_ADVANCE_DAYS = 14

# ── Timezone ────────────────────────────────────────────────────────
MMT_HOURS_OFFSET = 6
MMT_MINUTES_OFFSET = 30

# ── Pagination ──────────────────────────────────────────────────────
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

# ── Misc ────────────────────────────────────────────────────────────
SUGGESTIONS_FILE = "/root/psvibe-discord-bot/suggestions.json"
BRANCH_ID_DEFAULT = 1
