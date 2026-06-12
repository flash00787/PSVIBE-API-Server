# ═══════════════════════════════════════════════════
#  LOYALTY SYSTEM — Customer Loyalty & Rewards
# ═══════════════════════════════════════════════════

# ── Loyalty Settings ──

@router.get("/loyalty/settings")
async def get_loyalty_settings(user: dict = Depends(get_current_user)):
    """Get loyalty points settings."""
    try:
        row = _mysql_query_one("SELECT * FROM loyalty_settings WHERE id = 1")
        if not row:
            return {"success": True, "data": {
                "points_per_ks": 1.0, "points_per_min": 0.5,
                "min_redeem_points": 100, "points_per_ks_redeem": 1.0,
                "signup_bonus_points": 50, "birthday_bonus_points": 100,
                "referral_points": 25
            }}
        return {"success": True, "data": {
            "id": row["id"],
            "points_per_ks": float(row["points_per_ks"]),
            "points_per_min": float(row["points_per_min"]),
            "min_redeem_points": row["min_redeem_points"],
            "points_per_ks_redeem": float(row["points_per_ks_redeem"]),
            "signup_bonus_points": row["signup_bonus_points"],
            "birthday_bonus_points": row["birthday_bonus_points"],
            "referral_points": row["referral_points"],
            "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
        }}
    except Exception as e:
        logger.error(f"GET /loyalty/settings error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/loyalty/settings")
async def update_loyalty_settings(req: dict, user: dict = Depends(get_current_user)):
    """Update loyalty points settings."""
    try:
        _mysql_execute("""
            INSERT INTO loyalty_settings (id, points_per_ks, points_per_min, min_redeem_points,
                points_per_ks_redeem, signup_bonus_points, birthday_bonus_points, referral_points)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                points_per_ks = VALUES(points_per_ks),
                points_per_min = VALUES(points_per_min),
                min_redeem_points = VALUES(min_redeem_points),
                points_per_ks_redeem = VALUES(points_per_ks_redeem),
                signup_bonus_points = VALUES(signup_bonus_points),
                birthday_bonus_points = VALUES(birthday_bonus_points),
                referral_points = VALUES(referral_points)
        """, (
            req.get("points_per_ks", 1.0),
            req.get("points_per_min", 0.5),
            req.get("min_redeem_points", 100),
            req.get("points_per_ks_redeem", 1.0),
            req.get("signup_bonus_points", 50),
            req.get("birthday_bonus_points", 100),
            req.get("referral_points", 25),
        ))
        return {"success": True, "message": "Loyalty settings updated"}
    except Exception as e:
        logger.error(f"PUT /loyalty/settings error: {e}")
        return {"success": False, "error": str(e)}


# ── Member Loyalty ──

