# ═══════════════════════════════════════════════════════════════════
# 1. WALLET CREATION (Dedicated Virtual Account - Titan)
# ═══════════════════════════════════════════════════════════════════
@router.post("/create")
async def create_wallet(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    row = await database.fetch_one(
        "SELECT paystack_customer_code, virtual_account_number, virtual_bank_name FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if row and row["virtual_account_number"]:
        return {
            "account_number": row["virtual_account_number"], 
            "bank_name": row["virtual_bank_name"],
            "account_name": "Admerce User",
            "note": "Transfer from any Nigerian bank app into your Admerce account.",
            "kyc_status": "verified",
            "message": "Wallet already exists"
        }

    user = await database.fetch_one(
        "SELECT email, first_name, last_name, phone FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if not user or not user["email"]:
        raise HTTPException(status_code=400, detail="User email required for wallet creation")

    customer_code = row["paystack_customer_code"] if row else None

    if not customer_code:
        cust_payload = {
            "email": user["email"],
            "first_name": user["first_name"] or "Admerce",
            "last_name": user["last_name"] or "User",
            "phone": user["phone"] or "",
        }
        try:
            cust_res = await _paystack_post("/customer", cust_payload)
            customer_code = cust_res["data"]["customer_code"]
        except HTTPException as e:
            if e.status_code == 409:
                customer_code = await _get_paystack_customer(user["email"])
                if not customer_code:
                    raise HTTPException(400, "Customer exists but could not be retrieved.")
            else:
                raise e
                
        await database.execute(
            "UPDATE users SET paystack_customer_code = :cc WHERE id = :uid",
            {"cc": customer_code, "uid": user_id},
        )

    # Create Titan Virtual Account
    dva_payload = {
        "customer": customer_code,
        "preferred_bank": "titan-paystack",
    }
    dva_res = await _paystack_post("/dedicated_account", dva_payload)
    dva_data = dva_res["data"]

    await database.execute(
        """
        UPDATE users
           SET virtual_account_number = :van,
               virtual_bank_name = :vbn
         WHERE id = :uid
        """,
        {
            "van": dva_data["account_number"],
            "vbn": dva_data["bank"]["name"],
            "uid": user_id,
        },
    )
    
    await database.execute(
        """
        INSERT INTO user_ledger (user_id, bucket, amount)
        VALUES (:uid,'shopping',0),(:uid,'selling',0),(:uid,'services',0)
        ON CONFLICT DO NOTHING
        """,
        {"uid": user_id},
    )

    return {
        "account_number": dva_data["account_number"],
        "bank_name": dva_data["bank"]["name"],
        "account_name": f"{user['first_name'] or 'Admerce'} {user['last_name'] or 'User'}",
        "note": "Transfer from any Nigerian bank app into your Admerce account.",
        "kyc_status": "verified",
        "message": "Wallet created. Transfer to this account to fund it.",
    }

# ═══════════════════════════════════════════════════════════════════
# 2. READ: FUNDING DETAILS (Matches frontend path /funding/details)
# ═══════════════════════════════════════════════════════════════════
@funding_router.get("/details")
async def get_funding_details(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    row = await database.fetch_one(
        """
        SELECT u.virtual_account_number, u.virtual_bank_name, u.first_name, u.last_name 
        FROM users u WHERE u.id = :uid
        """,
        {"uid": user_id},
    )
    
    if row and row["virtual_account_number"]:
        return {
            "account_number": row["virtual_account_number"],
            "bank_name": row["virtual_bank_name"],
            "account_name": f"{row['first_name'] or 'Admerce'} {row['last_name'] or 'User'}",
            "note": "Transfer from any Nigerian bank app into your Admerce account.",
            "kyc_status": "verified",
            "message": "Existing virtual account"
        }
        
    try:
        create_res = await create_wallet(current_user)
        return create_res
    except Exception as e:
        print(f"⚠️ Failed to auto-create wallet: {e}")
        raise HTTPException(
            status_code=400, 
            detail="Could not generate funding account. Please ensure your profile has an email address."
        )