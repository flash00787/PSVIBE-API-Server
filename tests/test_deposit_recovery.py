"""Unit tests for the deposit auto-recovery policy (owner 2026-09-28).

Two layers:
  1. Pure-function tests of `_is_deposit_recoverable` (no DB).
  2. SELECT-only probes against SAMPLE rows (UNION ALL derived table) — these do
     NOT touch any production table, do NOT write, and do NOT run against prod
     data. They verify the SQL `minutes_since_cancel` computation, the
     recoverable-now flag, and the slot availability re-check decision.

Run:  /opt/psvibe/api/venv/bin/python tests/test_deposit_recovery.py
"""
import sys, os

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_USER", "psvibe_user")
os.environ.setdefault("MYSQL_PASSWORD", "PsVibe@User2024!")
os.environ.setdefault("MYSQL_DATABASE", "psvibe_api")

import pymysql

from routes.booking_routes import _is_deposit_recoverable, _DEPOSIT_RECOVERY_WINDOW_MIN

# ── Pure-function layer ──────────────────────────────────────────────────────
def test_helper():
    results = []
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        results.append((name, ok, got, expected))
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    W = _DEPOSIT_RECOVERY_WINDOW_MIN
    check("window constant == 30", W, 30)

    # boundaries
    check("just cancelled (0 min) recoverable", _is_deposit_recoverable("cancelled", "| Auto-cancelled: deposit timeout (15min)", "none", 0), True)
    check("29 min ago recoverable", _is_deposit_recoverable("cancelled", "deposit timeout", "none", 29), True)
    check("30 min ago recoverable (inclusive)", _is_deposit_recoverable("cancelled", "deposit timeout", "none", 30), True)
    check("31 min ago NOT recoverable", _is_deposit_recoverable("cancelled", "deposit timeout", "none", 31), False)
    check("negative (future) NOT recoverable", _is_deposit_recoverable("cancelled", "deposit timeout", "none", -1), False)

    # non-timeout cancel
    check("no-show cancel NOT recoverable", _is_deposit_recoverable("cancelled", "| Auto-cancelled: no-show", "none", 5), False)
    check("empty notes NOT recoverable", _is_deposit_recoverable("cancelled", "", "none", 5), False)

    # deposit already recorded
    check("deposit already paid NOT recoverable", _is_deposit_recoverable("cancelled", "deposit timeout", "paid", 5), False)
    check("deposit NULL treated as none (recoverable)", _is_deposit_recoverable("cancelled", "deposit timeout", None, 5), True)

    # non-cancelled status
    check("pending status NOT recoverable", _is_deposit_recoverable("pending", "deposit timeout", "none", 5), False)
    check("confirmed status NOT recoverable", _is_deposit_recoverable("confirmed", "deposit timeout", "none", 5), False)

    # unknown cancel time
    check("unknown cancel time NOT recoverable", _is_deposit_recoverable("cancelled", "deposit timeout", "none", None), False)

    return passed, failed, results


# ── SQL probe layer (SELECT-only, derived table) ─────────────────────────────
RECOVERY_SAMPLES = """
  SELECT 1 AS id, 'cancelled' AS status, 'none'  AS deposit_status,
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 0 MINUTE) AS cancelled_at,
         '| Auto-cancelled: deposit timeout (15min)' AS notes
  UNION ALL SELECT 2, 'cancelled', 'none',
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 29 MINUTE),
         '| Auto-cancelled: deposit timeout (15min)'
  UNION ALL SELECT 3, 'cancelled', 'none',
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 31 MINUTE),
         '| Auto-cancelled: deposit timeout (15min)'
  UNION ALL SELECT 4, 'cancelled', 'none',
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 5 MINUTE),
         '| Auto-cancelled: no-show'
  UNION ALL SELECT 5, 'cancelled', 'paid',
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 5 MINUTE),
         '| Auto-cancelled: deposit timeout (15min)'
  UNION ALL SELECT 6, 'cancelled', 'none', NULL,
         '| Auto-cancelled: deposit timeout (15min)'
"""

