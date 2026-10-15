import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request, Header
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from asyncpg.exceptions import UniqueViolationError

from app.db.database import database
from app.utils.security import get_current_user
from app.routes.notifications import send_push_to_user
from app.routes.auth import hash_password, verify_password
from app.services import anchor

logger = logging.getLogger(__name__)

router          = APIRouter(prefix="/wallet",   tags=["Wallet"])
webhook_router  = APIRouter(prefix="/webhooks", tags=["Webhooks"])


# ═══════════════════════════════════════════════════════════════════
# Rate limiter (unchanged)
# ═══════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════

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
    bank_code: str = Field(..., min_length=2, max_length=10)
    account_number: str = Field(..., min_length=10, max_length=10)
    account_name: Optional[str] = Field(None, max_length=120)
    pin: str = Field(..., min_length=4, max_length=6)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=64)


class InstantPickupRequest(BaseModel):
    listing_id: str = Field(..., min_length=1, max_length=128)
    storekeeper_id: str = Field(..., min_length=1, max_length=128)
    quantity: int = Field(1, ge=1, le=1000)


class DifferentiateRequest(BaseModel):
    from_bucket: str = Field(..., pattern="^(shopping|selling|services)$")
    to_bucket:   str = Field(..., pattern="^(shopping|selling|services)$")
    amount: float = Field(..., gt=0)


# ═══════════════════════════════════════════════════════════════════
# Helpers — notifications, background tasks
# ═══════════════════════════════════════════════════════════════════

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
    try:
        await database.execute(
            """
            INSERT INTO user_notifications
                (user_id, kind, title, body, data, is_read, created_at)
            VALUES (:uid, :kind, :title, :body, :data, FALSE, NOW())
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
        logger.warning("user_notifications write failed user=%s kind=%s err=%s",
                       user_id, kind, e)

    _fire_and_forget(send_push_to_user(user_id, title, body, data or {}))


# ═══════════════════════════════════════════════════════════════════
# Anchor helpers
# ═══════════════════════════════════════════════════════════════════

async def _require_anchor_ready() -> None:
    """Fail fast with a helpful message if Anchor isn't set up."""
    if not anchor.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Wallet is being migrated to virtual accounts. "
                "Payments will be enabled shortly."
            ),
        )


async def _get_user_sub_account(user_id: str) -> str:
    row = await database.fetch_one(
        "SELECT anchor_sub_account_id FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if not row or not row["anchor_sub_account_id"]:
        raise HTTPException(
            status_code=400,
            detail="Your wallet is not set up yet. Please complete KYC or try again.",
        )
    return row["anchor_sub_account_id"]


async def _get_escrow_holding_account() -> str:
    row = await database.fetch_one(
        "SELECT value FROM app_settings WHERE key = 'escrow_holding_sub_account_id'",
    )
    if not row or not row["value"]:
        raise HTTPException(
            status_code=503,
            detail="Escrow holding account not configured.",
        )
    return row["value"]


# ─── Ledger (display-only buckets) ───────────────────────────────

async def _ledger_total(user_id: str) -> float:
    row = await database.fetch_one(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM user_ledger WHERE user_id = :uid",
        {"uid": user_id},
    )
    return float(row["total"] or 0)


async def _credit_ledger(user_id: str, bucket: str, amount: float) -> None:
    await database.execute(
        """
        INSERT INTO user_ledger (user_id, bucket, amount, updated_at)
        VALUES (:uid, :b, :amt, NOW())
        ON CONFLICT (user_id, bucket)
        DO UPDATE SET amount = user_ledger.amount + :amt, updated_at = NOW()
        """,
        {"uid": user_id, "b": bucket, "amt": float(amount)},
    )


async def _debit_ledger(user_id: str, bucket: str, amount: float) -> None:
    # Fails loudly if insufficient — caller should have checked first
    result = await database.fetch_val(
        """
        UPDATE user_ledger
           SET amount = amount - :amt, updated_at = NOW()
         WHERE user_id = :uid AND bucket = :b AND amount >= :amt
        RETURNING amount
        """,
        {"uid": user_id, "b": bucket, "amt": float(amount)},
    )
    if result is None:
        raise HTTPException(status_code=400, detail="Insufficient balance in this bucket")


# ─── Money movement (book transfer between Admerce sub-accounts) ─

async def _book_transfer(
    *,
    from_sub_account_id: str,
    to_sub_account_id: str,
    amount: float,
    reference: str,
    reason: str,
) -> dict:
    """Wrapper that also records the pending transfer for reconciliation."""
    result = await anchor.book_transfer(
        from_sub_account_id=from_sub_account_id,
        to_sub_account_id=to_sub_account_id,
        amount=amount,
        reference=reference,
        reason=reason,
    )

    transfer_id = (result or {}).get("transfer_id") or (result or {}).get("id")

    await database.execute(
        """
        INSERT INTO anchor_pending_transfers
            (reference, transfer_id, kind, from_account, to_account, amount, status)
        VALUES (:ref, :tid, 'book', :from_, :to_, :amt, 'pending')
        ON CONFLICT (reference) DO NOTHING
        """,
        {
            "ref": reference,
            "tid": transfer_id,
            "from_": from_sub_account_id,
            "to_": to_sub_account_id,
            "amt": float(amount),
        },
    )

    return result or {}


# ═══════════════════════════════════════════════════════════════════
# Escrow status helpers (unchanged from original)
# ═══════════════════════════════════════════════════════════════════

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
    anchor_transfer_id: Optional[str] = None,
):
    if amount <= 0 or not user_id:
        logger.warning("Skipping wallet txn log: amount=%s user=%s", amount, user_id)
        return
    await database.execute(
        """
        INSERT INTO wallet_transactions
            (user_id, amount, type, description, reference, status,
             anchor_transfer_id, created_at)
        VALUES
            (:uid, :amt, :type, :desc, :ref, :status, :atid, NOW())
        """,
        {
            "uid": user_id,
            "amt": float(amount),
            "type": type,
            "desc": description,
            "ref": reference,
            "status": status,
            "atid": anchor_transfer_id,
        },
    )


