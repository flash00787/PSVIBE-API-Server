#!/usr/bin/env python3
"""PS VIBE — daily staleness sweep.

1) member_coupons: rows still flagged 'active' whose expiry_date has passed → 'expired'.
2) promotions:     rows still flagged 'active' whose end_date has passed     → 'inactive'.

Idempotent. Never touches balances, minutes or any other column, so history/audit
stays intact. Logs every row it changes; exits non-zero if anything is left stale.

Run manually:  /opt/psvibe/api/venv/bin/python /opt/psvibe/api/scripts/expire_coupons.py
Scheduled by:  psvibe-coupon-expire.timer (daily, 00:15 MMT)
"""
from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/opt/psvibe/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expire_stale")

COUPON_SELECT = (
    "SELECT id, coupon_code, member_id, balance_minutes, expiry_date "
    "FROM member_coupons "
    "WHERE status = 'active' AND expiry_date IS NOT NULL AND expiry_date < NOW() "
    "ORDER BY id"
)
COUPON_UPDATE = (
    "UPDATE member_coupons SET status = 'expired' "
    "WHERE status = 'active' AND expiry_date IS NOT NULL AND expiry_date < NOW()"
)

PROMO_SELECT = (
    "SELECT id, name, promo_type, status, start_date, end_date "
    "FROM promotions "
    "WHERE status = 'active' AND end_date IS NOT NULL AND end_date < CURDATE() "
    "ORDER BY id"
)
PROMO_UPDATE = (
    "UPDATE promotions SET status = 'inactive' "
    "WHERE status = 'active' AND end_date IS NOT NULL AND end_date < CURDATE()"
)


def sweep_coupons(query, execute) -> int:
    stale = query(COUPON_SELECT)
    if not stale:
        log.info("coupons: nothing to expire")
        return 0
    for r in stale:
        log.info(
            "coupon expire #%s %s member=%s unused=%s min (expired %s)",
            r.get("id"), r.get("coupon_code"), r.get("member_id"),
            r.get("balance_minutes"), r.get("expiry_date"),
        )
    execute(COUPON_UPDATE)
    log.info("coupons: expired %d", len(stale))
    return len(stale)


def sweep_promotions(query, execute) -> int:
    stale = query(PROMO_SELECT)
    if not stale:
        log.info("promotions: nothing to deactivate")
        return 0
    for r in stale:
        log.info(
            "promotion deactivate #%s %s type=%s (ended %s)",
            r.get("id"), r.get("name"), r.get("promo_type"), r.get("end_date"),
        )
    execute(PROMO_UPDATE)
    log.info("promotions: deactivated %d", len(stale))
    return len(stale)


def main() -> int:
    from mysql_db import query, execute  # import here so a syntax check never touches the DB

    sweep_coupons(query, execute)
    sweep_promotions(query, execute)

    left_c = query("SELECT COUNT(*) AS c FROM member_coupons WHERE status='active' AND expiry_date < NOW()")[0]["c"]
    left_p = query("SELECT COUNT(*) AS c FROM promotions WHERE status='active' AND end_date < CURDATE()")[0]["c"]
    if left_c or left_p:
        log.warning("still stale after sweep — coupons=%s promotions=%s", left_c, left_p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
