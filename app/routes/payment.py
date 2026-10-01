import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.database import database
from app.routes.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_VERIFY_URL = "https://api.paystack.co/transaction/verify"


class VerifyPaymentRequest(BaseModel):
    reference: str = Field(..., min_length=3, max_length=128)


def _extract_email(transaction: dict) -> Optional[str]:
    customer = transaction.get("customer") or {}
    email = customer.get("email")
    if isinstance(email, str):
        email = email.strip().lower()
        if email:
            return email
    return None


@router.post("/verify")
async def verify_payment(
    req: VerifyPaymentRequest,
    current_user: dict = Depends(get_current_user),
):
    if not PAYSTACK_SECRET_KEY:
        logger.error("PAYSTACK_SECRET_KEY not configured")
        raise HTTPException(status_code=500, detail="Payments unavailable")

    user_id = current_user["id"]
    logger.info("verify_payment user=%s reference=%s", user_id, req.reference)

    # ── 1. Ask Paystack to verify ────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{PAYSTACK_VERIFY_URL}/{req.reference}",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            )
    except httpx.HTTPError as e:
        logger.warning("Paystack request failed: %r", e)
        raise HTTPException(
            status_code=502,
            detail="Could not reach payment provider. Please try again.",
        )

    logger.info("Paystack verify status=%s reference=%s", resp.status_code, req.reference)

    try:
        data = resp.json()
    except Exception:
        logger.warning("Paystack returned non-JSON reference=%s", req.reference)
        raise HTTPException(status_code=502, detail="Invalid response from payment provider")

    if not data.get("status"):
        raise HTTPException(
            status_code=400,
            detail=data.get("message") or "Payment verification failed",
        )

    transaction = data.get("data") or {}
    if transaction.get("status") != "success":
        raise HTTPException(status_code=400, detail="Transaction not successful")

    # ── 2. Ownership check ───────────────────────────────────────────
    # The Paystack transaction must belong to the calling user. Without
    # this, any authenticated user who obtains a valid reference can
    # credit their own wallet with someone else's top-up.
    paystack_email = _extract_email(transaction)
    if not paystack_email:
        logger.warning("Paystack response missing customer email ref=%s", req.reference)
        raise HTTPException(status_code=502, detail="Malformed payment response")

    user_row = await database.fetch_one(
        "SELECT email FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if not user_row:
        raise HTTPException(status_code=401, detail="User not found")

    user_email = (user_row["email"] or "").strip().lower()
    if not user_email or user_email != paystack_email:
        logger.warning(
            "Ownership mismatch user_id=%s user_email=%s paystack_email=%s ref=%s",
            user_id, user_email, paystack_email, req.reference,
        )
        raise HTTPException(
            status_code=403,
            detail="This payment does not belong to your account",
        )

    # ── 3. Amount parsing ────────────────────────────────────────────
    try:
        amount_kobo = int(transaction["amount"])
    except (KeyError, TypeError, ValueError):
        logger.error("Paystack response missing amount ref=%s", req.reference)
        raise HTTPException(status_code=502, detail="Malformed payment response")

    if amount_kobo <= 0:
        logger.warning("Non-positive amount ref=%s amount_kobo=%s", req.reference, amount_kobo)
        raise HTTPException(status_code=400, detail="Invalid transaction amount")

    amount = amount_kobo / 100  # kobo → Naira
    reference = transaction.get("reference") or req.reference

    # ── 4. Atomic idempotency + credit ───────────────────────────────
    # Advisory lock keyed on the reference serializes concurrent verifies
    # for the same reference. Combined with the transaction boundary, this
    # prevents the check-then-insert race that double-credits.
    async with database.transaction():
        await database.fetch_val(
            "SELECT pg_advisory_xact_lock(hashtext(:ref))",
            {"ref": reference},
        )

        existing = await database.fetch_one(
            "SELECT id FROM wallet_transactions WHERE reference = :ref",
            {"ref": reference},
        )
        if existing:
            logger.info("Reference already credited ref=%s", reference)
            return {"message": "Already credited", "amount": amount}

        # Ensure wallet row exists. ON CONFLICT requires a UNIQUE on
        # wallets.user_id — if that constraint is missing, this INSERT
        # can still race across different references for the same user.
        await database.execute(
            """
            INSERT INTO wallets (user_id, balance)
            VALUES (:uid, 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            {"uid": user_id},
        )

        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": amount, "uid": user_id},
        )

        await database.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, amount, type, description, reference, status, created_at)
            VALUES
                (:uid, :amt, 'credit', :desc, :ref, 'completed', :now)
            """,
            {
                "uid": user_id,
                "amt": amount,
                "desc": "Wallet top-up via Paystack",
                "ref": reference,
                "now": datetime.now(timezone.utc),
            },
        )

    logger.info("Credited %.2f to user=%s ref=%s", amount, user_id, reference)
    return {"message": "Wallet credited", "amount": amount}