# app/services/seai_agent.py
import re
from typing import Dict, Any, Tuple, Optional
from app.db.database import database
from app.utils import haversine
from app.routes.shopper import (
    _geo_recall,
    _forage_recall,
    _trending_recall,
    _following_recall,
)

# ── All intent patterns (GPT + Agent) ──────────────────────────
INTENT_PATTERNS = [
    # GPT intents
    ("book_service", [
        r"(book|schedule|reserve)\s+(a|an)?\s*(?P<service>.+?)(\s+for\s+(?P<date>.+))?",
        r"i\s*want\s*to\s*book\s+(?P<service>.+)",
    ]),
    ("search", [
        r"(search|find|look\s+for|show\s+me)\s+(?P<query>.+)",
        r"(near|around)\s+(me|here)\s*(?P<query>.+)?",
        r"(new|latest|fresh)\s*(?P<query>.+)?",
        r"(popular|trending|hot)\s*(?P<query>.+)?",
        r"(following|followed|saved)\s*(?P<query>.+)?",
    ]),
    ("get_store_info", [
        r"(info|details)\s+(of|about)\s+(store\s+)?(?P<store>.+)",
        r"what\s+does\s+(?P<store>.+?)\s+(sell|offer|have)",
    ]),
    # Agent intents
    ("reserve_item", [
        r"(reserve|hold|set aside)\s+(?P<quantity>\d+)?\s*(?P<listing_name>.+)",
        r"i\s*want\s*to\s*reserve\s+(?P<listing_name>.+)",
    ]),
    ("create_listing", [
        r"(add|create|list)\s+(a|an)?\s*(new\s+)?listing\s+(for\s+)?(?P<title>.+?)\s*(₦|price\s*)?(?P<price>\d+)",
        r"add\s+item\s+(?P<title>.+)",
    ]),
    ("create_service", [
        r"(add|create|set up)\s+(a|an)?\s*(new\s+)?service\s+(for\s+)?(?P<title>.+?)\s*(₦|price\s*)?(?P<price>\d+)",
        r"new\s+service\s+(?P<title>.+)",
    ]),
    ("message_storekeeper", [
        r"(message|chat|text)\s+(with\s+)?(?P<storekeeper_id>.+)",
    ]),
    ("arrange_shelf", [
        r"(arrange|organise|organize)\s+(my\s+)?shelf",
    ]),
]

def classify_intent(text: str) -> Tuple[str, Dict[str, Any]]:
    text_lower = text.lower()
    for intent, patterns in INTENT_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                params = match.groupdict()
                params = {k: v.strip() for k, v in params.items() if v}
                return intent, params
    return "general_qa", {}

def _infer_recall_strategy(query: str) -> str:
    query_lower = query.lower()
    if any(w in query_lower for w in ["near", "close", "around", "proximity", "nearby"]):
        return "geo"
    if any(w in query_lower for w in ["new", "latest", "fresh", "just arrived", "recent"]):
        return "forage"
    if any(w in query_lower for w in ["popular", "trending", "hot", "top", "best"]):
        return "trending"
    if any(w in query_lower for w in ["following", "followed", "my stores", "saved"]):
        return "following"
    return "geo"

# ── Service recall ─────────────────────────────────────────────
async def _service_recall(query: str, lat: float, lng: float, radius_km: float, limit: int = 5):
    rows = await database.fetch_all(
        "SELECT s.service_id, s.title, s.price, s.lat, s.lng, s.image_url, s.provider_id, "
        "u.business_name, u.business_image_url "
        "FROM services s JOIN users u ON s.provider_id = u.id "
        "WHERE s.is_active = TRUE AND s.title ILIKE :q "
        "ORDER BY s.created_at DESC LIMIT 100",
        {"q": f"%{query}%"}
    )
    results = []
    for row in rows:
        row_dict = dict(row)
        dist = haversine(lat, lng, row_dict["lat"], row_dict["lng"])
        if dist <= radius_km:
            results.append({
                "type": "service",
                "id": row_dict["service_id"],
                "title": row_dict["title"],
                "price": row_dict["price"],
                "distance_km": round(dist, 3),
                "image_url": row_dict["image_url"],
                "business_name": row_dict["business_name"] or "Service Provider",
                "business_image_url": row_dict["business_image_url"],
                "provider_id": row_dict["provider_id"],
            })
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]

# ── Store recall ───────────────────────────────────────────────
async def _store_recall(query: str, lat: float, lng: float, radius_km: float, limit: int = 5):
    rows = await database.fetch_all(
        "SELECT store_id, name, description, latitude, longitude, store_image_url "
        "FROM stores WHERE name ILIKE :q "
        "ORDER BY name LIMIT 100",
        {"q": f"%{query}%"}
    )
    results = []
    for row in rows:
        row_dict = dict(row)
        if row_dict["latitude"] is None or row_dict["longitude"] is None:
            continue
        dist = haversine(lat, lng, row_dict["latitude"], row_dict["longitude"])
        if dist <= radius_km:
            results.append({
                "type": "store",
                "id": row_dict["store_id"],
                "title": row_dict["name"],
                "distance_km": round(dist, 3),
                "image_url": row_dict["store_image_url"],
                "description": row_dict.get("description", ""),
            })
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]

