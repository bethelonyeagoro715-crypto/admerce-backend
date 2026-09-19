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

INTENT_PATTERNS = [
    ("book_service", [
        r"(book|schedule|reserve)\s+(a|an)?\s*(?P<service>.+?)(\s+for\s+(?P<date>.+))?",
        r"i\s*want\s*to\s*book\s+(?P<service>.+)",
    ]),
    ("search", [
        r"(search|find|look\s+for|show\s+me)\s+(?P<query>.+)",
        r"i\s*(?:am|'?m)\s+looking\s+for\s+(?P<query>.+)",
        r"i\s+want\s+(?:to\s+(?:see|buy|find)\s+)?(?P<query>.+)",
        r"i\s+need\s+(?:to\s+(?:see|buy|find)\s+)?(?P<query>.+)",
        r"i'?d\s+like\s+(?:to\s+)?(?:see|find|buy)\s+(?P<query>.+)",
        r"(near|around)\s+(me|here)\s*(?P<query>.+)?",
        r"(?:can\s+i\s+see|do\s+you\s+have|where\s+can\s+i\s+(?:get|find|buy))\s+(?P<query>.+)",
    ]),
    ("get_store_info", [
        r"(info|details)\s+(of|about)\s+(store\s+)?(?P<store>.+)",
        r"what\s+does\s+(?P<store>.+?)\s+(sell|offer|have)",
        r"tell\s+me\s+about\s+(store\s+)?(?P<store>.+)",
    ]),
    ("reserve_item", [
        r"(reserve|hold|set aside)\s+(?P<quantity>\d+)?\s*(?P<listing_name>.+)",
    ]),
    ("create_listing", [
        r"(add|create|list)\s+(a|an)?\s*(new\s+)?listing\s+(for\s+)?(?P<title>.+?)\s*(₦|price\s*)?(?P<price>\d+)",
    ]),
    ("create_service", [
        r"(add|create|set up)\s+(a|an)?\s*(new\s+)?service\s+(for\s+)?(?P<title>.+?)\s*(₦|price\s*)?(?P<price>\d+)",
    ]),
    ("message_storekeeper", [
        r"(message|chat|text)\s+(with\s+)?(?P<storekeeper_id>.+)",
    ]),
    ("arrange_shelf", [
        r"(arrange|organise|organize)\s+(my\s+)?shelf",
    ]),
]

STOP_WORDS = {"me", "a", "an", "the", "for", "some", "any", "please"}


def _clean_query(q: str) -> str:
    words = [w for w in q.strip().split() if w.lower() not in STOP_WORDS]
    return " ".join(words) if words else q.strip()


def classify_intent(text: str) -> Tuple[str, Dict[str, Any]]:
    text_lower = text.lower()
    for intent, patterns in INTENT_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                params = match.groupdict()
                params = {k: _clean_query(v) if k == "query" else v.strip()
                          for k, v in params.items() if v}
                return intent, params
    return "general_qa", {}


def _infer_recall_strategy(query: str) -> str:
    ql = query.lower()
    if any(w in ql for w in ["near", "close", "around", "proximity", "nearby"]):
        return "geo"
    if any(w in ql for w in ["new", "latest", "fresh", "just arrived", "recent"]):
        return "forage"
    if any(w in ql for w in ["popular", "trending", "hot", "top", "best"]):
        return "trending"
    if any(w in ql for w in ["following", "followed", "my stores", "saved"]):
        return "following"
    return "geo"


def _travel_minutes(distance_km: float) -> int:
    """Rough city-driving estimate: ~30 km/h average in Nigerian cities."""
    if distance_km <= 0:
        return 0
    return max(1, int(round(distance_km * 2)))


def _directions_url(lat: float, lng: float, label: str = "") -> str:
    """Google Maps directions from the user's current location to (lat, lng)."""
    from urllib.parse import quote_plus
    base = "https://www.google.com/maps/dir/?api=1"
    dest = f"{lat},{lng}"
    url = f"{base}&destination={quote_plus(dest)}&travelmode=driving"
    if label:
        url += f"&destination_place_id={quote_plus(label)}"
    return url


