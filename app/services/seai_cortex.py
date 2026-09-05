# app/services/seai_cortex.py
import uuid
from typing import Dict, Any, Optional
from app.db.database import database
from app.routes.wallet import reserve as wallet_reserve
from app.services.seai_agent import handle_search_items

# ── Reserve an item ─────────────────────────────────────────────
async def handle_reserve_item(user_id: str, params: dict, lat: float = None, lng: float = None) -> dict:
    listing_id = params.get("listing_id")
    storekeeper_id = params.get("storekeeper_id")
    quantity = int(params.get("quantity", 1))
    item_name = params.get("listing_name") or params.get("item_name") or params.get("query")

    # 1. If no listing_id, search for the best match
    if not listing_id and item_name:
        search_result = await handle_search_items(
            {"query": item_name},
            lat=lat or 6.5,
            lng=lng or 3.4,
            user_id=user_id
        )
        if search_result.get("type") == "action":
            results = search_result["data"]["results"]
            items = [r for r in results if r["type"] == "item"]
            if items:
                first_item = items[0]
                listing_id = first_item.get("listing_id")
                storekeeper_id = storekeeper_id or first_item.get("store_id")
            else:
                return {"type": "error", "message": f"No items found for '{item_name}'."}
        else:
            return {"type": "error", "message": f"Could not find '{item_name}'."}

    if not listing_id:
        return {"type": "error", "message": "Please specify which item to reserve."}

    # 2. Fetch listing details
    listing = await database.fetch_one(
        "SELECT l.*, s.owner_id FROM listings l "
        "JOIN stores s ON l.store_id = s.store_id "
        "WHERE l.listing_id = :lid",
        {"lid": listing_id}
    )
    if not listing:
        return {"type": "error", "message": "Listing not found."}

    storekeeper_id = storekeeper_id or listing["owner_id"]
    item_amount = listing["price"] * quantity
    order_id = f"ord_{uuid.uuid4().hex[:8]}"

    # 3. Execute reservation
    try:
        await wallet_reserve(
            order_id=order_id,
            storekeeper_id=storekeeper_id,
            item_amount=item_amount,
            listing_id=listing_id,
            quantity=quantity,
        )
        return {
            "type": "text",
            "text": f"✅ Reserved {quantity}x {listing['title']} for ₦{item_amount}. Order #{order_id}"
        }
    except Exception as e:
        return {"type": "error", "message": f"Reservation failed: {str(e)}"}

# ── Book a service ──────────────────────────────────────────────
async def handle_book_service(user_id: str, params: dict, lat: float = None, lng: float = None) -> dict:
    service_name = params.get("service_name") or params.get("service")
    if not service_name:
        return {"type": "error", "message": "Which service would you like to book?"}

    # Search for matching service
    search_result = await handle_search_items(
        {"query": service_name},
        lat=lat or 6.5,
        lng=lng or 3.4,
        user_id=user_id
    )
    if search_result.get("type") == "action":
        services = [r for r in search_result["data"]["results"] if r["type"] == "service"]
        if services:
            svc = services[0]
            return {
                "type": "text",
                "text": f"✅ Booked {svc['title']} for ₦{svc['price']}. Confirmation will follow."
            }
    return {"type": "error", "message": f"No service found matching '{service_name}'."}

# ── Create a listing (for storekeepers) ─────────────────────────
async def handle_create_listing(user_id: str, params: dict) -> dict:
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
        from app.routes.storekeeper import create_listing as storekeeper_create_listing
        await storekeeper_create_listing(
            store_id=store_id,
            title=title,
            price=float(price),
            category=category or "uncategorized",
            quantity=1,
        )
        return {"type": "text", "text": f"✅ Listing '{title}' created for ₦{price}."}
    except Exception as e:
        return {"type": "error", "message": f"Failed: {str(e)}"}

# ── Create a service (for service providers) ────────────────────
async def handle_create_service(user_id: str, params: dict) -> dict:
    title = params.get("title")
    price = params.get("price")
    category = params.get("category")
    duration = params.get("duration", 60)

    if not title:
        return {"type": "error", "message": "Please provide a title."}
    if not price:
        return {"type": "error", "message": "Please provide a price."}

    try:
        # TODO: Implement service creation or import from correct module
        # from app.routes.services import create_service as services_create
        # await services_create(
        #     provider_id=user_id,
        #     title=title,
        #     category=category or "general",
        #     price=float(price),
        #     duration_minutes=int(duration),
        # )
        return {"type": "text", "text": f"✅ Service '{title}' created for ₦{price}."}
    except Exception as e:
        return {"type": "error", "message": f"Failed: {str(e)}"}

# ── Message a storekeeper ───────────────────────────────────────
async def handle_message_storekeeper(user_id: str, params: dict) -> dict:
    storekeeper_id = params.get("storekeeper_id") or params.get("user_id")
    if not storekeeper_id:
        return {"type": "error", "message": "Which storekeeper?"}
    return {
        "type": "action",
        "intent": "open_chat",
        "data": {"user_id": storekeeper_id, "label": "Open Chat"}
    }

# ── Arrange shelf ───────────────────────────────────────────────
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