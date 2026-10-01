import hashlib
import hmac
import json
import logging
import os
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.database import database
from app.routes.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_BASE = "https://api.paystack.co"

# Bounds for a single top-up. Tune as policy requires.
_MIN_TOPUP_NAIRA = 100
_MAX_TOPUP_NAIRA = 1_000_000


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────
class InitiatePaymentRequest(BaseModel):
    amount: float = Field(..., gt=0)
    callback_url: Optional[str] = Field(None, max_length=500)


class VerifyPaymentRequest(BaseModel):
    reference: str = Field(..., min_length=3, max_length=128)


# ─────────────────────────────────────────────────────────────
# Internal — the single credit path
# ─────────────────────────────────────────────────────────────
async def _apply_credit(
    *,
    reference: str,
    user_id: str,
    amount_kobo: int,
    source: str,
) -> dict:
    """
    Idempotent, transactional wallet credit.

    Returns {credited: bool, amount: float, reason: str}.

    Every credit path — /verify and /webhook — funnels through here.
    The advisory lock serializes concurrent calls on the same reference,
    so the check-then-act sequence cannot double-credit.
    """
    amount = amount_kobo / 100

    async with database.transaction():
        # Serialize concurrent callers on this reference.
        await database.fetch_val(
            "SELECT pg_advisory_xact_lock(hashtext(:ref))",
            {"ref": reference},
        )

        intent = await database.fetch_one(
            "SELECT status FROM payment_intents WHERE reference = :ref",
            {"ref": reference},
        )
        if not intent:
            logger.warning(
                "_apply_credit: intent missing ref=%s source=%s", reference, source
            )
            return {"credited": False, "amount": amount, "reason": "intent_missing"}

        if intent["status"] == "completed":
            logger.info(
                "_apply_credit: already completed ref=%s source=%s", reference, source
            )
            return {"credited": False, "amount": amount, "reason": "already_completed"}

        # Belt-and-suspenders: the ledger row is the ultimate source of truth.
        existing_txn = await database.fetch_one(
            "SELECT id FROM wallet_transactions WHERE reference = :ref",
            {"ref": reference},
        )
        if existing_txn:
            logger.warning(
                "_apply_credit: txn exists but intent not completed ref=%s", reference
            )
            await database.execute(
                """
                UPDATE payment_intents
                   SET status = 'completed', completed_at = NOW()
                 WHERE reference = :ref
                """,
                {"ref": reference},
            )
            return {"credited": False, "amount": amount, "reason": "txn_exists"}

        # Ensure wallet row exists.
        await database.execute(
            """
            INSERT INTO wallets (user_id, balance)
            VALUES (:uid, 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            {"uid": user_id},
        )

        # Credit.
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": amount, "uid": user_id},
        )

        # Ledger.
        await database.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, amount, type, description, reference, status, created_at)
            VALUES
                (:uid, :amt, 'credit', :desc, :ref, 'completed', NOW())
            """,
            {
                "uid": user_id,
                "amt": amount,
                "desc": f"Wallet top-up via Paystack ({source})",
                "ref": reference,
            },
        )

        # Mark intent complete.
        await database.execute(
            """
            UPDATE payment_intents
               SET status = 'completed', completed_at = NOW()
             WHERE reference = :ref
            """,
            {"ref": reference},
        )

    logger.info(
        "Credited %.2f to user=%s ref=%s source=%s", amount, user_id, reference, source
    )
    return {"credited": True, "amount": amount, "reason": "ok"}