async def _enrich_items(listing_ids: list[str]) -> Dict[str, dict]:
    if not listing_ids:
        return {}

    id_params = {f"id_{i}": lid for i, lid in enumerate(listing_ids)}
    placeholders = ",".join(f":{k}" for k in id_params.keys())

    rows = await database.fetch_all(
        f"""
        SELECT
            l.listing_id,
            l.title,
            l.price,
            l.image_url,
            l.store_id,
            l.lat            AS listing_lat,
            l.lng            AS listing_lng,
            s.name           AS store_name,
            s.store_image_url AS store_image_url,
            s.address        AS store_address,
            s.latitude       AS store_lat,
            s.longitude      AS store_lng
        FROM listings l
        LEFT JOIN stores s ON l.store_id = s.store_id
        WHERE l.listing_id IN ({placeholders})
        """,
        id_params,
    )
    return {row["listing_id"]: dict(row) for row in rows}


async def _service_recall(query: str, lat: float, lng: float, radius_km: float, limit: int = 5):
    rows = await database.fetch_all(
        "SELECT s.service_id, s.title, s.price, s.lat, s.lng, s.image_url, s.provider_id, "
        "s.address, "
        "u.business_name, u.business_image_url "
        "FROM services s JOIN users u ON s.provider_id = u.id "
        "WHERE s.is_active = TRUE AND s.title ILIKE :q "
        "ORDER BY s.created_at DESC LIMIT 100",
        {"q": f"%{query}%"}
    )
    results = []
    for row in rows:
        d = dict(row)
        dist = haversine(lat, lng, d["lat"], d["lng"])
        if dist <= radius_km:
            results.append({
                "type": "service",
                "id": d["service_id"],
                "title": d["title"],
                "price": d["price"],
                "distance_km": round(dist, 3),
                "image_url": d["image_url"],
                "business_name": d["business_name"] or "Service Provider",
                "business_image_url": d["business_image_url"],
                "provider_id": d["provider_id"],
                "lat": d["lat"],
                "lng": d["lng"],
                "address": d.get("address"),
            })
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]


async def _store_recall(query: str, lat: float, lng: float, radius_km: float, limit: int = 5):
    rows = await database.fetch_all(
        "SELECT store_id, name, description, address, latitude, longitude, store_image_url "
        "FROM stores WHERE name ILIKE :q "
        "ORDER BY name LIMIT 100",
        {"q": f"%{query}%"}
    )
    results = []
    for row in rows:
        d = dict(row)
        if d["latitude"] is None or d["longitude"] is None:
            continue
        dist = haversine(lat, lng, d["latitude"], d["longitude"])
        if dist <= radius_km:
            results.append({
                "type": "store",
                "id": d["store_id"],
                "title": d["name"],
                "distance_km": round(dist, 3),
                "image_url": d["store_image_url"],
                "description": d.get("description", ""),
                "address": d.get("address"),
                "lat": d["latitude"],
                "lng": d["longitude"],
            })
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]


