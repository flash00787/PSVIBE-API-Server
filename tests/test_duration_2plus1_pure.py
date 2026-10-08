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
