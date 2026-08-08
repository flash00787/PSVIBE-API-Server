# PS VIBE API Server — Changelog

## 2026-08-08 — Tournaments Backend (Phase 1)

### 🏆 New Feature: Tournament Registration & Bracket Management
- **New tables**: `tournaments`, `tournament_players`, `tournament_matches` — full bracket management
- **New router**: `routes/tournament_routes.py` (14 endpoints, 843 lines)
  - Admin CRUD: create, list, get, update tournaments
  - Admin player management: add, confirm payment, remove
  - Bracket ops: start (seed + generate R1), submit scores (advance winner), standings
  - Public: HTML registration page (`/t/{token}`), register endpoint, bracket view, player list
- **Wallet integration**: Paid tournaments with member_id auto-deduct from `member_wallets` balance, recording `eject` cash_movements. Uses existing cash_movements enum (`eject` type) — no new enum values needed.
- **Duplicate protection**: phone dedup per tournament (refund if wallet was deducted). Duplicate → 409.
- **Tournament registration page**: mobile-friendly Burmese/English HTML form at `/t/{token}`, served directly by FastAPI (no external deps).
- **Bracket logic**: standard single-elimination seeding with byes for non-power-of-2 counts. Winner auto-advances; final match → tournament `completed`.
- **Tests**: 14 tests covering CRUD, auth, free/paid registration, wallet deduction, cash_movements, duplicate rejection, bracket generation, score advancement, tie rejection, final completion, HTML page, public endpoints. All passing.

### Files Changed
| File | Change |
|------|--------|
| `routes/tournament_routes.py` | New — tournament endpoints |
| `tests/test_tournaments.py` | New — 14 test cases |
| `app.py` | +2 lines — import & register tournament router |
| `CHANGELOG.md` | This entry |

## 2026-07-01 — Security Hardening & Quality Cleanup

### 🔐 Security Hardening
- **#34**: Added deprecation warning for API key via query string. Header-based auth (`X-API-Key`) and JWT Bearer token now preferred.
- **#35**: CORS restricted from wildcard `*` to specific dashboard origins (`http://localhost:8888`, production host).
- **#36**: Token/key logging audit — verified no secrets leaked in logs. Token length logged instead of token value.
- **#37**: Added `auth=Depends(verify_api_key)` to 9 unprotected mutation endpoints (add/remove console, update multiplier, game CRUD).
- **#38**: Content-Type validation helper added. JSON endpoints now validate `application/json` Content-Type.
- **#39**: Standardized error response format across all endpoints. `error_response()` helper with consistent `status/error/detail` structure.

### 🧹 Code Quality
- **#21**: Archived one-time patch/migration scripts to `archive/` folder with README.
- **#22**: Added module docstrings to 19 Python files missing them.
- **#23**: Verified naming consistency — no issues found.
- **#24**: Deprecated GSheet imports/appended code — removed commented-out GSheet fallback code from `app.py`.

### 📚 Documentation
- **#45**: Created comprehensive `README.md` with architecture overview, setup, env vars, and API docs.
- **#46**: Created `SCHEMA.md` with database table documentation.
- **#47**: Created `constants.py` with named constants for all magic numbers.
- **#48**: Created this `CHANGELOG.md`.
- **#49**: Created `setup.sh` prerequisite checker script.

### 🔧 Infrastructure
- **#50**: Project migration path documented. API server remains at `/root/psvibe_api_server/` until next maintenance window.

### Files Changed
| File | Change |
|------|--------|
| `app.py` | CORS restriction, query-param deprecation warning, auth added to 9 endpoints, Content-Type validation, error standardization |
| `constants.py` | **NEW** — Named constants |
| `README.md` | **NEW** — API server documentation |
| `SCHEMA.md` | **NEW** — DB schema docs |
| `CHANGELOG.md` | **NEW** — This file |
| `setup.sh` | **NEW** — Prerequisite checker |
| `archive/` | **NEW** — Archived patch scripts |
| 19 `.py` files | Added module docstrings |