# ═══════════════════════════════════════════════════════════════════
# Wallet setup — one sub-account per user
# ═══════════════════════════════════════════════════════════════════

@router.post("/create")
async def create_wallet(current_user: dict = Depends(get_current_user)):
    """
    Idempotent: ensures this user has an Anchor sub-account with a virtual NUBAN.
    Safe to call repeatedly.
    """
    await _require_anchor_ready()

    user_id = current_user["id"]

    row = await database.fetch_one(
        """
        SELECT email, nickname, real_name, first_name, last_name, phone,
               anchor_customer_id, anchor_sub_account_id, anchor_virtual_nuban
          FROM users WHERE id = :uid
        """,
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if row["anchor_sub_account_id"]:
        return {
            "user_id": user_id,
            "sub_account_id": row["anchor_sub_account_id"],
            "virtual_nuban": row["anchor_virtual_nuban"],
            "message": "Wallet already exists",
        }

    display_name = (
        (row["real_name"] or "").strip()
        or (row["nickname"] or "").strip()
        or f"{(row['first_name'] or '').strip()} {(row['last_name'] or '').strip()}".strip()
        or "Admerce User"
    )
    email = row["email"] or f"{user_id}@admerce.local"

    # 1. Create customer (if we don't have one yet)
    customer_id = row["anchor_customer_id"]
    if not customer_id:
        cust = await anchor.create_customer(
            email=email,
            name=display_name,
            phone=row["phone"],
            metadata={"admerce_user_id": user_id},
        )
        customer_id = cust.get("customer_id") or cust.get("id")
        if not customer_id:
            raise HTTPException(status_code=502, detail="Could not create customer")
        await database.execute(
            "UPDATE users SET anchor_customer_id = :cid WHERE id = :uid",
            {"cid": customer_id, "uid": user_id},
        )

    # 2. Create sub-account with virtual NUBAN
    sub = await anchor.create_sub_account(
        customer_id=customer_id,
        metadata={"admerce_user_id": user_id},
    )
    sub_account_id = sub.get("sub_account_id") or sub.get("id")
    virtual_nuban  = sub.get("virtual_nuban") or sub.get("account_number")

    if not sub_account_id or not virtual_nuban:
        raise HTTPException(status_code=502, detail="Could not create sub-account")

    await database.execute(
        """
        UPDATE users
           SET anchor_sub_account_id = :sa,
               anchor_virtual_nuban  = :vn,
               anchor_sub_account_at = NOW()
         WHERE id = :uid
        """,
        {"sa": sub_account_id, "vn": virtual_nuban, "uid": user_id},
    )

    # Seed ledger buckets
    await database.execute(
        """
        INSERT INTO user_ledger (user_id, bucket, amount)
        VALUES (:uid,'shopping',0),(:uid,'selling',0),(:uid,'services',0)
        ON CONFLICT DO NOTHING
        """,
        {"uid": user_id},
    )

    return {
        "user_id": user_id,
        "sub_account_id": sub_account_id,
        "virtual_nuban": virtual_nuban,
        "bank_name": sub.get("bank_name"),
        "message": "Wallet created. Transfer into the virtual NUBAN to fund.",
    }


# ═══════════════════════════════════════════════════════════════════
# Balance + ledger
# ═══════════════════════════════════════════════════════════════════

@router.get("/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
    """
    Returns total balance (from ledger) plus per-bucket breakdown plus
    the user's virtual NUBAN (so the UI can display it for top-ups).
    """
    user_id = current_user["id"]

    rows = await database.fetch_all(
        "SELECT bucket, amount FROM user_ledger WHERE user_id = :uid",
        {"uid": user_id},
    )
    buckets = {"shopping": 0.0, "selling": 0.0, "services": 0.0}
    for r in rows:
        buckets[r["bucket"]] = float(r["amount"] or 0)

    total = sum(buckets.values())

    user_row = await database.fetch_one(
        "SELECT anchor_virtual_nuban FROM users WHERE id = :uid",
        {"uid": user_id},
    )

    return {
        "user_id": user_id,
        "balance": total,
        "buckets": buckets,
        "virtual_nuban": user_row["anchor_virtual_nuban"] if user_row else None,
    }


@router.post("/differentiate")
async def differentiate(
    req: DifferentiateRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Move an amount between display buckets. No Anchor call — this is UI-only.
    """
    if req.from_bucket == req.to_bucket:
        raise HTTPException(status_code=400, detail="Buckets must differ")

    user_id = current_user["id"]

    await _debit_ledger(user_id, req.from_bucket, req.amount)
    await _credit_ledger(user_id, req.to_bucket, req.amount)

    return {
        "from": req.from_bucket,
        "to": req.to_bucket,
        "amount": req.amount,
        "message": "Buckets updated",
    }


# ═══════════════════════════════════════════════════════════════════
# Legacy topup — still 410
# ═══════════════════════════════════════════════════════════════════

@router.post("/topup")
async def topup(current_user: dict = Depends(get_current_user)):
    raise HTTPException(
        status_code=410,
        detail="Direct top-up is retired. Transfer into your Admerce virtual NUBAN.",
    )


# ═══════════════════════════════════════════════════════════════════
# Withdrawal PIN (kept as 2FA on withdraw)
# ═══════════════════════════════════════════════════════════════════

@router.post("/set-pin")
async def set_pin(req: SetPinRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    _rate_limit(f"set-pin:{user_id}", max_calls=10, window_sec=900)

    wallet = await database.fetch_one(
        "SELECT withdrawal_pin FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not wallet:
        # Create a minimal wallets row if missing
        await database.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0) ON CONFLICT DO NOTHING",
            {"uid": user_id},
        )
        existing_pin = None
    else:
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


# ═══════════════════════════════════════════════════════════════════
# Withdraw — NIP transfer from sub-account to external bank
# ═══════════════════════════════════════════════════════════════════

@router.post("/withdraw")
async def withdraw(req: WithdrawRequest, current_user: dict = Depends(get_current_user)):
    await _require_anchor_ready()
    user_id = current_user["id"]
    _rate_limit(f"withdraw:{user_id}", max_calls=10, window_sec=900)

    # Idempotency replay
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

    # PIN check
    row = await database.fetch_one(
        "SELECT withdrawal_pin, pin_failed_count, pin_locked_until "
        "FROM wallets WHERE user_id = :uid",
        {"uid": user_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Wallet not found")

    wallet = dict(row)
    withdrawal_pin = wallet.get("withdrawal_pin")
    if not withdrawal_pin:
        raise HTTPException(status_code=403, detail="Withdrawal PIN not set.")

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
            raise HTTPException(status_code=429, detail="Locked for 15 minutes.")
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

    # Balance check
    total = await _ledger_total(user_id)
    if total < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # Counterparty cache
    sub_account_id = await _get_user_sub_account(user_id)

    cp_row = await database.fetch_one(
        """
        SELECT anchor_counterparty_id FROM anchor_counterparties
         WHERE user_id = :uid AND bank_code = :bc AND account_number = :an
        """,
        {"uid": user_id, "bc": req.bank_code, "an": req.account_number},
    )

    counterparty_id = cp_row["anchor_counterparty_id"] if cp_row else None

    if not counterparty_id:
        cp = await anchor.create_counterparty(
            bank_code=req.bank_code,
            account_number=req.account_number,
            account_name=req.account_name,
        )
        counterparty_id = cp.get("counterparty_id") or cp.get("id")
        if not counterparty_id:
            raise HTTPException(status_code=502, detail="Could not create counterparty")
        await database.execute(
            """
            INSERT INTO anchor_counterparties
                (user_id, bank_code, account_number, account_name, anchor_counterparty_id)
            VALUES (:uid, :bc, :an, :name, :cpid)
            ON CONFLICT (user_id, bank_code, account_number) DO UPDATE
              SET anchor_counterparty_id = EXCLUDED.anchor_counterparty_id
            """,
            {
                "uid": user_id,
                "bc": req.bank_code,
                "an": req.account_number,
                "name": req.account_name,
                "cpid": counterparty_id,
            },
        )

    txn_id = f"wdr_{uuid.uuid4().hex[:12]}"

    # Initiate NIP transfer
    result = await anchor.nip_transfer(
        from_sub_account_id=sub_account_id,
        counterparty_id=counterparty_id,
        amount=req.amount,
        reference=txn_id,
        reason="Wallet withdrawal",
    )
    transfer_id = (result or {}).get("transfer_id") or (result or {}).get("id")

    # Debit ledger (spread across buckets in priority order: selling → services → shopping)
    remaining = req.amount
    for bucket in ("selling", "services", "shopping"):
        if remaining <= 0:
            break
        bucket_row = await database.fetch_one(
            "SELECT amount FROM user_ledger WHERE user_id = :uid AND bucket = :b",
            {"uid": user_id, "b": bucket},
        )
        avail = float(bucket_row["amount"] or 0) if bucket_row else 0
        take = min(avail, remaining)
        if take > 0:
            await _debit_ledger(user_id, bucket, take)
            remaining -= take

    # Audit row
    await _log_wallet_transaction(
        user_id=user_id,
        amount=req.amount,
        type="debit",
        description=f"Withdrawal to {req.bank_code} •••• {req.account_number[-4:]}",
        reference=txn_id,
        status="pending",
        anchor_transfer_id=transfer_id,
    )

    # Queue for reconciliation
    await database.execute(
        """
        INSERT INTO anchor_pending_transfers
            (reference, transfer_id, kind, from_account, to_account, amount, status)
        VALUES (:ref, :tid, 'nip', :from_, :to_, :amt, 'pending')
        ON CONFLICT (reference) DO NOTHING
        """,
        {
            "ref": txn_id,
            "tid": transfer_id,
            "from_": sub_account_id,
            "to_": counterparty_id,
            "amt": req.amount,
        },
    )

    response = {
        "message": "Withdrawal initiated. Funds will arrive at your bank shortly.",
        "status": "pending",
        "transaction_id": txn_id,
        "anchor_transfer_id": transfer_id,
        "amount": req.amount,
    }

    if req.idempotency_key:
        try:
            await database.execute(
                "INSERT INTO withdraw_idempotency "
                "(idempotency_key, user_id, response_json) VALUES (:k, :u, :r)",
                {"k": req.idempotency_key, "u": user_id, "r": json.dumps(response)},
            )
        except UniqueViolationError:
            pass

    return response


# ═══════════════════════════════════════════════════════════════════
# Instant Pickup — direct book transfer shopper → storekeeper
# ═══════════════════════════════════════════════════════════════════

@router.post("/instant-pickup")
async def instant_pickup(
    req: InstantPickupRequest,
    current_user: dict = Depends(get_current_user),
):
    await _require_anchor_ready()
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
    store_name     = listing.get("store_name") or "Store"
    listing_title  = listing.get("title") or "Item"

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

    # Balance check
    shopping_row = await database.fetch_one(
        "SELECT amount FROM user_ledger WHERE user_id = :uid AND bucket='shopping'",
        {"uid": shopper_id},
    )
    shopping_balance = float(shopping_row["amount"] or 0) if shopping_row else 0
    if shopping_balance < total:
        raise HTTPException(status_code=400, detail="Insufficient shopping balance")

    txn_id = f"pickup_{uuid.uuid4().hex[:12]}"

    # Stock decrement
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
            raise HTTPException(status_code=400, detail="Item just sold out.")

    # Book transfer shopper → storekeeper
    shopper_sub = await _get_user_sub_account(shopper_id)
    seller_sub  = await _get_user_sub_account(store_owner_id)

    result = await _book_transfer(
        from_sub_account_id=shopper_sub,
        to_sub_account_id=seller_sub,
        amount=total,
        reference=txn_id,
        reason=f"Instant pickup — {listing_title}",
    )
    transfer_id = (result or {}).get("transfer_id")

    # Ledger
    await _debit_ledger(shopper_id, "shopping", total)
    await _credit_ledger(store_owner_id, "selling", total)

    # Audit
    await _log_wallet_transaction(
        user_id=shopper_id,
        amount=total,
        type="debit",
        description=f"Instant pickup · {listing_title} ({quantity} item{'s' if quantity > 1 else ''})",
        reference=txn_id,
        status="completed",
        anchor_transfer_id=transfer_id,
    )
    await _log_wallet_transaction(
        user_id=store_owner_id,
        amount=total,
        type="credit",
        description=f"Instant pickup payment · {listing_title} ({quantity} item{'s' if quantity > 1 else ''})",
        reference=f"{txn_id}:credit",
        status="completed",
        anchor_transfer_id=transfer_id,
    )

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


# ═══════════════════════════════════════════════════════════════════
# Reserve — book transfer shopper → escrow holding
# ═══════════════════════════════════════════════════════════════════

@router.post("/reserve")
async def reserve(req: ReserveRequest, current_user: dict = Depends(get_current_user)):
    await _require_anchor_ready()
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
    store_name     = listing.get("store_name") or "Store"
    listing_title  = listing.get("title") or "Item"

    unit_price = float(listing.get("price") or 0)
    if unit_price <= 0:
        raise HTTPException(status_code=500, detail="Listing has no price configured")

    item_amount = round(unit_price * quantity, 2)
    total       = round(item_amount + req.delivery_fee, 2)

    available = listing.get("quantity_available")
    if available is not None and available < quantity:
        raise HTTPException(status_code=400, detail=f"Only {available} items available")

    # Bucket check
    shopping_row = await database.fetch_one(
        "SELECT amount FROM user_ledger WHERE user_id = :uid AND bucket='shopping'",
        {"uid": shopper_id},
    )
    if (shopping_row["amount"] or 0) < total:
        raise HTTPException(status_code=400, detail="Insufficient shopping balance")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=window_hours)

    # Duplicate check
    existing = await database.fetch_one(
        "SELECT 1 AS x FROM escrow WHERE order_id = :oid AND order_store_id IS NULL",
        {"oid": req.order_id},
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"Order {req.order_id} already reserved")

    # Stock decrement
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
            raise HTTPException(status_code=400, detail="Item just sold out.")

    # Move money: shopper sub-account → escrow holding sub-account
    shopper_sub = await _get_user_sub_account(shopper_id)
    escrow_sub  = await _get_escrow_holding_account()

    transfer_ref = f"reserve:{req.order_id}"
    result = await _book_transfer(
        from_sub_account_id=shopper_sub,
        to_sub_account_id=escrow_sub,
        amount=total,
        reference=transfer_ref,
        reason=f"Escrow hold — {listing_title}",
    )
    transfer_id = (result or {}).get("transfer_id")

    # Insert escrow row
    try:
        await database.execute(
            """
            INSERT INTO escrow (
                order_id, shopper_id, storekeeper_id, courier_id,
                listing_id, quantity, item_amount, delivery_fee, total_amount,
                status, expires_at, created_at, anchor_transfer_id
            )
            VALUES (
                :order_id, :shopper_id, :storekeeper_id, NULL,
                :listing_id, :quantity, :item_amount, :delivery_fee, :total_amount,
                'locked', :expires_at, :created_at, :atid
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
                "atid": transfer_id,
            },
        )
    except UniqueViolationError:
        raise HTTPException(status_code=400, detail=f"Order {req.order_id} already reserved")

    # Ledger debit
    await _debit_ledger(shopper_id, "shopping", total)

    # Audit
    await _log_wallet_transaction(
        user_id=shopper_id,
        amount=total,
        type="debit",
        description=f"Reservation · {listing_title} ({quantity} item{'s' if quantity > 1 else ''}, {window_hours}h window)",
        reference=req.order_id,
        status="completed",
        anchor_transfer_id=transfer_id,
    )

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


# ═══════════════════════════════════════════════════════════════════
# Accept — state only, no Anchor call
# ═══════════════════════════════════════════════════════════════════

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
        exists = await database.fetch_one(
            "SELECT 1 AS x FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
        )
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


# ═══════════════════════════════════════════════════════════════════
# Decline — book transfer escrow → shopper
# ═══════════════════════════════════════════════════════════════════

@router.post("/decline")
async def decline_reservation(
    req: DeclineRequest,
    current_user: dict = Depends(get_current_user),
):
    await _require_anchor_ready()
    user_id = current_user["id"]

    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND storekeeper_id = :uid",
        {"oid": req.order_id, "uid": user_id},
    )
    if not escrow:
        exists = await database.fetch_one(
            "SELECT 1 AS x FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
        )
        if exists:
            raise HTTPException(status_code=403, detail="You are not the storekeeper for this order")
        raise HTTPException(status_code=404, detail="Order not found")

    if (escrow["status"] or "").lower() != "locked":
        raise HTTPException(status_code=400, detail="Only pending reservations can be declined.")

    reason     = (req.reason or "").strip()
    shopper_id = escrow["shopper_id"]
    total      = float(escrow["total_amount"] or 0)
    listing_id = escrow["listing_id"]
    qty        = escrow["quantity"] or 0
    short      = req.order_id[:8]

    storekeeper_name_row = await database.fetch_one(
        "SELECT COALESCE(NULLIF(real_name,''), NULLIF(nickname,''), 'Store') AS n FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    storekeeper_label = (storekeeper_name_row["n"] if storekeeper_name_row else "Store") or "Store"

    flipped = await _flip_escrow_status(req.order_id, "locked", "declined", storekeeper_id=user_id)
    if not flipped:
        return {
            "order_id": req.order_id,
            "status": "declined",
            "refunded": 0,
            "reason": reason or None,
            "message": "Reservation was already processed.",
        }

    # Restore stock
    if listing_id and qty:
        await database.execute(
            "UPDATE listings SET quantity_available = quantity_available + :qty "
            "WHERE listing_id = :lid AND quantity_available IS NOT NULL",
            {"qty": qty, "lid": listing_id},
        )

    # Refund: escrow holding → shopper
    refunded = 0.0
    if total > 0 and shopper_id:
        escrow_sub  = await _get_escrow_holding_account()
        shopper_sub = await _get_user_sub_account(shopper_id)

        result = await _book_transfer(
            from_sub_account_id=escrow_sub,
            to_sub_account_id=shopper_sub,
            amount=total,
            reference=f"decline:{req.order_id}:{user_id[:8]}",
            reason=f"Refund — declined reservation {short}",
        )
        transfer_id = (result or {}).get("transfer_id")
        refunded = total

        await _credit_ledger(shopper_id, "shopping", total)

        audit_desc = f"Refund: reservation {short} declined by store"
        if reason:
            audit_desc += f" — {reason}"
        await _log_wallet_transaction(
            user_id=shopper_id,
            amount=total,
            type="credit",
            description=audit_desc,
            reference=f"decline:{req.order_id}:{user_id[:8]}",
            status="completed",
            anchor_transfer_id=transfer_id,
        )

    notify_body = f"{storekeeper_label} declined your order #{short}. ₦{refunded:,.0f} has been refunded."
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


# ═══════════════════════════════════════════════════════════════════
# Confirm / Dispatch — book transfers escrow → storekeeper (+ courier)
# ═══════════════════════════════════════════════════════════════════

async def _release_escrow_to_parties(
    escrow: dict,
    order_id: str,
    *,
    final_status: str,
    shopper_id: str,
) -> None:
    """
    Shared logic for confirm + dispatch:
      - book transfer item_amount escrow → storekeeper
      - book transfer delivery_fee escrow → courier (if any)
      - ledger credits
    """
    item_amount    = float(escrow["item_amount"] or 0)
    delivery_fee   = float(escrow["delivery_fee"] or 0)
    storekeeper_id = escrow["storekeeper_id"]
    courier_id     = escrow.get("courier_id")
    order_store_id = escrow.get("order_store_id")

    if courier_id in (shopper_id, storekeeper_id):
        courier_id = None

    escrow_sub = await _get_escrow_holding_account()

    # Pay the storekeeper
    if item_amount > 0:
        seller_sub = await _get_user_sub_account(storekeeper_id)
        result = await _book_transfer(
            from_sub_account_id=escrow_sub,
            to_sub_account_id=seller_sub,
            amount=item_amount,
            reference=f"{order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:storekeeper",
            reason=f"Order release — {order_id[:8]}",
        )
        transfer_id = (result or {}).get("transfer_id")

        await _credit_ledger(storekeeper_id, "selling", item_amount)
        await _log_wallet_transaction(
            user_id=storekeeper_id,
            amount=item_amount,
            type="credit",
            description=f"Order #{order_id[:8]} released",
            reference=f"{order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:storekeeper",
            status="completed",
            anchor_transfer_id=transfer_id,
        )

    # Pay the courier
    if delivery_fee > 0 and courier_id:
        courier_sub = await _get_user_sub_account(courier_id)
        result = await _book_transfer(
            from_sub_account_id=escrow_sub,
            to_sub_account_id=courier_sub,
            amount=delivery_fee,
            reference=f"{order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:courier",
            reason=f"Delivery fee — {order_id[:8]}",
        )
        transfer_id = (result or {}).get("transfer_id")

        await _credit_ledger(courier_id, "services", delivery_fee)
        await _log_wallet_transaction(
            user_id=courier_id,
            amount=delivery_fee,
            type="credit",
            description=f"Delivery fee for #{order_id[:8]}",
            reference=f"{order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:courier",
            status="completed",
            anchor_transfer_id=transfer_id,
        )


@router.post("/confirm")
async def confirm(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    await _require_anchor_ready()
    shopper_id = current_user["id"]
    escrow = await _pick_escrow_row(req.order_id, shopper_id, req.order_store_id)

    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(status_code=400, detail="Order is not in a confirmable state")

    flipped = await _flip_escrow_status(req.order_id, escrow["status"], "picked_up")
    if not flipped:
        flipped = await _flip_escrow_status(
            req.order_id,
            "accepted" if escrow["status"] == "locked" else "locked",
            "picked_up",
        )
    if not flipped:
        return {"order_id": req.order_id, "status": "picked_up", "message": "Order was already processed."}

    await _release_escrow_to_parties(
        escrow, req.order_id, final_status="picked_up", shopper_id=shopper_id,
    )

    _fire_and_forget(notify_user(
        escrow["storekeeper_id"],
        "order",
        "Item picked up",
        f"Order #{req.order_id[:8]} picked up. ₦{float(escrow['item_amount'] or 0):,.0f} credited.",
        {"order_id": req.order_id, "kind": "order_picked_up"},
    ))

    return {"order_id": req.order_id, "status": "picked_up", "message": "Funds released."}


@router.post("/dispatch")
async def dispatch(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    await _require_anchor_ready()
    shopper_id = current_user["id"]
    escrow = await _pick_escrow_row(req.order_id, shopper_id, req.order_store_id)

    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(status_code=400, detail="Order must be accepted or locked to dispatch")

    flipped = await _flip_escrow_status(req.order_id, escrow["status"], "dispatched")
    if not flipped:
        flipped = await _flip_escrow_status(
            req.order_id,
            "accepted" if escrow["status"] == "locked" else "locked",
            "dispatched",
        )
    if not flipped:
        return {"order_id": req.order_id, "status": "dispatched", "message": "Order was already processed."}

    await _release_escrow_to_parties(
        escrow, req.order_id, final_status="dispatched", shopper_id=shopper_id,
    )

    return {"order_id": req.order_id, "status": "dispatched", "message": "Funds released."}


# ═══════════════════════════════════════════════════════════════════
# Return — book transfer escrow → shopper (item_amount only)
# ═══════════════════════════════════════════════════════════════════

@router.post("/return")
async def return_order(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    await _require_anchor_ready()
    shopper_id = current_user["id"]
    escrow = await _pick_escrow_row(req.order_id, shopper_id, req.order_store_id)

    if escrow["status"] not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Order must be in 'locked' or 'accepted' state to return")

    item_amount    = float(escrow["item_amount"] or 0)
    storekeeper_id = escrow["storekeeper_id"]
    order_store_id = escrow.get("order_store_id")

    flipped = await _flip_escrow_status(req.order_id, escrow["status"], "returned")
    if not flipped:
        flipped = await _flip_escrow_status(
            req.order_id,
            "accepted" if escrow["status"] == "locked" else "locked",
            "returned",
        )
    if not flipped:
        return {"order_id": req.order_id, "status": "returned", "message": "Order was already processed."}

    # Restore stock
    if order_store_id is not None:
        store_row = await database.fetch_one(
            "SELECT store_id FROM order_stores WHERE id = :osid", {"osid": order_store_id},
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
    else:
        listing_id = escrow["listing_id"]
        qty = escrow["quantity"] or 0
        if listing_id and qty:
            await database.execute(
                "UPDATE listings SET quantity_available = quantity_available + :qty "
                "WHERE listing_id = :lid AND quantity_available IS NOT NULL",
                {"qty": qty, "lid": listing_id},
            )

    # Refund item_amount only (delivery fee stays in escrow until reversed)
    if item_amount > 0:
        escrow_sub  = await _get_escrow_holding_account()
        shopper_sub = await _get_user_sub_account(shopper_id)

        result = await _book_transfer(
            from_sub_account_id=escrow_sub,
            to_sub_account_id=shopper_sub,
            amount=item_amount,
            reference=f"{req.order_id}:{order_store_id or escrow['listing_id']}:refund",
            reason=f"Refund — order #{req.order_id[:8]}",
        )
        transfer_id = (result or {}).get("transfer_id")

        await _credit_ledger(shopper_id, "shopping", item_amount)
        await _log_wallet_transaction(
            user_id=shopper_id,
            amount=item_amount,
            type="credit",
            description=f"Refund for order #{req.order_id[:8]}",
            reference=f"{req.order_id}:{order_store_id or escrow['listing_id']}:refund",
            status="completed",
            anchor_transfer_id=transfer_id,
        )

    _fire_and_forget(notify_user(
        storekeeper_id,
        "order",
        "Order returned",
        f"Order #{req.order_id[:8]} was returned by the shopper.",
        {"order_id": req.order_id, "kind": "order_returned"},
    ))

    return {"order_id": req.order_id, "status": "returned", "message": "Item cost refunded. Stock restored."}


# ═══════════════════════════════════════════════════════════════════
# Reversed-package — book transfer escrow → courier (delivery_fee)
# ═══════════════════════════════════════════════════════════════════

@router.post("/reversed-package")
async def reversed_package(
    req: ConfirmRequest,
    current_user: dict = Depends(get_current_user),
):
    await _require_anchor_ready()
    storekeeper_id = current_user["id"]

    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND storekeeper_id = :uid",
        {"oid": req.order_id, "uid": storekeeper_id},
    )
    if not escrow:
        exists = await database.fetch_one(
            "SELECT 1 AS x FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
        )
        if exists:
            raise HTTPException(status_code=403, detail="Only the storekeeper can release")
        raise HTTPException(status_code=404, detail="Order not found")

    if escrow["status"] != "returned":
        raise HTTPException(status_code=400, detail="Order not in returned state")

    delivery_fee   = float(escrow["delivery_fee"] or 0)
    courier_id     = escrow["courier_id"]
    shopper_id     = escrow["shopper_id"]
    order_store_id = escrow.get("order_store_id")

    if courier_id in (storekeeper_id, shopper_id):
        courier_id = None

    flipped = await _flip_escrow_status(req.order_id, "returned", "reversed", storekeeper_id=storekeeper_id)
    if not flipped:
        return {"order_id": req.order_id, "status": "reversed", "message": "Already reversed."}

    if delivery_fee > 0 and courier_id:
        escrow_sub  = await _get_escrow_holding_account()
        courier_sub = await _get_user_sub_account(courier_id)

        result = await _book_transfer(
            from_sub_account_id=escrow_sub,
            to_sub_account_id=courier_sub,
            amount=delivery_fee,
            reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:reversed",
            reason=f"Reversed package fee — {req.order_id[:8]}",
        )
        transfer_id = (result or {}).get("transfer_id")

        await _credit_ledger(courier_id, "services", delivery_fee)
        await _log_wallet_transaction(
            user_id=courier_id,
            amount=delivery_fee,
            type="credit",
            description=f"Reversed package fee for #{req.order_id[:8]}",
            reference=f"{req.order_id}:{storekeeper_id[:8]}:{order_store_id or 'single'}:reversed",
            status="completed",
            anchor_transfer_id=transfer_id,
        )

    return {"order_id": req.order_id, "status": "reversed", "message": "Courier fee released"}


# ═══════════════════════════════════════════════════════════════════
# Anchor webhook receiver
# ═══════════════════════════════════════════════════════════════════

@webhook_router.post("/anchor")
async def anchor_webhook(
    request: Request,
    x_anchor_signature: Optional[str] = Header(None, alias="x-anchor-signature"),
):
    raw = await request.body()

    if not anchor.verify_webhook(raw, x_anchor_signature or ""):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Idempotency: skip if we've seen this event
    event_id = (
        payload.get("event_id")
        or payload.get("id")
        or payload.get("data", {}).get("id")
    )
    event_type = payload.get("event") or payload.get("type") or "unknown"

    if event_id:
        inserted = await database.fetch_val(
            """
            INSERT INTO anchor_webhook_events
                (anchor_event_id, event_type, payload)
            VALUES (:eid, :et, :pl)
            ON CONFLICT (anchor_event_id) DO NOTHING
            RETURNING id
            """,
            {"eid": event_id, "et": event_type, "pl": json.dumps(payload)},
        )
        if not inserted:
            return {"ok": True, "duplicate": True}

    try:
        await _process_anchor_event(event_type, payload)
    except Exception as e:
        logger.exception("Anchor webhook processing failed")
        if event_id:
            await database.execute(
                "UPDATE anchor_webhook_events SET process_error = :err WHERE anchor_event_id = :eid",
                {"err": str(e)[:500], "eid": event_id},
            )
        raise HTTPException(status_code=500, detail="Processing failed")

    if event_id:
        await database.execute(
            "UPDATE anchor_webhook_events SET processed_at = NOW() WHERE anchor_event_id = :eid",
            {"eid": event_id},
        )

    return {"ok": True}


async def _process_anchor_event(event_type: str, payload: dict) -> None:
    """
    Route events. Adjust event names once you read Anchor's docs.
    Likely names (verify):
      - payment.received  (money hit a virtual NUBAN)
      - payment.settled   (funds cleared)
      - transfer.completed
      - transfer.failed
    """
    data = payload.get("data", {}) or {}

    if event_type in ("payment.received", "payment.successful"):
        nuban  = (
            data.get("virtualNuban", {}).get("accountNumber")
            or data.get("accountNumber")
        )
        amount = float(data.get("amount", 0))
        if not nuban or amount <= 0:
            return

        user_row = await database.fetch_one(
            "SELECT id FROM users WHERE anchor_virtual_nuban = :nuban",
            {"nuban": nuban},
        )
        if not user_row:
            logger.warning("payment.received for unknown NUBAN: %s", nuban)
            return

        # Credit the shopping bucket by default — user can differentiate later
        await _credit_ledger(user_row["id"], "shopping", amount)

        # Write an audit row so it appears in the transaction list
        reference = data.get("reference") or payload.get("id") or f"topup_{uuid.uuid4().hex[:12]}"
        await _log_wallet_transaction(
            user_id=user_row["id"],
            amount=amount,
            type="credit",
            description="Bank transfer into wallet",
            reference=reference,
            status="completed",
            anchor_transfer_id=data.get("id"),
        )

        _fire_and_forget(notify_user(
            user_row["id"],
            "wallet",
            "Wallet funded",
            f"₦{amount:,.0f} received. You can now spend it on Admerce.",
            {"reference": reference, "kind": "wallet_funded"},
        ))
        return

    if event_type in ("transfer.completed", "transfer.successful"):
        reference = data.get("reference") or ""
        transfer_id = data.get("id")
        await database.execute(
            """
            UPDATE wallet_transactions
               SET status = 'completed'
             WHERE reference = :ref
            """,
            {"ref": reference},
        )
        await database.execute(
            """
            UPDATE anchor_pending_transfers
               SET status = 'completed', completed_at = NOW(), transfer_id = :tid
             WHERE reference = :ref
            """,
            {"ref": reference, "tid": transfer_id},
        )
        return

    if event_type in ("transfer.failed", "transfer.reversed"):
        reference = data.get("reference") or ""
        reason = data.get("reason") or data.get("failure_reason") or "Transfer failed"

        # Roll back the ledger for the affected user
        txn = await database.fetch_one(
            "SELECT user_id, amount, type, reference FROM wallet_transactions WHERE reference = :ref",
            {"ref": reference},
        )
        if txn and txn["type"] == "debit":
            # A withdrawal failed — refund the user's ledger
            await _credit_ledger(txn["user_id"], "shopping", float(txn["amount"]))
            await _log_wallet_transaction(
                user_id=txn["user_id"],
                amount=float(txn["amount"]),
                type="credit",
                description=f"Withdrawal failed — refunded. Reason: {reason}",
                reference=f"rollback:{reference}",
            )

        await database.execute(
            "UPDATE wallet_transactions SET status = 'failed' WHERE reference = :ref",
            {"ref": reference},
        )
        await database.execute(
            """
            UPDATE anchor_pending_transfers
               SET status = 'failed', error = :err, completed_at = NOW()
             WHERE reference = :ref
            """,
            {"ref": reference, "err": str(reason)[:500]},
        )
        return

    logger.debug("Unhandled Anchor event type: %s", event_type)


# ═══════════════════════════════════════════════════════════════════
# Reconciliation — call from a scheduled job
# ═══════════════════════════════════════════════════════════════════

async def reconcile_pending_transfers(limit: int = 50) -> int:
    """
    For every pending transfer that's more than 2 minutes old, ask
    Anchor for its real status and update our DB + ledger accordingly.
    Call this from a background loop in main.py every ~60s.
    """
    if not anchor.is_configured():
        return 0

    rows = await database.fetch_all(
        """
        SELECT id, reference, transfer_id, amount, kind, status
          FROM anchor_pending_transfers
         WHERE status = 'pending'
           AND created_at < NOW() - INTERVAL '2 minutes'
         ORDER BY created_at ASC
         LIMIT :lim
        """,
        {"lim": limit},
    )

    updated = 0
    for r in rows:
        try:
            if not r["transfer_id"]:
                continue
            info = await anchor.get_transfer(r["transfer_id"])
            status = (info or {}).get("status", "").lower()

            if status in ("success", "completed", "successful"):
                await database.execute(
                    """
                    UPDATE anchor_pending_transfers
                       SET status = 'completed', completed_at = NOW()
                     WHERE id = :id
                    """,
                    {"id": r["id"]},
                )
                await database.execute(
                    "UPDATE wallet_transactions SET status = 'completed' "
                    "WHERE reference = :ref",
                    {"ref": r["reference"]},
                )
                updated += 1
            elif status in ("failed", "reversed"):
                await database.execute(
                    """
                    UPDATE anchor_pending_transfers
                       SET status = 'failed', error = :err, completed_at = NOW()
                     WHERE id = :id
                    """,
                    {"id": r["id"], "err": str(info)[:500]},
                )
                # Rolling back the ledger is handled by the webhook path.
                updated += 1
        except Exception as e:
            await database.execute(
                """
                UPDATE anchor_pending_transfers
                   SET attempts = attempts + 1,
                       last_checked_at = NOW(),
                       error = :err
                 WHERE id = :id
                """,
                {"id": r["id"], "err": str(e)[:500]},
            )

    return updated


# ═══════════════════════════════════════════════════════════════════
# Reads — mostly unchanged
# ═══════════════════════════════════════════════════════════════════

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


@router.get("/transaction/{reference}")
async def get_wallet_transaction_detail(
    reference: str,
    current_user: dict = Depends(get_current_user),
):
    row = await database.fetch_one(
        """
        SELECT id, user_id, amount, type, description, reference, status,
               anchor_transfer_id, created_at
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


@router.get("/instant-transactions")
async def get_instant_transactions(current_user: dict = Depends(get_current_user)):
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
            "prefix": "pickup\\_%",
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
        {"uid": user_id, "prefix": "svcpay\\_%"},
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

    return {
        "pickups": await enrich(pickups_rows),
        "services": await enrich(service_rows),
    }


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
    status: Optional[str] = Query(None),
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
        SELECT id, amount, type, description, reference, status,
               anchor_transfer_id, created_at
        FROM wallet_transactions
        WHERE user_id = :uid
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
        """,
        {"uid": user_id, "limit": limit, "offset": offset},
    )
    return [dict(row) for row in rows]