# ─────────────────────────────────────────────────────────────
# POST /payments/initiate
# ─────────────────────────────────────────────────────────────
@router.post("/initiate")
async def initiate_payment(
    req: InitiatePaymentRequest,
    current_user: dict = Depends(get_current_user),
):
    if not PAYSTACK_SECRET_KEY:
        logger.error("initiate: PAYSTACK_SECRET_KEY not configured")
        raise HTTPException(status_code=500, detail="Payments unavailable")

    if req.amount < _MIN_TOPUP_NAIRA or req.amount > _MAX_TOPUP_NAIRA:
        raise HTTPException(
            status_code=400,
            detail=f"Amount must be between ₦{_MIN_TOPUP_NAIRA:,} and ₦{_MAX_TOPUP_NAIRA:,}",
        )

    user_id = current_user["id"]
    amount_kobo = int(round(req.amount * 100))

    # Paystack requires an email. Phone-only signups get a deterministic
    # synthetic address — ownership is enforced by intent.user_id, not
    # by the email, so this is safe.
    email = (current_user.get("email") or "").strip().lower()
    if not email:
        email = f"user-{user_id[:12]}@admerce-payments.local"

    reference = f"topup_{uuid.uuid4().hex}"

    # 1. Create the intent FIRST. If Paystack is unreachable, we still
    #    have a record and can reconcile.
    try:
        await database.execute(
            """
            INSERT INTO payment_intents (reference, user_id, amount_kobo, status)
            VALUES (:ref, :uid, :amt, 'initiated')
            """,
            {"ref": reference, "uid": user_id, "amt": amount_kobo},
        )
    except Exception:
        logger.exception("initiate: intent insert failed user=%s", user_id)
        raise HTTPException(status_code=500, detail="Could not start payment. Please try again.")

    # 2. Ask Paystack to initialize.
    payload = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "currency": "NGN",
    }
    if req.callback_url:
        payload["callback_url"] = req.callback_url

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{PAYSTACK_BASE}/transaction/initialize",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
                json=payload,
            )
    except httpx.HTTPError as e:
        logger.warning("initiate: Paystack unreachable: %r", e)
        await database.execute(
            "UPDATE payment_intents SET status = 'failed' WHERE reference = :ref",
            {"ref": reference},
        )
        raise HTTPException(
            status_code=502,
            detail="Could not reach payment provider. Please try again.",
        )

    try:
        data = resp.json()
    except Exception:
        logger.warning("initiate: non-JSON from Paystack ref=%s", reference)
        raise HTTPException(status_code=502, detail="Invalid response from payment provider")

    if not data.get("status"):
        logger.warning(
            "initiate: Paystack error ref=%s msg=%s", reference, data.get("message")
        )
        raise HTTPException(
            status_code=400,
            detail=data.get("message") or "Could not start payment",
        )

    auth = data.get("data") or {}
    authorization_url = auth.get("authorization_url")
    returned_ref = auth.get("reference") or reference

    if not authorization_url:
        logger.error("initiate: no authorization_url ref=%s", reference)
        raise HTTPException(status_code=502, detail="Invalid response from payment provider")

    # Guard against Paystack returning a different reference than we sent.
    if returned_ref != reference:
        logger.warning(
            "initiate: Paystack returned different ref=%s (sent=%s)",
            returned_ref, reference,
        )

    return {
        "reference": returned_ref,
        "authorization_url": authorization_url,
        "amount": req.amount,
    }


# ─────────────────────────────────────────────────────────────
# POST /payments/verify
# ─────────────────────────────────────────────────────────────
@router.post("/verify")
async def verify_payment(
    req: VerifyPaymentRequest,
    current_user: dict = Depends(get_current_user),
):
    if not PAYSTACK_SECRET_KEY:
        logger.error("verify: PAYSTACK_SECRET_KEY not configured")
        raise HTTPException(status_code=500, detail="Payments unavailable")

    user_id = current_user["id"]

    # 1. Intent lookup — this is the ownership source of truth.
    intent = await database.fetch_one(
        "SELECT user_id, amount_kobo, status FROM payment_intents WHERE reference = :ref",
        {"ref": req.reference},
    )
    if not intent:
        logger.warning("verify: unknown reference=%s user=%s", req.reference, user_id)
        raise HTTPException(status_code=404, detail="Unknown payment reference")

    # 2. Exact ownership — no email heuristic.
    if intent["user_id"] != user_id:
        logger.warning(
            "verify: ownership mismatch user=%s intent_user=%s ref=%s",
            user_id, intent["user_id"], req.reference,
        )
        raise HTTPException(
            status_code=403,
            detail="This payment does not belong to your account",
        )

    # 3. Short-circuit if the webhook already credited it.
    if intent["status"] == "completed":
        return {
            "message": "Already credited",
            "amount": intent["amount_kobo"] / 100,
        }

    # 4. Ask Paystack to verify.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{PAYSTACK_BASE}/transaction/verify/{req.reference}",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            )
    except httpx.HTTPError as e:
        logger.warning("verify: Paystack unreachable: %r", e)
        raise HTTPException(
            status_code=502,
            detail="Could not reach payment provider. Please try again.",
        )

    try:
        data = resp.json()
    except Exception:
        logger.warning("verify: non-JSON from Paystack ref=%s", req.reference)
        raise HTTPException(status_code=502, detail="Invalid response from payment provider")

    if not data.get("status"):
        raise HTTPException(
            status_code=400,
            detail=data.get("message") or "Payment verification failed",
        )

    txn = data.get("data") or {}
    if txn.get("status") != "success":
        raise HTTPException(status_code=400, detail="Transaction not successful")

    try:
        amount_kobo = int(txn["amount"])
    except (KeyError, TypeError, ValueError):
        logger.error("verify: missing amount in Paystack response ref=%s", req.reference)
        raise HTTPException(status_code=502, detail="Malformed payment response")

    # 5. Amount must match the intent exactly. Blocks any tampering where
    #    the user somehow gets a smaller charge verified for a bigger intent.
    if amount_kobo != intent["amount_kobo"]:
        logger.error(
            "verify: amount mismatch ref=%s intent=%s paystack=%s",
            req.reference, intent["amount_kobo"], amount_kobo,
        )
        raise HTTPException(status_code=400, detail="Payment amount does not match intent")

    # 6. Credit — idempotent, transactional, serialized.
    result = await _apply_credit(
        reference=req.reference,
        user_id=user_id,
        amount_kobo=amount_kobo,
        source="verify",
    )

    if result["credited"]:
        return {"message": "Wallet credited", "amount": result["amount"]}
    if result["reason"] in ("already_completed", "txn_exists"):
        return {"message": "Already credited", "amount": result["amount"]}

    logger.error("verify: unexpected credit result ref=%s result=%s", req.reference, result)
    raise HTTPException(status_code=500, detail="Could not process payment. Please contact support.")


