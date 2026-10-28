"""
Admerce — wallet funding via Paystack Dedicated Virtual Account (DVA).
"""
import os
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException
from app.db.database import database
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/funding", tags=["Funding"])

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

async def _paystack_post(endpoint: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(f"https://api.paystack.co{endpoint}", json=payload, headers=headers)
        res.raise_for_status()
        return res.json()


@router.get("/details")
async def funding_details(current_user: dict = Depends(get_current_user)):
    """
    Return the caller's Paystack virtual NUBAN so the UI can display it.
    If it doesn't exist, provision it via Paystack.
    """
    user_id = current_user["id"]

    row = await database.fetch_one(
        """
        SELECT paystack_customer_code, virtual_account_number, virtual_bank_name, email
          FROM users
         WHERE id = :uid
        """,
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

    # If account doesn't exist, we need to create it
    customer_code = row["paystack_customer_code"]

    # 1. Create Paystack Customer if we don't have one
    if not customer_code:
        email = row["email"]
        if not email:
            raise HTTPException(status_code=400, detail="Email required for account creation")

        try:
            cust_res = await _paystack_post("/customer", {"email": email})
            customer_code = cust_res["data"]["customer_code"]
            await database.execute(
                "UPDATE users SET paystack_customer_code = :cc WHERE id = :uid",
                {"cc": customer_code, "uid": user_id},
            )
        except Exception as e:
            logger.error(f"Paystack customer creation failed: {e}")
            raise HTTPException(status_code=502, detail="Failed to create Paystack customer profile.")

    # 2. Create the Dedicated Virtual Account (DVA)
    try:
        dva_payload = {
            "customer": customer_code,
            "preferred_bank": "titan-paystack",
        }
        dva_res = await _paystack_post("/dedicated_account", dva_payload)
        
        account_number = dva_res["data"]["account_number"]
        bank_name = dva_res["data"]["bank"]["name"]

        await database.execute(
            """
            UPDATE users
               SET virtual_account_number = :van,
                   virtual_bank_name = :vbn
             WHERE id = :uid
            """,
            {"van": account_number, "vbn": bank_name, "uid": user_id},
        )

        return {
            "account_number": account_number,
            "bank_name": bank_name,
            "account_name": f"Admerce / {current_user.get('nickname') or user_id[:8]}",
            "note": "Transfer any amount from your bank app. Funds appear within seconds.",
            "kyc_status": "verified",
        }
        
    except Exception as e:
        logger.error(f"Paystack DVA creation failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to provision virtual account. Please try again.")