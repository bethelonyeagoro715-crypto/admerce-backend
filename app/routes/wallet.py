# ═══════════════════════════════════════════════════════════════════
# 8. READ: ESCROWS (For WalletShell Pending Items)
# ═══════════════════════════════════════════════════════════════════
@router.get("/escrows")
async def get_my_escrows(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT e.*, l.title AS listing_title, s.name AS store_name
        FROM escrow e
        LEFT JOIN listings l ON e.listing_id = l.listing_id
        LEFT JOIN stores s ON l.store_id = s.store_id
        WHERE e.shopper_id = :uid AND e.status IN ('locked', 'accepted', 'dispatched')
        ORDER BY e.created_at DESC
        """,
        {"uid": user_id}
    )
    return [dict(row) for row in rows]


# ═══════════════════════════════════════════════════════════════════
# 9. READ: FUNDING DETAILS (For AddMoneySheet)
# ═══════════════════════════════════════════════════════════════════
@router.get("/funding-details")
async def get_funding_details(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    # 1. Check if they already have a virtual account
    row = await database.fetch_one(
        "SELECT virtual_account_number, virtual_bank_name FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    
    if row and row["virtual_account_number"]:
        return {
            "virtual_account_number": row["virtual_account_number"],
            "bank_name": row["virtual_bank_name"],
            "message": "Existing virtual account"
        }
        
    # 2. If not, automatically create one for them
    try:
        # We call the create_wallet function we wrote earlier
        create_res = await create_wallet(current_user)
        return create_res
    except Exception as e:
        print(f"⚠️ Failed to auto-create wallet: {e}")
        raise HTTPException(
            status_code=400, 
            detail="Could not generate funding account. Please ensure your profile has an email address."
        )