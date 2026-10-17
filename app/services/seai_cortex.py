# app/services/seai_cortex.py
import uuid
from typing import Dict, Any, Optional
from fastapi import HTTPException

from app.db.database import database
# Removed handle_search_items import to do direct fuzzy DB search

# ── Reserve an item (auto‑search if no listing_id) ─────────────
async def handle_reserve_item(user_id: str, params: dict, lat: float = None, lng: float = None) -> dict:
    from app.routes.wallet import reserve as wallet_reserve_route, ReserveRequest

    listing_id = params.get("listing_id")
    storekeeper_id = params.get("storekeeper_id")
    quantity = int(params.get("quantity", 1))
    item_name = params.get("listing_name") or params.get("item_name") or params.get("query")
    store_name = params.get("store_name")

    # 1. If no listing_id, search directly using fuzzy matching
    if not listing_id and item_name:
        # Base query
        query = """
            SELECT l.listing_id, l.title, l.price, l.quantity_available, s.owner_id as storekeeper_id
            FROM listings l
            JOIN stores s ON l.store_id = s.store_id
            WHERE l.quantity_available > 0
        """
        args = {}

        if store_name:
            query += " AND s.name ILIKE :sname"
            args["sname"] = f"%{store_name}%"

        row = None
        
        # Step A: Try exact phrase (e.g., "meatpie" or "meat pies")
        args["q"] = f"%{item_name.strip()}%"
        row = await database.fetch_one(query + " AND l.title ILIKE :q LIMIT 1", args)
        
        if not row:
            # Step B: Try splitting words (e.g., "meatpie" -> "meat")
            first_word = item_name.split()[0] if item_name.split() else item_name
            if len(first_word) >= 3:
                args["q"] = f"%{first_word}%"
                row = await database.fetch_one(query + " AND l.title ILIKE :q LIMIT 1", args)
                
        if not row and "meatpie" in item_name.lower():
            # Step C: Hardcoded fallback for the "meat pie" vs "meatpie" case
            args["q"] = "%meat%"
            row = await database.fetch_one(query + " AND l.title ILIKE :q LIMIT 1", args)

        if not row:
            msg = f"No items found for '{item_name}'"
            if store_name:
                msg += f" at '{store_name}'"
            return {"type": "error", "message": f"{msg}. Try a different search term."}

        listing_id = row["listing_id"]
        storekeeper_id = row["storekeeper_id"]
        listing_title = row["title"]
        price = row["price"]
    else:
        # Fetch listing details directly if listing_id was provided
        row = await database.fetch_one(
            "SELECT l.*, s.owner_id FROM listings l "
            "JOIN stores s ON l.store_id = s.store_id "
            "WHERE l.listing_id = :lid",
            {"lid": listing_id}
        )
        if not row:
            return {"type": "error", "message": "Listing not found."}
        
        storekeeper_id = storekeeper_id or row["owner_id"]
        listing_title = row["title"]
        price = row["price"]

    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    item_amount = float(price) * quantity

    # 3. Execute reservation via the new Anchor wallet route
    try:
        req = ReserveRequest(
            order_id=order_id,
            storekeeper_id=storekeeper_id,
            listing_id=listing_id,
            quantity=quantity,
        )
        current_user = {"id": user_id}
        
        await wallet_reserve_route(req, current_user)
        
        return {
            "type": "text",
            "text": f"✅ Reserved {quantity}x {listing_title} for ₦{item_amount:,.0f}. Order #{order_id}"
        }
    except HTTPException as e:
        # Catch Anchor 503 or 400 errors gracefully
        return {"type": "error", "message": f"Reservation failed: {e.detail}"}
    except Exception as e:
        return {"type": "error", "message": f"Reservation failed: {str(e)}"}

