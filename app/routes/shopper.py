import math
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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

# ✅ New models for wanted alerts
class WantedAlertCreate(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    notes: Optional[str] = Field(None, max_length=500)
    category: Optional[str] = Field(None, max_length=50)
    budget: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class WantedAlertResponse(BaseModel):
    id: int
    user_id: str
    title: str
    notes: Optional[str] = None
    category: Optional[str] = None
    budget: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_active: bool = True
    created_at: Optional[str] = None
    expires_at: Optional[str] = None

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
    if not req.candidate_ids:
        return {"feed": [], "total": 0, "model_used": model is not None}

    now = datetime.utcnow()
    id_params = {f"id_{i}": cid for i, cid in enumerate(req.candidate_ids)}
    placeholders = ",".join(f":{k}" for k in id_params.keys())
    rows = await database.fetch_all(
        f"SELECT l.*, s.name AS store_name FROM listings l "
        f"JOIN stores s ON l.store_id = s.store_id "
        f"WHERE l.listing_id IN ({placeholders}) AND l.quantity_available > 0",
        id_params,
    )

    if not rows:
        return {"feed": [], "total": 0, "model_used": model is not None}

    if model is None:
        rows_by_id = {row["listing_id"]: row for row in rows}
        fallback_feed = []
        for cid in req.candidate_ids:
            row = rows_by_id.get(cid)
            if row is None:
                continue
            dist = haversine(req.lat, req.lng, row["lat"], row["lng"])
            created = _to_datetime(row["created_at"])
            mins = max((now - created).total_seconds() / 60.0, 0) if created else 0
            image_url = row["image_url"] if "image_url" in row else None
            fallback_feed.append({
                "listing_id": row["listing_id"],
                "title": row["title"],
                "price": row["price"],
                "distance_km": round(dist, 3),
                "score": 0.5,
                "minutes_since_listed": round(mins, 1),
                "title_quality": row["title_quality"] if row["title_quality"] is not None else 0.5,
                "image_url": image_url,
                "store_name": row["store_name"] if "store_name" in row else row["store_id"],
                "store_id": row["store_id"],
                "fallback": True,
            })
            if len(fallback_feed) >= 20:
                break
        return {"feed": fallback_feed, "total": len(fallback_feed), "model_used": False}

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

    if len(final_feed) < min(20, len(scored)):
        for item in scored:
            if item["listing_id"] in shown_set:
                continue
            final_feed.append(item)
            shown_set.add(item["listing_id"])
            if len(final_feed) >= 20:
                break

    return {"feed": final_feed, "total": len(final_feed), "model_used": True}

# ==================== RECALL HELPERS ====================
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
    return []

async def _collab_recall(user_id: str, lat: float, lng: float, radius_km: float, limit: int):
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

# ==================== SAVED ITEMS LIST ====================
@router.get("/saved")
async def get_saved_items(current_user: dict = Depends(get_current_user)):
    """
    Return the current user's saved listings, joined with listing + store
    details so the frontend has everything it needs to render cards.
    """
    user_id = current_user["id"]
    rows = await database.fetch_all(
        """
        SELECT
            si.listing_id,
            si.created_at AS saved_at,
            l.title,
            l.price,
            l.image_url,
            l.store_id,
            s.name AS store_name
        FROM saved_items si
        LEFT JOIN listings l ON si.listing_id = l.listing_id
        LEFT JOIN stores   s ON l.store_id     = s.store_id
        WHERE si.user_id = :uid
        ORDER BY si.created_at DESC
        """,
        {"uid": user_id},
    )
    return [dict(row) for row in rows]

# ==================== WANTED ALERTS ====================
@router.get("/wanted")
async def list_wanted_alerts(current_user: dict = Depends(get_current_user)):
    """
    Return the current user's own wanted alerts (things they posted that
    they want to buy). Active first, then newest.
    """
    rows = await database.fetch_all(
        """
        SELECT id, user_id, title, notes, category, budget, lat, lng,
               is_active, created_at, expires_at
        FROM wanted_alerts
        WHERE user_id = :uid
        ORDER BY is_active DESC, created_at DESC
        """,
        {"uid": current_user["id"]},
    )
    return [dict(row) for row in rows]

@router.post("/wanted", status_code=201)
async def create_wanted_alert(
    req: WantedAlertCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a wanted alert. Auto-expires after 30 days."""
    if req.budget is not None and req.budget < 0:
        raise HTTPException(status_code=400, detail="Budget cannot be negative")

    now = datetime.utcnow()
    expires_at = now + timedelta(days=30)

    row = await database.fetch_one(
        """
        INSERT INTO wanted_alerts
            (user_id, title, notes, category, budget, lat, lng, is_active, created_at, expires_at)
        VALUES
            (:uid, :title, :notes, :cat, :budget, :lat, :lng, TRUE, :now, :expires)
        RETURNING id, user_id, title, notes, category, budget, lat, lng,
                  is_active, created_at, expires_at
        """,
        {
            "uid": current_user["id"],
            "title": req.title.strip(),
            "notes": req.notes.strip() if req.notes else None,
            "cat": req.category.strip() if req.category else None,
            "budget": req.budget,
            "lat": req.lat,
            "lng": req.lng,
            "now": now,
            "expires": expires_at,
        },
    )
    return dict(row) if row else {"message": "Created"}

@router.delete("/wanted/{alert_id}")
async def delete_wanted_alert(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Delete one of the user's own wanted alerts."""
    row = await database.fetch_one(
        "SELECT user_id FROM wanted_alerts WHERE id = :aid",
        {"aid": alert_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    if row["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your alert")

    await database.execute(
        "DELETE FROM wanted_alerts WHERE id = :aid",
        {"aid": alert_id},
    )
    return {"message": "Deleted", "id": alert_id}

@router.patch("/wanted/{alert_id}/toggle")
async def toggle_wanted_alert(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Toggle active/paused for one of the user's own wanted alerts."""
    row = await database.fetch_one(
        "SELECT user_id, is_active FROM wanted_alerts WHERE id = :aid",
        {"aid": alert_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    if row["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your alert")

    new_state = not row["is_active"]
    await database.execute(
        "UPDATE wanted_alerts SET is_active = :state WHERE id = :aid",
        {"state": new_state, "aid": alert_id},
    )
    return {"id": alert_id, "is_active": new_state}

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