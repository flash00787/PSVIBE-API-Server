# PS VIBE API Server — Database Schema

> Database: `psvibe_api` (MySQL 8.0)  
> Last dump: 2026-06-22

## Core Tables

### `console_booking` — Console bookings (primary business table)
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | Booking ID |
| `branch_id` | INT DEFAULT 1 | Branch ID (FK → branches) |
| `console_id` | VARCHAR(20) | Console identifier |
| `member_id` | VARCHAR(50) | Member ID (FK → member_wallets) |
| `booking_date` | DATE | Booking date |
| `start_time` | DATETIME | Scheduled start time |
| `end_time` | DATETIME | Scheduled end time |
| `status` | VARCHAR(20) | pending/confirmed/checked_in/Active/cancelled/completed/no_show |
| `staff_name` | VARCHAR(100) | Handling staff |
| `notes` | TEXT | Notes |
| `created_at` | TIMESTAMP | Created timestamp |
| `telegram_chat_id` | VARCHAR(50) | Telegram chat ID for notifications |
| `duration_mins` | INT | Session duration in minutes |
| `phone` | VARCHAR(50) | Customer phone |
| `game_name` | VARCHAR(200) | Game to play |

**Indexes:** `booking_date`, `status`, `branch_id`, `console_id+status`, `date+console+time`

### `member_wallets` — Member wallet/balance (primary member table)
| Column | Type | Description |
|--------|------|-------------|
| `member_id` | VARCHAR(50) PK | Unique member ID (e.g. PSV_A_0001) |
| `branch_id` | INT DEFAULT 1 | Branch ID |
| `balance_mins` | INT | Current wallet minutes balance |
| `total_bought_mins` | INT | Lifetime minutes purchased |
| `total_used_mins` | INT | Lifetime minutes used |
| `member_name` | VARCHAR(200) | Display name |
| `phone` | VARCHAR(50) | Phone number |
| `effective_rate` | DECIMAL(10,2) | Effective hourly rate multiplier |
| `tier` | VARCHAR(50) | Membership tier (Warrior/Silver/Gold/Platinum) |
| `total_spend` | DECIMAL(15,2) | Total spend (MMK) |
| `lifetime_spend` | DECIMAL(12,2) | Lifetime spend |
| `ranking_net_spend` | DECIMAL(12,2) | Net spend for ranking |
| `reg_staff` | VARCHAR(100) | Registering staff |
| `referral_code` | VARCHAR(50) | Referral code |
| `join_date` | DATE | Join date |

### `console_status` — Live console state
| Column | Type | Description |
|--------|------|-------------|
| `console_id` | VARCHAR(20) PK | Console identifier |
| `status` | VARCHAR(50) | available/busy/maintenance |
| `console_type` | VARCHAR(50) | PS5 / PS5 Pro |
| `current_game` | TEXT | Currently played game |
| `current_member` | VARCHAR(100) | Current member name |
| `start_time` | DATETIME | Session start time |
| `branch_id` | INT DEFAULT 1 | Branch ID |

**Trigger:** `trg_console_status_audit` → logs status changes to `console_status_audit`

### `sales_daily` — Daily sales records
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | Record ID |
| `branch_id` | INT DEFAULT 1 | Branch ID |
| `voucher_no` | VARCHAR(50) | Voucher/receipt number |
| `sale_date` | DATE | Sale date |
| `console_id` | VARCHAR(20) | Console used |
| `member_id` | VARCHAR(50) | Member ID |
| `amount` | DECIMAL(10,2) | Total amount |
| `gross` | DECIMAL | Gross before discount |
| `discount` | DECIMAL | Discount amount |
| `net` | DECIMAL | Net after discount |
| `staff_name` | VARCHAR(100) | Staff name |
| `payment_method` | VARCHAR(50) | Payment method (split format) |
| `notes` | TEXT | Notes |