@router.get("/loyalty/members")
async def list_loyalty_members(
    search: str = Query(None),
    tier: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """List all member loyalty with optional search/tier filters."""
    try:
        where = ["1=1"]
        params = []
        if search:
            where.append("(ml.member_id LIKE %s OR m.member_name LIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        if tier:
            where.append("ml.tier = %s")
            params.append(tier)

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            f"SELECT COUNT(*) as cnt FROM member_loyalty ml LEFT JOIN members m ON ml.member_id = m.member_id WHERE {where_clause}",
            tuple(params)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            f"""SELECT ml.*, COALESCE(m.member_name, ml.member_id) as member_name
                FROM member_loyalty ml
                LEFT JOIN members m ON ml.member_id = m.member_id
                WHERE {where_clause}
                ORDER BY ml.total_points DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [page_size, offset])
        )

        members = []
        for r in rows:
            members.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "member_name": r.get("member_name", r["member_id"]),
                "total_points": r.get("total_points", 0),
                "redeemed_points": r.get("redeemed_points", 0),
                "available_points": r.get("available_points", 0),
                "lifetime_spent": float(r.get("lifetime_spent") or 0),
                "tier": r.get("tier", "Bronze"),
                "join_date": str(r["join_date"]) if r.get("join_date") else None,
                "birthday": str(r["birthday"]) if r.get("birthday") else None,
                "last_earn_date": str(r["last_earn_date"]) if r.get("last_earn_date") else None,
                "last_redeem_date": str(r["last_redeem_date"]) if r.get("last_redeem_date") else None,
            })

        return {"success": True, "data": members, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/members error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/loyalty/members/{member_id}")
async def get_loyalty_member(member_id: str, user: dict = Depends(get_current_user)):
    """Get one member's loyalty info."""
    try:
        row = _mysql_query_one(
            """SELECT ml.*, COALESCE(m.member_name, ml.member_id) as member_name
               FROM member_loyalty ml
               LEFT JOIN members m ON ml.member_id = m.member_id
               WHERE ml.member_id = %s""",
            (member_id,)
        )
        if not row:
            return {"success": False, "error": "Member not found in loyalty system"}

        return {"success": True, "data": {
            "id": row["id"],
            "member_id": row["member_id"],
            "member_name": row.get("member_name", row["member_id"]),
            "total_points": row.get("total_points", 0),
            "redeemed_points": row.get("redeemed_points", 0),
            "available_points": row.get("available_points", 0),
            "lifetime_spent": float(row.get("lifetime_spent") or 0),
            "tier": row.get("tier", "Bronze"),
            "join_date": str(row["join_date"]) if row.get("join_date") else None,
            "birthday": str(row["birthday"]) if row.get("birthday") else None,
            "last_earn_date": str(row["last_earn_date"]) if row.get("last_earn_date") else None,
            "last_redeem_date": str(row["last_redeem_date"]) if row.get("last_redeem_date") else None,
        }}
    except Exception as e:
        logger.error(f"GET /loyalty/members/{member_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/loyalty/members/adjust")
async def adjust_loyalty_points(req: dict, user: dict = Depends(get_current_user)):
    """Manual points adjustment (admin)."""
    try:
        member_id = req.get("member_id", "")
        points = req.get("points", 0)
        description = req.get("description", "Manual adjustment")
        staff_name = req.get("staff_name", user.get("username", "admin"))

        if not member_id:
            return {"success": False, "error": "member_id is required"}
        if points == 0:
            return {"success": False, "error": "points must be non-zero"}

        # Ensure member exists in loyalty
        existing = _mysql_query_one(
            "SELECT id, total_points FROM member_loyalty WHERE member_id = %s",
            (member_id,)
        )
        if not existing:
            return {"success": False, "error": "Member not found in loyalty system"}

        # Adjust points
        if points > 0:
            _mysql_execute(
                "UPDATE member_loyalty SET total_points = total_points + %s, last_earn_date = CURDATE() WHERE member_id = %s",
                (points, member_id)
            )
        else:
            # Deduction: ensure enough available
            current = _mysql_query_one("SELECT available_points FROM member_loyalty WHERE member_id = %s", (member_id,))
            if current and current["available_points"] < abs(points):
                return {"success": False, "error": f"Insufficient points. Available: {current['available_points']}"}
            _mysql_execute(
                "UPDATE member_loyalty SET total_points = total_points + %s WHERE member_id = %s",
                (points, member_id)
            )

        # Log transaction
        _mysql_execute(
            """INSERT INTO loyalty_transactions (member_id, points, transaction_type, description, created_by)
               VALUES (%s, %s, 'adjustment', %s, %s)""",
            (member_id, points, description, staff_name)
        )

        # Recalculate tier
        _recalc_tier(member_id)

        return {"success": True, "message": f"Points adjusted by {points}"}
    except Exception as e:
        logger.error(f"POST /loyalty/members/adjust error: {e}")
        return {"success": False, "error": str(e)}


# ── Points Transactions ──

@router.get("/loyalty/transactions")
async def list_loyalty_transactions(
    member_id: str = Query(None),
    transaction_type: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Transaction log with filters."""
    try:
        where = ["1=1"]
        params = []
        if member_id:
            where.append("lt.member_id = %s")
            params.append(member_id)
        if transaction_type:
            where.append("lt.transaction_type = %s")
            params.append(transaction_type)
        if date_from:
            where.append("DATE(lt.created_at) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(lt.created_at) <= %s")
            params.append(date_to)

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            f"SELECT COUNT(*) as cnt FROM loyalty_transactions lt WHERE {where_clause}",
            tuple(params)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            f"""SELECT lt.*, COALESCE(m.member_name, lt.member_id) as member_name
                FROM loyalty_transactions lt
                LEFT JOIN members m ON lt.member_id = m.member_id
                WHERE {where_clause}
                ORDER BY lt.created_at DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [page_size, offset])
        )

        transactions = []
        for r in rows:
            transactions.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "member_name": r.get("member_name", r["member_id"]),
                "points": r["points"],
                "transaction_type": r["transaction_type"],
                "reference_id": r.get("reference_id"),
                "description": r.get("description"),
                "branch_id": r.get("branch_id"),
                "created_by": r.get("created_by"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })

        return {"success": True, "data": transactions, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/transactions error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/loyalty/transactions/{member_id}")
async def get_member_transactions(
    member_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Get all transactions for one member."""
    try:
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            "SELECT COUNT(*) as cnt FROM loyalty_transactions WHERE member_id = %s",
            (member_id,)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            """SELECT * FROM loyalty_transactions
               WHERE member_id = %s
               ORDER BY created_at DESC
               LIMIT %s OFFSET %s""",
            (member_id, page_size, offset)
        )

        transactions = []
        for r in rows:
            transactions.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "points": r["points"],
                "transaction_type": r["transaction_type"],
                "reference_id": r.get("reference_id"),
                "description": r.get("description"),
                "branch_id": r.get("branch_id"),
                "created_by": r.get("created_by"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })

        return {"success": True, "data": transactions, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/transactions/{member_id} error: {e}")
        return {"success": False, "error": str(e)}


# ── Rewards Catalog ──

@router.get("/loyalty/rewards")
async def list_loyalty_rewards(
    is_active: bool = Query(None),
    user: dict = Depends(get_current_user),
):
    """List available rewards."""
    try:
        where = "1=1"
        params = []
        if is_active is not None:
            where = "is_active = %s"
            params = [1 if is_active else 0]

        rows = _mysql_query(
            f"SELECT * FROM loyalty_rewards WHERE {where} ORDER BY points_required ASC",
            tuple(params)
        )

        rewards = []
        for r in rows:
            rewards.append({
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description"),
                "points_required": r["points_required"],
                "reward_type": r["reward_type"],
                "reward_value": float(r["reward_value"]) if r.get("reward_value") else None,
                "stock": r.get("stock", -1),
                "is_active": bool(r.get("is_active", 1)),
                "image_url": r.get("image_url"),
            })

        return {"success": True, "data": rewards}
    except Exception as e:
        logger.error(f"GET /loyalty/rewards error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/loyalty/rewards")
async def create_loyalty_reward(req: dict, user: dict = Depends(get_current_user)):
    """Add a new reward to the catalog."""
    try:
        name = req.get("name", "")
        if not name:
            return {"success": False, "error": "name is required"}

        reward_id = _mysql_execute(
            """INSERT INTO loyalty_rewards (name, description, points_required, reward_type, reward_value, stock, is_active, image_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                name,
                req.get("description", ""),
                req.get("points_required", 0),
                req.get("reward_type", "discount_ks"),
                req.get("reward_value"),
                req.get("stock", -1),
                req.get("is_active", 1),
                req.get("image_url"),
            )
        )
        return {"success": True, "data": {"id": reward_id}, "message": "Reward created"}
    except Exception as e:
        logger.error(f"POST /loyalty/rewards error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/loyalty/rewards/{reward_id}")
async def update_loyalty_reward(reward_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update a reward."""
    try:
        existing = _mysql_query_one("SELECT id FROM loyalty_rewards WHERE id = %s", (reward_id,))
        if not existing:
            return {"success": False, "error": "Reward not found"}

        _mysql_execute(
            """UPDATE loyalty_rewards
               SET name = %s, description = %s, points_required = %s, reward_type = %s,
                   reward_value = %s, stock = %s, is_active = %s, image_url = %s
               WHERE id = %s""",
            (
                req.get("name", existing.get("name")),
                req.get("description", existing.get("description")),
                req.get("points_required", existing.get("points_required")),
                req.get("reward_type", existing.get("reward_type")),
                req.get("reward_value", existing.get("reward_value")),
                req.get("stock", existing.get("stock", -1)),
                req.get("is_active", existing.get("is_active", 1)),
                req.get("image_url", existing.get("image_url")),
                reward_id,
            )
        )
        return {"success": True, "message": "Reward updated"}
    except Exception as e:
        logger.error(f"PUT /loyalty/rewards/{reward_id} error: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/loyalty/rewards/{reward_id}")
async def deactivate_loyalty_reward(reward_id: int, user: dict = Depends(get_current_user)):
    """Deactivate a reward (soft delete)."""
    try:
        _mysql_execute("UPDATE loyalty_rewards SET is_active = 0 WHERE id = %s", (reward_id,))
        return {"success": True, "message": "Reward deactivated"}
    except Exception as e:
        logger.error(f"DELETE /loyalty/rewards/{reward_id} error: {e}")
        return {"success": False, "error": str(e)}


# ── Redemptions ──

@router.get("/loyalty/redemptions")
async def list_loyalty_redemptions(
    member_id: str = Query(None),
    status: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """List redemptions with filters."""
    try:
        where = ["1=1"]
        params = []
        if member_id:
            where.append("lr.member_id = %s")
            params.append(member_id)
        if status:
            where.append("lr.status = %s")
            params.append(status)
        if date_from:
            where.append("DATE(lr.redeemed_at) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(lr.redeemed_at) <= %s")
            params.append(date_to)

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        total = _mysql_query_one(
            f"SELECT COUNT(*) as cnt FROM loyalty_redemptions lr WHERE {where_clause}",
            tuple(params)
        )
        total = total["cnt"] if total else 0

        rows = _mysql_query(
            f"""SELECT lr.*, COALESCE(m.member_name, lr.member_id) as member_name
                FROM loyalty_redemptions lr
                LEFT JOIN members m ON lr.member_id = m.member_id
                WHERE {where_clause}
                ORDER BY lr.redeemed_at DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [page_size, offset])
        )

        redemptions = []
        for r in rows:
            redemptions.append({
                "id": r["id"],
                "member_id": r["member_id"],
                "member_name": r.get("member_name", r["member_id"]),
                "reward_id": r["reward_id"],
                "reward_name": r.get("reward_name"),
                "points_used": r["points_used"],
                "status": r.get("status", "pending"),
                "redeemed_at": str(r["redeemed_at"]) if r.get("redeemed_at") else None,
                "redeemed_by": r.get("redeemed_by"),
                "notes": r.get("notes"),
            })

        return {"success": True, "data": redemptions, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"GET /loyalty/redemptions error: {e}")
        return {"success": False, "error": str(e)}