# ── Book a service (auto‑search if needed) ──────────────────────
async def handle_book_service(user_id: str, params: dict, lat: float = None, lng: float = None) -> dict:
    from app.routes.service import book_service as book_service_route, BookServiceRequest

    service_name = params.get("service_name") or params.get("service")
    if not service_name:
        return {"type": "error", "message": "Which service would you like to book?"}

    # Use exact DB search for services
    row = await database.fetch_one(
        "SELECT s.service_id, s.title, s.price FROM services s "
        "WHERE s.is_active = TRUE AND s.title ILIKE :q LIMIT 1",
        {"q": f"%{service_name.strip()}%"}
    )

    if not row:
        return {"type": "error", "message": f"No service found matching '{service_name}'."}

    try:
        req = BookServiceRequest(
            notes=f"Booked via SEAI agent",
        )
        current_user = {"id": user_id}
        
        await book_service_route(row["service_id"], req, current_user)
        
        return {
            "type": "text",
            "text": f"✅ Booked {row['title']} for ₦{row['price']}. Confirmation will follow."
        }
    except HTTPException as e:
        return {"type": "error", "message": f"Booking failed: {e.detail}"}
    except Exception as e:
        return {"type": "error", "message": f"Booking failed: {str(e)}"}

# ── Create a listing (for storekeepers) ─────────────────────────
async def handle_create_listing(user_id: str, params: dict) -> dict:
    from app.routes.storekeeper import create_listing as storekeeper_create_listing

    title = params.get("title")
    price = params.get("price")
    category = params.get("category")
    store_id = params.get("store_id")

    if not title:
        return {"type": "error", "message": "Please provide a title."}
    if not price:
        return {"type": "error", "message": "Please provide a price."}

    if not store_id:
        store = await database.fetch_one(
            "SELECT store_id FROM stores WHERE owner_id = :uid LIMIT 1",
            {"uid": user_id}
        )
        if not store:
            return {"type": "error", "message": "You don't have a store yet."}
        store_id = store["store_id"]

    try:
        current_user = {"id": user_id}
        
        await storekeeper_create_listing(
            request=None, 
            current_user=current_user,
            store_id=store_id,
            title=title,
            price=float(price),
            lat=6.5,
            lng=3.4,
            category=category or "tech_electronics",
            quantity=1
        )
        return {"type": "text", "text": f"✅ Listing '{title}' created for ₦{price}."}
    except HTTPException as e:
        return {"type": "error", "message": f"Failed: {e.detail}"}
    except Exception as e:
        return {"type": "error", "message": f"Failed: {str(e)}"}

# ── Create a service (for service providers) ────────────────────
async def handle_create_service(user_id: str, params: dict) -> dict:
    from app.routes.service import create_service as services_create_route, CreateServiceRequest

    title = params.get("title")
    price = params.get("price")
    category = params.get("category")
    duration = params.get("duration", 60)

    if not title:
        return {"type": "error", "message": "Please provide a title."}
    if not price:
        return {"type": "error", "message": "Please provide a price."}

    try:
        req = CreateServiceRequest(
            title=title,
            category=category or "general",
            description="",
            price=float(price),
            duration_minutes=int(duration),
            location_lat=6.5,
            location_lng=3.4
        )
        current_user = {"id": user_id}
        
        await services_create_route(req, current_user)
        
        return {"type": "text", "text": f"✅ Service '{title}' created for ₦{price}."}
    except HTTPException as e:
        return {"type": "error", "message": f"Failed: {e.detail}"}
    except Exception as e:
        return {"type": "error", "message": f"Failed: {str(e)}"}

# ── Message a storekeeper (opens chat) ──────────────────────────
async def handle_message_storekeeper(user_id: str, params: dict) -> dict:
    storekeeper_id = params.get("storekeeper_id") or params.get("user_id")
    if not storekeeper_id:
        return {"type": "error", "message": "Which storekeeper?"}
    return {
        "type": "action",
        "intent": "open_chat",
        "data": {"user_id": storekeeper_id, "label": "Open Chat"}
    }

# ── Arrange shelf (opens visual shelf editor) ───────────────────
async def handle_arrange_shelf(user_id: str, params: dict) -> dict:
    return {
        "type": "action",
        "intent": "open_shelf_editor",
        "data": {"label": "Arrange Shelf"}
    }

# ── Handler mapping ─────────────────────────────────────────────
AGENT_HANDLERS = {
    "reserve_item": handle_reserve_item,
    "book_service": handle_book_service,
    "create_listing": handle_create_listing,
    "create_service": handle_create_service,
    "message_storekeeper": handle_message_storekeeper,
    "arrange_shelf": handle_arrange_shelf,
}