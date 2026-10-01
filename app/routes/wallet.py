import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from app.db.database import database
from app.utils.security import get_current_user
from app.routes.notifications import send_push_to_user

from app.routes.auth import hash_password, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wallet", tags=["Wallet"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ReserveRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=128)
    storekeeper_id: str = Field(..., min_length=1, max_length=128)
    listing_id: str = Field(..., min_length=1, max_length=128)
    quantity: int = Field(1, ge=1, le=1000)
    delivery_fee: float = Field(0.0, ge=0, le=50_000)
    pickup_window_hours: int = 3


class ConfirmRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=128)


class AcceptRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=128)


class DeclineRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=128)
    reason: Optional[str] = Field(None, max_length=500)


class SetPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6, pattern=r"^\d{4,6}$")
    current_pin: Optional[str] = Field(None, min_length=4, max_length=6)


class WithdrawRequest(BaseModel):
    amount: float = Field(..., gt=0, le=10_000_000)
    method: str = Field("mobile_money", max_length=32)
    account_number: str = Field(..., min_length=4, max_length=32)
    pin: str = Field(..., min_length=4, max_length=6)
    idempotency_key: Optional[str] = Field(None, max_length=64)


class InstantPickupRequest(BaseModel):
    listing_id: str = Field(..., min_length=1, max_length=128)
    storekeeper_id: str = Field(..., min_length=1, max_length=128)
    quantity: int = Field(1, ge=1, le=1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_background_tasks: set = set()


def _fire_and_forget(coro) -> None:
    async def _run():
        try:
            await coro
        except Exception:
            logger.exception("Background task failed")
    try:
        task = asyncio.create_task(_run())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        logger.exception("Failed to schedule background task")


def _update_won(result) -> bool:
    if isinstance(result, int):
        return result > 0
    if isinstance(result, str):
        try:
            return int(result.rsplit(" ", 1)[-1]) > 0
        except (ValueError, IndexError):
            return False
    return False


async def _log_wallet_transaction(
    user_id: str,
    amount: float,
    type: str,
    description: str,
    reference: str,
    status: str = "completed",
):
    if amount <= 0 or not user_id:
        logger.warning("Skipping wallet txn log: amount=%s user=%s", amount, user_id)
        return
    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, description, reference, status, created_at)
        VALUES
            (:uid, :amt, :type, :desc, :ref, :status, NOW())
        """,
        {
            "uid": user_id,
            "amt": float(amount),
            "type": type,
            "desc": description,
            "ref": reference,
            "status": status,
        },
    )

# ---------------------------------------------------------------------------
# Create Wallet
# ---------------------------------------------------------------------------

@router.post("/create")
async def create_wallet(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    existing = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if existing:
        return {
            "user_id": user_id,
            "balance": float(existing["balance"]),
            "message": "Wallet already exists",
        }
    await database.execute(
        "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
        {"uid": user_id},
    )
    return {"user_id": user_id, "balance": 0.0, "message": "Wallet created"}

# ---------------------------------------------------------------------------
# Topup — REMOVED
# ---------------------------------------------------------------------------

@router.post("/topup")
async def topup(current_user: dict = Depends(get_current_user)):
    raise HTTPException(
        status_code=410,
        detail=(
            "Direct top-up is no longer available. "
            "Please top up via the in-app payment flow."
        ),
    )

# ---------------------------------------------------------------------------
# Get Balance
# ---------------------------------------------------------------------------

@router.get("/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not wallet:
        await database.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
            {"uid": user_id},
        )
        return {"user_id": user_id, "balance": 0.0}
    return {"user_id": user_id, "balance": float(wallet["balance"])}

# ---------------------------------------------------------------------------
# Set / Change Withdrawal PIN
# ---------------------------------------------------------------------------

@router.post("/set-pin")
async def set_pin(req: SetPinRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    wallet = await database.fetch_one(
        "SELECT withdrawal_pin FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    existing_pin = wallet["withdrawal_pin"]
    if existing_pin:
        if not req.current_pin or not verify_password(req.current_pin, existing_pin):
            raise HTTPException(status_code=403, detail="Current PIN is incorrect")

    hashed_pin = hash_password(req.pin)
    await database.execute(
        "UPDATE wallets SET withdrawal_pin = :pin WHERE user_id = :uid",
        {"pin": hashed_pin, "uid": user_id},
    )
    return {"message": "Withdrawal PIN set successfully"}

# ---------------------------------------------------------------------------
# Withdraw
# ---------------------------------------------------------------------------

@router.post("/withdraw")
async def withdraw(req: WithdrawRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]

    row = await database.fetch_one(
        "SELECT balance, withdrawal_pin FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Wallet not found")

    wallet = dict(row)
    withdrawal_pin = wallet.get("withdrawal_pin")
    if not withdrawal_pin:
        raise HTTPException(
            status_code=403,
            detail="Withdrawal PIN not set. Please set a PIN first.",
        )

    if not verify_password(req.pin, withdrawal_pin):
        raise HTTPException(status_code=403, detail="Incorrect PIN")

    txn_id = f"wdr_{uuid.uuid4().hex[:12]}"

    try:
        async with database.transaction():
            result = await database.execute(
                """
                UPDATE wallets
                   SET balance = balance - :amt
                 WHERE user_id = :uid AND balance >= :amt
                """,
                {"amt": req.amount, "uid": user_id},
            )
            if not _update_won(result):
                raise HTTPException(status_code=400, detail="Insufficient balance")

            await _log_wallet_transaction(
                user_id=user_id,
                amount=req.amount,
                type="debit",
                description=f"Withdrawal via {req.method} (pending)",
                reference=txn_id,
                status="pending",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("withdraw failed user=%s", user_id)
        raise HTTPException(status_code=500, detail="Withdrawal failed. Please try again.")

    updated = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )

    return {
        "message": (
            "Withdrawal request received. Funds will be sent to your account "
            "once processing completes."
        ),
        "status": "pending",
        "transaction_id": txn_id,
        "amount": req.amount,
        "new_balance": float(updated["balance"]) if updated else None,
    }

# ---------------------------------------------------------------------------
# Instant Pickup
# ---------------------------------------------------------------------------

@router.post("/instant-pickup")
async def instant_pickup(
    req: InstantPickupRequest,
    current_user: dict = Depends(get_current_user),
):
    shopper_id = current_user["id"]
    quantity = req.quantity

    row = await database.fetch_one(
        """
        SELECT l.*, s.owner_id AS store_owner_id
          FROM listings l
          JOIN stores   s ON l.store_id = s.store_id
         WHERE l.listing_id = :lid AND s.owner_id = :oid
        """,
        {"lid": req.listing_id, "oid": req.storekeeper_id},
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Listing not found or not owned by that storekeeper",
        )

    listing = dict(row)
    store_owner_id = listing["store_owner_id"]

    unit_price = float(listing.get("price") or 0)
    if unit_price <= 0:
        raise HTTPException(status_code=500, detail="Listing has no price configured")

    total = round(unit_price * quantity, 2)
    available = listing.get("quantity_available")
    if available is not None and available < quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Only {available} item(s) left in stock.",
        )

    txn_id = f"pickup_{uuid.uuid4().hex[:12]}"

    try:
        async with database.transaction():
            debit = await database.execute(
                "UPDATE wallets SET balance = balance - :amt "
                "WHERE user_id = :uid AND balance >= :amt",
                {"amt": total, "uid": shopper_id},
            )
            if not _update_won(debit):
                raise HTTPException(status_code=400, detail="Insufficient balance")

            if available is not None:
                stock = await database.execute(
                    "UPDATE listings SET quantity_available = quantity_available - :qty "
                    "WHERE listing_id = :lid AND quantity_available >= :qty",
                    {"qty": quantity, "lid": req.listing_id},
                )
                if not _update_won(stock):
                    raise HTTPException(
                        status_code=400,
                        detail="Item just sold out. Please try again.",
                    )

            await database.execute(
                "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                {"amt": total, "uid": store_owner_id},
            )

            await _log_wallet_transaction(
                user_id=shopper_id,
                amount=total,
                type="debit",
                description=f"Instant pickup ({quantity} item{'s' if quantity > 1 else ''})",
                reference=txn_id,
            )
            await _log_wallet_transaction(
                user_id=store_owner_id,
                amount=total,
                type="credit",
                description=f"Instant pickup payment ({quantity} item{'s' if quantity > 1 else ''})",
                reference=f"{txn_id}:credit",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("instant_pickup failed shopper=%s", shopper_id)
        raise HTTPException(status_code=500, detail="Payment failed. Please try again.")

    return {
        "message": "Payment successful",
        "transaction_id": txn_id,
        "amount": total,
        "quantity": quantity,
    }

# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------

@router.post("/reserve")
async def reserve(req: ReserveRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    quantity = req.quantity
    window_hours = max(3, min(168, int(req.pickup_window_hours or 3)))

    row = await database.fetch_one(
        """
        SELECT l.*, s.owner_id AS store_owner_id
          FROM listings l
          JOIN stores   s ON l.store_id = s.store_id
         WHERE l.listing_id = :lid AND s.owner_id = :oid
        """,
        {"lid": req.listing_id, "oid": req.storekeeper_id},
    )
    if not row:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Listing {req.listing_id} not found or not owned by "
                f"storekeeper {req.storekeeper_id}"
            ),
        )

    listing = dict(row)
    store_owner_id = listing["store_owner_id"]

    unit_price = float(listing.get("price") or 0)
    if unit_price <= 0:
        raise HTTPException(status_code=500, detail="Listing has no price configured")

    item_amount = round(unit_price * quantity, 2)
    total = round(item_amount + req.delivery_fee, 2)

    available = listing.get("quantity_available")
    if available is not None and available < quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Only {available} items available",
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=window_hours)

    try:
        async with database.transaction():
            existing = await database.fetch_one(
                "SELECT 1 AS x FROM escrow WHERE order_id = :oid",
                {"oid": req.order_id},
            )
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Order {req.order_id} already reserved",
                )

            debit = await database.execute(
                "UPDATE wallets SET balance = balance - :amt "
                "WHERE user_id = :uid AND balance >= :amt",
                {"amt": total, "uid": shopper_id},
            )
            if not _update_won(debit):
                raise HTTPException(
                    status_code=400,
                    detail="Insufficient balance",
                )

            if available is not None:
                stock = await database.execute(
                    "UPDATE listings SET quantity_available = quantity_available - :qty "
                    "WHERE listing_id = :lid AND quantity_available >= :qty",
                    {"qty": quantity, "lid": req.listing_id},
                )
                if not _update_won(stock):
                    raise HTTPException(
                        status_code=400,
                        detail="Item just sold out. Please try again.",
                    )

            await database.execute(
                """
                INSERT INTO escrow (
                    order_id, shopper_id, storekeeper_id, courier_id,
                    listing_id, quantity, item_amount, delivery_fee, total_amount,
                    status, expires_at, created_at
                )
                VALUES (
                    :order_id, :shopper_id, :storekeeper_id, NULL,
                    :listing_id, :quantity, :item_amount, :delivery_fee, :total_amount,
                    'locked', :expires_at, :created_at
                )
                """,
                {
                    "order_id": req.order_id,
                    "shopper_id": shopper_id,
                    "storekeeper_id": store_owner_id,
                    "listing_id": req.listing_id,
                    "quantity": quantity,
                    "item_amount": item_amount,
                    "delivery_fee": float(req.delivery_fee),
                    "total_amount": total,
                    "expires_at": expires_at,
                    "created_at": now,
                },
            )

            await _log_wallet_transaction(
                user_id=shopper_id,
                amount=total,
                type="debit",
                description=(
                    f"Reservation ({quantity} item{'s' if quantity > 1 else ''}, "
                    f"{window_hours}h window)"
                ),
                reference=req.order_id,
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("reserve failed order=%s user=%s", req.order_id, shopper_id)
        raise HTTPException(
            status_code=500,
            detail="Could not create reservation. Please try again.",
        )

    _fire_and_forget(send_push_to_user(
        store_owner_id,
        "New Reservation!",
        (
            f"A shopper reserved {quantity} item"
            f"{'s' if quantity > 1 else ''}. Order #{req.order_id[:8]}"
        ),
        {"order_id": req.order_id},
    ))

    return {
        "order_id": req.order_id,
        "status": "locked",
        "total": total,
        "quantity": quantity,
        "pickup_window_hours": window_hours,
        "expires_at": expires_at.isoformat(),
        "message": "Funds reserved",
    }

# ---------------------------------------------------------------------------
# Accept Reservation
# ---------------------------------------------------------------------------
# 🔒 FIXED: order_id is NOT unique in escrow — multi-store baskets create
#    multiple rows sharing the same order_id. Scope the lookup to the
#    caller's own row (storekeeper_id = caller). Previously this fetched an
#    arbitrary row and rejected the legitimate owner with a 403.

@router.post("/accept")
async def accept_reservation(
    req: AcceptRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]

    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND storekeeper_id = :uid",
        {"oid": req.order_id, "uid": user_id},
    )
    if not escrow:
        # Distinguish "not your order" from "doesn't exist" for better UX.
        exists = await database.fetch_one(
            "SELECT 1 AS x FROM escrow WHERE order_id = :oid",
            {"oid": req.order_id},
        )
        if exists:
            raise HTTPException(
                status_code=403,
                detail="You are not the storekeeper for this order",
            )
        raise HTTPException(status_code=404, detail="Order not found")

    if escrow["status"] != "locked":
        raise HTTPException(
            status_code=400,
            detail="Order is not in a reservable state (status must be 'locked')",
        )

    result = await database.execute(
        "UPDATE escrow SET status = 'accepted' "
        "WHERE order_id = :oid AND storekeeper_id = :uid AND status = 'locked'",
        {"oid": req.order_id, "uid": user_id},
    )
    if not _update_won(result):
        return {
            "order_id": req.order_id,
            "status": "accepted",
            "message": "Already accepted.",
        }

    _fire_and_forget(send_push_to_user(
        escrow["shopper_id"],
        "Reservation Accepted!",
        f"Your order #{req.order_id[:8]} has been accepted by the storekeeper.",
        {"order_id": req.order_id},
    ))

    return {
        "order_id": req.order_id,
        "status": "accepted",
        "message": "Reservation accepted",
    }

# ---------------------------------------------------------------------------
# Decline Reservation
# ---------------------------------------------------------------------------
# 🔒 FIXED: same multi-store scoping as accept.

@router.post("/decline")
async def decline_reservation(
    req: DeclineRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]

    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND storekeeper_id = :uid",
        {"oid": req.order_id, "uid": user_id},
    )
    if not escrow:
        exists = await database.fetch_one(
            "SELECT 1 AS x FROM escrow WHERE order_id = :oid",
            {"oid": req.order_id},
        )
        if exists:
            raise HTTPException(
                status_code=403,
                detail="You are not the storekeeper for this order",
            )
        raise HTTPException(status_code=404, detail="Order not found")

    current_status = (escrow["status"] or "").lower()
    if current_status != "locked":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot decline a reservation with status '{current_status}'. "
                "Only pending reservations can be declined."
            ),
        )

    reason = (req.reason or "").strip()
    shopper_id = escrow["shopper_id"]
    total = float(escrow["total_amount"] or 0)
    listing_id = escrow["listing_id"]
    qty = escrow["quantity"] or 0
    short = req.order_id[:8]

    refunded = 0.0

    try:
        async with database.transaction():
            result = await database.execute(
                "UPDATE escrow SET status = 'declined' "
                "WHERE order_id = :oid AND storekeeper_id = :uid AND status = 'locked'",
                {"oid": req.order_id, "uid": user_id},
            )
            if not _update_won(result):
                return {
                    "order_id": req.order_id,
                    "status": "declined",
                    "refunded": 0,
                    "reason": reason or None,
                    "message": "Reservation was already processed.",
                }

            if listing_id and qty:
                await database.execute(
                    "UPDATE listings SET quantity_available = quantity_available + :qty "
                    "WHERE listing_id = :lid AND quantity_available IS NOT NULL",
                    {"qty": qty, "lid": listing_id},
                )

            if total > 0 and shopper_id:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": total, "uid": shopper_id},
                )
                refunded = total
                audit_desc = f"Refund: reservation {short} declined by store"
                if reason:
                    audit_desc += f" — {reason}"
                await _log_wallet_transaction(
                    user_id=shopper_id,
                    amount=total,
                    type="credit",
                    description=audit_desc,
                    reference=f"decline:{req.order_id}:{user_id[:8]}",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("decline failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not decline reservation.")

    notify_body = (
        f"Your order #{short} couldn't be fulfilled. "
        f"₦{refunded:,.0f} has been refunded to your wallet."
    )
    if reason:
        notify_body += f" Reason: {reason}"

    _fire_and_forget(send_push_to_user(
        shopper_id,
        "Reservation Declined",
        notify_body,
        {"order_id": req.order_id, "reason": reason or ""},
    ))

    return {
        "order_id": req.order_id,
        "status": "declined",
        "refunded": refunded,
        "reason": reason or None,
        "message": (
            f"Reservation declined. ₦{refunded:,.0f} returned to the shopper."
            if refunded > 0
            else "Reservation declined."
        ),
    }

# ---------------------------------------------------------------------------
# Confirm (shopper releases escrow)
# ---------------------------------------------------------------------------
# ⚠️  STILL BROKEN FOR MULTI-STORE ORDERS: this fetches an arbitrary row of
#     the order and pays out only that store. For a two-store basket, the
#     shopper needs to confirm pickup per-store. Fix requires an API contract
#     change — the request must carry an order_store_id (or store_id) so we
#     know WHICH row to confirm. Left as-is pending that decision.

@router.post("/confirm")
async def confirm(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid",
        {"oid": req.order_id},
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["shopper_id"] != shopper_id:
        raise HTTPException(
            status_code=403,
            detail="You can only confirm your own orders",
        )
    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(
            status_code=400,
            detail="Order is not in a confirmable state",
        )

    item_amount = float(escrow["item_amount"] or 0)
    delivery_fee = float(escrow["delivery_fee"] or 0)
    courier_id = escrow["courier_id"]
    storekeeper_id = escrow["storekeeper_id"]

    if courier_id in (shopper_id, storekeeper_id):
        courier_id = None

    try:
        async with database.transaction():
            result = await database.execute(
                "UPDATE escrow SET status = 'picked_up' "
                "WHERE order_id = :oid AND status IN ('accepted', 'locked')",
                {"oid": req.order_id},
            )
            if not _update_won(result):
                return {
                    "order_id": req.order_id,
                    "status": "picked_up",
                    "message": "Order was already processed.",
                }

            if item_amount > 0:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": item_amount, "uid": storekeeper_id},
                )
                await _log_wallet_transaction(
                    user_id=storekeeper_id,
                    amount=item_amount,
                    type="credit",
                    description=f"Order #{req.order_id[:8]} picked up",
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:storekeeper",
                )

            if delivery_fee > 0 and courier_id:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": delivery_fee, "uid": courier_id},
                )
                await _log_wallet_transaction(
                    user_id=courier_id,
                    amount=delivery_fee,
                    type="credit",
                    description=f"Delivery fee for #{req.order_id[:8]}",
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:courier",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("confirm failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not confirm order.")

    return {
        "order_id": req.order_id,
        "status": "picked_up",
        "message": "Order marked as picked up. Funds released.",
    }

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
# ⚠️  Same multi-store caveat as confirm.

@router.post("/dispatch")
async def dispatch(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid",
        {"oid": req.order_id},
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["shopper_id"] != shopper_id:
        raise HTTPException(status_code=403, detail="Only the shopper can dispatch")
    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(
            status_code=400,
            detail="Order must be accepted or locked to dispatch",
        )

    item_amount = float(escrow["item_amount"] or 0)
    delivery_fee = float(escrow["delivery_fee"] or 0)
    courier_id = escrow["courier_id"]
    storekeeper_id = escrow["storekeeper_id"]

    if courier_id in (shopper_id, storekeeper_id):
        courier_id = None

    try:
        async with database.transaction():
            result = await database.execute(
                "UPDATE escrow SET status = 'dispatched' "
                "WHERE order_id = :oid AND status IN ('accepted', 'locked')",
                {"oid": req.order_id},
            )
            if not _update_won(result):
                return {
                    "order_id": req.order_id,
                    "status": "dispatched",
                    "message": "Order was already processed.",
                }

            if item_amount > 0:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": item_amount, "uid": storekeeper_id},
                )
                await _log_wallet_transaction(
                    user_id=storekeeper_id,
                    amount=item_amount,
                    type="credit",
                    description=f"Order #{req.order_id[:8]} dispatched",
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:storekeeper",
                )

            if delivery_fee > 0 and courier_id:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": delivery_fee, "uid": courier_id},
                )
                await _log_wallet_transaction(
                    user_id=courier_id,
                    amount=delivery_fee,
                    type="credit",
                    description=f"Delivery fee for #{req.order_id[:8]}",
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:courier",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("dispatch failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not dispatch order.")

    return {
        "order_id": req.order_id,
        "status": "dispatched",
        "message": "Order dispatched. Funds released.",
    }

# ---------------------------------------------------------------------------
# Return
# ---------------------------------------------------------------------------
# ⚠️  Same multi-store caveat as confirm.

@router.post("/return")
async def return_order(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid",
        {"oid": req.order_id},
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["shopper_id"] != shopper_id:
        raise HTTPException(status_code=403, detail="Only the shopper can return")
    if escrow["status"] not in ("locked", "accepted"):
        raise HTTPException(
            status_code=400,
            detail="Order must be in 'locked' or 'accepted' state to return",
        )

    listing_id = escrow["listing_id"]
    quantity = escrow["quantity"] or 0
    item_amount = float(escrow["item_amount"] or 0)

    try:
        async with database.transaction():
            result = await database.execute(
                "UPDATE escrow SET status = 'returned' "
                "WHERE order_id = :oid AND status IN ('locked', 'accepted')",
                {"oid": req.order_id},
            )
            if not _update_won(result):
                return {
                    "order_id": req.order_id,
                    "status": "returned",
                    "message": "Order was already processed.",
                }

            if listing_id and quantity:
                await database.execute(
                    "UPDATE listings SET quantity_available = quantity_available + :qty "
                    "WHERE listing_id = :lid AND quantity_available IS NOT NULL",
                    {"qty": quantity, "lid": listing_id},
                )

            if item_amount > 0:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": item_amount, "uid": shopper_id},
                )
                await _log_wallet_transaction(
                    user_id=shopper_id,
                    amount=item_amount,
                    type="credit",
                    description=f"Refund for order #{req.order_id[:8]}",
                    reference=f"{req.order_id}:{listing_id}:refund",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("return failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not process return.")

    return {
        "order_id": req.order_id,
        "status": "returned",
        "message": "Item cost refunded. Stock restored.",
    }

# ---------------------------------------------------------------------------
# Reversed Package
# ---------------------------------------------------------------------------
# 🔒 FIXED: same multi-store scoping as accept — this is storekeeper-initiated.

@router.post("/reversed-package")
async def reversed_package(
    req: ConfirmRequest,
    current_user: dict = Depends(get_current_user),
):
    storekeeper_id = current_user["id"]

    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND storekeeper_id = :uid",
        {"oid": req.order_id, "uid": storekeeper_id},
    )
    if not escrow:
        exists = await database.fetch_one(
            "SELECT 1 AS x FROM escrow WHERE order_id = :oid",
            {"oid": req.order_id},
        )
        if exists:
            raise HTTPException(
                status_code=403,
                detail="Only the storekeeper can release",
            )
        raise HTTPException(status_code=404, detail="Order not found")

    if escrow["status"] != "returned":
        raise HTTPException(status_code=400, detail="Order not in returned state")

    delivery_fee = float(escrow["delivery_fee"] or 0)
    courier_id = escrow["courier_id"]
    shopper_id = escrow["shopper_id"]

    if courier_id in (storekeeper_id, shopper_id):
        courier_id = None

    try:
        async with database.transaction():
            result = await database.execute(
                "UPDATE escrow SET status = 'reversed' "
                "WHERE order_id = :oid AND storekeeper_id = :uid AND status = 'returned'",
                {"oid": req.order_id, "uid": storekeeper_id},
            )
            if not _update_won(result):
                return {
                    "order_id": req.order_id,
                    "status": "reversed",
                    "message": "Already reversed.",
                }

            if delivery_fee > 0 and courier_id:
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": delivery_fee, "uid": courier_id},
                )
                await _log_wallet_transaction(
                    user_id=courier_id,
                    amount=delivery_fee,
                    type="credit",
                    description=f"Reversed package fee for #{req.order_id[:8]}",
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:reversed",
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception("reversed_package failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not process reversal.")

    return {
        "order_id": req.order_id,
        "status": "reversed",
        "message": "Courier fee released",
    }

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("/escrows")
async def get_escrows(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        "SELECT * FROM escrow WHERE shopper_id = :uid AND status = 'locked'",
        {"uid": user_id},
    )
    return [dict(row) for row in rows]


@router.get("/orders")
async def get_orders(
    current_user: dict = Depends(get_current_user),
    status: Optional[str] = Query(None, description="Comma-separated statuses"),
):
    user_id = current_user["id"]
    query = """
        SELECT e.*,
               s.name             AS store_name,
               s.store_image_url  AS store_image_url,
               s.address          AS store_address,
               s.latitude         AS store_latitude,
               s.longitude        AS store_longitude,
               l.title            AS listing_title,
               l.image_url        AS listing_image_url,
               l.category         AS listing_category
        FROM escrow e
        LEFT JOIN listings l ON e.listing_id = l.listing_id
        LEFT JOIN stores   s ON l.store_id   = s.store_id
        WHERE e.shopper_id = :uid
    """
    params: dict = {"uid": user_id}

    if status:
        status_list = [s.strip() for s in status.split(",") if s.strip()]
        if status_list:
            placeholders = []
            for idx, s in enumerate(status_list):
                ph = f"status_{idx}"
                placeholders.append(f":{ph}")
                params[ph] = s
            query += f" AND e.status IN ({', '.join(placeholders)})"

    query += " ORDER BY e.created_at DESC NULLS LAST"

    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]


@router.get("/order/{order_id}")
async def get_order_detail(
    order_id: str,
    current_user: dict = Depends(get_current_user),
):
    order = await database.fetch_one(
        """
        SELECT e.*,
               COALESCE(
                   NULLIF(u.nickname, ''),
                   NULLIF(u.real_name, ''),
                   NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''),
                   NULLIF(u.phone, ''),
                   'Customer'
               ) AS customer_name,
               u.avatar_url AS customer_avatar,
               COALESCE(
                   NULLIF(sk.nickname, ''),
                   NULLIF(sk.real_name, ''),
                   NULLIF(TRIM(CONCAT_WS(' ', sk.first_name, sk.last_name)), ''),
                   NULLIF(sk.phone, ''),
                   'Storekeeper'
               ) AS storekeeper_name,
               s.name             AS store_name,
               s.store_image_url  AS store_image_url,
               s.address          AS store_address,
               s.latitude         AS store_latitude,
               s.longitude        AS store_longitude,
               l.title            AS listing_title,
               l.image_url        AS listing_image_url,
               l.category         AS listing_category
        FROM escrow e
        LEFT JOIN users    u  ON e.shopper_id     = u.id
        LEFT JOIN users    sk ON e.storekeeper_id = sk.id
        LEFT JOIN listings l  ON e.listing_id     = l.listing_id
        LEFT JOIN stores   s  ON l.store_id       = s.store_id
        WHERE e.order_id = :oid
        """,
        {"oid": order_id},
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    viewer_id = current_user["id"]
    if viewer_id not in (order["shopper_id"], order["storekeeper_id"]):
        raise HTTPException(status_code=403, detail="Not your order")

    return dict(order)


@router.get("/transactions")
async def get_wallet_transactions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT id, amount, type, description, reference, status, created_at
        FROM wallet_transactions
        WHERE user_id = :uid
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
        """,
        {"uid": user_id, "limit": limit, "offset": offset},
    )
    return [dict(row) for row in rows]

# ---------------------------------------------------------------------------
# Reminder (currently unused)
# ---------------------------------------------------------------------------

async def schedule_reminder(
    user_id: str,
    order_id: str,
    delay_seconds: float,
    fraction: float,
):
    await asyncio.sleep(delay_seconds)
    escrow = await database.fetch_one(
        "SELECT status FROM escrow WHERE order_id = :oid",
        {"oid": order_id},
    )
    if escrow and escrow["status"] in ("locked", "accepted"):
        await send_push_to_user(
            user_id,
            "⏰ Pickup Reminder",
            f"Your order #{order_id[:8]} is expiring soon! Only {int(fraction*100)}% of time left.",
            {"order_id": order_id},
        )