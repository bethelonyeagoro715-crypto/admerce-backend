from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import Optional, List
from app.db.database import database
from app.utils.security import get_current_user
from datetime import datetime, timedelta
import asyncio
import uuid
from app.routes.notifications import send_push_to_user

from app.routes.auth import hash_password, verify_password

router = APIRouter(prefix="/wallet", tags=["Wallet"])

# ---------- Models ----------
class ReserveRequest(BaseModel):
    order_id: str
    storekeeper_id: str
    item_amount: float
    listing_id: str
    quantity: int = 1
    courier_id: Optional[str] = None
    delivery_fee: float = 0.0

class ConfirmRequest(BaseModel):
    order_id: str

class AcceptRequest(BaseModel):
    order_id: str

class SetPinRequest(BaseModel):
    pin: str

class WithdrawRequest(BaseModel):
    amount: float
    method: str = "mobile_money"
    account_number: str
    pin: str

class InstantPickupRequest(BaseModel):
    listing_id: str
    storekeeper_id: str
    amount: float

# ---------- Helper to insert into wallet_transactions ----------
async def _log_wallet_transaction(
    user_id: str,
    amount: float,
    type: str,           # 'credit' or 'debit'
    description: str,
    reference: str,
    status: str = 'completed'
):
    await database.execute(
        """
        INSERT INTO wallet_transactions (user_id, amount, type, description, reference, status, created_at)
        VALUES (:uid, :amt, :type, :desc, :ref, :status, NOW())
        """,
        {
            "uid": user_id,
            "amt": amount,
            "type": type,
            "desc": description,
            "ref": reference,
            "status": status
        }
    )

# ---------- Create Wallet ----------
@router.post("/create")
async def create_wallet(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    existing = await database.fetch_one(
        "SELECT * FROM wallets WHERE user_id = :uid", {"uid": user_id}
    )
    if existing:
        return {"user_id": user_id, "balance": existing["balance"], "message": "Wallet already exists"}
    await database.execute(
        "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)", {"uid": user_id}
    )
    return {"user_id": user_id, "balance": 0.0, "message": "Wallet created"}

# ---------- Topup ----------
@router.post("/topup")
async def topup(amount: float, current_user: dict = Depends(get_current_user)):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    user_id = current_user["id"]
    wallet = await database.fetch_one(
        "SELECT * FROM wallets WHERE user_id = :uid", {"uid": user_id}
    )
    if not wallet:
        await database.execute("INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)", {"uid": user_id})

    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": amount, "uid": user_id}
    )
    updated = await database.fetch_one("SELECT balance FROM wallets WHERE user_id = :uid", {"uid": user_id})

    await _log_wallet_transaction(
        user_id=user_id,
        amount=amount,
        type='credit',
        description='Wallet top-up',
        reference=f"topup_{uuid.uuid4().hex[:8]}"
    )

    return {"user_id": user_id, "new_balance": updated["balance"], "message": f"Top-up of {amount} successful"}

# ---------- Get Balance ----------
@router.get("/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    wallet = await database.fetch_one("SELECT balance FROM wallets WHERE user_id = :uid", {"uid": user_id})
    if not wallet:
        await database.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)",
            {"uid": user_id}
        )
        return {"user_id": user_id, "balance": 0.0}
    return {"user_id": user_id, "balance": float(wallet["balance"])}

