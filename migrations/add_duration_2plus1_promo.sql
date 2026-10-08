-- ============================================================================
-- PS VIBE — Weekday Happy Hour "2+1" DURATION promotion (owner Osmo, 2026-10-08)
-- ============================================================================
-- Additive + reversible schema migration. Run via the idempotent runner:
--     /opt/psvibe/api/venv/bin/python migrations/add_duration_2plus1_promo.py
-- (The runner checks information_schema before each ALTER, so re-running is a no-op.)
--
-- New model
-- ---------
--   promotions.applies_auto       TINYINT(1)  — 1 = auto-bind at checkout (no picker)
--   promotions.weekday_mask       TINYINT     — bitmask, Mon=bit1 … Sun=bit7 (Mon–Fri = 62)
--   promotions.time_window_start  TIME        — inclusive lower bound (MMT), e.g. 09:00:00
--   promotions.time_window_end    TIME        — EXCLUSIVE upper bound (MMT), e.g. 15:00:00
--   promotions.tier_charge_mins   INT         — charge=120
--   promotions.tier_cycle_mins    INT         — cycle=180
--   console_sessions.promo_id / promo_applied / billable_mins / free_mins — checkout snapshot
--   sales_daily.billable_mins / free_mins — per-sale snapshot for reporting
--
-- Promo type is the EXISTING `promo_type` value 'duration_2plus1' (no new enum).
-- The promo is NEVER activated here — the owner switches it on from the dashboard.

-- ── promotions ─────────────────────────────────────────────────────────────
ALTER TABLE promotions
  ADD COLUMN applies_auto TINYINT(1) NOT NULL DEFAULT 0 AFTER notes;
ALTER TABLE promotions
  ADD COLUMN weekday_mask TINYINT NOT NULL DEFAULT 0 AFTER applies_auto;
ALTER TABLE promotions
  ADD COLUMN time_window_start TIME NULL AFTER weekday_mask;
ALTER TABLE promotions
  ADD COLUMN time_window_end TIME NULL AFTER time_window_start;
ALTER TABLE promotions
  ADD COLUMN tier_charge_mins INT NOT NULL DEFAULT 120 AFTER time_window_end;
ALTER TABLE promotions
  ADD COLUMN tier_cycle_mins INT NOT NULL DEFAULT 180 AFTER tier_charge_mins;

-- ── console_sessions ───────────────────────────────────────────────────────
ALTER TABLE console_sessions
  ADD COLUMN promo_id INT NULL AFTER linked_sale_id;
ALTER TABLE console_sessions
  ADD COLUMN promo_applied TINYINT(1) NOT NULL DEFAULT 0 AFTER promo_id;
ALTER TABLE console_sessions
  ADD COLUMN billable_mins INT NULL AFTER promo_applied;
ALTER TABLE console_sessions
  ADD COLUMN free_mins INT NULL AFTER billable_mins;

-- ── sales_daily ────────────────────────────────────────────────────────────
ALTER TABLE sales_daily
  ADD COLUMN billable_mins INT NULL AFTER game_name;
ALTER TABLE sales_daily
  ADD COLUMN free_mins INT NULL AFTER billable_mins;

-- ============================================================================
-- ROLLBACK (reverse) — run only if the feature must be reverted:
-- ============================================================================
-- ALTER TABLE promotions
--   DROP COLUMN applies_auto, DROP COLUMN weekday_mask, DROP COLUMN time_window_start,
--   DROP COLUMN time_window_end, DROP COLUMN tier_charge_mins, DROP COLUMN tier_cycle_mins;
-- ALTER TABLE console_sessions
--   DROP COLUMN promo_id, DROP COLUMN promo_applied, DROP COLUMN billable_mins, DROP COLUMN free_mins;
-- ALTER TABLE sales_daily DROP COLUMN billable_mins, DROP COLUMN free_mins;
