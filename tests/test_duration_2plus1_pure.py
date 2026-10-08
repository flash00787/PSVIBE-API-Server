"""Pure-function tests for the Weekday Happy Hour "2+1" duration promo.

SAFE TO RUN: these tests only exercise the pure helpers in ``sale_money``
(``billable_minutes`` / ``duration_promo_applies`` / ``compute_duration_2plus1``).
They never import the FastAPI app, never touch a fixture that touches the DB, and
perform NO I/O. Do NOT run the wider psvibe pytest suite against the live DB.
"""
import sys
import os
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sale_money as sm


def test_billable_minutes_table():
    """The owner-approved 8 example values MUST pass (charge=120, cycle=180)."""
    cases = {
        120: 120,
        180: 120,
        181: 121,
        240: 180,
        300: 240,
        360: 240,
        420: 300,
        480: 360,
    }
    for T, want in cases.items():
        assert sm.billable_minutes(T) == want, f"T={T}: expected {want}, got {sm.billable_minutes(T)}"


def test_billable_minutes_free_matches_total():
    for T in (60, 120, 179, 180, 181, 240, 300, 359, 360, 420, 480, 1000):
        b = sm.billable_minutes(T)
        free = T - b
        assert 0 <= b <= T
        assert free >= 0
        assert b + free == T


def test_billable_minutes_custom_tier():
    assert sm.billable_minutes(180, charge=90, cycle=180) == 90
    assert sm.billable_minutes(100, charge=120, cycle=180) == 100  # under one cycle, capped at charge


def _row(**over):
    base = dict(
        status="active",
        applies_auto=1,
        weekday_mask=62,  # Mon–Fri
        time_window_start=time(9, 0),
        time_window_end=time(15, 0),
        start_date=None,
        end_date=None,
        tier_charge_mins=120,
        tier_cycle_mins=180,
    )
    base.update(over)
    return base


def test_applies_weekday_window():
    # Mon 2026-10-05 10:00 MMT → applies
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 10, 0), _row()) is True
    # Fri 10:00 → applies
    assert sm.duration_promo_applies(datetime(2026, 10, 9, 10, 0), _row()) is True
    # Sat 10:00 → excluded
    assert sm.duration_promo_applies(datetime(2026, 10, 3, 10, 0), _row()) is False
    # Sun 10:00 → excluded
    assert sm.duration_promo_applies(datetime(2026, 10, 4, 10, 0), _row()) is False


def test_applies_time_window():
    # exactly 09:00 → applies (inclusive lower bound)
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 9, 0), _row()) is True
    # 14:59 → applies
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 14, 59), _row()) is True
    # 15:00 → excluded (EXCLUSIVE upper bound)
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 15, 0), _row()) is False
    # 08:59 → excluded
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 8, 59), _row()) is False


def test_applies_status_and_auto():
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 10, 0), _row(status="inactive")) is False
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 10, 0), _row(applies_auto=0)) is False
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 10, 0), None) is False
    assert sm.duration_promo_applies(None, _row()) is False


def test_applies_date_window():
    start = datetime(2026, 10, 5, 10, 0)
    assert sm.duration_promo_applies(start, _row(start_date="2026-10-01", end_date="2026-10-31")) is True
    assert sm.duration_promo_applies(start, _row(start_date="2026-10-06", end_date="2026-10-31")) is False
    assert sm.duration_promo_applies(start, _row(start_date="2026-10-01", end_date="2026-10-04")) is False


def test_applies_timezone_aware_utc_converts_to_mmt():
    # 09:30 MMT == 03:00 UTC on a Monday
    utc = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    assert sm.duration_promo_applies(utc, _row()) is True


def test_applies_timedelta_time_window():
    # pymysql returns TIME columns as timedelta
    row = _row(time_window_start=timedelta(hours=9), time_window_end=timedelta(hours=15))
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 10, 0), row) is True
    assert sm.duration_promo_applies(datetime(2026, 10, 5, 16, 0), row) is False


