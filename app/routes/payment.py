import os
import httpx
from datetime import datetime
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
        print("❌ PAYSTACK_SECRET_KEY not set in environment", flush=True)
        raise HTTPException(status_code=500, detail="Paystack secret key not configured")

    user_id = current_user["id"]
    print(f"💳 verify called: user={user_id} reference={req.reference}", flush=True)

    # ── 1. Ask Paystack to verify the transaction ────────────────────
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.paystack.co/transaction/verify/{req.reference}",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            )
    except httpx.HTTPError as e:
        print(f"❌ Paystack request failed: {e!r}", flush=True)
        raise HTTPException(
            status_code=502,
            detail="Could not reach Paystack. Please try again.",
        )

    print(
        f"🔎 Paystack status={resp.status_code} body={resp.text[:400]}",
        flush=True,
    )

    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Paystack returned non-JSON: {e!r}", flush=True)
        raise HTTPException(status_code=502, detail="Invalid response from Paystack")

    # ── 2. Interpret Paystack's response ─────────────────────────────
    if not data.get("status"):
        # Top-level `status: false` — bad reference, expired, etc.
        raise HTTPException(
            status_code=400,
            detail=data.get("message") or "Payment verification failed",
        )

    transaction = data.get("data") or {}
    if transaction.get("status") != "success":
        raise HTTPException(
            status_code=400,
            detail=f"Transaction not successful (status={transaction.get('status')})",
        )

    # Amount is in kobo — must be present and numeric
    try:
        amount_kobo = int(transaction["amount"])
    except (KeyError, TypeError, ValueError) as e:
        print(f"❌ Paystack response missing amount: {transaction!r}", flush=True)
        raise HTTPException(status_code=502, detail="Malformed Paystack response")

    amount = amount_kobo / 100  # kobo → Naira
    reference = transaction.get("reference") or req.reference

    # ── 3. Idempotency — has this reference already been credited? ───
    existing = await database.fetch_one(
        "SELECT id FROM wallet_transactions WHERE reference = :ref",
        {"ref": reference},
    )
    if existing:
        print(f"ℹ️  reference {reference} already credited", flush=True)
        return {"message": "Already credited", "amount": amount}

    # ── 4. Ensure wallet row exists ──────────────────────────────────
    wallet = await database.fetch_one(
        "SELECT user_id FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not wallet:
        await database.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0)",
            {"uid": user_id},
        )

    # ── 5. Credit wallet ─────────────────────────────────────────────
    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": amount, "uid": user_id},
    )

    # ── 6. Record transaction ────────────────────────────────────────
    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, reference, status, created_at)
        VALUES
            (:uid, :amt, 'topup', :ref, 'success', :now)
        """,
        {
            "uid": user_id,
            "amt": amount,
            "ref": reference,
            "now": datetime.utcnow(),
        },
    )

    print(f"✅ credited ₦{amount} to {user_id} (ref {reference})", flush=True)
    return {"message": "Wallet credited", "amount": amount}