import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from asyncpg.exceptions import UniqueViolationError

from app.db.database import database
from app.utils.security import get_current_user
from app.routes.notifications import send_push_to_user
from app.routes.auth import hash_password, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wallet", tags=["Wallet"])


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
_rate_buckets: dict[str, list[float]] = {}


def _rate_limit(key: str, max_calls: int, window_sec: int) -> None:
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_sec]
    if len(bucket) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again in a few minutes.",
        )
    bucket.append(now)
    if len(_rate_buckets) > 10_000:
        cutoff = now - 3600
        for k in list(_rate_buckets.keys()):
            _rate_buckets[k] = [t for t in _rate_buckets[k] if t > cutoff]
            if not _rate_buckets[k]:
                del _rate_buckets[k]


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
    order_store_id: Optional[int] = Field(None, ge=1)


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
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=64)


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


async def notify_user(
    user_id: str,
    kind: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """
    Write a user_notifications row and best-effort push. Failure is
    non-fatal — the wallet operation must not fail because the notification
    store is misconfigured.
    """
    try:
        await database.execute(
            """
            INSERT INTO user_notifications
                (user_id, kind, title, body, data, is_read, created_at)
            VALUES
                (:uid, :kind, :title, :body, :data, FALSE, NOW())
            """,
            {
                "uid": user_id,
                "kind": kind,
                "title": title,
                "body": body,
                "data": json.dumps(data or {}),
            },
        )
    except Exception as e:
        logger.warning("user_notifications write failed user=%s kind=%s err=%s", user_id, kind, e)

    _fire_and_forget(send_push_to_user(user_id, title, body, data or {}))


async def _flip_escrow_status(
    order_id: str,
    from_status: str,
    to_status: str,
    *,
    storekeeper_id: Optional[str] = None,
) -> bool:
    where = ["order_id = :oid", "status = :from_s"]
    params: dict = {"oid": order_id, "from_s": from_status, "to_s": to_status}
    if storekeeper_id is not None:
        where.append("storekeeper_id = :skid")
        params["skid"] = storekeeper_id
    sql = f"UPDATE escrow SET status = :to_s WHERE {' AND '.join(where)} RETURNING 1"
    row = await database.fetch_val(sql, params)
    return row is not None


async def _pick_escrow_row(
    order_id: str,
    shopper_id: str,
    order_store_id: Optional[int],
) -> dict:
    rows = await database.fetch_all(
        "SELECT * FROM escrow WHERE order_id = :oid AND shopper_id = :uid",
        {"oid": order_id, "uid": shopper_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Order not found")
    if len(rows) == 1:
        return dict(rows[0])
    if order_store_id is None:
        raise HTTPException(
            status_code=400,
            detail="This order has multiple stores. Specify order_store_id.",
        )
    for r in rows:
        if r["order_store_id"] == order_store_id:
            return dict(r)
    raise HTTPException(status_code=404, detail="Order row not found")


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


def _booking_link_candidates(ref: str) -> list[str]:
    m = re.match(r"^(book|confirm|complete|cancel|decline):(.+)$", ref)
    if not m:
        return []
    bid = m.group(2)
    return [f"{p}:{bid}" for p in ("book", "confirm", "complete", "cancel", "decline")]


def _display_name(u: dict) -> Optional[str]:
    if not u:
        return None
    real = (u.get("real_name") or "").strip()
    nick = (u.get("nickname") or "").strip()
    fn = (u.get("first_name") or "").strip()
    ln = (u.get("last_name") or "").strip()
    full = f"{fn} {ln}".strip()
    return real or nick or full or None


async def _lookup_counterparty_user(user_id: str) -> dict:
    """Fetch the user row + their store/business names (if any)."""
    row = await database.fetch_one(
        """
        SELECT u.id, u.real_name, u.nickname, u.first_name, u.last_name,
               u.role, u.business_name,
               s.name AS store_name
          FROM users u
          LEFT JOIN stores s ON s.owner_id = u.id
         WHERE u.id = :uid
         LIMIT 1
        """,
        {"uid": user_id},
    )
    if not row:
        return {
            "user_id": user_id,
            "display_name": "Admerce user",
            "role": None,
            "store_name": None,
            "business_name": None,
        }
    r = dict(row)
    return {
        "user_id": user_id,
        "display_name": _display_name(r) or "Admerce user",
        "role": r.get("role"),
        "store_name": r.get("store_name"),
        "business_name": r.get("business_name"),
    }


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
        detail="Direct top-up is no longer available. Please top up via the in-app payment flow.",
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
    _rate_limit(f"set-pin:{user_id}", max_calls=10, window_sec=900)

    wallet = await database.fetch_one(
        "SELECT withdrawal_pin FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    existing_pin = wallet["withdrawal_pin"]
    if existing_pin:
        if not req.current_pin:
            raise HTTPException(status_code=403, detail="Current PIN is required")
        ok = await run_in_threadpool(verify_password, req.current_pin, existing_pin)
        if not ok:
            raise HTTPException(status_code=403, detail="Current PIN is incorrect")

    hashed_pin = await run_in_threadpool(hash_password, req.pin)
    await database.execute(
        "UPDATE wallets SET withdrawal_pin = :pin, "
        "  pin_failed_count = 0, pin_locked_until = NULL "
        "WHERE user_id = :uid",
        {"pin": hashed_pin, "uid": user_id},
    )
    return {"message": "Withdrawal PIN set successfully"}


# ---------------------------------------------------------------------------
# Withdraw
# ---------------------------------------------------------------------------

@router.post("/withdraw")
async def withdraw(req: WithdrawRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    _rate_limit(f"withdraw:{user_id}", max_calls=10, window_sec=900)

    if req.idempotency_key:
        replay = await database.fetch_one(
            "SELECT response_json FROM withdraw_idempotency "
            "WHERE idempotency_key = :k AND user_id = :u",
            {"k": req.idempotency_key, "u": user_id},
        )
        if replay:
            payload = replay["response_json"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload

    row = await database.fetch_one(
        "SELECT balance, withdrawal_pin, pin_failed_count, pin_locked_until "
        "FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Wallet not found")

    wallet = dict(row)
    withdrawal_pin = wallet.get("withdrawal_pin")
    if not withdrawal_pin:
        raise HTTPException(status_code=403, detail="Withdrawal PIN not set. Please set a PIN first.")

    now = datetime.now(timezone.utc)
    locked_until = wallet.get("pin_locked_until")
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            remaining = int((locked_until - now).total_seconds())
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in {remaining} seconds.",
            )

    ok = await run_in_threadpool(verify_password, req.pin, withdrawal_pin)
    if not ok:
        failed = int(wallet.get("pin_failed_count") or 0) + 1
        if failed >= 5:
            lock = now + timedelta(minutes=15)
            await database.execute(
                "UPDATE wallets SET pin_failed_count = 0, pin_locked_until = :lu WHERE user_id = :uid",
                {"lu": lock, "uid": user_id},
            )
            raise HTTPException(status_code=429, detail="Too many failed attempts. Locked for 15 minutes.")
        await database.execute(
            "UPDATE wallets SET pin_failed_count = :c WHERE user_id = :uid",
            {"c": failed, "uid": user_id},
        )
        raise HTTPException(status_code=403, detail="Incorrect PIN")

    if (wallet.get("pin_failed_count") or 0) > 0:
        await database.execute(
            "UPDATE wallets SET pin_failed_count = 0 WHERE user_id = :uid",
            {"uid": user_id},
        )

    txn_id = f"wdr_{uuid.uuid4().hex[:12]}"

    try:
        async with database.transaction():
            new_balance = await database.fetch_val(
                """
                UPDATE wallets
                   SET balance = balance - :amt
                 WHERE user_id = :uid AND balance >= :amt
                RETURNING balance
                """,
                {"amt": req.amount, "uid": user_id},
            )
            if new_balance is None:
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

    response = {
        "message": "Withdrawal request received. Funds will be sent to your account once processing completes.",
        "status": "pending",
        "transaction_id": txn_id,
        "amount": req.amount,
        "new_balance": float(new_balance),
    }

    if req.idempotency_key:
        try:
            await database.execute(
                "INSERT INTO withdraw_idempotency (idempotency_key, user_id, response_json) VALUES (:k, :u, :r)",
                {"k": req.idempotency_key, "u": user_id, "r": json.dumps(response)},
            )
        except UniqueViolationError:
            pass

    return response


# ---------------------------------------------------------------------------
# Instant Pickup
# ---------------------------------------------------------------------------

@router.post("/instant-pickup")
async def instant_pickup(
    req: InstantPickupRequest,
    current_user: dict = Depends(get_current_user),
):
    shopper_id = current_user["id"]
    _rate_limit(f"pickup:{shopper_id}", max_calls=30, window_sec=60)
    quantity = req.quantity

    row = await database.fetch_one(
        """
        SELECT l.*, s.owner_id AS store_owner_id, s.name AS store_name
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
    store_name = listing.get("store_name") or "Store"
    listing_title = listing.get("title") or "Item"

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
            debited = await database.fetch_val(
                """
                UPDATE wallets
                   SET balance = balance - :amt
                 WHERE user_id = :uid AND balance >= :amt
                RETURNING balance
                """,
                {"amt": total, "uid": shopper_id},
            )
            if debited is None:
                raise HTTPException(status_code=400, detail="Insufficient balance")

            if available is not None:
                stock_after = await database.fetch_val(
                    """
                    UPDATE listings
                       SET quantity_available = quantity_available - :qty
                     WHERE listing_id = :lid AND quantity_available >= :qty
                    RETURNING quantity_available
                    """,
                    {"qty": quantity, "lid": req.listing_id},
                )
                if stock_after is None:
                    raise HTTPException(status_code=400, detail="Item just sold out. Please try again.")

            await database.execute(
                "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                {"amt": total, "uid": store_owner_id},
            )

            await _log_wallet_transaction(
                user_id=shopper_id,
                amount=total,
                type="debit",
                description=f"Instant pickup · {listing_title} ({quantity} item{'s' if quantity > 1 else ''})",
                reference=txn_id,
            )
            await _log_wallet_transaction(
                user_id=store_owner_id,
                amount=total,
                type="credit",
                description=f"Instant pickup payment · {listing_title} ({quantity} item{'s' if quantity > 1 else ''})",
                reference=f"{txn_id}:credit",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("instant_pickup failed shopper=%s", shopper_id)
        raise HTTPException(status_code=500, detail="Payment failed. Please try again.")

    # Notify storekeeper (in-app + push)
    _fire_and_forget(notify_user(
        store_owner_id,
        "order",
        "New instant pickup",
        f"{quantity} × {listing_title} — {store_name}",
        {"reference": txn_id, "kind": "pickup"},
    ))

    return {
        "message": "Payment successful",
        "transaction_id": txn_id,
        "amount": total,
        "quantity": quantity,
        "store_name": store_name,
        "listing_title": listing_title,
    }


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------

@router.post("/reserve")
async def reserve(req: ReserveRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    _rate_limit(f"reserve:{shopper_id}", max_calls=30, window_sec=60)
    quantity = req.quantity
    window_hours = max(3, min(168, int(req.pickup_window_hours or 3)))

    row = await database.fetch_one(
        """
        SELECT l.*, s.owner_id AS store_owner_id, s.name AS store_name
          FROM listings l
          JOIN stores   s ON l.store_id = s.store_id
         WHERE l.listing_id = :lid AND s.owner_id = :oid
        """,
        {"lid": req.listing_id, "oid": req.storekeeper_id},
    )
    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"Listing {req.listing_id} not found or not owned by storekeeper {req.storekeeper_id}",
        )

    listing = dict(row)
    store_owner_id = listing["store_owner_id"]
    store_name = listing.get("store_name") or "Store"
    listing_title = listing.get("title") or "Item"

    unit_price = float(listing.get("price") or 0)
    if unit_price <= 0:
        raise HTTPException(status_code=500, detail="Listing has no price configured")

    item_amount = round(unit_price * quantity, 2)
    total = round(item_amount + req.delivery_fee, 2)

    available = listing.get("quantity_available")
    if available is not None and available < quantity:
        raise HTTPException(status_code=400, detail=f"Only {available} items available")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=window_hours)

    try:
        async with database.transaction():
            existing = await database.fetch_one(
                "SELECT 1 AS x FROM escrow WHERE order_id = :oid AND order_store_id IS NULL",
                {"oid": req.order_id},
            )
            if existing:
                raise HTTPException(status_code=400, detail=f"Order {req.order_id} already reserved")

            debited = await database.fetch_val(
                """
                UPDATE wallets
                   SET balance = balance - :amt
                 WHERE user_id = :uid AND balance >= :amt
                RETURNING balance
                """,
                {"amt": total, "uid": shopper_id},
            )
            if debited is None:
                raise HTTPException(status_code=400, detail="Insufficient balance")

            if available is not None:
                stock_after = await database.fetch_val(
                    """
                    UPDATE listings
                       SET quantity_available = quantity_available - :qty
                     WHERE listing_id = :lid AND quantity_available >= :qty
                    RETURNING quantity_available
                    """,
                    {"qty": quantity, "lid": req.listing_id},
                )
                if stock_after is None:
                    raise HTTPException(status_code=400, detail="Item just sold out. Please try again.")

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
                description=f"Reservation · {listing_title} ({quantity} item{'s' if quantity > 1 else ''}, {window_hours}h window)",
                reference=req.order_id,
            )
    except HTTPException:
        raise
    except UniqueViolationError:
        raise HTTPException(status_code=400, detail=f"Order {req.order_id} already reserved")
    except Exception:
        logger.exception("reserve failed order=%s user=%s", req.order_id, shopper_id)
        raise HTTPException(status_code=500, detail="Could not create reservation. Please try again.")

    _fire_and_forget(notify_user(
        store_owner_id,
        "order",
        "New reservation",
        f"{quantity} × {listing_title} from {store_name} — awaiting your hold",
        {"order_id": req.order_id, "kind": "reservation"},
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
# Accept
# ---------------------------------------------------------------------------

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
        exists = await database.fetch_one("SELECT 1 AS x FROM escrow WHERE order_id = :oid", {"oid": req.order_id})
        if exists:
            raise HTTPException(status_code=403, detail="You are not the storekeeper for this order")
        raise HTTPException(status_code=404, detail="Order not found")

    flipped = await _flip_escrow_status(req.order_id, "locked", "accepted", storekeeper_id=user_id)
    if not flipped:
        return {"order_id": req.order_id, "status": "accepted", "message": "Already accepted."}

    _fire_and_forget(notify_user(
        escrow["shopper_id"],
        "order",
        "Reservation accepted",
        f"Your order #{req.order_id[:8]} is on hold. Pick it up soon.",
        {"order_id": req.order_id, "kind": "reservation_accepted"},
    ))

    return {"order_id": req.order_id, "status": "accepted", "message": "Reservation accepted"}


# ---------------------------------------------------------------------------
# Decline
# ---------------------------------------------------------------------------

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
        exists = await database.fetch_one("SELECT 1 AS x FROM escrow WHERE order_id = :oid", {"oid": req.order_id})
        if exists:
            raise HTTPException(status_code=403, detail="You are not the storekeeper for this order")
        raise HTTPException(status_code=404, detail="Order not found")

    current_status = (escrow["status"] or "").lower()
    if current_status != "locked":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot decline a reservation with status '{current_status}'. Only pending reservations can be declined.",
        )

    reason = (req.reason or "").strip()
    shopper_id = escrow["shopper_id"]
    total = float(escrow["total_amount"] or 0)
    listing_id = escrow["listing_id"]
    qty = escrow["quantity"] or 0
    short = req.order_id[:8]

    refunded = 0.0
    storekeeper_name_row = await database.fetch_one(
        "SELECT COALESCE(NULLIF(real_name,''), NULLIF(nickname,''), 'Store') AS n FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    storekeeper_label = (storekeeper_name_row["n"] if storekeeper_name_row else "Store") or "Store"

    try:
        async with database.transaction():
            flipped = await _flip_escrow_status(req.order_id, "locked", "declined", storekeeper_id=user_id)
            if not flipped:
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
    except UniqueViolationError:
        return {
            "order_id": req.order_id,
            "status": "declined",
            "refunded": 0,
            "reason": reason or None,
            "message": "Reservation was already processed.",
        }
    except Exception:
        logger.exception("decline failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not decline reservation.")

    # 🔔 Tell the shopper
    notify_body = f"{storekeeper_label} declined your order #{short}. ₦{refunded:,.0f} has been refunded to your wallet."
    if reason:
        notify_body += f" Reason: {reason}"
    _fire_and_forget(notify_user(
        shopper_id,
        "order",
        "Reservation declined",
        notify_body,
        {"order_id": req.order_id, "kind": "reservation_declined", "reason": reason or ""},
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
# Confirm / Dispatch / Return / Reversed (same as before, with notifications)
# ---------------------------------------------------------------------------

@router.post("/confirm")
async def confirm(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await _pick_escrow_row(req.order_id, shopper_id, req.order_store_id)

    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(status_code=400, detail="Order is not in a confirmable state")

    item_amount = float(escrow["item_amount"] or 0)
    delivery_fee = float(escrow["delivery_fee"] or 0)
    courier_id = escrow["courier_id"]
    storekeeper_id = escrow["storekeeper_id"]
    order_store_id = escrow.get("order_store_id")

    if courier_id in (shopper_id, storekeeper_id):
        courier_id = None

    try:
        async with database.transaction():
            flipped = await _flip_escrow_status(req.order_id, escrow["status"], "picked_up")
            if not flipped:
                flipped = await _flip_escrow_status(
                    req.order_id,
                    "accepted" if escrow["status"] == "locked" else "locked",
                    "picked_up",
                )
            if not flipped:
                return {"order_id": req.order_id, "status": "picked_up", "message": "Order was already processed."}

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
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:storekeeper",
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
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:courier",
                )
    except HTTPException:
        raise
    except UniqueViolationError:
        return {"order_id": req.order_id, "status": "picked_up", "message": "Order was already processed."}
    except Exception:
        logger.exception("confirm failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not confirm order.")

    _fire_and_forget(notify_user(
        storekeeper_id,
        "order",
        "Item picked up",
        f"Order #{req.order_id[:8]} picked up. ₦{item_amount:,.0f} credited to your wallet.",
        {"order_id": req.order_id, "kind": "order_picked_up"},
    ))

    return {"order_id": req.order_id, "status": "picked_up", "message": "Order marked as picked up. Funds released."}


@router.post("/dispatch")
async def dispatch(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await _pick_escrow_row(req.order_id, shopper_id, req.order_store_id)

    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(status_code=400, detail="Order must be accepted or locked to dispatch")

    item_amount = float(escrow["item_amount"] or 0)
    delivery_fee = float(escrow["delivery_fee"] or 0)
    courier_id = escrow["courier_id"]
    storekeeper_id = escrow["storekeeper_id"]
    order_store_id = escrow.get("order_store_id")

    if courier_id in (shopper_id, storekeeper_id):
        courier_id = None

    try:
        async with database.transaction():
            flipped = await _flip_escrow_status(req.order_id, escrow["status"], "dispatched")
            if not flipped:
                flipped = await _flip_escrow_status(
                    req.order_id,
                    "accepted" if escrow["status"] == "locked" else "locked",
                    "dispatched",
                )
            if not flipped:
                return {"order_id": req.order_id, "status": "dispatched", "message": "Order was already processed."}

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
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:storekeeper",
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
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:courier",
                )
    except HTTPException:
        raise
    except UniqueViolationError:
        return {"order_id": req.order_id, "status": "dispatched", "message": "Order was already processed."}
    except Exception:
        logger.exception("dispatch failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not dispatch order.")

    return {"order_id": req.order_id, "status": "dispatched", "message": "Order dispatched. Funds released."}


@router.post("/return")
async def return_order(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await _pick_escrow_row(req.order_id, shopper_id, req.order_store_id)

    if escrow["status"] not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Order must be in 'locked' or 'accepted' state to return")

    item_amount = float(escrow["item_amount"] or 0)
    order_store_id = escrow.get("order_store_id")
    storekeeper_id = escrow["storekeeper_id"]

    try:
        async with database.transaction():
            flipped = await _flip_escrow_status(req.order_id, escrow["status"], "returned")
            if not flipped:
                flipped = await _flip_escrow_status(
                    req.order_id,
                    "accepted" if escrow["status"] == "locked" else "locked",
                    "returned",
                )
            if not flipped:
                return {"order_id": req.order_id, "status": "returned", "message": "Order was already processed."}

            restored_items = 0
            if order_store_id is not None:
                store_row = await database.fetch_one(
                    "SELECT store_id FROM order_stores WHERE id = :osid",
                    {"osid": order_store_id},
                )
                if store_row:
                    items = await database.fetch_all(
                        "SELECT listing_id, quantity FROM order_items WHERE order_id = :oid AND store_id = :sid",
                        {"oid": req.order_id, "sid": store_row["store_id"]},
                    )
                    for item in items:
                        await database.execute(
                            "UPDATE listings SET quantity_available = quantity_available + :qty "
                            "WHERE listing_id = :lid AND quantity_available IS NOT NULL",
                            {"qty": item["quantity"], "lid": item["listing_id"]},
                        )
                        restored_items += 1

            if order_store_id is None:
                listing_id = escrow["listing_id"]
                qty = escrow["quantity"] or 0
                if listing_id and qty:
                    await database.execute(
                        "UPDATE listings SET quantity_available = quantity_available + :qty "
                        "WHERE listing_id = :lid AND quantity_available IS NOT NULL",
                        {"qty": qty, "lid": listing_id},
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
                    reference=f"{req.order_id}:{order_store_id or escrow['listing_id']}:refund",
                )
    except HTTPException:
        raise
    except UniqueViolationError:
        return {"order_id": req.order_id, "status": "returned", "message": "Order was already processed."}
    except Exception:
        logger.exception("return failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not process return.")

    _fire_and_forget(notify_user(
        storekeeper_id,
        "order",
        "Order returned",
        f"Order #{req.order_id[:8]} was returned by the shopper.",
        {"order_id": req.order_id, "kind": "order_returned"},
    ))

    return {"order_id": req.order_id, "status": "returned", "message": "Item cost refunded. Stock restored."}


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
        exists = await database.fetch_one("SELECT 1 AS x FROM escrow WHERE order_id = :oid", {"oid": req.order_id})
        if exists:
            raise HTTPException(status_code=403, detail="Only the storekeeper can release")
        raise HTTPException(status_code=404, detail="Order not found")

    if escrow["status"] != "returned":
        raise HTTPException(status_code=400, detail="Order not in returned state")

    delivery_fee = float(escrow["delivery_fee"] or 0)
    courier_id = escrow["courier_id"]
    shopper_id = escrow["shopper_id"]
    order_store_id = escrow.get("order_store_id")

    if courier_id in (storekeeper_id, shopper_id):
        courier_id = None

    try:
        async with database.transaction():
            flipped = await _flip_escrow_status(req.order_id, "returned", "reversed", storekeeper_id=storekeeper_id)
            if not flipped:
                return {"order_id": req.order_id, "status": "reversed", "message": "Already reversed."}

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
                    reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:reversed",
                )
    except HTTPException:
        raise
    except UniqueViolationError:
        return {"order_id": req.order_id, "status": "reversed", "message": "Already reversed."}
    except Exception:
        logger.exception("reversed_package failed order=%s", req.order_id)
        raise HTTPException(status_code=500, detail="Could not process reversal.")

    return {"order_id": req.order_id, "status": "reversed", "message": "Courier fee released"}


# ---------------------------------------------------------------------------
# Single-transaction detail — now with store/business names
# ---------------------------------------------------------------------------

@router.get("/transaction/{reference}")
async def get_wallet_transaction_detail(
    reference: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        """
        SELECT id, user_id, amount, type, description, reference, status, created_at
          FROM wallet_transactions
         WHERE reference = :ref AND user_id = :uid
         LIMIT 1
        """,
        {"ref": reference, "uid": current_user["id"]},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")

    txn = dict(row)
    opposite_type = "credit" if txn["type"] == "debit" else "debit"

    candidates: set[str] = {reference}

    if reference.startswith("pickup_"):
        if reference.endswith(":credit"):
            candidates.add(reference[: -len(":credit")])
        else:
            candidates.add(reference + ":credit")

    candidates.update(_booking_link_candidates(reference))

    base_like: Optional[str] = None
    m = re.match(r"^decline:(ord_[a-z0-9]+):([a-f0-9]+)$", reference)
    if m:
        base_like = m.group(1)
        candidates.add(base_like)

    if reference.startswith("ord_"):
        base = reference.split(":", 1)[0]
        base_like = base
        candidates.add(base)

    ref_list = list(candidates)
    placeholders = ", ".join(f":r{i}" for i in range(len(ref_list)))
    params: dict = {f"r{i}": r for i, r in enumerate(ref_list)}
    params["opp"] = opposite_type
    params["uid"] = current_user["id"]

    extra = ""
    if base_like:
        extra = " OR w.reference LIKE :base_like"
        params["base_like"] = f"{base_like}%"

    paired = await database.fetch_one(
        f"""
        SELECT w.user_id
          FROM wallet_transactions w
         WHERE (w.reference IN ({placeholders}){extra})
           AND w.type = :opp
           AND w.user_id != :uid
         ORDER BY w.created_at ASC NULLS LAST
         LIMIT 1
        """,
        params,
    )

    counterparty = None
    if paired:
        counterparty = await _lookup_counterparty_user(paired["user_id"])

    return {**txn, "counterparty": counterparty}


# ---------------------------------------------------------------------------
# Instant transactions feed — powers the Saved screen tabs
# ---------------------------------------------------------------------------

@router.get("/instant-transactions")
async def get_instant_transactions(current_user: dict = Depends(get_current_user)):
    """
    Returns two lists for the shopper:
      - pickups: wallet debits from instant pickups (reference LIKE pickup_%)
      - services: wallet debits from instant service pay (reference LIKE svcpay_%)

    NOTE: all string literals containing a colon (e.g. ':credit') MUST be
    passed as bound parameters. The `databases` library scans the whole SQL
    string with a `:name` regex and would otherwise try to bind `:credit`
    as a parameter, which fails with IndeterminateDatatypeError.
    """
    user_id = current_user["id"]

    pickups_rows = await database.fetch_all(
        """
        SELECT w.id, w.reference, w.amount, w.description, w.status, w.created_at,
               wc.user_id AS counterparty_user_id
          FROM wallet_transactions w
          LEFT JOIN wallet_transactions wc
                 ON wc.reference = (w.reference || :credit_suffix)
                AND wc.type = 'credit'
         WHERE w.user_id = :uid
           AND w.reference LIKE :prefix
           AND w.reference NOT LIKE :not_suffix
           AND w.type = 'debit'
         ORDER BY w.created_at DESC
         LIMIT 50
        """,
        {
            "uid": user_id,
            "credit_suffix": ":credit",
            "prefix": "pickup\\_%",     # escaped _ so LIKE treats it literally
            "not_suffix": "%:credit",
        },
    )

    service_rows = await database.fetch_all(
        """
        SELECT w.id, w.reference, w.amount, w.description, w.status, w.created_at,
               wc.user_id AS counterparty_user_id
          FROM wallet_transactions w
          LEFT JOIN wallet_transactions wc
                 ON wc.reference = w.reference
                AND wc.type = 'credit'
                AND wc.user_id != w.user_id
         WHERE w.user_id = :uid
           AND w.reference LIKE :prefix
           AND w.type = 'debit'
         ORDER BY w.created_at DESC
         LIMIT 50
        """,
        {
            "uid": user_id,
            "prefix": "svcpay\\_%",
        },
    )

    async def enrich(rows) -> list:
        out = []
        for r in rows:
            d = dict(r)
            cp_user_id = d.pop("counterparty_user_id", None)
            cp = await _lookup_counterparty_user(cp_user_id) if cp_user_id else None
            d["counterparty"] = cp
            out.append(d)
        return out

    pickups = await enrich(pickups_rows)
    services = await enrich(service_rows)

    return {"pickups": pickups, "services": services}


# ---------------------------------------------------------------------------
# Reads — lists
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
                   NULLIF(u.nickname, ''), NULLIF(u.real_name, ''),
                   NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''),
                   NULLIF(u.phone, ''), 'Customer'
               ) AS customer_name,
               u.avatar_url AS customer_avatar,
               COALESCE(
                   NULLIF(sk.nickname, ''), NULLIF(sk.real_name, ''),
                   NULLIF(TRIM(CONCAT_WS(' ', sk.first_name, sk.last_name)), ''),
                   NULLIF(sk.phone, ''), 'Storekeeper'
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