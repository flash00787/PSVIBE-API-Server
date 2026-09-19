#!/usr/bin/env python3
"""PS VIBE — verify TRANSFER direction end-to-end (money correctness).

Canonical convention under test:
    transfer_out  -> stored NEGATIVE on the SOURCE account (money leaves)
    transfer_in   -> stored POSITIVE on the DESTINATION account (money arrives)

Decisive test:
  * a 1000 Ks  Cash -> KPay  transfer must show  delta(Cash) = -1000, delta(KPay) = +1000
    on GET /api/accounts/transferable  (SOURCE MUST GO DOWN)
  * the reverse 1000 Ks KPay -> Cash transfer must return BOTH to baseline
  * /api/dashboard/finance/balances, /api/dashboard/financial/balance-sheet and
    /api/dashboard/financial/cashflow must show the SAME direction and must not
    introduce a new discrepancy.

The script deletes ONLY the rows it created and proves the leftover count is 0.

Requires: JWT_SECRET_KEY in the environment (same secret the API uses).
Run:  JWT_SECRET_KEY=... /opt/psvibe/api/venv/bin/python scripts/verify_transfer_direction.py
Exit code 0 = all checks passed; 1 = a check failed (details printed).
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/opt/psvibe/api")

from auth import create_access_token  # noqa: E402
from mysql_db import query, execute    # noqa: E402

BASE = os.environ.get("PSVIBE_API_BASE", "http://127.0.0.1:8000")
AMOUNT = 1000

if not os.environ.get("JWT_SECRET_KEY"):
    print("FATAL: JWT_SECRET_KEY env var is required (source /etc/psvibe/secrets.env).")
    sys.exit(2)

ADMIN = create_access_token({"sub": "admin"})
STAFF = create_access_token({"sub": "demo", "role": "staff", "scope": "staff-webapp"})
ADMIN_H = {"Authorization": "Bearer " + ADMIN}
STAFF_H = {"Authorization": "Bearer " + STAFF}

FAILURES = []
CREATED_REFS = []


def _req(method, path, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def check(label, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(label + (" :: " + detail if detail else ""))


def transferable(headers):
    s, d = _req("GET", "/api/accounts/transferable", headers)
    assert s == 200, (s, d)
    return {a["account"]: a["balance"] for a in d["accounts"]}, d


def finance_balances(headers):
    s, d = _req("GET", "/api/dashboard/finance/balances", headers)
    assert s == 200, (s, d)
    return {a["label"]: a["balance"] for a in d["accounts"]}


def balance_sheet(headers):
    s, d = _req("GET", "/api/dashboard/financial/balance-sheet?year=2026&month=9", headers)
    assert s == 200, (s, d)
    items = ((d.get("data") or {}).get("assets") or {}).get("current", {}).get("items", [])
    return {i["account"]: i["balance"] for i in items}, (d.get("data") or {}).get("assets", {}).get("current", {}).get("total")


def cashflow(headers):
    s, d = _req("GET", "/api/dashboard/financial/cashflow?year=2026&month=9", headers)
    assert s == 200, (s, d)
    return d.get("data") or {}


def do_transfer(frm, to, amount, note):
    body = {"from_account": frm, "to_account": to, "amount": amount,
            "note": note, "staff_name": "verify-script"}
    s, d = _req("POST", "/api/accounts/transfer", ADMIN_H, body)
    assert s == 200, (s, d)
    CREATED_REFS.append(d["ref"])
    return d["ref"]


def cleanup():
    removed = 0
    for ref in CREATED_REFS:
        removed += execute("DELETE FROM cash_movements WHERE note LIKE %s", (f"%{ref}%",))
        removed += execute("DELETE FROM account_transfers WHERE ref = %s", (ref,))
    left = 0
    for ref in CREATED_REFS:
        left += int((query("SELECT COUNT(*) AS c FROM cash_movements WHERE note LIKE %s", (f"%{ref}%",))[0] or {}).get("c") or 0)
        left += int((query("SELECT COUNT(*) AS c FROM account_transfers WHERE ref = %s", (ref,))[0] or {}).get("c") or 0)
    # global integrity check
    pos_out = int((query("SELECT COUNT(*) AS c FROM cash_movements WHERE movement_type='transfer_out' AND amount > 0")[0] or {}).get("c") or 0)
    neg_in = int((query("SELECT COUNT(*) AS c FROM cash_movements WHERE movement_type='transfer_in' AND amount < 0")[0] or {}).get("c") or 0)
    return removed, left, pos_out, neg_in


def main():
    print("=" * 72)
    print("PS VIBE transfer-direction verification")
    print("=" * 72)

    base_t, _ = transferable(ADMIN_H)
    base_f = finance_balances(ADMIN_H)
    base_bs, base_bs_total = balance_sheet(ADMIN_H)
    cf_before = cashflow(ADMIN_H)
    print("baseline /transferable :", json.dumps(base_t))
    print("baseline /finance/bal  :", json.dumps(base_f))

    print("\n[1] 1000 Ks Cash -> KPay")
    do_transfer("Cash", "KPay", AMOUNT, "verify fwd")
    fwd_t, _ = transferable(ADMIN_H)
    fwd_f = finance_balances(ADMIN_H)
    fwd_bs, fwd_bs_total = balance_sheet(ADMIN_H)
    cf_fwd = cashflow(ADMIN_H)
    dC = fwd_t["Cash"] - base_t["Cash"]
    dK = fwd_t["KPay"] - base_t["KPay"]
    check("source Cash goes DOWN by 1000 on /transferable", dC == -AMOUNT, f"delta={dC}")
    check("dest KPay goes UP by 1000 on /transferable", dK == +AMOUNT, f"delta={dK}")
    dCf = fwd_f["Cash"] - base_f["Cash"]
    dKf = fwd_f["KPay"] - base_f["KPay"]
    check("source Cash goes DOWN on /dashboard/finance/balances", dCf == -AMOUNT, f"delta={dCf}")
    check("dest KPay goes UP on /dashboard/finance/balances", dKf == +AMOUNT, f"delta={dKf}")
    dCb = fwd_bs.get("Cash", 0) - base_bs.get("Cash", 0)
    dKb = fwd_bs.get("KPay", 0) - base_bs.get("KPay", 0)
    check("balance-sheet: Cash DOWN", dCb == -AMOUNT, f"delta={dCb}")
    check("balance-sheet: KPay UP", dKb == +AMOUNT, f"delta={dKb}")
    check("balance-sheet total unchanged by an internal transfer",
          fwd_bs_total == base_bs_total, f"{base_bs_total} -> {fwd_bs_total}")
    _cb = float((cf_before.get("summary") or {}).get("closing_balance") or 0)
    _ca = float((cf_fwd.get("summary") or {}).get("closing_balance") or 0)
    check("cashflow closing unchanged by an internal transfer (nets to zero)",
          _ca == _cb, f"{_cb} -> {_ca}")

    print("\n[2] reverse 1000 Ks KPay -> Cash")
    do_transfer("KPay", "Cash", AMOUNT, "verify rev")
    rev_t, _ = transferable(ADMIN_H)
    check("Cash back to baseline", rev_t["Cash"] == base_t["Cash"], f"{rev_t['Cash']} vs {base_t['Cash']}")
    check("KPay back to baseline", rev_t["KPay"] == base_t["KPay"], f"{rev_t['KPay']} vs {base_t['KPay']}")

    print("\n[3] cleanup (delete only our own test rows)")
    _removed, left, pos_out, neg_in = cleanup()
    post_t, _ = transferable(ADMIN_H)
    check("balances restored after cleanup", post_t == base_t, json.dumps(post_t))
    check("leftover test rows == 0", left == 0, f"leftover={left}")
    check("no positive transfer_out rows remain anywhere", pos_out == 0, f"count={pos_out}")
    check("no negative transfer_in rows remain anywhere", neg_in == 0, f"count={neg_in}")

    print("\n[4] cash-flow vs balance-sheet consistency (no NEW discrepancy)")
    cf_after = cashflow(ADMIN_H)
    _, bs_total_after = balance_sheet(ADMIN_H)
    close_before = float((cf_before.get("summary") or {}).get("closing_balance") or 0)
    close_after = float((cf_after.get("summary") or {}).get("closing_balance") or 0)
    gap_before = close_before - float(base_bs_total or 0)
    gap_after = close_after - float(bs_total_after or 0)
    # The transfer sign fix must not CHANGE this gap. A non-zero residual here is a
    # pre-existing, transfer-independent cashflow/BS modelling difference (reported,
    # not introduced by this change).
    check("cashflow vs balance-sheet gap UNCHANGED (no newly-introduced discrepancy)",
          abs(gap_after - gap_before) <= 1,
          f"gap_before={gap_before} gap_after={gap_after} (pre-existing residual)")

    print("\n[5] staff vs admin balances access")
    s_t, t_d = transferable(STAFF_H)
    a_t, a_d = transferable(ADMIN_H)
    check("staff sees 4 shop accounts on /transferable", len(t_d["accounts"]) == 4,
          ",".join(t_d["accounts"][i]["account"] for i in range(len(t_d["accounts"]))))
    check("staff sees NO owner-only account", not any(a["owner_only"] for a in t_d["accounts"]))
    check("admin sees all 6 accounts on /transferable", len(a_d["accounts"]) == 6)
    check("admin sees 2 owner-only flagged", sum(1 for a in a_d["accounts"] if a["owner_only"]) == 2)
    s, sb = _req("GET", "/api/accounts/balances", STAFF_H)
    check("staff GET /accounts/balances -> 4 accounts", s == 200 and len(sb.get("accounts", [])) == 4, f"status={s} n={len(sb.get('accounts', []))}")
    s, ab = _req("GET", "/api/accounts/balances", ADMIN_H)
    check("admin GET /accounts/balances -> 6 accounts", s == 200 and len(ab.get("accounts", [])) == 6, f"status={s} n={len(ab.get('accounts', []))}")
    s, ob = _req("GET", "/api/finance/account-balances", STAFF_H)
    check("staff still 403 on owner-only /api/finance/account-balances", s == 403, f"status={s}")
    s, ob2 = _req("GET", "/api/finance/account-balances", ADMIN_H)
    check("admin 200 on owner-only /api/finance/account-balances", s == 200, f"status={s}")

    print("\n[6] transfer history contract")
    s, h = _req("GET", "/api/accounts/transfers?limit=5", ADMIN_H)
    row_ok = bool(h.get("transfers")) and all(
        k in h["transfers"][0] for k in ("ref", "from_account", "to_account", "amount", "note", "staff_name", "created_at"))
    check("admin history rows have required fields", s == 200 and row_ok, f"status={s}")
    check("admin history includes created_by_user", s == 200 and "created_by_user" in (h.get("transfers") or [{}])[0])
    check("history includes total", "total" in h, f"total={h.get('total')}")
    s, hs = _req("GET", "/api/accounts/transfers?limit=5", STAFF_H)
    check("staff history excludes created_by_user", s == 200 and (not hs.get("transfers") or "created_by_user" not in hs["transfers"][0]))

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} check(s) failed)")
        for f in FAILURES:
            print("   - " + f)
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED ✅")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # ensure cleanup even on unexpected error
        print(f"\nUNEXPECTED ERROR: {e}")
        try:
            _r, left, pos, neg = cleanup()
            print(f"[cleanup after error] leftover={left} pos_out={pos} neg_in={neg}")
        except Exception as e2:
            print(f"cleanup failed: {e2}")
        sys.exit(1)
