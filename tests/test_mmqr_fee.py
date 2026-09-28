"""Pure (no-DB, no-write) unit tests for the MMQR merchant-fee net display.

Owner 2026-09-28 Osmo: the real MMQR merchant account (CB, "PS Vibe- SC")
credits QR payments NET of a 0.3% transaction fee while the app records gross.
This suite verifies the fee math + config fallback in routes/finance_routes.py:

  * compute_fee_fields — 2dp rounding, zero-fee accounts, fee-flagged accounts
  * _coerce_fee_pct   — config read/fallback (None / 0 / bad string → default 0.3)
  * constants          — default 0.3%, fee-flagged label set, config key

Acceptance identity (from the owner's bank app): 313,467 × 0.997 = 312,526.60,
i.e. fee = 940.40, net = 312,526.60.

Run:  /opt/psvibe/api/venv/bin/python tests/test_mmqr_fee.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_mmqr_fee.py -q
"""
import os
import sys

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes.finance_routes import (
    compute_fee_fields,
    _coerce_fee_pct,
    _MMQR_FEE_KEY,
    _MMQR_FEE_DEFAULT_PCT,
    _FEE_ACCOUNT_LABELS,
)

PASS = 0
FAIL = 0


def check(name, got, expected):
    global PASS, FAIL
    ok = got == expected
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")


print("=== MMQR fee (net-of-fee) math tests ===")

# ── 1. Acceptance identity: 313,467 gross → fee 940.40, net 312,526.60 ──
f = compute_fee_fields(313467, 0.3, "MMQR - CB")
check("MMQR: fee_amount == 940.40", f["fee_amount"], 940.40)
check("MMQR: net_balance == 312526.60", f["net_balance"], 312526.60)
check("MMQR: fee_pct == 0.3", f["fee_pct"], 0.3)
check("MMQR: fee_note == '0.3% MMQR fee'", f["fee_note"], "0.3% MMQR fee")

# net + fee must equal the gross balance (no money lost to rounding).
check("MMQR: net + fee == gross", round(f["net_balance"] + f["fee_amount"], 2), 313467.0)

# ── 2. Rounding to 2dp ──
f2 = compute_fee_fields(313467, 0.3, "MMQR - CB")
check("MMQR: fee rounded to 2dp", f2["fee_amount"] == round(313467 * 0.3 / 100.0, 2), True)

# ── 3. Zero-fee accounts → fee 0 / net == balance ──
for lbl in ["Cash", "KBZ Bank", "ACM's Acc", "KPay", "WavePay", "AYA Pay"]:
    z = compute_fee_fields(12345, 0.3, lbl)
    check(f"zero-fee {lbl}: fee_amount == 0", z["fee_amount"], 0.0)
    check(f"zero-fee {lbl}: net_balance == balance", z["net_balance"], 12345.0)
    check(f"zero-fee {lbl}: fee_pct == 0", z["fee_pct"], 0.0)
    check(f"zero-fee {lbl}: fee_note == ''", z["fee_note"], "")

# ── 4. MMQR with fee_pct 0 / negative → no fee ──
z0 = compute_fee_fields(313467, 0, "MMQR - CB")
check("MMQR fee_pct=0 → fee 0", z0["fee_amount"], 0.0)
check("MMQR fee_pct=0 → net == balance", z0["net_balance"], 313467.0)
check("MMQR fee_pct=0 → fee_pct 0", z0["fee_pct"], 0.0)

# ── 5. _coerce_fee_pct config read/fallback ──
check("coerce: None → default 0.3", _coerce_fee_pct(None), 0.3)
check("coerce: '' → default 0.3", _coerce_fee_pct(""), 0.3)
check("coerce: '0' → default 0.3", _coerce_fee_pct("0"), 0.3)
check("coerce: '0.0' → default 0.3", _coerce_fee_pct("0.0"), 0.3)
check("coerce: 'abc' → default 0.3", _coerce_fee_pct("abc"), 0.3)
check("coerce: negative -1 → default 0.3", _coerce_fee_pct(-1), 0.3)
check("coerce: '0.25' → 0.25", _coerce_fee_pct("0.25"), 0.25)
check("coerce: 0.3 → 0.3", _coerce_fee_pct(0.3), 0.3)
check("coerce: '0.5' → 0.5", _coerce_fee_pct("0.5"), 0.5)

# ── 6. Constants (single source of truth) ──
check("_MMQR_FEE_KEY == 'mmqr_fee_pct'", _MMQR_FEE_KEY, "mmqr_fee_pct")
check("_MMQR_FEE_DEFAULT_PCT == 0.3", _MMQR_FEE_DEFAULT_PCT, 0.3)
check("_FEE_ACCOUNT_LABELS == {'MMQR - CB'}", _FEE_ACCOUNT_LABELS, {"MMQR - CB"})

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
