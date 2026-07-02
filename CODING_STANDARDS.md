# PS VIBE — Coding Standards & Architecture Rules

> **Established:** 2026-07-01  
> **Reason:** GSheet removal cleanup revealed ~25% of code was dead/legacy.  
> **Goal:** Write clean, modular code from Day 1 so refactors aren't needed later.

---

## 1. Golden Rules

### Rule #1: NO Monoliths
- **Max 500 lines per file** for new code
- If a file exceeds 1000 lines, split it within the same sprint
- Each file = ONE concern (one endpoint group, one domain)

### Rule #2: Clean from Day 1
- New features → new module file, not appended to existing
- Don't add to `app.py` or `__init__.py` — create a proper module
- Delete dead code immediately, don't comment it out

### Rule #3: No Hardcoded Values
- ALL external values → env vars with `os.environ.get()` + sensible fallback
- NO hardcoded chat IDs, API keys, passwords, URLs in code
- Use `config.py` or `.env` for all config

### Rule #4: Explicit over Implicit
- Use explicit imports: `from module import specific_function`
- Avoid `from module import *` (except for legacy compat during transition)
- Every function has a docstring explaining what it does

---

## 2. Project Structure (Target)

```
psvibe_api_server/
├── app.py                  # FastAPI app creation + middleware ONLY (<200 lines)
├── config.py               # All config/env vars
├── auth.py                 # Authentication
├── db.py                   # MySQL connection pool + helpers
├── routes/                 # All endpoint handlers
│   ├── __init__.py
│   ├── bookings.py         # Booking CRUD + checkin/extend/cancel
│   ├── members.py          # Member CRUD + wallet
│   ├── consoles.py         # Console status + games
│   ├── finance.py          # PNL, Balance Sheet, Cashflow
│   ├── food.py             # Food cart + menu
│   ├── stock.py            # Stock in/out + inventory
│   ├── staff.py            # Staff, salary, attendance
│   ├── coupons.py          # Coupon generation + validation
│   ├── promotions.py       # Promotions
│   ├── reports.py          # Daily/weekly reports
│   ├── admin.py            # Admin panel endpoints
│   ├── webhooks.py         # Webhook endpoints
│   └── bot_users.py        # Bot user tracking
├── services/               # Business logic (no FastAPI deps)
│   ├── __init__.py
│   ├── timer.py            # Session timer logic
│   ├── notifications.py    # Notification helpers
│   └── finance_calc.py     # Financial calculations
├── models/                 # Pydantic models
│   └── __init__.py
├── tests/                  # Test suite
│   ├── conftest.py
│   ├── test_bookings.py
│   ├── test_members.py
│   └── ...
└── static/                 # Dashboard built assets
```

## 3. Before/After Writing Code

### Before starting:
1. Check: does this belong in an existing module?
2. If new: create `routes/<feature>.py` with proper docstring
3. Register route in `routes/__init__.py`

### After finishing:
1. `python3 -c "import ast; ast.parse(open('routes/new_file.py').read()); print('OK')"`
2. Test with curl/Postman
3. Update `CHANGELOG.md` 
4. If modifying existing: run `python3 /root/coordination/auto_doc_updater.py --summary "..."`

---

## 4. Type Hints (Mandatory for new code)

```python
# ✅ Good
async def get_booking(booking_id: int, user: dict = Depends(get_current_user)) -> dict:
    """Return booking details by ID."""
    row = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
    return row or {}

# ❌ Bad
async def get_booking(booking_id, user=Depends(get_current_user)):
    row = _mysql_query_one("SELECT * FROM console_booking WHERE id = %s", (booking_id,))
    return row or {}
```

---

## 5. Error Handling

```python
# ✅ Good — specific exceptions
try:
    _mysql_exec("INSERT INTO ...")
except pymysql.IntegrityError as e:
    logger.warning("Duplicate: %s", e)
    return error_response("Already exists")

# ❌ Bad — bare except
try:
    _mysql_exec("INSERT INTO ...")
except:
    return error_response("Failed")
```

---

## 6. Testing (Minimum)

Every new endpoint MUST have at least:
1. **Smoke test:** Does it return 200 with valid auth?
2. **Auth test:** Does it return 401 without auth?
3. **Edge case:** What if param is missing/wrong?

```python
# tests/test_bookings.py
async def test_get_bookings_requires_auth(client):
    resp = await client.get("/api/bookings")
    assert resp.status_code == 401

async def test_get_bookings_returns_list(client, auth_headers):
    resp = await client.get("/api/bookings", headers=auth_headers)
    assert resp.status_code == 200
    assert "data" in resp.json()
```

---

## 7. Migration Plan (Existing Code)

### Phase 1: Low Risk (this sprint)
- [ ] Create `routes/` directory structure
- [ ] Move standalone endpoints (health, config, webhooks) first
- [ ] Add type hints to all new code

### Phase 2: Medium Risk (next sprint)
- [ ] Split `booking_flow.py` → `services/timer.py` + `services/notifications.py`
- [ ] Move finance routes from `dashboard_routes.py` → `routes/finance.py`
- [ ] Add basic test suite (smoke tests for critical paths)

### Phase 3: High Risk (planned)
- [ ] Split `app.py` → `routes/` modules (requires downtime window)
- [ ] Full type hints coverage
- [ ] Comprehensive test suite

---

## 8. Lessons Learned (2026-07-01 GSheet Removal)

| Lesson | Rule |
|--------|------|
| GSheet was still a live dependency after "migration" | Always verify, don't assume |
| `from app import *` hides what's actually imported | Use explicit imports |
| Lazy import wrappers silently deleted during cleanup | Test after every refactor |
| 18,878 lines of Python with 0 tests | Write tests alongside code |
| `_mysql_exec` vs `_mysql_execute` confusion | Consistent naming convention |
| 120 endpoints in one file | Max 500 lines per file |

---

_This file is living documentation. Update it when you learn something new._