def test_compute_duration_2plus1():
    row = _row(id=99, promo_name="Weekday Happy Hour 2+1")
    d = sm.compute_duration_2plus1(datetime(2026, 10, 5, 10, 0), 180, row)
    assert d is not None
    assert d["billable_mins"] == 120
    assert d["free_mins"] == 60
    assert d["total_mins"] == 180
    assert d["title"] == "Weekday Happy Hour 2+1"
    # outside window → None
    assert sm.compute_duration_2plus1(datetime(2026, 10, 5, 16, 0), 180, row) is None
    # zero duration → None
    assert sm.compute_duration_2plus1(datetime(2026, 10, 5, 10, 0), 0, row) is None


# ════════════════════════════════════════════════════════════════════════
#  Money correctness: the 2+1 promo must price EXACTLY like a normal session
#  of ``billable_mins`` — it only changes the MINUTES, never the rate.
# ════════════════════════════════════════════════════════════════════════

def test_price_duration_2plus1_cash_c09_3h():
    # C-09 (mult 1.2, base 10,000), T=180 → billable 120 → 2 × 12,000 = 24,000
    p = sm.price_duration_2plus1(120, 10000, 1.2, None)
    assert p["cash_ks"] == 24000
    assert p["wallet_mins"] == 0
    assert p["cash_mins"] == 120
    assert p["scaled_cash_mins"] == 144


def test_price_duration_2plus1_cash_c09_2h():
    # T=120 → billable 120 (no free tier yet) → 24,000
    assert sm.price_duration_2plus1(120, 10000, 1.2, None)["cash_ks"] == 24000


def test_price_duration_2plus1_cash_c09_5h():
    # T=300 → billable 240 → 4 × 12,000 = 48,000
    assert sm.price_duration_2plus1(240, 10000, 1.2, None)["cash_ks"] == 48000


def test_price_duration_2plus1_cash_c01_3h():
    # C-01 (mult 1.0), T=180 → billable 120 → 2 × 10,000 = 20,000
    assert sm.price_duration_2plus1(120, 10000, 1.0, None)["cash_ks"] == 20000


def test_price_duration_2plus1_member_wallet_scaled():
    # Member C-09, T=180 → billable 120 → wallet deduct 144 SCALED minutes (120 × 1.2)
    p = sm.price_duration_2plus1(120, 10000, 1.2, 200)
    assert p["wallet_mins"] == 144
    assert p["cash_ks"] == 0
    assert p["scaled_cash_mins"] == 0


def test_price_duration_2plus1_partial_wallet():
    # Member C-09 with only 100 scaled minutes → wallet 100, cash covers 44 scaled min
    p = sm.price_duration_2plus1(120, 10000, 1.2, 100)
    assert p["wallet_mins"] == 100
    assert p["scaled_cash_mins"] == 44
    assert p["cash_ks"] == 7333  # 44 × 10,000 / 60


def test_price_duration_2plus1_zero_balance_is_cash():
    p = sm.price_duration_2plus1(120, 10000, 1.2, 0)
    assert p["wallet_mins"] == 0
    assert p["cash_ks"] == 24000


def test_compute_totals_d2p_cash_c09_pure():
    # Mirrors the checkout hook's req for a C-09 3h walk-in.
    # No promo_id/coupon → compute_totals is PURE (no DB access).
    req = {
        "duration_2plus1": {
            "promo_id": 1, "title": "Weekday Happy Hour 2+1",
            "total_mins": 180, "billable_mins": 120, "free_mins": 60,
            "cash_mins": 144,   # SCALED (120 × 1.2)
            "wallet_mins": 0,
        },
        "base_rate": 10000, "multiplier": 1.2,
        "game_amt": 24000, "gross_total": 24000,
    }
    res = sm.compute_totals(req)
    assert res["game_amt"] == 24000
    assert res["gross_total"] == 24000
    assert res["net_total"] == 24000
    assert res["total_discount"] == 0


def test_compute_totals_d2p_member_wallet_c09_pure():
    req = {
        "duration_2plus1": {
            "promo_id": 1, "title": "Weekday Happy Hour 2+1",
            "total_mins": 180, "billable_mins": 120, "free_mins": 60,
            "cash_mins": 0, "wallet_mins": 144,   # SCALED
        },
        "base_rate": 10000, "multiplier": 1.2,
        "is_member": True, "wallet_mins": 144, "effective_cost_mins": 144,
    }
    res = sm.compute_totals(req)
    assert res["wallet_game_value"] == 24000
    assert res["gross_total"] == 24000
    assert res["net_total"] == 0   # wallet covers the game fully
