#!/usr/bin/env python3
"""
PS VIBE — Analytics Sheet Referral_Log Sync
============================================
Reads Referral_Log data from the Main Sheet and appends new rows
to the Analytics Sheet's Referral_Log tab.

Designed to run via cron, e.g.:
    */30 * * * * /usr/bin/python3 /root/psvibe_api_server/analytics_sync.py >> /var/log/analytics_sync.log 2>&1

Author: OpenClaw
Date:   2026-06-03
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── Config ──────────────────────────────────────────────────────────────────
MAIN_SHEET_ID      = "1VFNvhdcYVlVrr5TS49n2peIZa3U6y_AI-Mfo7q7gVsA"
ANALYTICS_SHEET_ID = "1fh9Rga-XU2971BxdcAnboaFQuJfcT-c1xDfUVNrIuPE"
SA_FILE            = "/root/psvibe-sales-bot/service_account.json"
TAB_NAME           = "Referral_Log"
HEADERS            = ["Timestamp", "Staff ID", "Staff Name", "Member ID", "Referral Code", "Action"]
STATE_FILE         = "/tmp/analytics_referral_sync_state.txt"

# Myanmar Time
MMT = timezone(timedelta(hours=6, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def authorize():
    """Authorize with Google Sheets using the service account."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SA_FILE, scope)
    return gspread.authorize(creds)


def ensure_tab(sheet, name: str, rows: int = 1000, cols: int = 6):
    """Return worksheet for *name*; create with headers if missing."""
    try:
        return sheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(name, rows=rows, cols=cols)
        ws.update(values=[HEADERS], range_name="A1:F1")
        log.info("Created tab '%s' with headers", name)
        return ws


def read_state() -> int:
    """Return the last-synced row index from the state file (0 = no state)."""
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_state(last_row: int) -> None:
    """Persist the last-synced row index."""
    with open(STATE_FILE, "w") as f:
        f.write(str(last_row))


def map_row(row: list) -> list:
    """Map a Main Sheet Referral_Log row to the Analytics Sheet format.

    Main Sheet columns (no header row — data-only):
      A = Date (e.g. "5/31/2026")
      B = Time (e.g. "1:03:28")
      C = Staff ID  (e.g. "999")
      D = Member ID (e.g. "888")
      E = Referral Code / Name (e.g. "testuser")
      F = Action (e.g. "clicked")

    Analytics Sheet columns (with header row):
      A = Timestamp   (combined Date + Time)
      B = Staff ID
      C = Staff Name  (empty — not in main sheet)
      D = Member ID
      E = Referral Code
      F = Action
    """
    if len(row) < 6:
        return [""] * 6

    date_str  = (row[0] or "").strip()
    time_str  = (row[1] or "").strip()
    timestamp = f"{date_str} {time_str}".strip() if date_str or time_str else ""

    return [
        timestamp,                    # A: Timestamp
        (row[2] or "").strip(),       # B: Staff ID
        "",                           # C: Staff Name (not available in main)
        (row[3] or "").strip(),       # D: Member ID
        (row[4] or "").strip(),       # E: Referral Code
        (row[5] or "").strip(),       # F: Action
    ]


def sync() -> int:
    """Sync Referral_Log from Main → Analytics. Returns number of new rows appended."""
    client = authorize()

    # Open sheets
    main_sheet      = client.open_by_key(MAIN_SHEET_ID)
    analytics_sheet = client.open_by_key(ANALYTICS_SHEET_ID)

    # Ensure tab exists
    analytics_ws = ensure_tab(analytics_sheet, TAB_NAME)

    # Read main sheet Referral_Log
    try:
        main_ws = main_sheet.worksheet(TAB_NAME)
        main_data = main_ws.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        log.warning("Main sheet has no '%s' tab — nothing to sync", TAB_NAME)
        return 0

    if not main_data:
        log.info("Main Referral_Log is empty — nothing to sync")
        return 0

    # Read analytics sheet to find last row (skip header row)
    analytics_data = analytics_ws.get_all_values()
    analytics_row_count = len(analytics_data)  # includes header

    # Determine starting point: how many data rows already synced?
    # Strategy: track last-synced index from main sheet
    last_synced = read_state()
    total_main_rows = len(main_data)

    # Main sheet has NO header row — all rows are data
    if last_synced >= total_main_rows:
        log.info("Already synced — last_synced=%d, total_main_rows=%d", last_synced, total_main_rows)
        return 0

    # Map new rows
    new_rows = []
    # Also track by timestamp to avoid duplicates from state file corruption
    existing_ts = set()
    for row in analytics_data[1:]:  # skip header
        if row and row[0]:
            existing_ts.add(row[0].strip())

    for i in range(last_synced, total_main_rows):
        mapped = map_row(main_data[i])
        ts = mapped[0]
        # Skip if timestamp already exists in analytics (dedup guard)
        if ts and ts in existing_ts:
            continue
        new_rows.append(mapped)
        if ts:
            existing_ts.add(ts)

    if not new_rows:
        log.info("No new rows to append (dedup filtered all)")
        write_state(total_main_rows)
        return 0

    # Append to analytics sheet
    start_row = analytics_row_count + 1
    end_row   = start_row + len(new_rows) - 1
    range_name = f"A{start_row}:F{end_row}"
    analytics_ws.update(values=new_rows, range_name=range_name)
    log.info("Appended %d rows → Analytics Referral_Log (%s)", len(new_rows), range_name)

    # Update state
    write_state(total_main_rows)

    return len(new_rows)


def main():
    """Entry point — sync and report."""
    start = time.monotonic()
    try:
        count = sync()
        elapsed = (time.monotonic() - start) * 1000
        log.info("Sync complete — %d new rows in %.0f ms", count, elapsed)
    except Exception as exc:
        log.exception("Sync failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
