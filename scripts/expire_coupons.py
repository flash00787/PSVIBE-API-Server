#!/usr/bin/env python3
"""PS VIBE — expire member coupons whose expiry_date has passed.

Idempotent: marks member_coupons.status = 'expired' for rows still flagged
'active' whose expiry_date is in the past. Never touches balance_minutes or
any other column, so history/audit stays intact.

Run manually:  /opt/psvibe/api/venv/bin/python /opt/psvibe/api/scripts/expire_coupons.py
Scheduled by:  psvibe-coupon-expire.timer (daily, 00:15 MMT)
"""
from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/opt/psvibe/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expire_coupons")

SELECT_STALE = (
    "SELECT id, coupon_code, member_id, balance_minutes, expiry_date "
    "FROM member_coupons "
    "WHERE status = 'active' AND expiry_date IS NOT NULL AND expiry_date < NOW() "
    "ORDER BY id"
)
UPDATE_STALE = (
    "UPDATE member_coupons SET status = 'expired' "
    "WHERE status = 'active' AND expiry_date IS NOT NULL AND expiry_date < NOW()"
)


def main() -> int:
    from mysql_db import query, execute  # imported here so --help style checks never touch the DB

    stale = query(SELECT_STALE)
    if not stale:
        log.info("no coupons to expire")
        return 0

    for r in stale:
        log.info(
            "expiring #%s %s member=%s unused=%s min (expired %s)",
            r.get("id"), r.get("coupon_code"), r.get("member_id"),
            r.get("balance_minutes"), r.get("expiry_date"),
        )
    execute(UPDATE_STALE)
    log.info("expired %d coupon(s)", len(stale))

    left = query("SELECT COUNT(*) AS c FROM member_coupons WHERE status='active' AND expiry_date < NOW()")[0]["c"]
    if left:
        log.warning("%s coupon(s) still flagged active past expiry — check again", left)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
