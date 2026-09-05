import re
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List, Literal
from datetime import datetime
from app.db.database import database
from app.utils import haversine

router = APIRouter(prefix="/seai", tags=["SEAI Search"])

class SearchRequest(BaseModel):
    query: str
    lat: float
    lng: float
    radius_km: float = 10.0
    page: int = 0
    limit: int = 20

class SearchResult(BaseModel):
    listing_id: str
    title: str
    price: float
    distance_km: float
    score: float
    title_quality: float
    minutes_since_listed: float
    image_url: Optional[str] = None
    store_id: str
    store_name: Optional[str] = None
    location_name: Optional[str] = None
    type: Literal["product", "service", "store"] = "product"

class SuggestionResult(BaseModel):
    label: str
    type: Literal["product", "service", "store"]
    listing_id: str

def parse_datetime(value):
    if value is None:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.utcnow()
    try:
        return datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return datetime.utcnow()

def _get_coords(row) -> Optional[tuple]:
    keys = set(row.keys())
    for lat_col, lng_col in [
        ("lat", "lng"), ("lat", "lon"),
        ("latitude", "longitude"),
        ("store_lat", "store_lng"), ("store_lat", "store_lon"),
        ("store_latitude", "store_longitude"),
    ]:
        if lat_col in keys and lng_col in keys:
            lat_val, lng_val = row[lat_col], row[lng_col]
            if lat_val is not None and lng_val is not None:
                return float(lat_val), float(lng_val)
    return None

