"""PS VIBE — Weekday Happy Hour "2+1" duration promo migration (owner Osmo, 2026-10-08).

Idempotent + additive + reversible. Re-running is a no-op (each ALTER is guarded by an
information_schema check). Mirror of migrations/add_duration_2plus1_promo.sql.

New columns
-----------
promotions:       applies_auto, weekday_mask, time_window_start, time_window_end,
                  tier_charge_mins, tier_cycle_mins
console_sessions: promo_id, promo_applied, billable_mins, free_mins
sales_daily:      billable_mins, free_mins

This migration does NOT create or activate a promo row — the owner switches the
'duration_2plus1' promo on from the dashboard (status stays inactive until then).
"""
import os

import pymysql

HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MYSQL_PORT", "3306"))
USER = os.environ.get("MYSQL_USER", "psvibe_user")
PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
DATABASE = os.environ.get("MYSQL_DATABASE", "psvibe_api")


def _load_secrets(path="/etc/psvibe/secrets.env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


_secrets = _load_secrets()


def _cred(key, default):
    return os.environ.get(key) or _secrets.get(key) or default


HOST = _cred("MYSQL_HOST", HOST)
PORT = int(_cred("MYSQL_PORT", str(PORT)))
USER = _cred("MYSQL_USER", USER)
PASSWORD = _cred("MYSQL_PASSWORD", PASSWORD)
DATABASE = _cred("MYSQL_DATABASE", DATABASE)

# (table, column, definition)
COLUMNS = [
    ("promotions", "applies_auto", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("promotions", "weekday_mask", "TINYINT NOT NULL DEFAULT 0"),
    ("promotions", "time_window_start", "TIME NULL"),
    ("promotions", "time_window_end", "TIME NULL"),
    ("promotions", "tier_charge_mins", "INT NOT NULL DEFAULT 120"),
    ("promotions", "tier_cycle_mins", "INT NOT NULL DEFAULT 180"),
    ("console_sessions", "promo_id", "INT NULL"),
    ("console_sessions", "promo_applied", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("console_sessions", "billable_mins", "INT NULL"),
    ("console_sessions", "free_mins", "INT NULL"),
    ("sales_daily", "billable_mins", "INT NULL"),
    ("sales_daily", "free_mins", "INT NULL"),
]


def column_exists(cur, table, column):
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (DATABASE, table, column),
    )
    return cur.fetchone()[0] > 0


def run():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           database=DATABASE, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            for table, column, definition in COLUMNS:
                if column_exists(cur, table, column):
                    print(f"{table}.{column} already exists (skip)")
                else:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    print(f"ALTER {table}: added {column} {definition}")
        conn.commit()
        print("Migration complete — no-op safe on re-run.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