# ─────────────────────────────────────────────────────────────
# POST /payments/webhook
# ─────────────────────────────────────────────────────────────
@router.post("/webhook")
async def paystack_webhook(request: Request):
    """
    Paystack webhook receiver.

    No auth — verified by HMAC-SHA512 signature of the raw body keyed
    on PAYSTACK_SECRET_KEY. This is what makes the flow permanent: even
    if the user never returns to the app after paying, Paystack calls
    here and we credit server-side.

    Always returns 200 on a request we accepted, even if internal
    processing errored — otherwise Paystack retries in a loop. Bad
    signatures get 401 (a real Paystack event always has a valid one).
    """
    if not PAYSTACK_SECRET_KEY:
        logger.error("webhook: PAYSTACK_SECRET_KEY missing")
        return {"received": True}

    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    if not signature or not hmac.compare_digest(expected, signature):
        logger.warning("webhook: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        event = json.loads(raw_body)
    except Exception:
        logger.warning("webhook: malformed JSON body")
        return {"received": True}

    event_type = event.get("event", "")
    data = event.get("data") or {}
    reference = data.get("reference") or ""

    logger.info("webhook: event=%s ref=%s", event_type, reference)

    if event_type == "charge.success":
        if not reference:
            logger.warning("webhook: charge.success without reference")
            return {"received": True}

        intent = await database.fetch_one(
            "SELECT user_id, amount_kobo, status FROM payment_intents WHERE reference = :ref",
            {"ref": reference},
        )
        if not intent:
            logger.warning("webhook: no intent for ref=%s", reference)
            return {"received": True}

        try:
            amount_kobo = int(data["amount"])
        except (KeyError, TypeError, ValueError):
            logger.warning("webhook: missing/invalid amount ref=%s", reference)
            return {"received": True}

        if amount_kobo != intent["amount_kobo"]:
            logger.error(
                "webhook: amount mismatch ref=%s intent=%s paystack=%s",
                reference, intent["amount_kobo"], amount_kobo,
            )
            return {"received": True}

        try:
            result = await _apply_credit(
                reference=reference,
                user_id=intent["user_id"],
                amount_kobo=amount_kobo,
                source="webhook",
            )
            logger.info("webhook: credit result ref=%s result=%s", reference, result)
        except Exception:
            logger.exception("webhook: _apply_credit failed ref=%s", reference)
            # Still return 200 — Paystack retrying won't fix a code bug.
            # We reconcile via the Paystack dashboard if needed.

    # Everything else (transfer.success, transfer.failed, etc.) is
    # acknowledged. Wire those to withdraw-status updates when Transfers
    # is integrated — see pending items.
    return {"received": True}