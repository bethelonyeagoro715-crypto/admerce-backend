import os
import hmac
import hashlib
import json
import uuid
import httpx
from datetime import datetime, timezone
from typing import Optional, Dict
from fastapi import APIRouter, HTTPException, Depends, Request, Header, Query
from pydantic import BaseModel, Field
from app.db.database import database
from app.utils.security import get_current_user
from app.routes.notifications import send_push_to_user

# ─── ROUTERS DEFINED AT THE TOP ─────────────────────────────────────
router = APIRouter(prefix="/wallet", tags=["Wallet"])
webhook_router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
funding_router = APIRouter(prefix="/funding", tags=["Funding"])

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_BASE_URL = "https://api.paystack.co"

# ─── Helper: Paystack HTTP Client ───────────────────────────────────
def _paystack_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

async def _paystack_post(endpoint: str, payload: dict) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYSTACK_BASE_URL}{endpoint}",
            headers=_paystack_headers(),
            json=payload,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            print(f"❌ Paystack API Error ({endpoint}): {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"Paystack error: {resp.text}")
        return resp.json()

async def _get_paystack_customer(email: str) -> Optional[str]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PAYSTACK_BASE_URL}/customer?email={email}",
            headers=_paystack_headers()
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data") and len(data["data"]) > 0:
                return data["data"][0]["customer_code"]
    return None

# ─── Models ─────────────────────────────────────────────────────────
class InstantPickupRequest(BaseModel):
    listing_id: str
    storekeeper_id: str
    quantity: int = Field(1, ge=1, le=1000)

class ReserveRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=128)
    storekeeper_id: str = Field(..., min_length=1, max_length=128)
    listing_id: str = Field(..., min_length=1, max_length=128)
    quantity: int = Field(1, ge=1, le=1000)

class ConfirmRequest(BaseModel):
    order_id: str

# ─── Ledger Helpers ─────────────────────────────────────────────────
async def _credit_ledger(user_id: str, bucket: str, amount: float):
    await database.execute(
        """
        INSERT INTO user_ledger (user_id, bucket, amount, updated_at)
        VALUES (:uid, :b, :amt, NOW())
        ON CONFLICT (user_id, bucket)
        DO UPDATE SET amount = user_ledger.amount + :amt, updated_at = NOW()
        """,
        {"uid": user_id, "b": bucket, "amt": amount},
    )

async def _debit_ledger(user_id: str, bucket: str, amount: float):
    result = await database.fetch_val(
        """
        UPDATE user_ledger
           SET amount = amount - :amt, updated_at = NOW()
         WHERE user_id = :uid AND bucket = :b AND amount >= :amt
        RETURNING amount
        """,
        {"uid": user_id, "b": bucket, "amt": amount},
    )
    if result is None:
        raise HTTPException(status_code=400, detail="Insufficient balance")

