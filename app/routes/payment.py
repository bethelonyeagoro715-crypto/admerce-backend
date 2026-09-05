import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.db.database import database
from app.routes.auth import get_current_user

router = APIRouter(prefix="/payments", tags=["Payments"])

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

class VerifyPaymentRequest(BaseModel):
    reference: str

@router.post("/verify")
async def verify_payment(
    req: VerifyPaymentRequest,
    current_user: dict = Depends(get_current_user)
):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Paystack secret key not configured")

    user_id = current_user["id"]

    # 1. Verify with Paystack
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.paystack.co/transaction/verify/{req.reference}",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        )
        data = resp.json()

    if not data.get("status"):
        raise HTTPException(status_code=400, detail="Payment verification failed")

    transaction = data["data"]
    if transaction["status"] != "success":
        raise HTTPException(status_code=400, detail="Transaction not successful")

    amount = transaction["amount"] / 100  # Convert kobo to Naira
    reference = transaction["reference"]

    # 2. Check if already credited (idempotency)
    existing = await database.fetch_one(
        "SELECT id FROM wallet_transactions WHERE reference = :ref",
        {"ref": reference}
    )
    if existing:
        return {"message": "Already credited"}

    # 3. Ensure wallet exists
    wallet = await database.fetch_one(
        "SELECT user_id FROM wallets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not wallet:
        await database.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
            {"uid": user_id}
        )

    # 4. Credit wallet
    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": amount, "uid": user_id}
    )

    # 5. Record transaction
    await database.execute(
        """
        INSERT INTO wallet_transactions (user_id, amount, type, reference, status, created_at)
        VALUES (:uid, :amt, 'topup', :ref, 'success', NOW())
        """,
        {"uid": user_id, "amt": amount, "ref": reference}
    )

    return {"message": "Wallet credited", "amount": amount}