# PS VIBE API Server — Deployment Summary

## Overview
FastAPI REST API server for PS VIBE - PS5 Gaming Lounge.
Reads Google Sheets data directly via Service Account.

## Location
- **Server:*/ /root/psvibe_api_server/
- **Python venv:** /root/psvibe_api_server/venv/
-  **SA Key:** /root/psvibe_api_server/service_account.json

## Running
- **Process:** Uvicorn on port 8000 (http://0.0.0.0:8000)
- **Health Check:** `curl http://localhost:8000/api/health`

### systemd Service (recommended)
Create service file at `/etc/systemd/system/psvibe-api.service`:
```ini
[Unit]
Description=PS VIBE API Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/psvibe_api_server
ExecStart=/root/psvibe_api_server/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now psvibe-api.service
```

## Endpoints (30+)

### System
- `GET /api/health` — Health check

### Members
- `GET /api/fetch_members` — All member IDs
- `GET /api/fetch_member_data/{member_id}` — Full member data
- `GET /api/fetch_wallet_mins/{member_id}` — Wallet balance (formula)
- `GET /api/fetch_balance_mins/{member_id}` — Live balance (bypass cache)
- `GET /api/fetch_member_tier/{member_id}` — Current tier
- `GET /api/fetch_member_effective_rate/{member_id}` — Stored effective rate
- `GET /api/build_member_rate_dict` — All member rates
- `GET /api/next_member_id` — Auto-increment member ID
- `GET /api/next_member_row_no` — Next sequential row number
- `GET /api/fetch_referral_code/{member_id}` — Referral code
- `PUT /api/update_member_effective_rate` — Update effective rate
- `POST /api/save_referral_code` — Save referral code

### Console
- `GET /api/fetch_console_status` — Console live status
- `POST /api/add_console_to_setting` — Add console
- `DELETE /api/remove_console_from_setting/{console_id}` — Remove console

### Games
- `GET /api/fetch_games` — All games (cached 10m)
- `GET /api/fetch_game_library` — Alias
- `GET /api/fetch_console_games` — Console-game installations (cached 5m)
- `GET /api/get_games_on_console/{console_id}` � Games on a console
- `GET /api/get_consoles_with_game` — Consoles with a game
- `POST /api/add_console_game` — Add game installation
- `DELETE /api/remove_console_game` — Remove game installation
- `PUD /api/set_game_disc_count` — Update disc count
- `PUT /api/update_game_library_install` — Toggle install checkbox

### Staff & Attendance
- `GET /api/fetch_staff` — Staff names
- `GET /api/fetch_staff_names` — Alias
- `GET /api/fetch_base_salaries` — Base salaries
- `GET /api/fetch_allowed_staff_ids` — Staff whitelist
- `GET /api/fetch_attendance/{month_str}` — Attendance records
- `POST /api/save_attendance` — Save/update attendance
- `GET /api/fetch_salary_advances/{month_str}` — Salary advances

### Settings
- `GET /api/fetch_base_rate` — Hourly base rate
- `GET /api/fetch_console_multiplier/{console_id}` — Console multiplier
- `GET /api/fetch_new_member_defaults` — Default card price & mins
- `GET /api/fetch_rank_thresholds` — Rank thresholds
- `GET /api/fetch_bonus_table` — Bonus table
- `GET /api/fetch_rank_table_display` — Formatted rank table

### Food
- `GET /api/fetch_food_prices` — Food prices
- `GET /api/fetch_food_costs` — Food costs

### Analytics
- `GET /api/fetch_alltime_effective_rate` — All-time Ks/min

### Sales
- `GET /api/next_voucher` — Next voucher number

### Bookings
- `POST /api/create_booking` — Create booking
- `PUT /api/end_booking/{booking_id}` — End booking
- `PUT /api/cancel_booking/{booking_id}` — Cancel booking

### Receipts
- `POST /api/save_receipt_json` — Save receipt

### Meta
- `GET /api/sheets/config` — Cached bot config

## Files

| File | Description |
|-----|-----------|
| app.py | Main FastAPI app (51KB, all endpoints) |
| config.py | Configuration (Sheets ID, ports, etc.) |
| sheets_client.py | Google Sheets API client (SA auth) |
| models.py | Pydantic models |
| requirements.txt | Python dependencies |
| Dockerfile | Container build |
| service_account.json | Google SA credentials |
| server.log | Runtime log |
| venv/ | Python virtual environment |

## Notes
- All GET endpoints support optional `?api_key=` parameter for auth (configurable)
- Google Sheets caching: 30s default, 10m for games, 5m for console-games
- OpenAPI docs: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
