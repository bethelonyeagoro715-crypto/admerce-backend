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
        r"(add|create|set\s+up)\s+(a|an)?\s*(new\s+)?service\s+(for\s+)?(?P<title>.+?)\s*(₦|price\s*)?(?P<price>\d+)",
    ]),
    ("message_storekeeper", [
        r"(message|chat|text)\s+(with\s+)?(?P<storekeeper_id>.+)",
    ]),
    ("arrange_shelf", [
        r"(arrange|organise|organize)\s+(my\s+)?shelf",
    ]),
]

STOP_WORDS = {
    "me", "a", "an", "the", "for", "some", "any", "please",
    # Verbs/fillers that would pollute the search filter
    "i", "you", "need", "want", "looking", "look", "find", "show",
    "search", "see", "buy", "get", "give", "help", "can", "could",
    "would", "have", "has", "is", "are", "was", "were", "be",
    "to", "of", "in", "on", "at", "with", "from", "my", "your",
    "and", "or", "but", "if", "as", "so",
}


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


def _tokenize(text: str) -> list[str]:
    """Extract meaningful tokens for filtering a search query."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


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


# ════════════════════════════════════════════════════════════
# ✅ NEW — direct query-relevant item search
# Filters by the user's actual query FIRST, then ranks by
# (relevance, distance). Only returns items the user asked for.
# ════════════════════════════════════════════════════════════
async def _query_relevant_items(
    query: str,
    lat: float,
    lng: float,
    radius_km: float = 50.0,
    limit: int = 5,
) -> list[dict]:
    tokens = _tokenize(query)
    if not tokens:
        return []

    # Build OR conditions across title / category / store name
    or_parts = []
    params: dict = {}
    for i, tok in enumerate(tokens):
        or_parts.append(
            f"LOWER(l.title) LIKE :w{i} "
            f"OR LOWER(COALESCE(l.category, '')) LIKE :w{i} "
            f"OR LOWER(COALESCE(s.name, '')) LIKE :w{i}"
        )
        params[f"w{i}"] = f"%{tok}%"

    where = " OR ".join(f"({p})" for p in or_parts)

    # Relevance score — title match weights 3, category 2, store name 1
    score_terms = []
    for i, tok in enumerate(tokens):
        score_terms.append(f"CASE WHEN LOWER(l.title) LIKE :w{i} THEN 3 ELSE 0 END")
        score_terms.append(
            f"CASE WHEN LOWER(COALESCE(l.category, '')) LIKE :w{i} THEN 2 ELSE 0 END"
        )
        score_terms.append(
            f"CASE WHEN LOWER(COALESCE(s.name, '')) LIKE :w{i} THEN 1 ELSE 0 END"
        )
    score_expr = " + ".join(score_terms)

    sql = f"""
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
            s.longitude      AS store_lng,
            ({score_expr})   AS relevance
        FROM listings l
        LEFT JOIN stores s ON l.store_id = s.store_id
        WHERE ({where})
          AND (l.quantity_available IS NULL OR l.quantity_available > 0)
        LIMIT 100
    """

    try:
        rows = await database.fetch_all(sql, params)
    except Exception as e:
        print(f"⚠️  _query_relevant_items sql error: {e}")
        return []

    scored = []
    for row in rows:
        d = dict(row)
        relevance = int(d.get("relevance") or 0)
        if relevance <= 0:
            continue

        item_lat = d.get("store_lat") if d.get("store_lat") is not None else d.get("listing_lat")
        item_lng = d.get("store_lng") if d.get("store_lng") is not None else d.get("listing_lng")

        if item_lat is None or item_lng is None:
            # No coords — can't compute distance. Rank it last but keep it.
            d["distance_km"] = 9999.0
        else:
            dist = haversine(lat, lng, item_lat, item_lng)
            if dist > radius_km:
                continue
            d["distance_km"] = dist

        # ✅ Combined score — relevance dominates (×100), distance breaks ties
        d["_score"] = relevance * 100 - d["distance_km"]
        scored.append(d)

    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored[:limit]


# ════════════════════════════════════════════════════════════
# Service recall — unchanged but capped at 3
# ════════════════════════════════════════════════════════════
async def _service_recall(query: str, lat: float, lng: float, radius_km: float, limit: int = 3):
    tokens = _tokenize(query)
    if not tokens:
        return []

    or_parts = []
    params: dict = {}
    for i, tok in enumerate(tokens):
        or_parts.append(f"LOWER(s.title) LIKE :w{i}")
        params[f"w{i}"] = f"%{tok}%"

    where = " OR ".join(f"({p})" for p in or_parts)

    rows = await database.fetch_all(
        f"""
        SELECT s.service_id, s.title, s.price, s.lat, s.lng, s.image_url,
               s.provider_id, s.address,
               u.business_name, u.business_image_url
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.is_active = TRUE
          AND ({where})
        LIMIT 50
        """,
        params,
    )
    results = []
    for row in rows:
        d = dict(row)
        if d["lat"] is None or d["lng"] is None:
            continue
        dist = haversine(lat, lng, d["lat"], d["lng"])
        if dist > radius_km:
            continue
        results.append({
            "type": "service",
            "id": d["service_id"],
            "title": d["title"],
            "price": d["price"],
            "distance_km": round(dist, 2),
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


# ════════════════════════════════════════════════════════════
# Store recall — capped at 3
# ════════════════════════════════════════════════════════════
async def _store_recall(query: str, lat: float, lng: float, radius_km: float, limit: int = 3):
    tokens = _tokenize(query)
    if not tokens:
        return []

    or_parts = []
    params: dict = {}
    for i, tok in enumerate(tokens):
        or_parts.append(f"LOWER(name) LIKE :w{i}")
        params[f"w{i}"] = f"%{tok}%"

    where = " OR ".join(f"({p})" for p in or_parts)

    rows = await database.fetch_all(
        f"""
        SELECT store_id, name, description, address, latitude, longitude, store_image_url
        FROM stores
        WHERE ({where})
        LIMIT 50
        """,
        params,
    )
    results = []
    for row in rows:
        d = dict(row)
        if d["latitude"] is None or d["longitude"] is None:
            continue
        dist = haversine(lat, lng, d["latitude"], d["longitude"])
        if dist > radius_km:
            continue
        results.append({
            "type": "store",
            "id": d["store_id"],
            "title": d["name"],
            "distance_km": round(dist, 2),
            "image_url": d["store_image_url"],
            "description": d.get("description", ""),
            "address": d.get("address"),
            "lat": d["latitude"],
            "lng": d["longitude"],
        })
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]


# ════════════════════════════════════════════════════════════
# ✅ REWRITTEN — strict query filter, tiered fallback, capped output
# ════════════════════════════════════════════════════════════
async def handle_search_items(
    params: dict,
    lat: float = 6.5,
    lng: float = 3.4,
    user_id: str = None,
) -> dict:
    query = params.get("query", "").strip()
    if not query:
        return {"type": "text", "text": "What are you looking for?"}

    # ── Tier 1: items matching the query ─────────────────────
    items = await _query_relevant_items(query, lat, lng, radius_km=50, limit=5)

    # ── Tier 2 (only if no items): services matching the query
    services = []
    if not items:
        try:
            services = await _service_recall(query, lat, lng, radius_km=50, limit=3)
        except Exception:
            services = []

    # ── Tier 3 (only if no items AND no services): stores
    stores = []
    if not items and not services:
        try:
            stores = await _store_recall(query, lat, lng, radius_km=50, limit=3)
        except Exception:
            stores = []

    all_results: list[dict] = []

    for d in items:
        r_lat = d.get("store_lat") if d.get("store_lat") is not None else d.get("listing_lat")
        r_lng = d.get("store_lng") if d.get("store_lng") is not None else d.get("listing_lng")
        all_results.append({
            "type": "item",
            "listing_id": d["listing_id"],
            "title": d["title"] or "No Title",
            "price": d["price"],
            "distance_km": round(d["distance_km"], 2),
            "travel_minutes": _travel_minutes(d["distance_km"]),
            "image_url": d.get("image_url"),
            "store_name": d.get("store_name") or "Local Store",
            "store_id": d.get("store_id"),
            "store_image_url": d.get("store_image_url"),
            "address": d.get("store_address"),
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
            "directions_url": (
                _directions_url(sv["lat"], sv["lng"])
                if sv.get("lat") and sv.get("lng") else None
            ),
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
            "directions_url": (
                _directions_url(st["latitude"], st["longitude"])
                if st.get("latitude") and st.get("longitude") else None
            ),
        })

    # Cap at 5 — even across the tiers
    top = all_results[:5]

    if not top:
        return {
            "type": "text",
            "text": (
                f"Nothing near you currently matches '{query}'. "
                "Try a broader term, or ask me to post a wanted alert so "
                "you're notified when one is listed."
            ),
        }

    return {
        "type": "action",
        "intent": "search_results",
        "data": {"query": query, "results": top},
    }


# ── Book service ───────────────────────────────────────────────
async def handle_book_service(user_id: str, params: dict) -> dict:
    service_name = params.get("service", "")
    if not service_name:
        return {"type": "text", "text": "Which service would you like to book?"}
    rows = await database.fetch_all(
        "SELECT s.service_id, s.title, s.price, u.business_name "
        "FROM services s JOIN users u ON s.provider_id = u.id "
        "WHERE s.is_active = TRUE AND s.title ILIKE :name LIMIT 1",
        {"name": f"%{service_name}%"},
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


# ── Store info ─────────────────────────────────────────────────
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
            "directions_url": (
                _directions_url(st["latitude"], st["longitude"])
                if st["latitude"] and st["longitude"] else None
            ),
        },
    }


# ── Placeholder recall functions (kept for import compatibility) ─
async def _embedding_recall(user_id, lat, lng, radius_km, limit):
    return []


async def _collab_recall(user_id, lat, lng, radius_km, limit):
    return []