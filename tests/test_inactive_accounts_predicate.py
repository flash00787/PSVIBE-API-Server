"""Pure (no-DB, no-write) unit test for the inactive-account predicates.

Verifies the SINGLE SOURCE OF TRUTH in routes/account_routes.py:
  * _OPERATIONAL (active, staff-selectable) == Cash + MMQR - CB
  * _INACTIVE_ACCOUNTS == KPay / WavePay / AYA Pay
  * ALL_ACCOUNTS still contains all 7 (owner money stays visible)
  * _KEY_BY_NAME still maps the inactive accounts (balance lookup keeps working)
  * _allowed_accounts / _is_active_account role semantics (staff vs admin)
  * which pickers/validations accept which accounts (transfer + daily open/close)

No DB connection, no prod writes. Run:
  /opt/psvibe/api/venv/bin/python tests/test_inactive_accounts_predicate.py
"""
import os
import sys

sys.path.insert(0, "/opt/psvibe/api")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-secret-not-for-prod")

from routes.account_routes import (
    _OPERATIONAL,
    _INACTIVE_ACCOUNTS,
    _OWNER_ONLY,
    ALL_ACCOUNTS,
    _KEY_BY_NAME,
    _is_active_account,
    _allowed_accounts,
)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


print("=== inactive-account predicate tests ===")

# 1. The ACTIVE (operational) set is exactly Cash + MMQR - CB.
check("_OPERATIONAL == [Cash, MMQR - CB]", _OPERATIONAL == ["Cash", "MMQR - CB"])

# 2. The INACTIVE set is exactly the three disabled accounts.
check("_INACTIVE_ACCOUNTS == [KPay, WavePay, AYA Pay]", _INACTIVE_ACCOUNTS == ["KPay", "WavePay", "AYA Pay"])

# 3. ALL_ACCOUNTS still contains every account (money stays visible, no deletions).
check("ALL_ACCOUNTS has 7 accounts", len(ALL_ACCOUNTS) == 7)
for name in ["Cash", "MMQR - CB", "KPay", "WavePay", "AYA Pay", "ACM's Acc", "KBZ Bank"]:
    check(f"ALL_ACCOUNTS contains {name}", name in ALL_ACCOUNTS)

# 4. _KEY_BY_NAME keeps mappings for the inactive accounts (balance lookup works).
for name, key in [("Cash", "cash"), ("MMQR - CB", "mmqr_cb"), ("KPay", "kpay"),
                  ("WavePay", "wave"), ("AYA Pay", "aya_pay")]:
    check(f"_KEY_BY_NAME[{name}] == {key}", _KEY_BY_NAME.get(name) == key)

# 5. _is_active_account flags.
for act in ["Cash", "MMQR - CB", "ACM's Acc", "KBZ Bank"]:
    check(f"_is_active_account({act!r}) is True", _is_active_account(act) is True)
for inact in ["KPay", "WavePay", "AYA Pay"]:
    check(f"_is_active_account({inact!r}) is False", _is_active_account(inact) is False)

# 6. Role semantics.
staff = _allowed_accounts({"role": "staff"})
admin = _allowed_accounts({"role": "admin"})
check("staff allowed == [Cash, MMQR - CB]", staff == ["Cash", "MMQR - CB"])
check("admin allowed == all 7", admin == ALL_ACCOUNTS and len(admin) == 7)

# 7. Which pickers/validations accept which accounts.
#    (a) daily-balance open/close validates against _OPERATIONAL.
for inact in _INACTIVE_ACCOUNTS:
    check(f"daily open/close rejects inactive {inact}", inact not in _OPERATIONAL)
for act in ["Cash", "MMQR - CB"]:
    check(f"daily open/close accepts active {act}", act in _OPERATIONAL)

#    (b) transfer validation: staff cannot select inactive/owner-only accounts.
def transfer_rejected(role, account):
    allowed = _allowed_accounts({"role": role})
    if account not in ALL_ACCOUNTS:
        return "unknown"
    if account not in allowed:
        return "owner-only" if account in _OWNER_ONLY else "inactive"
    return None

check("staff transfer -> KPay rejected (inactive)", transfer_rejected("staff", "KPay") == "inactive")
check("staff transfer -> WavePay rejected (inactive)", transfer_rejected("staff", "WavePay") == "inactive")
check("staff transfer -> AYA Pay rejected (inactive)", transfer_rejected("staff", "AYA Pay") == "inactive")
check("staff transfer -> ACM's Acc rejected (owner-only)", transfer_rejected("staff", "ACM's Acc") == "owner-only")
check("staff transfer -> Cash allowed", transfer_rejected("staff", "Cash") is None)
check("staff transfer -> MMQR - CB allowed", transfer_rejected("staff", "MMQR - CB") is None)
check("admin transfer -> KPay allowed", transfer_rejected("admin", "KPay") is None)
check("admin transfer -> WavePay allowed", transfer_rejected("admin", "WavePay") is None)
check("admin transfer -> AYA Pay allowed", transfer_rejected("admin", "AYA Pay") is None)
check("admin transfer -> ACM's Acc allowed", transfer_rejected("admin", "ACM's Acc") is None)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
