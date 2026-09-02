"""Phase 2 migration — add client_ref idempotency columns + unique keys.

Idempotent: re-running is a no-op (guarded by information_schema checks).

    sales_daily: ADD COLUMN client_ref VARCHAR(64) NULL + UNIQUE KEY uq_sales_client_ref
    topup_log:   ADD COLUMN client_ref VARCHAR(64) NULL + UNIQUE KEY uq_topup_client_ref

Multiple NULLs are allowed in MySQL UNIQUE indexes, so existing bot rows
(which never set client_ref) are unaffected.
"""
import pymysql
import os

HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MYSQL_PORT", "3306"))
USER = os.environ.get("MYSQL_USER", "psvibe_user")
PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
DATABASE = os.environ.get("MYSQL_DATABASE", "psvibe_api")

# Read creds from secrets.env (startswith, NOT regex)
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

def _cred(key, default):
    return os.environ.get(key) or _secrets.get(key) or default

_secrets = _load_secrets()
HOST = _cred("MYSQL_HOST", HOST)
PORT = int(_cred("MYSQL_PORT", str(PORT)))
USER = _cred("MYSQL_USER", USER)
PASSWORD = _cred("MYSQL_PASSWORD", PASSWORD)
DATABASE = _cred("MYSQL_DATABASE", DATABASE)


def column_exists(cur, table, column):
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (DATABASE, table, column),
    )
    return cur.fetchone()[0] > 0


def index_exists(cur, table, index_name):
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s",
        (DATABASE, table, index_name),
    )
    return cur.fetchone()[0] > 0


def run():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            # sales_daily
            if not column_exists(cur, "sales_daily", "client_ref"):
                cur.execute("ALTER TABLE sales_daily ADD COLUMN client_ref VARCHAR(64) NULL")
                print("ALTER sales_daily: added client_ref column")
            else:
                print("sales_daily.client_ref already exists (skip)")

            if not index_exists(cur, "sales_daily", "uq_sales_client_ref"):
                cur.execute("ALTER TABLE sales_daily ADD UNIQUE KEY uq_sales_client_ref (client_ref)")
                print("ALTER sales_daily: added uq_sales_client_ref unique key")
            else:
                print("sales_daily.uq_sales_client_ref already exists (skip)")

            # topup_log
            if not column_exists(cur, "topup_log", "client_ref"):
                cur.execute("ALTER TABLE topup_log ADD COLUMN client_ref VARCHAR(64) NULL")
                print("ALTER topup_log: added client_ref column")
            else:
                print("topup_log.client_ref already exists (skip)")

            if not index_exists(cur, "topup_log", "uq_topup_client_ref"):
                cur.execute("ALTER TABLE topup_log ADD UNIQUE KEY uq_topup_client_ref (client_ref)")
                print("ALTER topup_log: added uq_topup_client_ref unique key")
            else:
                print("topup_log.uq_topup_client_ref already exists (skip)")

        conn.commit()
        print("Migration complete — no-op safe on re-run.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