@router.post("/loyalty/redemptions")
async def redeem_loyalty_reward(req: dict, user: dict = Depends(get_current_user)):
    """Staff redeems reward for a member."""
    try:
        member_id = req.get("member_id", "")
        reward_id = req.get("reward_id", 0)
        staff_name = req.get("staff_name", user.get("username", "admin"))
        notes = req.get("notes", "")

        if not member_id:
            return {"success": False, "error": "member_id is required"}
        if not reward_id:
            return {"success": False, "error": "reward_id is required"}

        # Get reward
        reward = _mysql_query_one(
            "SELECT * FROM loyalty_rewards WHERE id = %s AND is_active = 1",
            (reward_id,)
        )
        if not reward:
            return {"success": False, "error": "Reward not found or inactive"}

        # Check stock
        if reward["stock"] == 0:
            return {"success": False, "error": "Reward out of stock"}

        # Get member loyalty
        loyalty = _mysql_query_one(
            "SELECT * FROM member_loyalty WHERE member_id = %s",
            (member_id,)
        )
        if not loyalty:
            return {"success": False, "error": "Member not found in loyalty system"}

        points_needed = reward["points_required"]
        if loyalty["available_points"] < points_needed:
            return {"success": False, "error": f"Insufficient points. Need {points_needed}, have {loyalty['available_points']}"}

        # Deduct points (increase redeemed, not decrease total_points — available is computed)
        _mysql_execute(
            "UPDATE member_loyalty SET redeemed_points = redeemed_points + %s, last_redeem_date = CURDATE() WHERE member_id = %s",
            (points_needed, member_id)
        )

        # Log transaction
        _mysql_execute(
            """INSERT INTO loyalty_transactions (member_id, points, transaction_type, reference_id, description, created_by)
               VALUES (%s, %s, 'redeem', %s, %s, %s)""",
            (member_id, -points_needed, str(reward_id), f"Redeemed: {reward['name']}", staff_name)
        )

        # Reduce stock if limited
        if reward["stock"] > 0:
            _mysql_execute("UPDATE loyalty_rewards SET stock = stock - 1 WHERE id = %s", (reward_id,))

        # Create redemption record
        redemption_id = _mysql_execute(
            """INSERT INTO loyalty_redemptions (member_id, reward_id, reward_name, points_used, status, redeemed_by, notes)
               VALUES (%s, %s, %s, %s, 'redeemed', %s, %s)""",
            (member_id, reward_id, reward["name"], points_needed, staff_name, notes)
        )

        return {"success": True, "data": {"id": redemption_id}, "message": f"Redeemed '{reward['name']}' for {points_needed} points"}
    except Exception as e:
        logger.error(f"POST /loyalty/redemptions error: {e}")
        return {"success": False, "error": str(e)}