def _dedupe(results: list) -> list:
    seen, unique = set(), []
    for r in results:
        key = (r["type"], r["listing_id"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique

# ---------------------------------------------------------------------------
# Suggest endpoint (autocomplete)
# ---------------------------------------------------------------------------

@router.get("/suggest", response_model=List[SuggestionResult])
async def seai_suggest(
    q: str = Query(..., min_length=1),
    lat: float = Query(...),
    lng: float = Query(...),
    radius_km: float = Query(10.0),
    limit: int = Query(10),
):
    pattern = f"{q.strip().lower()}%"
    suggestions: List[dict] = []

    # Products
    rows = await database.fetch_all(
        "SELECT * FROM listings WHERE quantity_available > 0 AND LOWER(title) LIKE :p LIMIT 50",
        {"p": pattern},
    )
    for row in rows:
        coords = _get_coords(row)
        if coords is None:
            continue
        dist = haversine(lat, lng, *coords)
        if dist <= radius_km:
            suggestions.append({
                "label": row["title"],
                "type": "product",
                "listing_id": row["listing_id"],
                "_dist": dist,
            })

    # Services
    rows = await database.fetch_all(
        "SELECT * FROM services WHERE LOWER(title) LIKE :p LIMIT 50",
        {"p": pattern},
    )
    for row in rows:
        coords = _get_coords(row)
        if coords is None:
            continue
        dist = haversine(lat, lng, *coords)
        if dist <= radius_km:
            suggestions.append({
                "label": row["title"],
                "type": "service",
                "listing_id": row["service_id"],
                "_dist": dist,
            })

    # Stores
    rows = await database.fetch_all(
        "SELECT * FROM stores WHERE LOWER(name) LIKE :p LIMIT 50",
        {"p": pattern},
    )
    for row in rows:
        coords = _get_coords(row)
        if coords is None:
            continue
        dist = haversine(lat, lng, *coords)
        if dist <= radius_km:
            suggestions.append({
                "label": row["name"],
                "type": "store",
                "listing_id": row["store_id"],
                "_dist": dist,
            })

    suggestions.sort(key=lambda x: x["_dist"])
    return [
        {"label": s["label"], "type": s["type"], "listing_id": s["listing_id"]}
        for s in suggestions[:limit]
    ]

# ---------------------------------------------------------------------------
# Full search helpers
# ---------------------------------------------------------------------------

async def _search_products(lat, lng, radius_km, pattern=None):
    if pattern:
        like = f"%{pattern.lower()}%"
        rows = await database.fetch_all(
            """
            SELECT l.*, s.name AS store_name
            FROM listings l
            LEFT JOIN stores s ON l.store_id = s.store_id
            WHERE l.quantity_available > 0
              AND LOWER(l.title) LIKE :p
            """,
            {"p": like},
        )
    else:
        rows = await database.fetch_all(
            """
            SELECT l.*, s.name AS store_name
            FROM listings l
            LEFT JOIN stores s ON l.store_id = s.store_id
            WHERE l.quantity_available > 0
            """
        )
    now = datetime.utcnow()
    results = []
    for row in rows:
        coords = _get_coords(row)
        if coords is None:
            continue
        dist = haversine(lat, lng, *coords)
        if dist > radius_km:
            continue
        created_dt = parse_datetime(row["created_at"])
        results.append({
            "listing_id": row["listing_id"],
            "title": row["title"],
            "price": row["price"],
            "distance_km": round(dist, 3),
            "score": round(1.0 / (1.0 + dist), 4),
            "title_quality": row["title_quality"] if "title_quality" in row.keys() else 1.0,
            "minutes_since_listed": (now - created_dt).total_seconds() / 60.0,
            "image_url": row["image_url"] if "image_url" in row.keys() else None,
            "store_id": row["store_id"],
            "store_name": row["store_name"] or row["store_id"],
            "location_name": f"{dist:.1f} km away",
            "type": "product",
        })
    return results

async def _search_services(lat, lng, radius_km, pattern=None):
    if pattern:
        like = f"%{pattern.lower()}%"
        rows = await database.fetch_all(
            """
            SELECT * FROM services
            WHERE LOWER(title) LIKE :p
               OR LOWER(COALESCE(description,'')) LIKE :p
            """,
            {"p": like},
        )
    else:
        rows = await database.fetch_all("SELECT * FROM services")
    now = datetime.utcnow()
    results = []
    for row in rows:
        coords = _get_coords(row)
        if coords is None:
            continue
        dist = haversine(lat, lng, *coords)
        if dist > radius_km:
            continue
        created_dt = parse_datetime(row["created_at"])
        results.append({
            "listing_id": row["service_id"],
            "title": row["title"],
            "price": row["price"],
            "distance_km": round(dist, 3),
            "score": round(1.0 / (1.0 + dist), 4),
            "title_quality": 1.0,
            "minutes_since_listed": (now - created_dt).total_seconds() / 60.0,
            "image_url": None,
            "store_id": row["provider_id"],
            "store_name": "Service Provider",
            "location_name": f"{dist:.1f} km away",
            "type": "service",
        })
    return results

async def _search_stores(lat, lng, radius_km, pattern=None):
    if pattern:
        like = f"%{pattern.lower()}%"
        rows = await database.fetch_all(
            """
            SELECT * FROM stores
            WHERE LOWER(name) LIKE :p
               OR LOWER(COALESCE(description,'')) LIKE :p
            """,
            {"p": like},
        )
    else:
        rows = await database.fetch_all("SELECT * FROM stores")
    now = datetime.utcnow()
    results = []
    for row in rows:
        coords = _get_coords(row)
        if coords is None:
            continue
        dist = haversine(lat, lng, *coords)
        if dist > radius_km:
            continue
        created_dt = parse_datetime(row["created_at"])
        results.append({
            "listing_id": row["store_id"],
            "title": row["name"],
            "price": 0.0,
            "distance_km": round(dist, 3),
            "score": round(1.0 / (1.0 + dist), 4),
            "title_quality": 1.0,
            "minutes_since_listed": (now - created_dt).total_seconds() / 60.0,
            "image_url": row["image_url"] if "image_url" in row.keys() else None,
            "store_id": row["store_id"],
            "store_name": row["name"],
            "location_name": f"{dist:.1f} km away",
            "type": "store",
        })
    return results

# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@router.post("/search", response_model=List[SearchResult])
async def seai_search(req: SearchRequest):
    pattern = req.query.strip() or None

    results = (
        await _search_products(req.lat, req.lng, req.radius_km, pattern)
        + await _search_services(req.lat, req.lng, req.radius_km, pattern)
        + await _search_stores(req.lat, req.lng, req.radius_km, pattern)
    )

    # If no query, show everything within radius.
    # If query exists and no matches -> empty list (no fallback).
    if not pattern:
        results = (
            await _search_products(req.lat, req.lng, req.radius_km)
            + await _search_services(req.lat, req.lng, req.radius_km)
            + await _search_stores(req.lat, req.lng, req.radius_km)
        )

    results = _dedupe(results)
    results.sort(key=lambda x: x["score"], reverse=True)
    start = req.page * req.limit
    return results[start : start + req.limit]