"""PS VIBE — money-path regression tests (SAFE: no writes).

Rules baked in:
  * NO INSERT/UPDATE/DELETE anywhere in this module.
  * DB checks are read-only SELECTs and auto-skip when MySQL is unreachable.
  * Never import a fixture that touches the live DB schema (2026-06 lesson:
    an autouse fixture once wiped live tournaments).

Run:  /opt/psvibe/api/venv/bin/python -m pytest tests/test_money_guards.py -q
"""
import os
import sys
from datetime import datetime, time

import pytest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, API_DIR)

import sale_money as sm  # pure helpers, no I/O


# ── 1) 2+1 duration promo: billed minutes table (owner-approved values) ──────
def test_billable_minutes_table():
    cases = {120: 120, 179: 120, 180: 120, 181: 121, 240: 180, 300: 240, 360: 240}
    for total, want in cases.items():
        assert sm.billable_minutes(total) == want, f"{total}m -> {sm.billable_minutes(total)}, want {want}"
    # free minutes can never be negative or exceed the session
    for t in (60, 120, 179, 180, 181, 480, 1000):
        b = sm.billable_minutes(t)
        assert 0 <= b <= t


# ── 2) Happy-hour window boundary (14:59 in, 15:05 out) ─────────────────────
def test_promo_window_boundary():
    row = dict(
        status="active", applies_auto=1, weekday_mask=62,
        time_window_start=time(9, 0), time_window_end=time(15, 0),
        start_date=None, end_date=None, tier_charge_mins=120, tier_cycle_mins=180,
    )
    inside = datetime(2026, 10, 27, 14, 59)
    outside = datetime(2026, 10, 27, 15, 5)
    assert sm.duration_promo_applies(inside, row) is True
    assert sm.duration_promo_applies(outside, row) is False


# ── 3) payment-method amount cleaner ('6000 (dep)' / '6000(DepositRedeemed)') ─
def test_pm_clean_strips_deposit_markers():
    from routes.finance_routes import _pm_clean
    assert _pm_clean("6000 (dep)") == "6000"
    assert _pm_clean("6000(DepositRedeemed)") == "6000"
    assert _pm_clean("") == "0"
    assert float(_pm_clean("14000")) == 14000.0


# ── DB-backed checks (read-only) ────────────────────────────────────────────
def _db():
    try:
        import pymysql
        return pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "psvibe_user"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "psvibe_api"),
            charset="utf8mb4",
        )
    except Exception:
        return None


def test_deposit_never_redeemed_twice():
    """A booking must have at most ONE redemption row (idempotency guard)."""
    c = _db()
    if not c:
        pytest.skip("MySQL not reachable")
    try:
        cur = c.cursor()
        cur.execute(
            "SELECT booking_id, COUNT(*) n FROM deposit_redemptions "
            "GROUP BY booking_id HAVING n > 1 LIMIT 5"
        )
        dupes = cur.fetchall()
        assert not dupes, f"duplicate redemptions: {dupes}"
    finally:
        c.close()


def test_balance_equals_component_sum():
    """The reported balance must equal its own components for plain accounts."""
    c = _db()
    if not c:
        pytest.skip("MySQL not reachable")
    c.close()
    sys.path.insert(0, "/root/coordination")
    from psvibe_balance_audit import audit

    rows = {r["key"]: r for r in audit()}
    for key in ("cash", "mmqr_cb", "kpay"):
        if key not in rows:
            continue
        r = rows[key]
        assert r["delta_vs_formula"] == 0, (
            f"{key}: balance {r['balance']} != formula {r['formula_core']} "
            f"(delta {r['delta_vs_formula']})"
        )
