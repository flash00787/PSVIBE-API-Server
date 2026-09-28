"""Unit-style test for the 15-min deposit-hold + timeout + timeline predicates.

Pure SELECT against SAMPLE rows (UNION ALL derived table) — does NOT touch any
production table, does NOT write, does NOT run against prod data.  Uses the same
MySQL connection config as the app but only executes read-only SELECT statements.

Run:  /opt/psvibe/api/venv/bin/python tests/test_deposit_hold_predicate.py
"""
import sys, os

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_USER", "psvibe_user")
os.environ.setdefault("MYSQL_PASSWORD", "PsVibe@User2024!")
os.environ.setdefault("MYSQL_DATABASE", "psvibe_api")

import pymysql

HOLD_EXPR = "DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)"
TIMEOUT_EXPR = "DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 15 MINUTE)"

# Sample rows as a derived table. created_at is expressed relative to NOW() (MMT)
# so the test is stable regardless of wall clock.
SAMPLES = """
  SELECT 1  AS id, 'pending' AS status, 'none'      AS deposit_status, 0      AS deposit_amount, NULL       AS member_id, DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 20 MINUTE) AS created_at, '2026-09-28' AS booking_date
  UNION ALL SELECT 2,  'pending', 'none',      0,      NULL,       DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL  5 MINUTE), '2026-09-28'
  UNION ALL SELECT 3,  'pending', 'paid',      5000,   NULL,       DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 20 MINUTE), '2026-09-28'
  UNION ALL SELECT 4,  'pending', 'none',      0,      'PSV_A_000', DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 20 MINUTE), '2026-09-28'
  UNION ALL SELECT 5,  'pending', NULL,        NULL,   NULL,       DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 20 MINUTE), '2026-09-28'
  UNION ALL SELECT 6,  'pending', 'verified',  6000,   NULL,       DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 20 MINUTE), '2026-09-28'
  UNION ALL SELECT 7,  'confirmed', 'none',    0,      NULL,       DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL 20 MINUTE), '2026-09-28'
  UNION ALL SELECT 8,  'pending', 'none',      0,      'Guest',    DATE_SUB(DATE_ADD(NOW(), INTERVAL 390 MINUTE), INTERVAL  5 MINUTE), '2026-09-28'
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


def main():
    results = []
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        got = sorted(int(r["id"]) for r in got)
        ok = got == sorted(expected)
        results.append((name, ok, got, sorted(expected)))
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got} expected={sorted(expected)}")

    # ── Test 1: pending-deposit timeout predicate ─────────────────────────
    rows = run_sql(f"""
        SELECT id FROM ({SAMPLES}) s
        WHERE status='pending'
          AND (deposit_status IS NULL OR deposit_status='none' OR deposit_amount IS NULL OR deposit_amount=0)
          AND (member_id IS NULL OR member_id = '')
          AND created_at < {TIMEOUT_EXPR}
        ORDER BY id
    """)
    # expired: r1 (20min, none, guest), r5 (20min, NULL dep, guest). r2 (5min) not expired.
    check("timeout: guest/no-deposit older than 15min selected", rows, [1, 5])

    # ── Test 2: 15-min hold predicate (conflict/occupancy) ────────────────
    rows = run_sql(f"""
        SELECT id FROM ({SAMPLES}) s
        WHERE (status IN ('confirmed','pending_check_in','checked_in','Active')
               OR (status='pending' AND (deposit_status='paid'
                   OR (member_id IS NOT NULL AND member_id != '')
                   OR created_at >= {HOLD_EXPR})))
        ORDER BY id
    """)
    # HOLDS: r2 (5min guest), r3 (paid), r4 (member), r6 (verified? -> wait: 'verified' not in predicate 'paid'),
    #        r7 (confirmed), r8 (5min 'Guest' member_id -> member_id='Guest' is NOT empty -> holds).
    # NOTE: 'verified' (r6) is NOT in the hold predicate ('paid' only) — matches legacy behaviour.
    #       r1 (20min guest no-deposit) does NOT hold.
    check("hold: 15-min guest + paid + member + confirmed hold; old guest freed", rows, [2, 3, 4, 7, 8])

    # ── Test 3: timeline filter (hide unpaid guest pending) ───────────────
    rows = run_sql(f"""
        SELECT id FROM ({SAMPLES}) s
        WHERE booking_date='2026-09-28'
          AND (LOWER(status) <> 'pending' OR deposit_status IN ('paid','verified')
               OR (member_id IS NOT NULL AND member_id != ''))
        ORDER BY id
    """)
    # visible: r3 (paid), r4 (member), r6 (verified), r7 (confirmed), r8 (Guest member_id='Guest' non-empty)
    # hidden: r1, r2, r5 (pending guest no deposit)
    check("timeline: unpaid guest pending hidden; paid/verified/member/confirmed visible", rows, [3, 4, 6, 7, 8])

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
