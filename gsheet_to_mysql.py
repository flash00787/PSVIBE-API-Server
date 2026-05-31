#!/usr/bin/env python3
"""One-time GSheet -> MySQL migration: Card_Wallet -> member_wallets (FULL)"""
import pymysql, gspread, os, sys
import pymysql.cursors
from google.oauth2.service_account import Credentials

# Load env
env_files = ["/etc/psvibe/secrets.env", "/root/psvibe_api_server/.env"]
for ef in env_files:
    try:
        for line in open(ef):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)
    except:
        pass

# Service account file
sa_file = os.path.join(os.path.dirname(__file__), "service_account.json")
if not os.path.exists(sa_file):
    sa_file = "/root/psvibe_api_server/service_account.json"

print(f"Using SA: {sa_file}")
print(f"Sheet ID: {os.environ.get("SHEET_ID", "NOT SET")}")

scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.readonly"]
creds = Credentials.from_service_account_file(sa_file, scopes=scopes)
gc = gspread.authorize(creds)
wb = gc.open_by_key(os.environ["SHEET_ID"])
ws = wb.worksheet("Card_Wallet")
rows = ws.get_all_values()
print(f"Found {len(rows)-1} data rows in Card_Wallet")

conn = pymysql.connect(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "psvibe_user"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    database=os.environ.get("MYSQL_DATABASE", "psvibe_api")
)

SQL = (
    "INSERT INTO member_wallets "
    "(member_id, member_name, phone, balance_mins, lifetime_spend, ranking_net_spend, "
    " tier, total_spend, reg_staff, referral_code, join_date, last_updated) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) "
    "ON DUPLICATE KEY UPDATE "
    "member_name=VALUES(member_name), phone=VALUES(phone), "
    "balance_mins=VALUES(balance_mins), lifetime_spend=VALUES(lifetime_spend), "
    "ranking_net_spend=VALUES(ranking_net_spend), tier=VALUES(tier), "
    "total_spend=VALUES(total_spend), reg_staff=VALUES(reg_staff), "
    "referral_code=VALUES(referral_code), join_date=VALUES(join_date), "
    "last_updated=NOW()"
)

with conn.cursor() as cur:
    count = 0
    errors = 0
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0].strip():
            continue
        try:
            member_id = row[0].strip() if len(row) > 0 else ""
            name = row[1].strip() if len(row) > 1 else ""
            phone = row[2].strip() if len(row) > 2 else ""
            # Col H (index 7) = balance_mins
            balance = 0
            if len(row) > 7 and row[7].strip():
                balance = float(row[7].replace(",", ""))
            # Col E (index 4) = lifetime_spend
            lifetime_spend = 0
            if len(row) > 4 and row[4].strip():
                lifetime_spend = float(row[4].replace(",", ""))
            # Col F (index 5) = ranking_net_spend
            ranking_net_spend = 0
            if len(row) > 5 and row[5].strip():
                ranking_net_spend = float(row[5].replace(",", ""))
            # Col O (index 14) = tier
            tier = "Warrior"
            if len(row) > 14 and row[14].strip():
                tier = row[14].strip()
            # Col P (index 15) = total_spend
            total_spend = 0
            if len(row) > 15 and row[15].strip():
                total_spend = float(row[15].replace(",", ""))
            # Col K (index 10) = reg_staff
            reg_staff = ""
            if len(row) > 10 and row[10].strip():
                reg_staff = row[10].strip()
            # Col Q (index 16) = referral_code
            referral_code = ""
            if len(row) > 16 and row[16].strip():
                referral_code = row[16].strip()
            # Col M (index 12) = join_date
            join_date = None
            if len(row) > 12 and row[12].strip():
                join_date = row[12].strip()
            
            cur.execute(SQL, (member_id, name, phone, balance, lifetime_spend,
                            ranking_net_spend, tier, total_spend, reg_staff,
                            referral_code, join_date))
            count += 1
            if count <= 5 or count % 50 == 0:
                print(f"  [{count}] {member_id} ({name}) - bal={balance}, tier={tier}, lifetime={lifetime_spend}")
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  ERROR row {i}: {e}")
    conn.commit()
    print(f"Migrated {count} members ({errors} errors)")

conn.close()
print("Done")