# ── Unified search (items + services + stores) ─────────────────
async def handle_search_items(params: dict, lat: float = 6.5, lng: float = 3.4, user_id: str = None) -> dict:
    query = params.get("query", "")
    if not query:
        return {"type": "error", "message": "What are you looking for?"}

    strategy = _infer_recall_strategy(query)

    # 1. Items from existing recall
    try:
        if strategy == "geo":
            items = await _geo_recall(lat, lng, radius_km=50, limit=10)
        elif strategy == "forage":
            items = await _forage_recall(lat, lng, radius_km=50, limit=10)
        elif strategy == "trending":
            items = await _trending_recall(lat, lng, radius_km=50, limit=10)
        elif strategy == "following" and user_id:
            items = await _following_recall(user_id, lat, lng, radius_km=50, limit=10)
        else:
            items = await _geo_recall(lat, lng, radius_km=50, limit=10)
    except Exception:
        items = []

    # 2. Services
    try:
        services = await _service_recall(query, lat, lng, radius_km=50, limit=5)
    except Exception:
        services = []

    # 3. Stores
    try:
        stores = await _store_recall(query, lat, lng, radius_km=50, limit=5)
    except Exception:
        stores = []

    # 4. Combine all results with type labels
    all_results = []
    for it in items:
        all_results.append({
            "type": "item",
            "listing_id": it["listing_id"],
            "title": it.get("title", "No Title"),
            "price": it.get("price"),
            "distance_km": it.get("distance_km", 0),
            "image_url": it.get("image_url"),
            "store_name": it.get("store_name", "Unknown"),
            "store_id": it.get("store_id"),
        })
    for sv in services:
        all_results.append({
            "type": "service",
            "service_id": sv["id"],
            "title": sv["title"],
            "price": sv.get("price"),
            "distance_km": sv["distance_km"],
            "image_url": sv["image_url"],
            "provider_name": sv["business_name"],
            "provider_image_url": sv["business_image_url"],
            "provider_id": sv["provider_id"],
        })
    for st in stores:
        all_results.append({
            "type": "store",
            "store_id": st["id"],
            "title": st["title"],
            "distance_km": st["distance_km"],
            "image_url": st["image_url"],
            "description": st.get("description", ""),
        })

    all_results.sort(key=lambda x: x["distance_km"])
    top = all_results[:10]

    if not top:
        return {"type": "text", "text": "I couldn't find any items, services, or stores matching that."}

    return {
        "type": "action",
        "intent": "search_results",
        "data": {
            "query": query,
            "results": top,
        }
    }

# ── Book a service (GPT) ───────────────────────────────────────
async def handle_book_service(user_id: str, params: dict) -> dict:
    service_name = params.get("service", "")
    if not service_name:
        return {"type": "error", "message": "I need to know which service you want to book."}
    rows = await database.fetch_all(
        "SELECT s.service_id, s.title, s.price, u.business_name "
        "FROM services s JOIN users u ON s.provider_id = u.id "
        "WHERE s.is_active = TRUE AND s.title ILIKE :name LIMIT 1",
        {"name": f"%{service_name}%"}
    )
    if not rows:
        return {"type": "error", "message": f"No service found matching '{service_name}'."}
    service = dict(rows[0])
    return {
        "type": "action",
        "intent": "book_service",
        "data": {
            "service_id": service["service_id"],
            "title": service["title"],
            "price": service["price"],
            "provider_name": service["business_name"],
            "action_label": "Book Now",
            "confirm_text": f"Book {service['title']} for ₦{service['price']}?",
        }
    }

# ── Get store info (GPT) ───────────────────────────────────────
async def handle_get_store_info(params: dict) -> dict:
    store_name = params.get("store", "")
    if not store_name:
        return {"type": "error", "message": "Which store are you asking about?"}
    store = await database.fetch_one(
        "SELECT store_id, name, description, address, store_image_url "
        "FROM stores WHERE name ILIKE :name LIMIT 1",
        {"name": f"%{store_name}%"}
    )
    if not store:
        return {"type": "text", "text": f"I couldn't find a store called '{store_name}'."}
    store = dict(store)
    return {
        "type": "action",
        "intent": "get_store_info",
        "data": {
            "store_id": store["store_id"],
            "name": store["name"],
            "description": store["description"] or "No description.",
            "address": store["address"],
            "image_url": store["store_image_url"],
            "action_label": "View Store",
        }
    }

# Placeholder recall helpers (unused but kept for compatibility)
async def _embedding_recall(user_id: str, lat: float, lng: float, radius_km: float, limit: int):
    return []

async def _collab_recall(user_id: str, lat: float, lng: float, radius_km: float, limit: int):
    return []