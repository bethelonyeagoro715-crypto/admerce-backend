import math
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from app.db.database import database
from app.utils import model, haversine
from app.routes.auth import get_current_user, get_optional_user
from app.services.image_embedder import json_to_embedding, cosine_similarity

router = APIRouter(prefix="/shopper", tags=["Shopper"])

# ---------- Helper ----------
def _to_datetime(value):
    """Return a datetime object from either a string or an existing datetime."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value) if value else None

# ---------- Models ----------
class FeedItem(BaseModel):
    listing_id: str
    distance: float
    position: int
    minutes_since_listed: int
    title_quality: float

class FeedRequest(BaseModel):
    items: list[FeedItem]

class RecallRequest(BaseModel):
    lat: float
    lng: float
    radius_km: float = 10.0
    mix: Optional[Dict[str, float]] = None

class RankRequest(BaseModel):
    lat: float
    lng: float
    candidate_ids: List[str]
    session_items_shown: List[str] = []

# ---------- Existing ranking endpoint (legacy) ----------
@router.post("/feed")
async def rank_feed(req: FeedRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not available")
    scored = []
    for item in req.items:
        X = [[item.position, item.distance, item.minutes_since_listed, item.title_quality]]
        score = model.predict_proba(X)[0, 1]
        scored.append({
            "listing_id": item.listing_id,
            "score": round(score, 4),
            "distance": item.distance,
            "position": item.position,
            "minutes_since_listed": item.minutes_since_listed,
            "title_quality": item.title_quality
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"ranked_feed": scored}

# ---------- Real feed (public, simple) ----------
@router.get("/real-feed")
async def real_feed(lat: float, lng: float):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not available")
    rows = await database.fetch_all(
        "SELECT l.*, s.name AS store_name FROM listings l "
        "JOIN stores s ON l.store_id = s.store_id "
    )
    now = datetime.utcnow()
    scored = []
    for row in rows:
        dist = haversine(lat, lng, row["lat"], row["lng"])
        created = _to_datetime(row["created_at"])
        mins = max((now - created).total_seconds() / 60.0, 0) if created else 0
        pos = 1 if row["price"] < 15000 else 2
        X = [[pos, dist, mins, row["title_quality"]]]
        score = model.predict_proba(X)[0, 1]
        image_url = row["image_url"] if "image_url" in row else None
        scored.append({
            "listing_id": row["listing_id"],
            "title": row["title"],
            "price": row["price"],
            "distance_km": round(dist, 3),
            "score": round(score, 4),
            "minutes_since_listed": round(mins, 1),
            "title_quality": row["title_quality"],
            "image_url": image_url,
            "store_name": row["store_name"] if "store_name" in row else row["store_id"],
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"feed": scored}

# ---------- Listing detail ----------
@router.get("/listing/{listing_id}")
async def get_listing(listing_id: str):
    row = await database.fetch_one("SELECT * FROM listings WHERE listing_id = :lid", {"lid": listing_id})
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    return dict(row)

# ==================== PUBLIC RECALL ====================
@router.post("/feed/recall")
async def feed_recall(req: RecallRequest, current_user: Optional[dict] = Depends(get_optional_user)):
    user_id = current_user["id"] if current_user else None

    weights = {
        "geo": 0.40,
        "forage": 0.15,
        "trending": 0.10,
        "following": 0.10,
        "embedding": 0.15,
        "collab": 0.10,
    }
    if req.mix:
        weights.update(req.mix)

    total_candidates = 500
    candidates: Dict[str, dict] = {}

    # 1. Geo
    try:
        geo_limit = int(total_candidates * weights["geo"])
        geo_items = await _geo_recall(req.lat, req.lng, req.radius_km, geo_limit)
        for item in geo_items:
            candidates[item["listing_id"]] = {
                "listing_id": item["listing_id"],
                "title": item.get("title", ""),
                "pool": "geo",
                "distance_km": item.get("distance_km", 0),
            }
    except Exception as e:
        print(f"geo recall error: {e}")

    # 2. Forage
    try:
        forage_limit = int(total_candidates * weights["forage"])
        forage_items = await _forage_recall(req.lat, req.lng, req.radius_km, forage_limit)
        for item in forage_items:
            if item["listing_id"] not in candidates:
                candidates[item["listing_id"]] = {"listing_id": item["listing_id"], "pool": "forage"}
    except Exception as e:
        print(f"forage recall error: {e}")

    # 3. Trending
    try:
        trending_limit = int(total_candidates * weights["trending"])
        trending_items = await _trending_recall(req.lat, req.lng, req.radius_km, trending_limit)
        for item in trending_items:
            if item["listing_id"] not in candidates:
                candidates[item["listing_id"]] = {"listing_id": item["listing_id"], "pool": "trending"}
    except Exception as e:
        print(f"trending recall error: {e}")

    # 4. Following
    if user_id:
        try:
            following_limit = int(total_candidates * weights["following"])
            following_items = await _following_recall(user_id, req.lat, req.lng, req.radius_km, following_limit)
            for item in following_items:
                if item["listing_id"] not in candidates:
                    candidates[item["listing_id"]] = {"listing_id": item["listing_id"], "pool": "following"}
        except Exception as e:
            print(f"following recall error: {e}")

    # 5. Embedding
    if user_id:
        try:
            embed_limit = int(total_candidates * weights["embedding"])
            embed_items = await _embedding_recall(user_id, req.lat, req.lng, req.radius_km, embed_limit)
            for item in embed_items:
                if item["listing_id"] not in candidates:
                    candidates[item["listing_id"]] = {"listing_id": item["listing_id"], "pool": "embedding"}
        except Exception as e:
            print(f"embedding recall error: {e}")

    # 6. Collaborative
    if user_id:
        try:
            collab_limit = int(total_candidates * weights["collab"])
            collab_items = await _collab_recall(user_id, req.lat, req.lng, req.radius_km, collab_limit)
            for item in collab_items:
                if item["listing_id"] not in candidates:
                    candidates[item["listing_id"]] = {"listing_id": item["listing_id"], "pool": "collab"}
        except Exception as e:
            print(f"collab recall error: {e}")

    result = list(candidates.values())[:total_candidates]
    return {"candidates": result, "total": len(result), "mix_weights": weights}

# ==================== RANK ENDPOINT ====================
@router.post("/feed/rank")
async def rank_feed_endpoint(req: RankRequest, current_user: Optional[dict] = Depends(get_optional_user)):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not available")

    if not req.candidate_ids:
        return {"feed": [], "total": 0}

    now = datetime.utcnow()
    placeholders = ','.join(f"'{cid}'" for cid in req.candidate_ids)
    rows = await database.fetch_all(
        f"SELECT l.*, s.name AS store_name FROM listings l "
        f"JOIN stores s ON l.store_id = s.store_id "
        f"WHERE l.listing_id IN ({placeholders}) AND l.quantity_available > 0"
    )

    if not rows:
        return {"feed": [], "total": 0}

    scored = []
    for row in rows:
        dist = haversine(req.lat, req.lng, row["lat"], row["lng"])
        created = _to_datetime(row["created_at"])
        mins = max((now - created).total_seconds() / 60.0, 0) if created else 0
        pos = 1 if (row["price"] or 0) < 15000 else 2
        features = [pos, round(dist, 3), round(mins, 1), row["title_quality"] or 0.5]
        try:
            score = model.predict_proba([features])[0, 1]
        except Exception:
            score = 0.5
        image_url = row["image_url"] if "image_url" in row else None
        scored.append({
            "listing_id": row["listing_id"],
            "title": row["title"],
            "price": row["price"],
            "distance_km": round(dist, 3),
            "score": round(score, 4),
            "minutes_since_listed": round(mins, 1),
            "title_quality": row["title_quality"],
            "image_url": image_url,
            "store_name": row["store_name"] if "store_name" in row else row["store_id"],
            "store_id": row["store_id"],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Re‑ranking policies (Diversity & Burst)
    final_feed = []
    store_counter = {}
    shown_set = set(req.session_items_shown)
    for item in scored:
        lid = item["listing_id"]
        sid = item["store_id"]
        if lid in shown_set:
            continue
        if store_counter.get(sid, 0) >= 2:
            continue
        final_feed.append(item)
        shown_set.add(lid)
        store_counter[sid] = store_counter.get(sid, 0) + 1

    # Fallback
    if len(final_feed) < min(20, len(scored)):
        final_feed = [item for item in scored if item["listing_id"] not in shown_set][:20]

    return {"feed": final_feed, "total": len(final_feed)}

# ==================== RECALL HELPERS (with TEXT→TIMESTAMP casts) ====================
async def _geo_recall(lat: float, lng: float, radius_km: float, limit: int):
    lat_diff = radius_km / 111.0
    lng_diff = radius_km / (111.0 * abs(math.cos(math.radians(lat))) + 1e-8)
    rows = await database.fetch_all(
        "SELECT listing_id, title, lat, lng FROM listings "
        "WHERE quantity_available > 0 AND lat BETWEEN :min_lat AND :max_lat "
        "AND lng BETWEEN :min_lng AND :max_lng "
        "ORDER BY created_at DESC LIMIT :lim",
        {"min_lat": lat - lat_diff, "max_lat": lat + lat_diff,
         "min_lng": lng - lng_diff, "max_lng": lng + lng_diff, "lim": limit}
    )
    return [dict(row) | {"distance_km": round(haversine(lat, lng, row["lat"], row["lng"]), 3)} for row in rows]

async def _forage_recall(lat: float, lng: float, radius_km: float, limit: int):
    rows = await database.fetch_all(
        "SELECT listing_id FROM listings WHERE quantity_available > 0 "
        "AND title_quality < 0.5 AND created_at::timestamp > NOW() - INTERVAL '2 days' "
        "ORDER BY created_at DESC LIMIT :lim", {"lim": limit}
    )
    return [dict(row) for row in rows]

async def _trending_recall(lat: float, lng: float, radius_km: float, limit: int):
    rows = await database.fetch_all(
        "SELECT l.listing_id FROM listings l "
        "JOIN listing_events e ON l.listing_id = e.listing_id "
        "WHERE l.quantity_available > 0 "
        "AND e.created_at::timestamp > NOW() - INTERVAL '1 day' "
        "AND e.event_type IN ('click', 'reserve') "
        "GROUP BY l.listing_id ORDER BY COUNT(*) DESC LIMIT :lim",
        {"lim": limit}
    )
    return [dict(row) for row in rows]

async def _following_recall(user_id: str, lat: float, lng: float, radius_km: float, limit: int):
    rows = await database.fetch_all(
        "SELECT l.listing_id FROM listings l "
        "JOIN stores s ON l.store_id = s.store_id "
        "JOIN favorites f ON s.store_id = f.store_id "
        "WHERE f.user_id = :uid AND l.quantity_available > 0 "
        "ORDER BY l.created_at DESC LIMIT :lim",
        {"uid": user_id, "lim": limit}
    )
    return [dict(row) for row in rows]

async def _embedding_recall(user_id: str, lat: float, lng: float, radius_km: float, limit: int):
    # PLACEHOLDER – implement when user embeddings are stored
    return []

async def _collab_recall(user_id: str, lat: float, lng: float, radius_km: float, limit: int):
    # PLACEHOLDER – requires collaborative filtering model
    return []

# ==================== FOLLOW / UNFOLLOW STORE ====================
@router.post("/{store_id}/follow")
async def follow_store(store_id: str, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "INSERT INTO favorites (user_id, store_id) VALUES (:uid, :sid) ON CONFLICT DO NOTHING",
        {"uid": current_user["id"], "sid": store_id}
    )
    return {"message": "Followed"}

@router.delete("/{store_id}/unfollow")
async def unfollow_store(store_id: str, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "DELETE FROM favorites WHERE user_id = :uid AND store_id = :sid",
        {"uid": current_user["id"], "sid": store_id}
    )
    return {"message": "Unfollowed"}

@router.get("/{store_id}/follow-status")
async def get_follow_status(store_id: str, current_user: dict = Depends(get_current_user)):
    row = await database.fetch_one(
        "SELECT * FROM favorites WHERE user_id = :uid AND store_id = :sid",
        {"uid": current_user["id"], "sid": store_id}
    )
    return {"following": row is not None}

# ==================== SAVE / UNSAVE LISTING ====================
@router.post("/save/{listing_id}")
async def save_listing(listing_id: str, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "INSERT INTO saved_items (user_id, listing_id) VALUES (:uid, :lid) ON CONFLICT DO NOTHING",
        {"uid": current_user["id"], "lid": listing_id}
    )
    return {"message": "Saved"}

@router.delete("/save/{listing_id}")
async def unsave_listing(listing_id: str, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "DELETE FROM saved_items WHERE user_id = :uid AND listing_id = :lid",
        {"uid": current_user["id"], "lid": listing_id}
    )
    return {"message": "Unsaved"}

@router.get("/save/{listing_id}/status")
async def get_save_status(listing_id: str, current_user: dict = Depends(get_current_user)):
    row = await database.fetch_one(
        "SELECT * FROM saved_items WHERE user_id = :uid AND listing_id = :lid",
        {"uid": current_user["id"], "lid": listing_id}
    )
    return {"saved": row is not None}

# ==================== PROVIDER SERVICES (public) ====================
@router.get("/provider/{provider_id}")
async def get_provider_services(provider_id: str):
    rows = await database.fetch_all(
        "SELECT * FROM services WHERE provider_id = :pid",
        {"pid": provider_id}
    )
    return rows

    # ── Helper for agentic SEAI search ───────────────────────────
async def search_shopper_items(query: str, lat: float, lng: float, radius_km: float = 10, limit: int = 10):
    """Return up to `limit` items whose title matches the query (ILIKE) and are within `radius_km`."""
    rows = await database.fetch_all(
        "SELECT listing_id, title, price, lat, lng, image_url, store_id "
        "FROM listings WHERE quantity_available > 0 AND title ILIKE :q "
        "ORDER BY created_at DESC LIMIT 100",
        {"q": f"%{query}%"}
    )
    results = []
    for row in rows:
        row_dict = dict(row)
        dist = haversine(lat, lng, row_dict["lat"], row_dict["lng"])
        if dist <= radius_km:
            results.append({
                "listing_id": row_dict["listing_id"],
                "title": row_dict["title"],
                "price": row_dict["price"],
                "distance_km": round(dist, 3),
                "image_url": row_dict["image_url"],
                "store_id": row_dict["store_id"],
            })
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]