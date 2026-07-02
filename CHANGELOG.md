# PS VIBE API Server — Changelog

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
