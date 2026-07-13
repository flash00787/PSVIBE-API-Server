# Booking Payment Overhaul — Implementation Plan

> **Created:** 2026-07-12  
> **Requester:** Osmo (@kingkong00787)  
> **Status:** Planning Phase (Draft)

---

## Table of Contents

1. [Business Requirements Summary](#1-business-requirements-summary)
2. [Current State Analysis](#2-current-state-analysis)
3. [Database Schema Changes](#3-database-schema-changes)
4. [API Endpoints](#4-api-endpoints)
5. [Customer Bot Integration](#5-customer-bot-integration)
6. [Dashboard Frontend Changes](#6-dashboard-frontend-changes)
7. [Admin Telegram Bot Changes](#7-admin-telegram-bot-changes)
8. [Migration Steps](#8-migration-steps)
9. [File-by-File Implementation Order](#9-file-by-file-implementation-order)

---

## 1. Business Requirements Summary

From Osmo (@kingkong00787):

### 1.1 Split Payment Model
- **First payment:** 50% of total booking amount (required for booking confirmation)
- **Remaining 3 payments:** Equal installments of the remaining 50%
- Payments are tracked per-booking with status (pending, paid, overdue)
- Payment plan starts after booking is confirmed

### 1.2 Amount Verification
- Customer pays via KPay/Bank Transfer
- Customer sends a screenshot + stated amount
- Staff checks the actual amount received
- If the stated amount ≠ actual amount → staff can edit the amount first, THEN approve
- **Edit-then-approve** workflow: Staff edits first, approval is a separate action
- Only after approval → the payment is recorded

### 1.3 Payment Screenshot Viewer
- Customer sends payment screenshot → stored on server
- Staff can view the screenshot on the web dashboard
- Screenshots shown inline in the booking edit modal

### 1.4 Better Booking UI
- Enhance the existing modal with payment-related sections
- Payment history display inside booking details
- Split payment status progress bar

---

## 2. Current State Analysis

### 2.1 Console Bookings Table (`console_bookings`)
```sql
id              INT AUTO_INCREMENT PRIMARY KEY
branch_id       INT DEFAULT 1
console_id      VARCHAR(20)
member_id       VARCHAR(50)
booking_date    DATE
planned_start   DATETIME
planned_end     DATETIME
planned_duration_mins INT DEFAULT 60
status          VARCHAR(20) DEFAULT 'pending'
staff_name      VARCHAR(100)
notes           TEXT
telegram_chat_id VARCHAR(50)
phone           VARCHAR(50)
game_name       VARCHAR(200)
admin_notify_msg_id VARCHAR(255)
created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
cancelled_at    DATETIME
```

### 2.2 Current Booking Flow (No Payments)
```
Customer Bot → POST /api/bookings (status=pending)
Staff Bot → Approve (PATCH status=confirmed)
Staff Dashboard → Check-in → Start Session → End Session
```

### 2.3 Existing Payment Infrastructure
- **10 consoles** (C-01 through C-10), all PS5 with multipliers
- **`base_rate`** = 10,000 MMK per 60 min (config key in `settings_config`)
- **`console_multipliers`** = JSON mapping per console (1.0 or 1.2)
- Cash movement tracking system exists (`cash_movements`, `cash_transfers`)
- No booking-specific payment table exists

### 2.4 Current JS UI for Booking Management
- Single file: `dashboard-dist/assets/BookingsManagement-LkSq52ld.js` (13KB)
- Single modal for editing bookings with: date, time, duration, console, status dropdown, notes
- Status transition logic: pending→confirmed→cancelled, confirmed→cancelled, active→done
- No payment-related UI elements

### 2.5 Customer Bot
- Located at `/root/psvibe-sales-bot/customer_bot/`
- Booking flow uses ConversationHandler (16 states)
- After booking submission → "Staff မှ အတည်ပြုချက် စောင့်ဆိုင်းပေးပါ"
- Customer can view bookings with `/mybookings` 
- Uses async API layer (`customer_bot/api.py`)

### 2.6 Admin Bot
- Located at `/root/psvibe-sales-bot/bot/handlers/admin_bookings.py`
- Inline buttons for approve/reject on pending booking cards
- Auto-assigns console on approval

---

## 3. Database Schema Changes

### 3.1 New Table: `booking_payments`

```sql
CREATE TABLE booking_payments (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    booking_id        INT NOT NULL,
    payment_number    TINYINT NOT NULL COMMENT '1=first(50%), 2=2nd, 3=3rd, 4=4th',
    total_amount      INT NOT NULL COMMENT 'Total booking price in MMK (set at booking creation)',
    installment_amount INT NOT NULL COMMENT 'This installment amount in MMK',
    
    -- Customer-submitted data
    stated_amount     INT DEFAULT NULL COMMENT 'Amount customer claims they paid',
    stated_method     VARCHAR(20) DEFAULT NULL COMMENT 'KPay / Bank Transfer / Cash',
    screenshot_path   VARCHAR(500) DEFAULT NULL COMMENT 'Path to uploaded screenshot',
    screenshot_msg_id INT DEFAULT NULL COMMENT 'Telegram message ID of screenshot',
    
    -- Staff verification
    actual_amount     INT DEFAULT NULL COMMENT 'Actual amount verified by staff',
    staff_notes       TEXT DEFAULT NULL COMMENT 'Staff notes on this payment',
    verified_by       VARCHAR(100) DEFAULT NULL COMMENT 'Staff name who verified',
    verified_at       DATETIME DEFAULT NULL COMMENT 'When verified',
    
    -- Status
    status            VARCHAR(20) DEFAULT 'pending' COMMENT 'pending|paid|rejected|refunded',
    
    -- Timestamps
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    FOREIGN KEY (booking_id) REFERENCES console_bookings(id) ON DELETE CASCADE,
    INDEX idx_booking_id (booking_id),
    INDEX idx_status (status),
    INDEX idx_booking_status (booking_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.2 New Table: `booking_pricing` (Console pricing at time of booking)

```sql
CREATE TABLE booking_pricing (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    booking_id        INT NOT NULL,
    console_id        VARCHAR(20) NOT NULL,
    base_rate_per_hour INT NOT NULL COMMENT 'MMK per 60 min at time of booking',
    multiplier        DECIMAL(3,1) DEFAULT 1.0 COMMENT 'Console multiplier at time of booking',
    duration_mins     INT NOT NULL,
    calculated_total   INT NOT NULL COMMENT 'base_rate * multiplier * (duration_mins/60)',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (booking_id) REFERENCES console_bookings(id) ON DELETE CASCADE,
    INDEX idx_booking_id (booking_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.3 Add Column to `console_bookings`

```sql
ALTER TABLE console_bookings
ADD COLUMN payment_status VARCHAR(20) DEFAULT 'none' 
    COMMENT 'none|pending|partial|paid_full|overdue' AFTER status,
ADD COLUMN total_price INT DEFAULT NULL 
    COMMENT 'Total price in MMK (computed at booking confirmation)' AFTER payment_status;
```

### 3.4 Screenshot Storage Directory

```
/root/psvibe_api_server/static/payments/
├── bk_<id>/
│   ├── first_payment.jpg
│   ├── payment_2.jpg
│   ├── payment_3.jpg
│   └── payment_4.jpg
```

- Nginx or FastAPI static file serving at `/static/payments/`
- Max file size: 10MB per image

---

## 4. API Endpoints

All endpoints go in `/root/psvibe_api_server/routes/booking_routes.py` (or a new file `routes/payment_routes.py`).

### 4.1 `POST /api/bookings/{booking_id}/payments`
**Payment submission endpoint — Customer bot posts payment proof here**

```python
@router.post("/bookings/{booking_id}/payments", response_model=GenericResponse)
async def api_submit_booking_payment(booking_id: int, req: dict, auth=Depends(verify_api_key)):
```

**Payload:**
```json
{
    "payment_number": 1,           // 1=first payment (50%)
    "stated_amount": 45000,
    "stated_method": "KPay",       // KPay | Bank Transfer | Cash
    "photo": "<base64 or multipart>" // Optional if submitted via Telegram
}
```

**Returns:**
```json
{
    "success": true,
    "data": {
        "payment_id": 1,
        "booking_id": 131,
        "status": "pending",
        "installment_amount": 45000
    }
}
```

### 4.2 `PATCH /api/bookings/{booking_id}/payments/{payment_id}`
**Staff edits payment amount before approval**

```python
@router.patch("/bookings/{booking_id}/payments/{payment_id}", response_model=GenericResponse)
async def api_edit_booking_payment(booking_id: int, payment_id: int, req: dict, auth=Depends(verify_api_key)):
```

**Payload (edit before approve):**
```json
{
    "actual_amount": 45000,
    "staff_notes": "Correct amount — approved"
}
```

**Returns the updated payment record.**

### 4.3 `POST /api/bookings/{booking_id}/payments/{payment_id}/approve`
**Staff approves a payment AFTER editing/correcting**

```python
@router.post("/bookings/{booking_id}/payments/{payment_id}/approve", response_model=GenericResponse)
async def api_approve_booking_payment(booking_id: int, payment_id: int, req: dict, auth=Depends(verify_api_key)):
```

**Payload:**
```json
{
    "verified_by": "StaffName"
}
```

**Logic:**
1. Payment must be `pending`
2. If `total_amount` and `actual_amount` differ → allow (already edited)
3. Set `status='paid', verified_by, verified_at`
4. Check if all required installments are paid → update `console_bookings.payment_status`

### 4.4 `POST /api/bookings/{booking_id}/payments/{payment_id}/reject`
**Staff rejects a payment**

```python
@router.post("/bookings/{booking_id}/payments/{payment_id}/reject", response_model=GenericResponse)
async def api_reject_booking_payment(booking_id: int, payment_id: int, req: dict, auth=Depends(verify_api_key)):
```

**Payload:**
```json
{
    "staff_notes": "Amount incorrect — please resend",
    "verified_by": "StaffName"
}
```

**Logic:**
1. Set `status='rejected'`
2. Notify customer via Telegram: "ငွေစာရင်းမှားယွင်းနေပါသည် — ပြန်လည်ပေးပို့ပေးပါ"

### 4.5 `GET /api/bookings/{booking_id}/payments`
**Get all payments for a booking (with screenshot URLs)**

```python
@router.get("/bookings/{booking_id}/payments", response_model=GenericResponse)
async def api_get_booking_payments(booking_id: int, auth=Depends(verify_api_key)):
```

**Returns:**
```json
{
    "success": true,
    "data": {
        "booking_id": 131,
        "payment_status": "partial",  // none | pending | partial | paid_full | overdue
        "total_price": 90000,
        "payments": [
            {
                "id": 1,
                "payment_number": 1,
                "installment_amount": 45000,
                "stated_amount": 45000,
                "stated_method": "KPay",
                "actual_amount": 45000,
                "status": "paid",
                "verified_by": "StaffName",
                "screenshot_url": "/static/payments/bk_131/first_payment.jpg",
                "created_at": "2026-07-12T10:00:00"
            },
            {
                "id": 2,
                "payment_number": 2,
                "installment_amount": 15000,
                "status": "pending",
                "created_at": null
            }
        ]
    }
}
```

### 4.6 `POST /api/bookings/{booking_id}/payments/screenshot`
**Upload payment screenshot (from staff dashboard or customer bot)**

```python
@router.post("/bookings/{booking_id}/payments/screenshot", response_model=GenericResponse)
async def api_upload_payment_screenshot(booking_id: int, req: dict (or UploadFile), auth=Depends(verify_api_key)):
```

- Accepts multipart file upload
- Saves to `/root/psvibe_api_server/static/payments/bk_{id}/`
- Returns URL path

### 4.7 `GET /api/bookings/{booking_id}/pricing`
**Get calculated pricing for a booking**

```python
@router.get("/bookings/{booking_id}/pricing", response_model=GenericResponse)
async def api_get_booking_pricing(booking_id: int, auth=Depends(verify_api_key)):
```

**Logic:**
1. Look up `console_id` on the booking
2. Get `base_rate` from `settings_config`
3. Get `multiplier` from `console_multipliers` config JSON
4. Calculate: `total = base_rate * multiplier * (duration_mins / 60)`
5. First payment: 50% of total
6. Remaining 3 payments: (remaining 50%) / 3

**Returns:**
```json
{
    "success": true,
    "data": {
        "console_id": "C-01",
        "base_rate_per_hour": 10000,
        "multiplier": 1.0,
        "duration_mins": 60,
        "total_price": 10000,
        "first_payment_50pct": 5000,
        "remaining_3_payments": 1667,
        "payment_plan": [
            {"number": 1, "label": "First Payment (50%)", "amount": 5000},
            {"number": 2, "label": "2nd Payment", "amount": 1667},
            {"number": 3, "label": "3rd Payment", "amount": 1667},
            {"number": 4, "label": "4th Payment", "amount": 1666}
        ]
    }
}
```

### 4.8 `PATCH /api/bookings/{booking_id}/pricing` (admin)
**Staff manually adjusts the price (override)**

```python
@router.patch("/bookings/{booking_id}/pricing", response_model=GenericResponse)
async def api_adjust_booking_pricing(booking_id: int, req: dict, auth=Depends(verify_api_key)):
```

**Payload:**
```json
{
    "total_price": 90000,
    "note": "Discount applied — returning customer"
}
```

### 4.9 Side Effect: Modify `PATCH /api/bookings/{booking_id}/status`
**When approving a booking → create payment plan**

After status changes to `confirmed`, if `payment_status` is `none`:
1. Calculate total price via pricing endpoint
2. Insert 4 `booking_payments` rows (pending status)
3. Set `console_bookings.payment_status = 'pending'`

---

## 5. Customer Bot Integration

### 5.1 New States for Payment Flow

Add these states to the existing booking flow in `customer_bot/booking_handlers.py`:

| State | Constant | Purpose |
|-------|----------|---------|
| 17    | `BK_PAY_CHOOSE` | After booking confirmed — ask "Pay now?" |
| 18    | `BK_PAY_AMOUNT` | Customer enters amount |
| 19    | `BK_PAY_METHOD` | KPay / Bank Transfer / Cash |
| 20    | `BK_PAY_SCREENSHOT` | Upload payment screenshot (photo message) |
| 21    | `BK_PAY_CONFIRM` | Review & submit payment |
| 22    | `BK_PAY_STATUS` | Show payment status (already submitted) |

### 5.2 Payment Flow in Bot

```
[Booking Confirmed Notification]
→ "ပထမအကြိမ် ငွေပေးချေရန် 50% (45,000 Ks) ကျပါတယ်။
   KPay/Bank Transfer ဖြင့် ပေးချေနိုင်ပါတယ်။"

→ [Customer selects "Pay Now"]
→ "ငွေပေးချေမည့် ပမာဏ ရိုက်ထည့်ပါ:"
→ [Customer enters: 45000]
→ "ဘယ် Payment Method လဲ?"
   [KPay] [Bank Transfer] [Cash]
→ "Payment Screenshot ပို့ပေးပါ (သို့) Skip နှိပ်ပါ:"
→ [Customer sends photo]
→ "အတည်ပြုပါသလား?
   ငွေပမာဏ: 45,000 Ks
   Method: KPay"
   [✅ Confirm] [❌ Cancel]
→ "ကျေးဇူးတင်ပါတယ်။ Staff မှ စစ်ဆေးအတည်ပြုပေးပါမယ်။"
```

### 5.3 Webhook for Payment Notifications
When customer submits payment, the bot should call `POST /api/bookings/{id}/payments` and forward the photo.

### 5.4 Payment Reminders
Bot should check for unpaid payment installments:
- 1 hour after booking confirmation → "ကျေးဇူးပြုပြီး ပထမအကြိမ်ငွေပေးချေပါ (45,000 Ks)"
- 24h before booking date → remind if first payment still unpaid

### 5.5 New File: `customer_bot/payment_handlers.py`
Create a dedicated payment handlers file:

```python
# States
BK_PAY_CHOOSE, BK_PAY_AMOUNT, BK_PAY_METHOD, BK_PAY_SCREENSHOT, BK_PAY_CONFIRM = range(17, 22)

async def pay_entry(update, context):
    # Entry point — called after booking is confirmed
    booking_id = context.user_data.get("_bk_last_id")
    pricing = await _api._api_get(f"bookings/{booking_id}/pricing")
    first_payment_amount = pricing["data"]["first_payment_50pct"]
    
    # Ask if user wants to pay now
    kb = ReplyKeyboardMarkup([["💳 Pay Now"], ["⏰ Pay Later"]], ...)
    await update.message.reply_text(
        f"ပထမအကြိမ်ငွေပေးချေရန် {first_payment_amount} Ks ကျပါတယ်...",
        reply_markup=kb
    )
    return BK_PAY_CHOOSE
```

---

## 6. Dashboard Frontend Changes

### 6.1 JS Changes (`BookingsManagement-LkSq52ld.js`)

Since the JS is minified, the approach is to:
1. **Extract source map** or **reverse-engineer** the component
2. **Add payment section** inside the existing edit modal
3. Rebuild with Vite/build tool

**Alternatively:** Create a separate payment modal component that opens from the booking row.

### 6.2 New Payment UI Sections

#### In Edit Modal: Bottom section

```
┌──────────────────────────────────────┐
│  Edit Booking #131              [X] │
├──────────────────────────────────────┤
│  [Date]  [Time]  [Duration]          │
│  [Console]  [Status]                 │
│  [Notes]                             │
├──────────────────────────────────────┤
│  💳 Payment Summary                  │
│  ──────────────────────              │
│  Total Price:  90,000 Ks             │
│  ┌─────────────────────────────┐     │
│  │ ████████████░░░░░░ 50% Paid │     │  ← Progress bar
│  └─────────────────────────────┘     │
│                                      │
│  Payment 1 (50%)  [✅ Paid]   45,000 │  ← Click to view screenshot
│  Payment 2        [⏳ Pending] 15,000│     → Modal opens with screenshot image
│  Payment 3        [⏳ Pending] 15,000│
│  Payment 4        [⏳ Pending] 15,000│
│                                      │
│  [Edit Pricing] [Adjust Amount]      │
├──────────────────────────────────────┤
│  [Cancel]  [Save]                    │
└──────────────────────────────────────┘
```

#### Payment Screenshot Modal

```
┌──────────────────────────────────────┐
│  Payment Screenshot — Payment 1  [X] │
├──────────────────────────────────────┤
│                                      │
│         ┌────────────┐              │
│         │            │              │
│         │  IMAGE     │              │
│         │            │              │
│         └────────────┘              │
│                                      │
│  Stated Amount:  45,000 Ks          │
│  Actual Amount:  [45000]   ✏️       │  ← Editable before approval
│  Method: KPay                        │
│                                      │
│  Staff Notes:  [_______________]    │
│                                      │
│  [❌ Reject]     [✅ Approve]       │
└──────────────────────────────────────┘
```

### 6.3 Vue Component Structure (For rebuild)

```vue
<template>
  <!-- Booking Table Section (existing) -->
  <div class="bookings-table">
    <table>...</table>
  </div>

  <!-- Edit Booking Modal (enhanced) -->
  <Modal v-if="showEditModal">
    <BookingForm />                <!-- existing fields -->
    <PaymentSummary />             <!-- NEW: payment progress -->
    <PaymentList />                <!-- NEW: per-payment rows -->
  </Modal>

  <!-- Screenshot Viewer Modal (NEW) -->
  <Modal v-if="showScreenshot">
    <img :src="screenshotUrl" />
    <PaymentEditForm />
    <ApproveRejectButtons />
  </Modal>
</template>
```

### 6.4 API Changes Needed in JS

The JS currently calls these endpoints:
- `GET /api/dashboard/bookings` — booking list
- `PUT /api/dashboard/bookings/{id}` — update booking
- `PUT /api/dashboard/bookings/{id}/timer` — adjust timer

**New calls needed:**
- `GET /api/bookings/{id}/payments` — fetch payment data
- `PATCH /api/bookings/{id}/payments/{pid}` — edit amount
- `POST /api/bookings/{id}/payments/{pid}/approve` — approve
- `POST /api/bookings/{id}/payments/{pid}/reject` — reject
- `POST /api/bookings/{id}/payments/screenshot` — upload screenshot

### 6.5 New Frontend File

If rebuilding is complex, create a separate HTML/JS page:
- `/root/psvibe_api_server/dashboard-dist/payments.html`
- Or add to the existing dashboard routes: `GET /admin/payments/{booking_id}`

---

## 7. Admin Telegram Bot Changes

### 7.1 Payment Notification in Admin Bot

When customer submits a payment via customer bot → **notify admin group**:

```
💳 New Payment Submission
━━━━━━━━━━━━━━━━━━
Booking #131 — Customer Name
Payment 1 (50%) — 45,000 Ks stated
Method: KPay
📸 [View Screenshot]
━━━━━━━━━━━━━━━━━━
[✅ Approve] [✏️ Edit Amount] [❌ Reject]
```

### 7.2 Admin Bot Inline Handlers

New callback handlers in `admin_bookings.py`:

| Callback Data | Handler | Purpose |
|---------------|---------|---------|
| `bkpay:view:{bid}:{pid}` | `cb_view_payment` | Show payment details + screenshot |
| `bkpay:approve:{pid}` | `cb_approve_payment` | Approve payment inline |
| `bkpay:edit:{pid}` | `cb_edit_payment_amount` | Prompt for actual amount |
| `bkpay:reject:{pid}` | `cb_reject_payment` | Prompt for rejection reason |

### 7.3 File Ownership
- **API changes:** `/root/psvibe_api_server/routes/booking_routes.py`
- **Bot changes:** `/root/psvibe-sales-bot/bot/handlers/admin_bookings.py`
- **Customer bot changes:** `/root/psvibe-sales-bot/customer_bot/booking_handlers.py` + new `payment_handlers.py`

---

## 8. Migration Steps

### Phase 1: Database (Estimated: 30 min)
```bash
# 1. Connect and run schema migration
mysql -h 127.0.0.1 -u psvibe_user -p psvibe_api

# Execute:
SOURCE /root/psvibe_api_server/migrations/007_booking_payments.sql
```

### Phase 2: API Endpoints (Estimated: 2-3 hours)
1. Create payment routes (can be added to booking_routes.py or new payment_routes.py)
2. Wire up pricing calculation using settings_config
3. Register new router in app.py

### Phase 3: Dashboard UI (Estimated: 4-6 hours)
1. Reverse-engineer Vue component or rebuild from source
2. Add payment summary section to edit modal
3. Add screenshot viewer modal
4. Add edit-then-approve workflow in UI
5. Test all status transitions

### Phase 4: Customer Bot (Estimated: 3-4 hours)
1. Create `customer_bot/payment_handlers.py`
2. Add payment states to ConversationHandler
3. Integrate after booking confirmation
4. Handle photo upload (screenshot)
5. Add payment reminder logic

### Phase 5: Admin Bot (Estimated: 2 hours)
1. Add payment notification when customer submits payment
2. Add inline approve/edit/reject handlers
3. Test full flow: customer submits → admin approves → payment recorded

### Phase 6: Testing & Polish (Estimated: 2-3 hours)
1. Test full flow end-to-end: Booking → Pay 50% → Approve → Pay remaining → Play
2. Test edge cases: wrong amount edit, reject+resubmit, overdue payments
3. Test screenshot viewing on mobile dashboard
4. Verify payment_status transitions

---

## 9. File-by-File Implementation Order

| # | File | Changes | Est. Time |
|---|------|---------|-----------|
| 1 | `routes/booking_routes.py` | Add pricing endpoint, payment endpoints (sections 4.1-4.9) | 2h |
| 2 | `routes/payment_routes.py` (NEW) | Separate payment module (imported by booking_routes) | 1h |
| 3 | `models.py` | Add `BookingPayment`, `BookingPricing` Pydantic models | 15m |
| 4 | `app.py` | Register new router (if separate file) | 5m |
| 5 | `customer_bot/payment_handlers.py` (NEW) | Payment flow states + handlers | 1.5h |
| 6 | `customer_bot/booking_handlers.py` | Wire payment entry after confirmation, add states to handler map | 1h |
| 7 | `customer_bot/api.py` | Add `_upload_photo` helper for screenshot | 30m |
| 8 | `bot/handlers/admin_bookings.py` | Payment inline handlers (view, approve, edit, reject) | 1.5h |
| 9 | `dashboard-dist/assets/BookingsManagement-LkSq52ld.js` | Reverse-engineer + rebuild with payment sections (see section 6) | 3-4h |
| 10 | Migration SQL file | `migrations/007_booking_payments.sql` | 30m |
| 11 | `static/payments/` | Create directory + nginx config for serving images | 15m |

---

## Appendix A: Pricing Calculation Details

```python
def calculate_booking_price(console_id: str, duration_mins: int) -> dict:
    """
    Calculate price for a booking.
    
    Example:
        console_id="C-09" (multiplier=1.2)
        base_rate=10000 MMK/hour
        duration=60 mins
        
        total = 10000 * 1.2 * (60/60) = 12000 MMK
        first_payment = 50% = 6000 MMK
        remaining = 6000 MMK / 3 = 2000 MMK per payment
    """
    multiplier = get_console_multiplier(console_id)  # from settings_config
    base_rate = get_base_rate()  # from settings_config
    hours = duration_mins / 60
    total = int(base_rate * multiplier * hours)
    first_payment = total // 2  # 50%
    remaining = total - first_payment
    per_payment = remaining // 3
    last_payment = remaining - (per_payment * 2)
    
    return {
        "total_price": total,
        "first_payment_50pct": first_payment,
        "remaining_total": remaining,
        "installments": [per_payment, per_payment, last_payment]
    }
```

## Appendix B: Edit-Then-Approve Workflow

```
1. Customer submits payment via bot
   → POST /api/bookings/131/payments
   → payment.status = "pending"

2. Staff opens booking in dashboard
   → Sees payment with stated_amount="45000" + screenshot

3. Staff checks KPay/Bank
   → Actual received = "44000" (wrong!)

4. Staff clicks "Edit Amount"
   → Changes stated_amount to 44000
   → PATCH /api/bookings/131/payments/1
   → actual_amount=44000

5. Staff clicks "Approve Payment"
   → POST /api/bookings/131/payments/1/approve
   → payment.status = "paid"
   → If all payments done: booking.payment_status = "partial" or "paid_full"

6. If wrong: Staff clicks "Reject"
   → POST /api/bookings/131/payments/1/reject
   → payment.status = "rejected"
   → Customer gets notified: "ငွေစာရင်းမှားယွင်းနေပါသည် — ပြန်လည်ပေးပို့ပေးပါ"
```

## Appendix C: Payment Status Logic

```
console_bookings.payment_status values:

"none"     → Booking created, no payment plan yet
"pending"  → Booking confirmed, payment plan created, first payment not yet paid
"partial"  → At least one payment approved, not all 4 done
"paid_full" → All 4 payments approved
"overdue"  → Booking date has passed, payment not complete (auto-calc / manual)
```

## Appendix D: Database Migration SQL File

Create file: `/root/psvibe_api_server/migrations/007_booking_payments.sql`

```sql
-- Migration 007: Booking Payment System
-- Execute: mysql -h 127.0.0.1 -u psvibe_user -p psvibe_api < migrations/007_booking_payments.sql

-- 1. Booking Pricing Table
CREATE TABLE IF NOT EXISTS booking_pricing (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    booking_id        INT NOT NULL,
    console_id        VARCHAR(20) NOT NULL,
    base_rate_per_hour INT NOT NULL DEFAULT 10000,
    multiplier        DECIMAL(3,1) DEFAULT 1.0,
    duration_mins     INT NOT NULL,
    calculated_total  INT NOT NULL,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (booking_id) REFERENCES console_bookings(id) ON DELETE CASCADE,
    INDEX idx_booking_pricing_booking (booking_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Booking Payments Table
CREATE TABLE IF NOT EXISTS booking_payments (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    booking_id        INT NOT NULL,
    payment_number    TINYINT NOT NULL COMMENT '1-4: 1=first(50%), 2/3/4=remaining',
    installment_amount INT NOT NULL COMMENT 'Scheduled amount for this installment',
    stated_amount     INT DEFAULT NULL COMMENT 'Customer-stated amount',
    stated_method     VARCHAR(20) DEFAULT NULL COMMENT 'KPay / Bank Transfer / Cash',
    screenshot_path   VARCHAR(500) DEFAULT NULL COMMENT 'Path to screenshot file',
    screenshot_msg_id INT DEFAULT NULL COMMENT 'TG message ID of screenshot',
    actual_amount     INT DEFAULT NULL COMMENT 'Staff-verified actual amount',
    staff_notes       TEXT DEFAULT NULL,
    verified_by       VARCHAR(100) DEFAULT NULL,
    verified_at       DATETIME DEFAULT NULL,
    status            VARCHAR(20) DEFAULT 'pending' COMMENT 'pending|paid|rejected|refunded',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (booking_id) REFERENCES console_bookings(id) ON DELETE CASCADE,
    INDEX idx_bkpay_booking (booking_id),
    INDEX idx_bkpay_status (status),
    INDEX idx_bkpay_booking_status (booking_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. Add columns to console_bookings
ALTER TABLE console_bookings
ADD COLUMN payment_status VARCHAR(20) DEFAULT 'none' 
    COMMENT 'none|pending|partial|paid_full|overdue' AFTER status,
ADD COLUMN total_price INT DEFAULT NULL 
    COMMENT 'Total price in MMK' AFTER payment_status;
```

---

> **Next Steps:**  
> 1. Share this plan with Osmo for review  
> 2. Start with Phase 1 (DB migration)  
> 3. Implement API endpoints  
> 4. Wire up customer bot payment flow  
> 5. Rebuild dashboard UI component  
> 6. Test full end-to-end flow  
> 7. Deploy