### `topup_log` — Wallet top-up transactions
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | Record ID |
| `branch_id` | INT DEFAULT 1 | Branch ID |
| `member_id` | VARCHAR(50) | Member ID |
| `amount` | DECIMAL(10,2) | Top-up amount (MMK) |
| `mins_added` | INT | Minutes added |
| `topup_date` | DATETIME | Top-up date |
| `staff_name` | VARCHAR(100) | Staff name |
| `payment_method` | VARCHAR(50) | Payment method |
| `balance_before` | DECIMAL | Balance before |
| `balance_after` | DECIMAL | Balance after |
| `balance_mins_before` | INT | Wallet mins before |
| `balance_mins_after` | INT | Wallet mins after |

## Configuration & Settings

### `settings_config` — Key-value configuration store
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `config_key` | VARCHAR(100) UNIQUE | Config key |
| `config_value` | TEXT | Config value (string, int, float, json, bool) |
| `config_type` | ENUM | string/int/float/json/bool |
| `category` | VARCHAR(50) | Category grouping |
| `description` | VARCHAR(255) | Description |

### `branches` — Branch configuration
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `name` | VARCHAR(100) | Branch name |
| `code` | VARCHAR(20) UNIQUE | Short code |
| `address` | TEXT | Physical address |
| `phone` | VARCHAR(50) | Contact phone |
| `is_active` | TINYINT(1) | Active flag |
| `open_time` / `close_time` | TIME | Operating hours |
| `telegram_group_id` | VARCHAR(50) | Notification group |

## Gamification & Loyalty

### `games_library` — Game catalog
| Column | Type | Description |
|--------|------|-------------|
| `game_title` | VARCHAR(255) PK | Game title (unique) |
| `genre` | VARCHAR(100) | Game genre |
| `solo_multi` | VARCHAR(50) | Single/Multiplayer |
| `final_status` | TEXT | Aggregated install status |
| `disc_count` | INT | Physical disc count |

### `console_games` — Game-to-console mapping
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `console_id` | VARCHAR(20) | Console (FK) |
| `game_title` | VARCHAR(255) | Game title |
| `status` | VARCHAR(20) | active/Installed/Session/Not Installed |
| `install_type` | VARCHAR(50) | SSD/Disc/Digital |
| `slot_position` | INT | SSD slot position |

### `promotions` — Active promotions & campaigns
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `name` | VARCHAR(100) | Promotion name |
| `promo_type` | VARCHAR(20) | cashback_coupon / discount |
| `promo_name` | VARCHAR(100) | Display name |
| `discount_type` | VARCHAR(50) | percent / fixed |
| `discount_value` | DECIMAL(10,2) | Discount value |
| `start_date` / `end_date` | DATE | Active date range |
| `coupon_expiry_date` | DATE | Coupon expiration |
| `status` | VARCHAR(20) | active/inactive |

### `member_coupons` — Issued coupons
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `coupon_code` | VARCHAR(20) UNIQUE | Unique code (e.g. CBXXXXXX) |
| `member_id` | VARCHAR(50) | Member ID |
| `original_minutes` / `balance_minutes` | INT | Coupon minutes |
| `issued_date` / `expiry_date` | DATETIME | Validity period |
| `status` | VARCHAR(20) | active/redeemed/expired |
| `promo_id` | INT FK | → promotions |
| `source_session_id` | INT | Originating session |

## Bot Users & Feedback

### `customer_bot_users` — Telegram bot user profiles
| Column | Type | Description |
|--------|------|-------------|
| `tg_id` | VARCHAR(64) PK | Telegram user ID |
| `username` | VARCHAR(255) | Telegram @username |
| `first_name` | VARCHAR(255) | Telegram first name |
| `first_seen` / `last_seen` | DATETIME | Activity timestamps |
| `total_interactions` | INT | Total message count |
| `member_id` | VARCHAR(50) | Linked PS VIBE member |
| `phone` | VARCHAR(50) | Phone number |
| `session_count` / `booking_count` | INT | Session/booking stats |
| `last_funnel_step` | VARCHAR(64) | Booking funnel position |