async def handle_search_items(params: dict, lat: float = 6.5, lng: float = 3.4, user_id: str = None) -> dict:
    query = params.get("query", "").strip()
    if not query:
        return {"type": "text", "text": "What are you looking for?"}

    strategy = _infer_recall_strategy(query)

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

    try:
        enriched = await _enrich_items([it["listing_id"] for it in items])
    except Exception as e:
        print(f"⚠️  enrich error: {e}")
        enriched = {}

    try:
        services = await _service_recall(query, lat, lng, radius_km=50, limit=5)
    except Exception:
        services = []

    try:
        stores = await _store_recall(query, lat, lng, radius_km=50, limit=5)
    except Exception:
        stores = []

    all_results = []

    for it in items:
        lid = it["listing_id"]
        meta = enriched.get(lid, {})

        r_lat = meta.get("store_lat") if meta.get("store_lat") is not None else meta.get("listing_lat")
        r_lng = meta.get("store_lng") if meta.get("store_lng") is not None else meta.get("listing_lng")

        if r_lat is not None and r_lng is not None:
            dist = haversine(lat, lng, r_lat, r_lng)
        else:
            dist = it.get("distance_km", 0)
            r_lat, r_lng = None, None

        all_results.append({
            "type": "item",
            "listing_id": lid,
            "title": meta.get("title") or it.get("title") or "No Title",
            "price": meta.get("price"),
            "distance_km": round(dist, 2),
            "travel_minutes": _travel_minutes(dist),
            "image_url": meta.get("image_url"),
            "store_name": meta.get("store_name") or "Local Store",
            "store_id": meta.get("store_id") or it.get("store_id"),
            "store_image_url": meta.get("store_image_url"),
            "address": meta.get("store_address"),
            "latitude": r_lat,
            "longitude": r_lng,
            "directions_url": _directions_url(r_lat, r_lng) if r_lat and r_lng else None,
        })

    for sv in services:
        all_results.append({
            "type": "service",
            "service_id": sv["id"],
            "title": sv["title"],
            "price": sv.get("price"),
            "distance_km": sv["distance_km"],
            "travel_minutes": _travel_minutes(sv["distance_km"]),
            "image_url": sv["image_url"],
            "provider_name": sv["business_name"],
            "provider_image_url": sv["business_image_url"],
            "provider_id": sv["provider_id"],
            "address": sv.get("address"),
            "latitude": sv.get("lat"),
            "longitude": sv.get("lng"),
            "directions_url": _directions_url(sv["lat"], sv["lng"]) if sv.get("lat") and sv.get("lng") else None,
        })

    for st in stores:
        all_results.append({
            "type": "store",
            "store_id": st["id"],
            "title": st["title"],
            "distance_km": st["distance_km"],
            "travel_minutes": _travel_minutes(st["distance_km"]),
            "image_url": st["image_url"],
            "description": st.get("description", ""),
            "address": st.get("address"),
            "latitude": st.get("latitude"),
            "longitude": st.get("longitude"),
            "directions_url": _directions_url(st["latitude"], st["longitude"]) if st.get("latitude") and st.get("longitude") else None,
        })

    all_results.sort(key=lambda x: x["distance_km"])
    top = all_results[:10]

    if not top:
        return {
            "type": "text",
            "text": (
                f"Nothing near you currently matches '{query}'. "
                "Try a broader term, or ask me to post a wanted alert so you're notified when one is listed."
            ),
        }

    return {
        "type": "action",
        "intent": "search_results",
        "data": {"query": query, "results": top},
    }


async def handle_book_service(user_id: str, params: dict) -> dict:
    service_name = params.get("service", "")
    if not service_name:
        return {"type": "text", "text": "Which service would you like to book?"}
    rows = await database.fetch_all(
        "SELECT s.service_id, s.title, s.price, u.business_name "
        "FROM services s JOIN users u ON s.provider_id = u.id "
        "WHERE s.is_active = TRUE AND s.title ILIKE :name LIMIT 1",
        {"name": f"%{service_name}%"}
    )
    if not rows:
        return {"type": "text", "text": f"No service found matching '{service_name}'."}
    s = dict(rows[0])
    return {
        "type": "action",
        "intent": "book_service",
        "data": {
            "service_id": s["service_id"],
            "title": s["title"],
            "price": s["price"],
            "provider_name": s["business_name"],
            "action_label": "Book Now",
        },
    }


async def handle_get_store_info(params: dict) -> dict:
    name = params.get("store", "")
    if not name:
        return {"type": "text", "text": "Which store are you asking about?"}
    st = await database.fetch_one(
        "SELECT store_id, name, description, address, latitude, longitude, store_image_url "
        "FROM stores WHERE name ILIKE :name LIMIT 1",
        {"name": f"%{name}%"},
    )
    if not st:
        return {"type": "text", "text": f"I couldn't find a store called '{name}'."}
    st = dict(st)
    return {
        "type": "action",
        "intent": "get_store_info",
        "data": {
            "store_id": st["store_id"],
            "name": st["name"],
            "description": st["description"] or "No description.",
            "address": st["address"],
            "image_url": st["store_image_url"],
            "latitude": st["latitude"],
            "longitude": st["longitude"],
            "directions_url": _directions_url(st["latitude"], st["longitude"]) if st["latitude"] and st["longitude"] else None,
        },
    }


async def _embedding_recall(user_id, lat, lng, radius_km, limit):
    return []


async def _collab_recall(user_id, lat, lng, radius_km, limit):
    return []