from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from app.db.database import database
from app.utils.security import get_current_user
from datetime import datetime, timedelta
import uuid
import asyncio
from app.routes.notifications import send_push_to_user

router = APIRouter(prefix="/basket", tags=["Basket"])

# ---------- Models ----------
class AddToBasketRequest(BaseModel):
    listing_id: str
    store_id: str
    quantity: int = 1

class UpdateBasketItemRequest(BaseModel):
    quantity: int

class CheckoutRequest(BaseModel):
    pass

# ---------- Helper: Get or Create Basket ----------
async def get_or_create_basket(user_id: str) -> str:
    basket = await database.fetch_one(
        "SELECT basket_id FROM baskets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if basket:
        return basket["basket_id"]

    basket_id = f"basket_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow()
    await database.execute(
        "INSERT INTO baskets (basket_id, user_id, created_at, updated_at) "
        "VALUES (:bid, :uid, :now, :now)",
        {"bid": basket_id, "uid": user_id, "now": now}
    )
    return basket_id

# ---------- 1. Add to Basket ----------
@router.post("/add")
async def add_to_basket(
    req: AddToBasketRequest,
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]
    basket_id = await get_or_create_basket(user_id)

    listing = await database.fetch_one(
        "SELECT price, store_id FROM listings WHERE listing_id = :lid",
        {"lid": req.listing_id}
    )
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    if listing["store_id"] and req.store_id and listing["store_id"] != req.store_id:
        raise HTTPException(
            status_code=400,
            detail="store_id does not match the listing's actual store."
        )

    existing = await database.fetch_one(
        "SELECT id, quantity FROM basket_items WHERE basket_id = :bid AND listing_id = :lid",
        {"bid": basket_id, "lid": req.listing_id}
    )

    if existing:
        new_qty = existing["quantity"] + req.quantity
        await database.execute(
            "UPDATE basket_items SET quantity = :qty WHERE id = :id",
            {"qty": new_qty, "id": existing["id"]}
        )
    else:
        # ✅ FIX: include `user_id` — NOT NULL column on basket_items.
        await database.execute(
            """
            INSERT INTO basket_items (basket_id, user_id, listing_id, store_id, quantity)
            VALUES (:bid, :uid, :lid, :sid, :qty)
            """,
            {
                "bid": basket_id,
                "uid": user_id,
                "lid": req.listing_id,
                "sid": req.store_id,
                "qty": req.quantity
            }
        )

    await database.execute(
        "UPDATE baskets SET updated_at = :now WHERE basket_id = :bid",
        {"now": datetime.utcnow(), "bid": basket_id}
    )

    return {
        "message": "Added to basket",
        "basket_id": basket_id,
        "quantity": req.quantity
    }

# ---------- 2. Get Basket ----------
@router.get("/")
async def get_basket(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    basket = await database.fetch_one(
        "SELECT basket_id FROM baskets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not basket:
        return {"items": [], "total": 0, "store_count": 0}

    items = await database.fetch_all(
        """
        SELECT bi.*, l.title AS product_name, l.price AS current_price
        FROM basket_items bi
        JOIN listings l ON bi.listing_id = l.listing_id
        WHERE bi.basket_id = :bid
        """,
        {"bid": basket["basket_id"]}
    )

    store_groups = {}
    total = 0
    for item in items:
        store_id = item["store_id"]
        if store_id not in store_groups:
            store_groups[store_id] = {
                "store_id": store_id,
                "items": [],
                "subtotal": 0
            }
        unit_price = item["current_price"]
        subtotal = item["quantity"] * unit_price
        store_groups[store_id]["items"].append({
            "id": item["id"],
            "listing_id": item["listing_id"],
            "name": item["product_name"],
            "quantity": item["quantity"],
            "price": unit_price,
            "subtotal": subtotal
        })
        store_groups[store_id]["subtotal"] += subtotal
        total += subtotal

    return {
        "basket_id": basket["basket_id"],
        "items": items,
        "store_groups": list(store_groups.values()),
        "total": total,
        "store_count": len(store_groups)
    }

# ---------- 3. Remove from Basket ----------
@router.delete("/item/{item_id}")
async def remove_from_basket(
    item_id: int,
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]
    basket = await database.fetch_one(
        "SELECT basket_id FROM baskets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    await database.execute(
        "DELETE FROM basket_items WHERE id = :id AND basket_id = :bid",
        {"id": item_id, "bid": basket["basket_id"]}
    )

    return {"message": "Item removed from basket"}

# ---------- 4. Update Quantity ----------
@router.put("/item/{item_id}")
async def update_basket_item(
    item_id: int,
    req: UpdateBasketItemRequest,
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]
    basket = await database.fetch_one(
        "SELECT basket_id FROM baskets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    if req.quantity <= 0:
        await database.execute(
            "DELETE FROM basket_items WHERE id = :id AND basket_id = :bid",
            {"id": item_id, "bid": basket["basket_id"]}
        )
        return {"message": "Item removed"}
    else:
        await database.execute(
            "UPDATE basket_items SET quantity = :qty WHERE id = :id AND basket_id = :bid",
            {"qty": req.quantity, "id": item_id, "bid": basket["basket_id"]}
        )
        return {"message": "Quantity updated"}

# ---------- 5. Clear Basket ----------
@router.delete("/clear")
async def clear_basket(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    basket = await database.fetch_one(
        "SELECT basket_id FROM baskets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if basket:
        await database.execute(
            "DELETE FROM basket_items WHERE basket_id = :bid",
            {"bid": basket["basket_id"]}
        )
    return {"message": "Basket cleared"}

# ---------- 6. Checkout (Place Reservations) ----------
@router.post("/checkout")
async def checkout(
    req: CheckoutRequest,
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user["id"]

    basket = await database.fetch_one(
        "SELECT basket_id FROM baskets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    items = await database.fetch_all(
        """
        SELECT bi.*, l.price AS current_price, l.quantity_available, l.store_id,
               l.title AS product_name
        FROM basket_items bi
        JOIN listings l ON bi.listing_id = l.listing_id
        WHERE bi.basket_id = :bid
        """,
        {"bid": basket["basket_id"]}
    )

    if not items:
        raise HTTPException(status_code=400, detail="Basket is empty")

    store_totals = {}
    total_order = 0

    for item in items:
        if item["quantity_available"] is not None and item["quantity_available"] < item["quantity"]:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough stock for {item['product_name']}. Only {item['quantity_available']} available."
            )

        store_id = item["store_id"]
        if store_id not in store_totals:
            store_totals[store_id] = {
                "store_id": store_id,
                "items": [],
                "subtotal": 0,
                "delivery_fee": 0,
                "fulfillment_type": "pickup"
            }
        unit_price = item["current_price"]
        subtotal = item["quantity"] * unit_price
        store_totals[store_id]["items"].append({
            "listing_id": item["listing_id"],
            "quantity": item["quantity"],
            "price": unit_price,
            "subtotal": subtotal
        })
        store_totals[store_id]["subtotal"] += subtotal
        total_order += subtotal

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not wallet or wallet["balance"] < total_order:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow()
    expires_at = datetime.utcnow() + timedelta(hours=2)

    await database.execute(
        """
        INSERT INTO orders (order_id, user_id, total_amount, status, created_at, updated_at, expires_at)
        VALUES (:oid, :uid, :total, 'pending', :now, :now, :exp)
        """,
        {"oid": order_id, "uid": user_id, "total": total_order, "now": now, "exp": expires_at}
    )

    for store_id, store_data in store_totals.items():
        store_order_id = f"so_{uuid.uuid4().hex[:10]}"
        await database.execute(
            """
            INSERT INTO order_stores (order_id, store_id, subtotal, delivery_fee, fulfillment_type, status, created_at, updated_at)
            VALUES (:oid, :sid, :subtotal, :dfee, :ftype, 'pending', :now, :now)
            """,
            {
                "oid": order_id,
                "sid": store_id,
                "subtotal": store_data["subtotal"],
                "dfee": store_data.get("delivery_fee", 0),
                "ftype": store_data["fulfillment_type"],
                "now": now
            }
        )
        order_store_row = await database.fetch_one(
            "SELECT id FROM order_stores WHERE order_id = :oid AND store_id = :sid",
            {"oid": order_id, "sid": store_id}
        )
        order_store_id = order_store_row["id"]

        for item in store_data["items"]:
            await database.execute(
                """
                INSERT INTO order_items (order_id, listing_id, store_id, quantity, price, subtotal)
                VALUES (:oid, :lid, :sid, :qty, :price, :subtotal)
                """,
                {
                    "oid": order_id,
                    "lid": item["listing_id"],
                    "sid": store_id,
                    "qty": item["quantity"],
                    "price": item["price"],
                    "subtotal": item["subtotal"]
                }
            )

            await database.execute(
                "UPDATE listings SET quantity_available = quantity_available - :qty WHERE listing_id = :lid",
                {"qty": item["quantity"], "lid": item["listing_id"]}
            )

        storekeeper_row = await database.fetch_one(
            "SELECT owner_id FROM stores WHERE store_id = :sid",
            {"sid": store_id}
        )
        if not storekeeper_row:
            continue
        storekeeper_id = storekeeper_row["owner_id"]

        escrow_id = f"esc_{uuid.uuid4().hex[:12]}"
        await database.execute(
            """
            INSERT INTO escrow (order_id, order_store_id, shopper_id, storekeeper_id,
                                listing_id, quantity, item_amount, delivery_fee, total_amount, status, expires_at)
            VALUES (:oid, :osid, :sid, :skid, :lid, :qty, :item_amt, :dfee, :total, 'locked', :exp)
            """,
            {
                "oid": order_id,
                "osid": order_store_id,
                "sid": user_id,
                "skid": storekeeper_id,
                "lid": store_data["items"][0]["listing_id"],
                "qty": sum(item["quantity"] for item in store_data["items"]),
                "item_amt": store_data["subtotal"],
                "dfee": store_data.get("delivery_fee", 0),
                "total": store_data["subtotal"] + store_data.get("delivery_fee", 0),
                "exp": expires_at
            }
        )

        await database.execute(
            "UPDATE order_stores SET escrow_id = :eid WHERE id = :osid",
            {"eid": escrow_id, "osid": order_store_id}
        )

    await database.execute(
        "UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid",
        {"amt": total_order, "uid": user_id}
    )

    await database.execute(
        "DELETE FROM basket_items WHERE basket_id = :bid",
        {"bid": basket["basket_id"]}
    )

    for store_id in store_totals.keys():
        store = await database.fetch_one(
            "SELECT owner_id FROM stores WHERE store_id = :sid",
            {"sid": store_id}
        )
        if store:
            await send_push_to_user(
                store["owner_id"],
                "New Order!",
                f"A shopper placed an order. Order #{order_id[:8]}",
                {"order_id": order_id}
            )

    return {
        "order_id": order_id,
        "total": total_order,
        "status": "pending",
        "message": "Reservations placed successfully"
    }