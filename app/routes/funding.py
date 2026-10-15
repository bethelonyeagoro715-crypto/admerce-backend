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


@router.get("/details")
async def funding_details(current_user: dict = Depends(get_current_user)):
    """
    Return the caller's virtual NUBAN so the UI can display it and the
    user can transfer from any Nigerian bank app.
    """
    user_id = current_user["id"]

    row = await database.fetch_one(
        """
        SELECT anchor_virtual_nuban,
               anchor_sub_account_id,
               anchor_kyc_status
          FROM users
         WHERE id = :uid
        """,
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if not row["anchor_virtual_nuban"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Your Admerce wallet is being set up. "
                "Please try again in a few minutes, or complete KYC if prompted."
            ),
        )

    return {
        "account_number": row["anchor_virtual_nuban"],
        "bank_name": "Admerce Escrow",  # Anchor will tell you the exact partner bank
        "account_name": "Admerce / " + (current_user.get("nickname") or user_id[:8]),
        "note": "Transfer any amount from your bank app. Funds appear within seconds.",
        "kyc_status": row["anchor_kyc_status"] or "pending",
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