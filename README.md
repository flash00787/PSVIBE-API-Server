# PS VIBE API Server

FastAPI REST API for PS VIBE gaming lounge — booking management, member wallets, inventory, staff, and analytics.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     PS VIBE API Server                       │
│                   (FastAPI + Uvicorn)                        │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐ │
│  │  app.py  │  │ auth.py  │  │config.py │  │models.py   │ │
│  │  (core)  │  │  (JWT)   │  │ (env)    │  │(Pydantic)  │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬──────┘ │
│       │              │              │               │        │
│  ┌────┴──────────────┴──────────────┴───────────────┴─────┐ │
│  │                    Data Layer                            │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │ │
│  │  │  mysql_db.py │  │sheets_client │  │  FIFO modules│ │ │
│  │  │  (MySQL 8.0) │  │  (GSheets)   │  │ stock/inv.   │ │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘ │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Feature Modules                            │ │
│  │  dashboard_routes.py  digest_routes.py  kora_cmds.py   │ │
│  │  loyalty_endpoints.py  new_game_endpoints.py            │ │
│  │  patch_routes.py  session_timer.py  booking_reminder.py│ │
│  │  analytics.py  analytics_sync.py  daily_summary.py     │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**Stack:** Python 3.12 + FastAPI + Uvicorn → MySQL 8.0 (primary) + Google Sheets (legacy read)

## Quick Start

```bash
# 1. Check prerequisites
bash setup.sh

# 2. Set required environment variables
export JWT_SECRET_KEY="your-secure-random-key"
export API_KEY="your-api-key"              # For bot/n8n integrations
export MYSQL_HOST="localhost"
export MYSQL_USER="root"
export MYSQL_PASSWORD="your-db-password"
export MYSQL_DATABASE="psvibe_api"
export CUSTOMER_BOT_TOKEN="..."            # Telegram customer bot
export BOT_TOKEN="..."                     # Telegram sale bot

# 3. Start the server
python3 app.py
# Or:
sudo systemctl start psvibe-api
```

API docs available at:
- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JWT_SECRET_KEY` | ✅ | — | JWT signing key for dashboard auth |
| `API_KEY` | ⚠️ | — | API key for bot/n8n integrations |
| `MYSQL_HOST` | ⚠️ | — | MySQL host (omit for file fallback) |
| `MYSQL_USER` | ⚠️ | — | MySQL user |
| `MYSQL_PASSWORD` | ⚠️ | — | MySQL password |
| `MYSQL_DATABASE` | — | `psvibe` | Database name |
| `CUSTOMER_BOT_TOKEN` | — | — | Telegram customer bot token |
| `BOT_TOKEN` | — | — | Telegram sale bot token |
| `WEBHOOK_SECRET` | — | — | n8n webhook shared secret |
| `CORS_EXTRA_ORIGINS` | — | — | Comma-separated extra CORS origins |
| `HOST` | — | `0.0.0.0` | Bind address |
| `PORT` | — | `8000` | Bind port |
| `DEBUG` | — | `false` | Enable debug mode |
| `SYNC_INTERVAL` | — | `300` | GSheet sync interval (seconds) |
| `SHEET_ID` | — | — | Google Sheets spreadsheet ID |
| `SERVICE_ACCOUNT_FILE` | — | `service_account.json` | GSheet SA file path |

## API Endpoints

### Authentication
All `/api/*` endpoints (except health checks and webhooks) require authentication via:
- **Header:** `X-API-Key: your-key` (preferred)
- **Query:** `?api_key=your-key` (deprecated)
- **Bearer:** `Authorization: Bearer <jwt_token>` (dashboard users)

### System
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/health` | — | Health check |
| GET | `/api/mysql/health` | — | MySQL connectivity check |
| GET | `/api/mysql/status` | — | MySQL table status |

### Bookings
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/bookings` | API Key | Create booking |
| GET | `/api/bookings` | API Key | List bookings |
| GET | `/api/bookings/{id}` | API Key | Get booking detail |
| PATCH | `/api/bookings/{id}/status` | API Key | Approve/reject booking |
| POST | `/api/bookings/checkin` | API Key | Check in |
| PUT | `/api/end_booking/{id}` | API Key | End booking |
| POST | `/api/bookings/cancel` | API Key | Cancel booking |
| POST | `/api/booking-conflicts` | API Key | Check conflicts |
| POST | `/api/bookings/auto-cancel-no-show` | API Key | No-show cleanup (cron) |
| GET | `/api/available-slots` | API Key | Get available slots |

### Members
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/fetch_members` | API Key | List all members |
| GET | `/api/fetch_member_data/{id}` | API Key | Member detail |
| GET | `/api/fetch_wallet_mins/{id}` | API Key | Wallet minutes |
| POST | `/api/wallet/deduct` | API Key | Deduct wallet |
| POST | `/api/members/register` | API Key | Register new member |
| POST | `/api/member/wallet/update` | API Key | Update wallet balance |

### Consoles & Games
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/fetch_console_status` | API Key | Console statuses |
| GET | `/api/fetch_games` | API Key | Game list |
| POST | `/api/add_game` | API Key | Add game |
| PUT | `/api/edit_game` | API Key | Edit game |
| DELETE | `/api/delete_game/{title}` | API Key | Delete game |

### Staff & Attendance
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/fetch_staff` | API Key | Staff list |
| POST | `/api/save_attendance` | API Key | Record attendance |

### Sales & Top-ups
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/sales/record` | API Key | Record sale |
| POST | `/api/topup/log` | API Key | Log top-up |

### Admin / Dashboard
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| * | `/api/opex/*` | JWT | Expense management |
| * | `/api/suggestions/*` | JWT | Game suggestions |

## Deployment

```bash
# Systemd service
sudo systemctl enable psvibe-api
sudo systemctl start psvibe-api

# Logs
journalctl -u psvibe-api -f

# Restart
sudo systemctl restart psvibe-api
```

## Database

Primary: **MySQL 8.0** (`psvibe_api`)  
Legacy: Google Sheets (read-only fallback for analytics)

See [SCHEMA.md](SCHEMA.md) for full database documentation.

## Project Structure

```
psvibe_api_server/
├── app.py                  # Main FastAPI application (5200+ lines)
├── config.py               # Configuration constants from env
├── constants.py            # Named constants (magic numbers)
├── auth.py                 # JWT + dashboard authentication
├── models.py               # Pydantic response models
├── mysql_db.py             # MySQL wrapper (query/execute)
├── sheets_client.py        # Google Sheets client (legacy)
├── dashboard_routes.py     # Dashboard web routes
├── digest_routes.py        # Analytics digest API
├── kora_cmds.py            # Kora shell command API
├── loyalty_endpoints.py    # Loyalty program endpoints
├── new_game_endpoints.py   # Game management endpoints
├── patch_routes.py         # Additional API routes
├── session_timer.py        # Session reminder timers
├── booking_reminder.py     # Booking reminder system
├── analytics.py            # Analytics queries
├── analytics_sync.py       # GSheet analytics sync
├── daily_summary.py        # Daily summary reports
├── digest_engine.py        # Digest generation engine
├── stock_fifo.py           # FIFO stock tracking
├── inventory_fifo.py       # FIFO inventory tracking
├── fifo_wallet.py          # FIFO wallet operations
├── field_utils.py          # Field name normalization
├── branch_utils.py         # Branch context utilities
├── console_status.py       # Console status sync
├── setup.sh                # Prerequisite checker
├── SCHEMA.md               # Database documentation
├── CHANGELOG.md            # Change history
├── README.md               # This file
├── archive/                # Archived migration scripts
└── staging/                # Staging files & DB dumps
```