### `customer_bot_activity_log` — Bot user activity log
| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT AUTO_INCREMENT PK | |
| `tg_id` | VARCHAR(64) | Telegram user |
| `activity_type` | VARCHAR(32) | Event type |
| `detail` | VARCHAR(512) | Detail text |
| `created_at` | DATETIME | Timestamp |

### `customer_feedback` — Customer feedback/ratings
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `tg_id` | VARCHAR(64) | Telegram user |
| `booking_id` | VARCHAR(50) | Booking reference |
| `rating` | INT | Star rating (1-5) |
| `comment` | TEXT | Text comment |
| `console_id` | VARCHAR(50) | Console used |

## Staff & HR

### `staff_records` — Staff directory
| Column | Type | Description |
|--------|------|-------------|
| `staff_id` | INT AUTO_INCREMENT PK | Staff ID |
| `staff_name` | VARCHAR(200) UNIQUE | Full name |
| `base_salary` | DECIMAL(12,2) | Monthly base salary |
| `hourly_rate` | DECIMAL(10,2) | Hourly rate |
| `role` | VARCHAR(100) | Job role |
| `is_active` | TINYINT(1) | Active flag |
| `branch_id` | INT | Branch assignment |

### `attendance_log` — Staff attendance
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `staff_id` | INT FK | → staff_records |
| `check_in` / `check_out` | DATETIME | Clock in/out |
| `hours_worked` | DECIMAL(5,2) | Calculated hours |
| `daily_pay` | DECIMAL(12,2) | Daily wage |
| `status` | VARCHAR(20) | checked_in/checked_out/late/leave |
| `date` | DATE | Work date |

### `dashboard_users` — Staff dashboard login
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `username` | VARCHAR(50) UNIQUE | Login username |
| `password_hash` | VARCHAR(255) | bcrypt hash |
| `role` | ENUM('admin','staff') | Access level |
| `display_name` | VARCHAR(100) | Display name |
| `is_active` | TINYINT(1) | Active flag |
| `last_login` | TIMESTAMP | Last login time |

## Finance & Accounting

### `cash_movements` — Cash account movements
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `movement_type` | ENUM(inject/eject/transfer_out/transfer_in/dividend) | |
| `account` | VARCHAR(50) | Account name (Cash/KPay/WavePay/etc.) |
| `amount` | DECIMAL(12,0) | Amount (MMK) |
| `staff_name` | VARCHAR(100) | Staff reference |

### `accounts` — Payment account balances
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `account_name` | VARCHAR(100) | Account name |
| `account_type` | VARCHAR(50) | Account type |
| `balance` | DECIMAL(15,2) | Current balance |

### `opex` — Operating expenses
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `category` | VARCHAR(100) | Expense category |
| `description` | TEXT | Description |
| `amount` | INT | Amount (MMK) |
| `payment_method` | VARCHAR(50) | Payment method |
| `expense_date` | DATE | Expense date |

### `finance_assets` — Fixed asset register
Depreciation tracking, book value, disposal management.

### `capital_movements` — Shareholder capital injections/ejections  
### `shareholders` — Shareholder registry  
### `profit_distributions` + `profit_distribution_details` — Profit sharing

## Inventory

### `inventory` — Inventory items
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT PK | |
| `item_name` | VARCHAR(200) | Item name |
| `category` | VARCHAR(100) | Item category |
| `quantity` | INT | Current stock |
| `unit_price` | DECIMAL(10,2) | Per-unit price |
| `reorder_level` | INT | Reorder point |

### `stock_in` — Stock purchase records (FIFO)
### `stock_out` — Stock sale/usage records  
### `stock_hold` — Items held for active bookings (reserved inventory)  
### `food_cart` — Food item cart for active bookings