@router.put("/loyalty/redemptions/{redemption_id}")
async def update_redemption_status(redemption_id: int, req: dict, user: dict = Depends(get_current_user)):
    """Update redemption status (confirm or cancel)."""
    try:
        status = req.get("status", "")
        if status not in ("redeemed", "cancelled"):
            return {"success": False, "error": "status must be 'redeemed' or 'cancelled'"}

        existing = _mysql_query_one(
            "SELECT * FROM loyalty_redemptions WHERE id = %s",
            (redemption_id,)
        )
        if not existing:
            return {"success": False, "error": "Redemption not found"}

        old_status = existing["status"]
        if old_status == status:
            return {"success": True, "message": f"Already {status}"}

        # If cancelling a redeemed reward, refund points
        if status == "cancelled" and old_status == "redeemed":
            _mysql_execute(
                "UPDATE member_loyalty SET redeemed_points = redeemed_points - %s WHERE member_id = %s",
                (existing["points_used"], existing["member_id"])
            )
            # Log refund transaction
            _mysql_execute(
                """INSERT INTO loyalty_transactions (member_id, points, transaction_type, description, created_by)
                   VALUES (%s, %s, 'adjustment', %s, %s)""",
                (existing["member_id"], existing["points_used"], f"Refund from cancelled redemption #{redemption_id}", user.get("username", "admin"))
            )
            # Restore stock
            _mysql_execute("UPDATE loyalty_rewards SET stock = stock + 1 WHERE id = %s AND stock >= 0", (existing["reward_id"],))

        # If confirming a pending redemption
        if status == "redeemed" and old_status == "cancelled":
            # Re-deduct points
            loyalty = _mysql_query_one(
                "SELECT available_points FROM member_loyalty WHERE member_id = %s",
                (existing["member_id"],)
            )
            if not loyalty or loyalty["available_points"] < existing["points_used"]:
                return {"success": False, "error": "Member doesn't have enough points anymore"}
            _mysql_execute(
                "UPDATE member_loyalty SET redeemed_points = redeemed_points + %s WHERE member_id = %s",
                (existing["points_used"], existing["member_id"])
            )
            _mysql_execute("UPDATE loyalty_rewards SET stock = stock - 1 WHERE id = %s AND stock > 0", (existing["reward_id"],))

        _mysql_execute(
            "UPDATE loyalty_redemptions SET status = %s, notes = CONCAT(COALESCE(notes,''), %s) WHERE id = %s",
            (status, f" | Status changed to {status} by {user.get('username', 'admin')}", redemption_id)
        )

        return {"success": True, "message": f"Redemption status updated to {status}"}
    except Exception as e:
        logger.error(f"PUT /loyalty/redemptions/{redemption_id} error: {e}")
        return {"success": False, "error": str(e)}


# ── Tier Calculation Helper ──

def _recalc_tier(member_id: str):
    """Recalculate member tier based on lifetime_spent."""
    tiers = [
        ("Platinum", 500000),
        ("Gold", 300000),
        ("Silver", 100000),
        ("Bronze", 0),
    ]
    row = _mysql_query_one(
        "SELECT lifetime_spent FROM member_loyalty WHERE member_id = %s",
        (member_id,)
    )
    if not row:
        return
    spent = float(row.get("lifetime_spent") or 0)
    for tier_name, threshold in tiers:
        if spent >= threshold:
            _mysql_execute(
                "UPDATE member_loyalty SET tier = %s WHERE member_id = %s AND tier != %s",
                (tier_name, member_id, tier_name)
            )
            break
