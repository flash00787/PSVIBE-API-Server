"""Field name normalization — backward-compatible camelCase ↔ snake_case mapping.
Import `normalize_response` in endpoint handlers to add missing aliases."""

import logging
logger = logging.getLogger(__name__)

# Primary: snake_case (Python convention)
# Aliases: camelCase (legacy dashboard / bot clients)
FIELD_ALIASES = {
    "console_id": "consoleId",
    "console_type": "consoleType",
    "member_id": "memberId",
    "staff_name": "staffName",
    "customer_name": "customerName",
    "booking_date": "bookingDate",
    "time_slot": "timeSlot",
    "start_time": "startTime",
    "end_time": "endTime",
    "game_name": "gameName",
    "duration_mins": "durationMins",
    "total_amount": "totalAmount",
    "payment_method": "paymentMethod",
    "created_at": "createdAt",
    "booking_id": "bookingId",
    "balance_mins": "balanceMins",
    "phone_number": "phoneNumber",
    "telegram_chat_id": "telegramChatId",
    "voucher_no": "voucherNo",
    "sale_date": "saleDate",
    "coupon_code": "couponCode",
    "total_bought_mins": "totalBoughtMins",
    "total_spend": "totalSpend",
    "last_updated": "lastUpdated",
}

def normalize_response(record: dict) -> dict:
    """Add camelCase aliases to a dict for backward compatibility.
    Does NOT overwrite existing keys — snake_case is primary.
    Usage: `return {"bookings": [normalize_response(b) for b in bookings]}`"""
    if not isinstance(record, dict):
        return record
    for snake_key, camel_key in FIELD_ALIASES.items():
        if snake_key in record and camel_key not in record:
            record[camel_key] = record[snake_key]
    return record


def normalize_list(records: list) -> list:
    """Normalize a list of dicts."""
    return [normalize_response(r) for r in records] if records else records


def resolve_key(record: dict, *keys: str):
    """Resolve a value by trying multiple keys. First match wins (None-safe).
    Usage: `name = resolve_key(b, "customerName", "customer_name", "staff_name", "member_id", "Guest")`"""
    if not isinstance(record, dict):
        return keys[-1] if keys else None
    for k in keys[:-1]:
        val = record.get(k)
        if val:
            return val
    return keys[-1] if keys else None
