"""PS VIBE FIFO Wallet Calculator"""
def fifo_calc(topups, bal):
    if not topups:
        return {"liability": 0, "consumed": 0}
    total = float(sum(t["mins_added"] for t in topups))
    bal = float(bal)
    consumed = max(0.0, min(total - bal, total))
    if consumed <= 0:
        liab = float(sum(float(t["amount"] or 0) for t in topups if float(t["mins_added"]) > 0))
        return {"liability": round(liab, 0), "consumed": 0}
    liab = cval = 0.0
    rem = consumed
    for t in topups:
        m = float(t["mins_added"])
        a = float(t["amount"] or 0)
        rate = a / m if m > 0 and a > 0 else 0.0
        if rem <= 0:
            liab += m * rate
        elif m <= rem:
            cval += m * rate
            rem -= m
        else:
            cval += rem * rate
            liab += (m - rem) * rate
            rem = 0
    return {"liability": round(liab, 0), "consumed": round(cval, 0)}

def get_all_fifo(conn, year_month=None):
    """
    Calculate total wallet liability and consumption using FIFO.
    
    Args:
        conn: MySQL connection
        year_month: If provided (e.g. "2026-06"), count only topups up to that month.
                    consumed = sum of topup values up to month - remaining balance value.
                    If None, calculates lifetime values.
    """
    cur = conn.cursor()
    cur.execute("SELECT member_id, balance_mins FROM member_wallets")
    total_liab = total_cons = 0.0
    all_members = cur.fetchall()
    cur.close()
    
    for (mid, bal) in all_members:
        bal = float(bal or 0)
        cur2 = conn.cursor()
        if year_month:
            # Filter topups up to and including the specified month
            cur2.execute(
                "SELECT amount, mins_added FROM topup_log "
                "WHERE member_id=%s AND DATE_FORMAT(topup_date, '%%Y-%%m') <= %s "
                "ORDER BY topup_date",
                (mid, year_month)
            )
        else:
            cur2.execute(
                "SELECT amount, mins_added FROM topup_log "
                "WHERE member_id=%s ORDER BY topup_date",
                (mid,)
            )
        topups = [{"amount": float(r[0] or 0), "mins_added": float(r[1] or 0)} for r in cur2.fetchall()]
        cur2.close()
        
        # Skip members with no topups (no wallet activity)
        if not topups:
            continue
        
        r = fifo_calc(topups, bal)
        total_liab += r["liability"]
        total_cons += r["consumed"]
    
    return {"liability": round(total_liab, 0), "consumed": round(total_cons, 0)}