# ═══════════════════════════════════════════════════════════════════
# 1. WALLET CREATION (Titan Paystack)
# ═══════════════════════════════════════════════════════════════════
@router.post("/create")
async def create_wallet(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    row = await database.fetch_one(
        "SELECT paystack_customer_code, virtual_account_number, virtual_bank_name FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if row and row["virtual_account_number"]:
        return {
            "account_number": row["virtual_account_number"], 
            "bank_name": row["virtual_bank_name"],
            "account_name": "Admerce User",
            "note": "Transfer from any Nigerian bank app into your Admerce account.",
            "kyc_status": "verified",
            "message": "Wallet already exists"
        }

    user = await database.fetch_one(
        "SELECT email, first_name, last_name, phone FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    if not user or not user["email"]:
        raise HTTPException(status_code=400, detail="User email required for wallet creation")

    customer_code = row["paystack_customer_code"] if row else None

    if not customer_code:
        cust_payload = {
            "email": user["email"],
            "first_name": user["first_name"] or "Admerce",
            "last_name": user["last_name"] or "User",
            "phone": user["phone"] or "",
        }
        try:
            cust_res = await _paystack_post("/customer", cust_payload)
            customer_code = cust_res["data"]["customer_code"]
        except HTTPException as e:
            if e.status_code == 409:
                customer_code = await _get_paystack_customer(user["email"])
                if not customer_code:
                    raise HTTPException(400, "Customer exists but could not be retrieved.")
            else:
                raise e
                
        await database.execute(
            "UPDATE users SET paystack_customer_code = :cc WHERE id = :uid",
            {"cc": customer_code, "uid": user_id},
        )

    dva_payload = {
        "customer": customer_code,
        "preferred_bank": "titan-paystack",
    }
    dva_res = await _paystack_post("/dedicated_account", dva_payload)
    dva_data = dva_res["data"]

    await database.execute(
        """
        UPDATE users
           SET virtual_account_number = :van,
               virtual_bank_name = :vbn
         WHERE id = :uid
        """,
        {
            "van": dva_data["account_number"],
            "vbn": dva_data["bank"]["name"],
            "uid": user_id,
        },
    )
    
    await database.execute(
        """
        INSERT INTO user_ledger (user_id, bucket, amount)
        VALUES (:uid,'shopping',0),(:uid,'selling',0),(:uid,'services',0)
        ON CONFLICT DO NOTHING
        """,
        {"uid": user_id},
    )

    return {
        "account_number": dva_data["account_number"],
        "bank_name": dva_data["bank"]["name"],
        "account_name": f"{user['first_name'] or 'Admerce'} {user['last_name'] or 'User'}",
        "note": "Transfer from any Nigerian bank app into your Admerce account.",
        "kyc_status": "verified",
        "message": "Wallet created. Transfer to this account to fund it.",
    }

# ═══════════════════════════════════════════════════════════════════
# 2. READ: FUNDING DETAILS
# ═══════════════════════════════════════════════════════════════════
@funding_router.get("/details")
async def get_funding_details(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    row = await database.fetch_one(
        "SELECT virtual_account_number, virtual_bank_name, first_name, last_name FROM users WHERE id = :uid",
        {"uid": user_id},
    )
    
    if row and row["virtual_account_number"]:
        return {
            "account_number": row["virtual_account_number"],
            "bank_name": row["virtual_bank_name"],
            "account_name": f"{row['first_name'] or 'Admerce'} {row['last_name'] or 'User'}",
            "note": "Transfer from any Nigerian bank app into your Admerce account.",
            "kyc_status": "verified",
            "message": "Existing virtual account"
        }
        
    try:
        create_res = await create_wallet(current_user)
        return create_res
    except Exception as e:
        print(f"⚠️ Failed to auto-create wallet: {e}")
        raise HTTPException(status_code=400, detail="Could not generate funding account.")

# ═══════════════════════════════════════════════════════════════════
# 3. READ: BALANCE
# ═══════════════════════════════════════════════════════════════════
@router.get("/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
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
        "SELECT virtual_account_number, virtual_bank_name FROM users WHERE id = :uid",
        {"uid": user_id},
    )

    return {
        "user_id": user_id,
        "balance": total,
        "buckets": buckets,
        "virtual_account_number": user_row["virtual_account_number"] if user_row else None,
        "virtual_bank_name": user_row["virtual_bank_name"] if user_row else None,
    }

# ═══════════════════════════════════════════════════════════════════
# 4. READ: TRANSACTIONS
# ═══════════════════════════════════════════════════════════════════
@router.get("/transactions")
async def get_wallet_transactions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
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

# ═══════════════════════════════════════════════════════════════════
# 5. READ: ESCROWS
# ═══════════════════════════════════════════════════════════════════
@router.get("/escrows")
async def get_my_escrows(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT e.*, l.title AS listing_title, s.name AS store_name
        FROM escrow e
        LEFT JOIN listings l ON e.listing_id = l.listing_id
        LEFT JOIN stores s ON l.store_id = s.store_id
        WHERE e.shopper_id = :uid AND e.status IN ('locked', 'accepted', 'dispatched')
        ORDER BY e.created_at DESC
        """,
        {"uid": user_id}
    )
    return [dict(row) for row in rows]

# ═══════════════════════════════════════════════════════════════════
# 6. INSTANT PICKUP
# ═══════════════════════════════════════════════════════════════════
@router.post("/instant-pickup")
async def instant_pickup(
    req: InstantPickupRequest,
    current_user: dict = Depends(get_current_user),
):
    shopper_id = current_user["id"]
    
    row = await database.fetch_one(
        """
        SELECT l.*, s.owner_id AS store_owner_id, s.name AS store_name
          FROM listings l
          JOIN stores s ON l.store_id = s.store_id
         WHERE l.listing_id = :lid AND s.owner_id = :oid
        """,
        {"lid": req.listing_id, "oid": req.storekeeper_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found or wrong storekeeper")
    
    listing = dict(row)
    unit_price = float(listing.get("price") or 0)
    total = round(unit_price * req.quantity, 2)

    await _debit_ledger(shopper_id, "shopping", total)

    store_row = await database.fetch_one(
        "SELECT paystack_recipient_code, bank_code, account_number FROM users WHERE id = :uid",
        {"uid": listing["store_owner_id"]},
    )
    recipient_code = store_row["paystack_recipient_code"] if store_row else None
    
    if not recipient_code:
        if not store_row or not store_row["account_number"]:
            raise HTTPException(status_code=400, detail="Storekeeper has not set up payout details")
        
        rec_payload = {
            "type": "nuban",
            "name": listing["store_name"],
            "account_number": store_row["account_number"],
            "bank_code": store_row["bank_code"],
            "currency": "NGN",
        }
        rec_res = await _paystack_post("/transferrecipient", rec_payload)
        recipient_code = rec_res["data"]["recipient_code"]
        await database.execute(
            "UPDATE users SET paystack_recipient_code = :rc WHERE id = :uid",
            {"rc": recipient_code, "uid": listing["store_owner_id"]},
        )

    transfer_ref = f"pickup_{uuid.uuid4().hex[:12]}"
    transfer_payload = {
        "source": "balance",
        "amount": int(total * 100),
        "recipient": recipient_code,
        "reference": transfer_ref,
        "reason": f"Instant pickup: {listing['title']}",
    }
    await _paystack_post("/transfer", transfer_payload)

    await _credit_ledger(listing["store_owner_id"], "selling", total)

    await database.execute(
        "INSERT INTO wallet_transactions (user_id, amount, type, description, reference, status, created_at) VALUES (:uid, :amt, 'debit', :desc, :ref, 'completed', NOW())",
        {"uid": shopper_id, "amt": total, "desc": f"Instant pickup: {listing['title']}", "ref": transfer_ref},
    )
    await database.execute(
        "INSERT INTO wallet_transactions (user_id, amount, type, description, reference, status, created_at) VALUES (:uid, :amt, 'credit', :desc, :ref, 'completed', NOW())",
        {"uid": listing["store_owner_id"], "amt": total, "desc": f"Sale: {listing['title']}", "ref": transfer_ref},
    )

    await send_push_to_user(
        user_id=listing["store_owner_id"],
        title="New instant pickup",
        body=f"{req.quantity} × {listing['title']} — {listing['store_name']}",
        data={"url": "/notifications", "type": "order"},
    )

    return {"message": "Payment successful", "amount": total}

# ═══════════════════════════════════════════════════════════════════
# 7. RESERVATION
# ═══════════════════════════════════════════════════════════════════
@router.post("/reserve")
async def reserve(
    req: ReserveRequest,
    current_user: dict = Depends(get_current_user),
):
    shopper_id = current_user["id"]
    
    row = await database.fetch_one(
        """
        SELECT l.*, s.owner_id AS store_owner_id, s.name AS store_name
          FROM listings l
          JOIN stores s ON l.store_id = s.store_id
         WHERE l.listing_id = :lid AND s.owner_id = :oid
        """,
        {"lid": req.listing_id, "oid": req.storekeeper_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    listing = dict(row)
    total = round(float(listing.get("price") or 0) * req.quantity, 2)

    await _debit_ledger(shopper_id, "shopping", total)

    sub_row = await database.fetch_one(
        "SELECT paystack_subaccount_code FROM users WHERE id = :uid",
        {"uid": listing["store_owner_id"]},
    )
    subaccount_code = sub_row["paystack_subaccount_code"] if sub_row else None

    if not subaccount_code:
        store_owner = await database.fetch_one(
            "SELECT bank_code, account_number FROM users WHERE id = :uid",
            {"uid": listing["store_owner_id"]},
        )
        sub_payload = {
            "business_name": listing["store_name"],
            "settlement_bank": store_owner["bank_code"],
            "account_number": store_owner["account_number"],
            "percentage_charge": 0,
            "settlement_schedule": "manual",
        }
        sub_res = await _paystack_post("/subaccount", sub_payload)
        subaccount_code = sub_res["data"]["subaccount_code"]
        await database.execute(
            "UPDATE users SET paystack_subaccount_code = :sc WHERE id = :uid",
            {"sc": subaccount_code, "uid": listing["store_owner_id"]},
        )

    await database.execute(
        """
        INSERT INTO escrow (
            order_id, shopper_id, storekeeper_id, listing_id, quantity,
            total_amount, status, created_at
        ) VALUES (
            :oid, :sid, :skid, :lid, :qty, :total, 'locked', NOW()
        )
        """,
        {
            "oid": req.order_id,
            "sid": shopper_id,
            "skid": listing["store_owner_id"],
            "lid": req.listing_id,
            "qty": req.quantity,
            "total": total,
        },
    )

    await database.execute(
        "INSERT INTO wallet_transactions (user_id, amount, type, description, reference, status, created_at) VALUES (:uid, :amt, 'debit', :desc, :ref, 'pending', NOW())",
        {"uid": shopper_id, "amt": total, "desc": f"Reservation: {listing['title']}", "ref": req.order_id},
    )

    await send_push_to_user(
        user_id=listing["store_owner_id"],
        title="New reservation",
        body=f"{req.quantity} × {listing['title']} — awaiting your hold",
        data={"url": "/notifications", "type": "order"},
    )

    return {"message": "Reserved successfully", "order_id": req.order_id, "status": "locked"}

# ═══════════════════════════════════════════════════════════════════
# 8. CONFIRM
# ═══════════════════════════════════════════════════════════════════
@router.post("/confirm")
async def confirm_pickup(
    req: ConfirmRequest,
    current_user: dict = Depends(get_current_user),
):
    shopper_id = current_user["id"]
    
    escrow = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND shopper_id = :uid",
        {"oid": req.order_id, "uid": shopper_id},
    )
    if not escrow:
        raise HTTPException(status_code=404, detail="Order not found")
    if escrow["status"] != "locked":
        raise HTTPException(status_code=400, detail="Order is not locked")

    await database.execute(
        "UPDATE escrow SET status = 'picked_up' WHERE order_id = :oid",
        {"oid": req.order_id},
    )

    storekeeper_id = escrow["storekeeper_id"]
    sub_row = await database.fetch_one(
        "SELECT paystack_subaccount_code FROM users WHERE id = :uid",
        {"uid": storekeeper_id},
    )
    if sub_row and sub_row["paystack_subaccount_code"]:
        await _paystack_post(
            "/settlement/initiate",
            {"subaccount": sub_row["paystack_subaccount_code"]},
        )

    await _credit_ledger(storekeeper_id, "selling", float(escrow["total_amount"]))

    await database.execute(
        "UPDATE wallet_transactions SET status = 'completed' WHERE reference = :ref",
        {"ref": req.order_id},
    )
    await database.execute(
        "INSERT INTO wallet_transactions (user_id, amount, type, description, reference, status, created_at) VALUES (:uid, :amt, 'credit', 'Escrow released', :ref, 'completed', NOW())",
        {"uid": storekeeper_id, "amt": float(escrow["total_amount"]), "ref": req.order_id},
    )

    await send_push_to_user(
        user_id=storekeeper_id,
        title="Item picked up",
        body=f"Order #{req.order_id[:8]} picked up. Funds credited.",
        data={"url": "/notifications", "type": "order"},
    )

    return {"message": "Funds released", "order_id": req.order_id, "status": "picked_up"}

# ═══════════════════════════════════════════════════════════════════
# 9. WEBHOOK
# ═══════════════════════════════════════════════════════════════════
@webhook_router.post("/paystack")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(None, alias="x-paystack-signature"),
):
    body = await request.body()
    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode(),
        body,
        hashlib.sha512,
    ).hexdigest()
    
    if not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    event = payload.get("event")
    data = payload.get("data", {})

    if event == "charge.success":
        auth = data.get("authorization", {})
        account_number = auth.get("receiver_bank_account_number")
        amount = float(data.get("amount", 0)) / 100 
        reference = data.get("reference")

        if account_number and amount > 0:
            user = await database.fetch_one(
                "SELECT id FROM users WHERE virtual_account_number = :van",
                {"van": account_number},
            )
            if user:
                await _credit_ledger(user["id"], "shopping", amount)
                
                await database.execute(
                    "INSERT INTO wallet_transactions (user_id, amount, type, description, reference, status, created_at) VALUES (:uid, :amt, 'credit', 'Wallet top-up via transfer', :ref, 'completed', NOW())",
                    {"uid": user["id"], "amt": amount, "ref": reference},
                )

                await send_push_to_user(
                    user_id=user["id"],
                    title="Wallet funded",
                    body=f"₦{amount:,.0f} received.",
                    data={"url": "/wallet", "type": "wallet"},
                )
    return {"status": "ok"}