# ---------- Set Withdrawal PIN ----------
@router.post("/set-pin")
async def set_pin(req: SetPinRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    wallet = await database.fetch_one("SELECT * FROM wallets WHERE user_id = :uid", {"uid": user_id})
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if len(req.pin) < 4:
        raise HTTPException(status_code=400, detail="PIN must be at least 4 digits")
    hashed_pin = hash_password(req.pin)
    await database.execute(
        "UPDATE wallets SET withdrawal_pin = :pin WHERE user_id = :uid",
        {"pin": hashed_pin, "uid": user_id}
    )
    return {"message": "Withdrawal PIN set successfully"}

# ---------- Withdraw ----------
@router.post("/withdraw")
async def withdraw(req: WithdrawRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    wallet = await database.fetch_one(
        "SELECT * FROM wallets WHERE user_id = :uid", {"uid": user_id}
    )
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    # ✅ Use bracket notation, not .get()
    withdrawal_pin = wallet["withdrawal_pin"]
    balance = wallet["balance"]

    if not withdrawal_pin:
        raise HTTPException(status_code=403, detail="Withdrawal PIN not set. Please set a PIN first.")

    if not verify_password(req.pin, withdrawal_pin):
        raise HTTPException(status_code=403, detail="Incorrect PIN")

    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    if balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    new_balance = balance - req.amount
    await database.execute(
        "UPDATE wallets SET balance = :bal WHERE user_id = :uid",
        {"bal": new_balance, "uid": user_id}
    )

    txn_id = f"wdr_{uuid.uuid4().hex[:8]}"
    await _log_wallet_transaction(
        user_id=user_id,
        amount=req.amount,
        type='debit',
        description=f"Withdrawal via {req.method}",
        reference=txn_id
    )

    return {
        "message": "Withdrawal successful",
        "transaction_id": txn_id,
        "amount": req.amount,
        "new_balance": new_balance
    }
    

# ---------- Instant Pickup (Pay in person) – FIXED ----------
@router.post("/instant-pickup")
async def instant_pickup(req: InstantPickupRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    row = await database.fetch_one(
        "SELECT * FROM listings WHERE listing_id = :lid AND store_id IN "
        "(SELECT store_id FROM stores WHERE owner_id = :oid)",
        {"lid": req.listing_id, "oid": req.storekeeper_id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found or not owned by that storekeeper")

    listing = dict(row)

    if listing.get("quantity_available") is not None and listing["quantity_available"] <= 0:
        raise HTTPException(status_code=400, detail="Item out of stock")

    wallet = await database.fetch_one("SELECT balance FROM wallets WHERE user_id = :uid", {"uid": shopper_id})
    if not wallet or wallet["balance"] < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    await database.execute("UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid",
                           {"amt": req.amount, "uid": shopper_id})
    await database.execute("UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                           {"amt": req.amount, "uid": req.storekeeper_id})

    if listing.get("quantity_available") is not None:
        await database.execute(
            "UPDATE listings SET quantity_available = quantity_available - 1 WHERE listing_id = :lid",
            {"lid": req.listing_id}
        )

    txn_id = f"pickup_{uuid.uuid4().hex[:8]}"
    await _log_wallet_transaction(
        user_id=shopper_id,
        amount=req.amount,
        type='debit',
        description='Instant pickup payment',
        reference=txn_id
    )

    return {"message": "Payment successful", "transaction_id": txn_id, "amount": req.amount}


# ---------- Reserve – FIXED (with quantity) ----------
@router.post("/reserve")
async def reserve(req: ReserveRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    total = req.item_amount + req.delivery_fee

    if total <= 0:
        raise HTTPException(status_code=400, detail="Item amount must be positive")

    wallet = await database.fetch_one("SELECT balance FROM wallets WHERE user_id = :uid", {"uid": shopper_id})
    if not wallet:
        raise HTTPException(status_code=400, detail="Wallet not found. Please create a wallet first.")
    if wallet["balance"] < total:
        raise HTTPException(status_code=400, detail=f"Insufficient balance. Your balance: ₦{wallet['balance']}, required: ₦{total}")

    existing = await database.fetch_one("SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id})
    if existing:
        raise HTTPException(status_code=400, detail=f"Order {req.order_id} already reserved")

    row = await database.fetch_one(
        "SELECT * FROM listings WHERE listing_id = :lid AND store_id IN "
        "(SELECT store_id FROM stores WHERE owner_id = :oid)",
        {"lid": req.listing_id, "oid": req.storekeeper_id}
    )
    if not row:
        raise HTTPException(status_code=400, detail=f"Listing {req.listing_id} not found or not owned by storekeeper {req.storekeeper_id}")
    listing = dict(row)

    if listing.get("quantity_available") is not None and listing["quantity_available"] < req.quantity:
        raise HTTPException(status_code=400, detail=f"Only {listing['quantity_available']} items available")

    await database.execute("UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid", {"amt": total, "uid": shopper_id})

    expires_at = datetime.utcnow() + timedelta(hours=2)

    # Convert numeric values to strings if the escrow columns are TEXT
    item_amount_str = str(req.item_amount)
    delivery_fee_str = str(req.delivery_fee)
    total_amount_str = str(total)

    query = """
    INSERT INTO escrow (order_id, shopper_id, storekeeper_id, courier_id,
                        listing_id, quantity, item_amount, delivery_fee, total_amount, status, expires_at)
    VALUES (:order_id, :shopper_id, :storekeeper_id, :courier_id,
            :listing_id, :quantity, :item_amount, :delivery_fee, :total_amount, 'locked', :expires_at)
    """
    await database.execute(query, {
        "order_id": req.order_id,
        "shopper_id": shopper_id,
        "storekeeper_id": req.storekeeper_id,
        "courier_id": req.courier_id,
        "listing_id": req.listing_id,
        "quantity": req.quantity,
        "item_amount": item_amount_str,
        "delivery_fee": delivery_fee_str,
        "total_amount": total_amount_str,
        "expires_at": expires_at.isoformat(),
    })

    await _log_wallet_transaction(
        user_id=shopper_id,
        amount=total,
        type='debit',
        description='Item reservation',
        reference=req.order_id
    )

    asyncio.create_task(send_push_to_user(
        req.storekeeper_id,
        "New Reservation!",
        f"A shopper just reserved {req.quantity} item(s). Order #{req.order_id[:8]}",
        {"order_id": req.order_id}
    ))

    return {"order_id": req.order_id, "status": "locked", "total": total, "message": "Funds reserved"}

# ---------- Accept Reservation (Storekeeper) ----------
@router.post("/accept")
async def accept_reservation(req: AcceptRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["storekeeper_id"] != user_id:
        raise HTTPException(status_code=403, detail="You are not the storekeeper for this order")
    if escrow["status"] != "locked":
        raise HTTPException(status_code=400, detail="Order is not in a reservable state (status must be 'locked')")

    await database.execute(
        "UPDATE escrow SET status = 'accepted' WHERE order_id = :oid",
        {"oid": req.order_id}
    )

    await send_push_to_user(
        escrow["shopper_id"],
        "Reservation Accepted!",
        f"Your order #{req.order_id[:8]} has been accepted by the storekeeper.",
        {"order_id": req.order_id}
    )

    return {"order_id": req.order_id, "status": "accepted", "message": "Reservation accepted"}

# ---------- Confirm (mark picked up) ----------
@router.post("/confirm")
async def confirm(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["shopper_id"] != shopper_id:
        raise HTTPException(status_code=403, detail="You can only confirm your own orders")
    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(status_code=400, detail="Order is not in a confirmable state (must be 'accepted' or 'locked')")

    item_amount = float(escrow["item_amount"])
    delivery_fee = float(escrow["delivery_fee"])

    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": item_amount, "uid": escrow["storekeeper_id"]}
    )
    if delivery_fee > 0 and escrow["courier_id"]:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": delivery_fee, "uid": escrow["courier_id"]}
        )

    await database.execute(
        "UPDATE escrow SET status = 'picked_up' WHERE order_id = :oid",
        {"oid": req.order_id}
    )

    return {"order_id": req.order_id, "status": "picked_up", "message": "Order marked as picked up. Funds released."}

# ---------- Dispatch ----------
@router.post("/dispatch")
async def dispatch(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["shopper_id"] != shopper_id:
        raise HTTPException(status_code=403, detail="Only the shopper can dispatch")
    if escrow["status"] not in ("accepted", "locked"):
        raise HTTPException(status_code=400, detail="Order must be accepted or locked to dispatch")

    item_amount = float(escrow["item_amount"])
    delivery_fee = float(escrow["delivery_fee"])

    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": item_amount, "uid": escrow["storekeeper_id"]}
    )
    if delivery_fee > 0 and escrow["courier_id"]:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": delivery_fee, "uid": escrow["courier_id"]}
        )

    await database.execute(
        "UPDATE escrow SET status = 'dispatched' WHERE order_id = :oid",
        {"oid": req.order_id}
    )
    return {"order_id": req.order_id, "status": "dispatched", "message": "Order dispatched. Funds released."}

# ---------- Return ----------
@router.post("/return")
async def return_order(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["shopper_id"] != shopper_id:
        raise HTTPException(status_code=403, detail="Only the shopper can return")
    if escrow["status"] not in ("locked", "accepted"):
        raise HTTPException(status_code=400, detail="Order must be in 'locked' or 'accepted' state to return")

    listing_id = escrow["listing_id"] if "listing_id" in escrow else None
    quantity = escrow["quantity"] if "quantity" in escrow else None

    if listing_id and quantity:
        listing = await database.fetch_one(
            "SELECT quantity_available FROM listings WHERE listing_id = :lid",
            {"lid": listing_id}
        )
        if listing and listing["quantity_available"] is not None:
            await database.execute(
                "UPDATE listings SET quantity_available = quantity_available + :qty WHERE listing_id = :lid",
                {"qty": quantity, "lid": listing_id}
            )

    item_amount = float(escrow["item_amount"])

    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": item_amount, "uid": escrow["shopper_id"]}
    )
    await database.execute(
        "UPDATE escrow SET status = 'returned' WHERE order_id = :oid",
        {"oid": req.order_id}
    )
    return {"order_id": req.order_id, "status": "returned", "message": "Item cost refunded. Stock restored."}

# ---------- Reversed Package ----------
@router.post("/reversed-package")
async def reversed_package(req: ConfirmRequest, current_user: dict = Depends(get_current_user)):
    storekeeper_id = current_user["id"]
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id}
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["storekeeper_id"] != storekeeper_id:
        raise HTTPException(status_code=403, detail="Only the storekeeper can release")
    if escrow["status"] != "returned":
        raise HTTPException(status_code=400, detail="Order not in returned state")

    delivery_fee = float(escrow["delivery_fee"])
    if delivery_fee > 0 and escrow["courier_id"]:
        await database.execute(
            "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
            {"amt": delivery_fee, "uid": escrow["courier_id"]}
        )

    await database.execute(
        "UPDATE escrow SET status = 'reversed' WHERE order_id = :oid",
        {"oid": req.order_id}
    )
    return {"order_id": req.order_id, "status": "reversed", "message": "Courier fee released"}

# ---------- Get Escrows (shopper's locked orders) ----------
@router.get("/escrows")
async def get_escrows(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    query = "SELECT * FROM escrow WHERE shopper_id = :uid AND status = 'locked'"
    rows = await database.fetch_all(query, {"uid": user_id})
    return [dict(row) for row in rows]

# ---------- Get All Orders (shopper) – FIXED for comma‑separated status ----------
@router.get("/orders")
async def get_orders(
    current_user: dict = Depends(get_current_user),
    status: Optional[str] = Query(None, description="Comma-separated statuses, e.g., locked,accepted,picked_up")
):
    user_id = current_user["id"]
    query = "SELECT * FROM escrow WHERE shopper_id = :uid"
    params: dict = {"uid": user_id}

    if status:
        status_list = [s.strip() for s in status.split(",") if s.strip()]
        if status_list:
            placeholders = []
            for idx, s in enumerate(status_list):
                ph = f"status_{idx}"
                placeholders.append(f":{ph}")
                params[ph] = s
            query += f" AND status IN ({', '.join(placeholders)})"

    query += " ORDER BY created_at DESC"

    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]

# ---------- Get Order Detail (with customer name and store name) ----------
@router.get("/order/{order_id}")
async def get_order_detail(order_id: str, current_user: dict = Depends(get_current_user)):
    order = await database.fetch_one(
        """
        SELECT e.*,
               COALESCE(u.nickname, 'Customer') AS customer_name,
               s.name AS store_name
        FROM escrow e
        LEFT JOIN users u ON e.shopper_id = u.id
        LEFT JOIN stores s ON e.storekeeper_id = s.owner_id
        WHERE e.order_id = :oid
        """,
        {"oid": order_id}
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return dict(order)

# ---------- Get Wallet Transactions ----------
@router.get("/transactions")
async def get_wallet_transactions(
    limit: int = 20,
    offset: int = 0,
    current_user: dict = Depends(get_current_user)
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
        {"uid": user_id, "limit": limit, "offset": offset}
    )
    return [dict(row) for row in rows]

# ---------- Schedule Reminder ----------
async def schedule_reminder(user_id: str, order_id: str, delay_seconds: float, fraction: float):
    await asyncio.sleep(delay_seconds)
    escrow = await database.fetch_one("SELECT status FROM escrow WHERE order_id = :oid", {"oid": order_id})
    if escrow and escrow["status"] in ("locked", "accepted"):
        await send_push_to_user(
            user_id,
            "⏰ Pickup Reminder",
            f"Your order #{order_id[:8]} is expiring soon! Only {int(fraction*100)}% of time left.",
            {"order_id": order_id}
        )