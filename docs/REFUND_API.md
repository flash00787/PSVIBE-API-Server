# Deposit Refund System — API & Flow

## Overview
Refund system for cancelled bookings. Allows staff to return deposits to customers.
Timeline: Auto-cancelled bookings show for 2 hours; forfeited deposits show all day; refunded are hidden.

## Deposit Status Values
| Status | Meaning | Timeline Display |
|--------|---------|-----------------|
| `none` | No deposit | Hidden (pending without deposit hidden entirely) |
| `paid` | Customer submitted, pending staff verify | Hidden after 2hr cancel |
| `verified` | Staff verified, money in | Hidden after 2hr cancel |
| `forfeited` | No-show, shop keeps money | ✅ Always visible |
| `refunded` | Money returned to customer | ❌ Hidden (Boss request) |

## API Endpoint

### `POST /api/bookings/{id}/refund`
Refund a booking's deposit.

**Rules:**
- Booking must be cancelled
- Deposit status must be `forfeited`, `paid`, or `verified`
- Deposit amount must be > 0
- **1-hour rule:** Only allowed if current time < booking start - 1 hour
- Staff identity auto-extracted from JWT Bearer token

**Actions:**
1. `deposit_status` → `refunded`
2. Booking notes updated with refund info
3. `cash_movements` eject entry created with `[Refund]` tag
4. Customer notified via Telegram (CUSTOMER_BOT_TOKEN)
5. Admin group notified (DEPOSIT_NOTIFY_CHAT)

**Response:**
```json
{
  "success": true,
  "data": {
    "booking_id": 1969,
    "deposit_status": "refunded",
    "deposit_amount": 9000,
    "cash_movement": "eject 9000 Ks from KPay"
  }
}
```

## UI Locations

### Timeline (Vue SPA — `#/timeline`)
- Click cancelled booking → modal opens
- **🔄 Refund Deposit** button shows if:
  - Booking cancelled ✅
  - Deposit is forfeited/paid/verified ✅
  - **Current time < booking start - 1 hour** ✅
- Shows deadline: "Refund available until 16:00 MMT"
- After deadline: "⏰ Refund window closed"

### Bookings Management (`#/bookings`)
- **↩️ Refund** button in Actions column
- Shows for all deposits: forfeited, paid, verified
- Confirmation dialog before refunding

## Cash Movements Tagging
All refund entries use `[Refund]` prefix in note field:
```sql
-- Query all refunds:
SELECT * FROM cash_movements WHERE note LIKE '%[Refund]%';

-- Query refunds by period:
SELECT SUM(amount) AS total_refunded FROM cash_movements
WHERE note LIKE '%[Refund]%'
  AND created_at >= '2026-07-01'
  AND created_at < '2026-08-01';
```

## Notification Routing

| Notification | Target Group (Env Var) |
|-------------|----------------------|
| Deposit Forfeited | `DEPOSIT_NOTIFY_CHAT` |
| Deposit Refunded | `DEPOSIT_NOTIFY_CHAT` |
| Deposit Verified | `DEPOSIT_NOTIFY_CHAT` |
| Deposit Submitted + SS | `DEPOSIT_NOTIFY_CHAT` |
| New Booking | `DEPOSIT_NOTIFY_CHAT` |

## Implementation Details

**Backend:** `/root/psvibe_api_server/routes/booking_routes.py`
- Refund endpoint: `api_booking_refund()`
- Cancel auto-forfeit: `api_booking_cancel()` (added `deposit_status=CASE WHEN...`)
- Notifications: `_notify_deposit_forfeited()`, `_notify_deposit_refunded()`
- Pending timeout: part of `api_auto_cancel_no_show()` — 15 min without deposit → auto-cancel

**Frontend:**
- `/root/psvibe-dashboard/src/views/TimelineView.vue` — Modal refund button + time check
- `/root/psvibe-dashboard/src/views/BookingsManagement.vue` — Table refund button
- `/root/psvibe-dashboard/src/api/timeline.ts` — `refundDeposit()`, cancelled 2hr filter, pending deposit filter

**Deployed:** 2026-07-24