# Slot availability re-check: target slot is now(MMT) + 60min → +120min.
TARGET_START = "DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 60 MINUTE)"
TARGET_END   = "DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 120 MINUTE)"

FREE_SLOT_SAMPLES = f"""
  SELECT 2 AS id, 'C-01' AS console_id, 'confirmed' AS status, 'paid' AS deposit_status, NULL AS member_id,
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 180 MINUTE) AS planned_start,
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 240 MINUTE) AS planned_end,
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 30 MINUTE) AS created_at
  UNION ALL SELECT 3, 'C-01', 'pending', 'none', NULL,
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 60 MINUTE),
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 120 MINUTE),
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 30 MINUTE)
"""

TAKEN_SLOT_SAMPLES = f"""
  SELECT 2 AS id, 'C-01' AS console_id, 'confirmed' AS status, 'paid' AS deposit_status, NULL AS member_id,
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 90 MINUTE) AS planned_start,
         DATE_ADD(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 150 MINUTE) AS planned_end,
         DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 30 MINUTE) AS created_at
"""

CONFLICT_SQL = f"""
    SELECT id FROM ({{S}}) s
    WHERE console_id='C-01'
      AND (status IN ('confirmed','pending_check_in','checked_in','Active')
           OR (status='pending' AND (deposit_status='paid'
               OR (member_id IS NOT NULL AND member_id != '')
               OR created_at >= DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE))))
      AND planned_start < {TARGET_END}
      AND planned_end > {TARGET_START}
    ORDER BY id
"""


def run_sql(sql):
    conn = pymysql.connect(host=os.environ["MYSQL_HOST"], port=int(os.environ["MYSQL_PORT"]),
                           user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
                           database=os.environ["MYSQL_DATABASE"], charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def test_sql():
    passed = failed = 0
    results = []

    def check(name, got_ids, expected_ids):
        nonlocal passed, failed
        got = sorted(int(r["id"]) for r in got_ids)
        exp = sorted(expected_ids)
        ok = got == exp
        results.append((name, ok, got, exp))
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got} expected={exp}")

    # recoverable-now flag (30-min rule + MMT comparison)
    rows = run_sql(f"""
        SELECT id,
               TIMESTAMPDIFF(MINUTE, cancelled_at, DATE_ADD(NOW(), INTERVAL 390 MINUTE)) AS mins,
               CASE WHEN notes LIKE '%deposit timeout%'
                     AND (deposit_status IS NULL OR deposit_status='none' OR deposit_status='')
                     AND TIMESTAMPDIFF(MINUTE, cancelled_at, DATE_ADD(NOW(), INTERVAL 390 MINUTE)) BETWEEN 0 AND {_DEPOSIT_RECOVERY_WINDOW_MIN}
                    THEN 1 ELSE 0 END AS recoverable
        FROM ({RECOVERY_SAMPLES}) s
        ORDER BY id
    """)
    recov_rows = [{"id": r["id"]} for r in rows if r["recoverable"] == 1]
    check("recoverable-now: just-cancelled + 29min recoverable (31min/noshow/paid/NULL excluded)", recov_rows, [1, 2])

    # availability re-check: free slot (no conflict)
    free = run_sql(CONFLICT_SQL.format(S=FREE_SLOT_SAMPLES))
    check("re-check: free slot -> no conflict", free, [])

    # availability re-check: taken slot (overlapping confirmed booking)
    taken = run_sql(CONFLICT_SQL.format(S=TAKEN_SLOT_SAMPLES))
    check("re-check: taken slot -> conflict id=2", taken, [2])

    return passed, failed, results


def main():
    p, f, _ = test_helper()
    p2, f2, _ = test_sql()
    print(f"\nhelper: {p} passed, {f} failed")
    print(f"sql:     {p2} passed, {f2} failed")
    total_p, total_f = p + p2, f + f2
    print(f"TOTAL:   {total_p} passed, {total_f} failed")
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
