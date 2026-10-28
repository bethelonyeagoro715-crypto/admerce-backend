"""
Admerce — wallet funding via virtual NUBAN.

This replaces the old Paystack top-up. Users transfer money into their
Anchor-issued virtual NUBAN; Anchor webhooks Admerce when the money
arrives; the wallet webhook handler credits the display ledger.

No merchant account in the middle. No custody. No ₦250M fine.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException

from app.db.database import database
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/funding", tags=["Funding"])


@router.get("/funding/details")
async def funding_details(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]

    row = await database.fetch_one(
        "SELECT paystack_customer_code, virtual_account_number, virtual_bank_name, email "
        "FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # If the account already exists, return it immediately
    if row["virtual_account_number"]:
        return {
            "account_number": row["virtual_account_number"],
            "bank_name": row["virtual_bank_name"] or "Paystack Titan",
            "account_name": f"Admerce / {current_user.get('nickname') or user_id[:8]}",
            "note": "Transfer any amount from your bank app. Funds appear within seconds.",
            "kyc_status": "verified",
        }

    # Create Paystack DVA if missing
    customer_code = row["paystack_customer_code"]
    
    if not customer_code:
        email = row["email"]
        if not email:
            raise HTTPException(status_code=400, detail="Email required for account creation")
            
        # Call Paystack to create customer
        import httpx
        import os
        headers = {
            "Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}",
            "Content-Type": "application/json"
        }
        async with httpx.AsyncClient() as client:
            res = await client.post("https://api.paystack.co/customer", json={"email": email}, headers=headers)
            if res.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to create Paystack customer")
            customer_code = res.json()["data"]["customer_code"]
            await database.execute(
                "UPDATE users SET paystack_customer_code = :cc WHERE id = :uid",
                {"cc": customer_code, "uid": user_id}
            )

    # Call Paystack to create DVA
    import httpx
    import os
    headers = {
        "Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/dedicated_account",
            json={"customer": customer_code, "preferred_bank": "titan-paystack"},
            headers=headers
        )
        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to create virtual account")
            
        data = res.json()["data"]
        account_number = data["account_number"]
        bank_name = data["bank"]["name"]
        
        await database.execute(
            "UPDATE users SET virtual_account_number = :van, virtual_bank_name = :vbn WHERE id = :uid",
            {"van": account_number, "vbn": bank_name, "uid": user_id}
        )
        
        return {
            "account_number": account_number,
            "bank_name": bank_name,
            "account_name": f"Admerce / {current_user.get('nickname') or user_id[:8]}",
            "note": "Transfer any amount from your bank app. Funds appear within seconds.",
            "kyc_status": "verified",
        }


@router.get("/history")
async def funding_history(
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
):
    """
    Recent bank-transfer credits into this user's wallet.
    Reads wallet_transactions filtered by source — the webhook writes
    a row every time Anchor fires payment.received.
    """
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT id, amount, description, reference, status, created_at
          FROM wallet_transactions
         WHERE user_id = :uid
           AND type = 'credit'
           AND description ILIKE 'Bank transfer%'
         ORDER BY created_at DESC
         LIMIT :lim
        """,
        {"uid": user_id, "lim": max(1, min(limit, 100))},
    )
    return [dict(r) for r in rows]