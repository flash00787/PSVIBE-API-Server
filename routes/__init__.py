"""PS VIBE API — Route Modules

Target structure (phased migration from monolithic app.py/patch_routes.py):

Phase 1 ✅ (this sprint):
  - config.py       → /api/config, /api/consoles
  - reports.py      → /api/reports/*, /api/stock/*
  - staff.py        → /api/staff/*
  - promotions.py   → /api/promotions/*

Phase 2 🟡 (next sprint):
  - bookings.py     → /api/bookings/*
  - members.py      → /api/members/*
  - finance.py      → /api/finance/*, /api/dashboard/financial/*
  - food.py         → /api/food/*
  
Phase 3 🔴 (planned downtime):
  - consoles.py     → /api/console*, /api/fetch_console*
  - admin.py        → /admin/* endpoints
  - webhooks.py     → /webhook/* endpoints

Import convention: All route modules export a FastAPI APIRouter as `router`.
Registered in app.py via: `app.include_router(x.router)`
"""

# Phase 1 — Already live via separate endpoints, will be moved here
# NOTE: Submodules imported lazily to avoid circular imports with app.py
# from routes import config, reports, staff, promotions  # deferred
