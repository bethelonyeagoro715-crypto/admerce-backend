from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from app.services.image_embedder import (
    image_to_embedding,
    json_to_embedding,
    cosine_similarity,
)
from app.services.llm_agent import GEMINI_VISION_MODELS
from app.db.database import database
from app.utils.security import get_current_user
import numpy as np
import os
import uuid
import asyncio
from typing import Optional

router = APIRouter(prefix="/seai/lens", tags=["SEAI Lens"])


def _bbox(lat: float, lng: float, radius_km: float) -> dict:
    lat_diff = radius_km / 111.0
    lng_diff = radius_km / (111.0 * abs(np.cos(np.radians(lat))) + 1e-8)
    return {
        "min_lat": lat - lat_diff,
        "max_lat": lat + lat_diff,
        "min_lng": lng - lng_diff,
        "max_lng": lng + lng_diff,
    }


async def _visual_search(query_emb, lat: float, lng: float, radius_km: float):
    box = _bbox(lat, lng, radius_km)
    try:
        rows = await database.fetch_all(
            "SELECT listing_id, store_id, title, price, image_url, lat, lng, embedding "
            "FROM listings WHERE embedding IS NOT NULL "
            "AND lat BETWEEN :min_lat AND :max_lat "
            "AND lng BETWEEN :min_lng AND :max_lng "
            "LIMIT 500",
            box,
        )
    except Exception as e:
        print(f"⚠️  visual search sql error: {e}", flush=True)
        return []

    scored = []
    for row in rows:
        try:
            stored = json_to_embedding(row["embedding"])
            sim = cosine_similarity(query_emb, stored)
        except Exception:
            continue
        scored.append((sim, dict(row)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:20]


async def _describe_image_with_llm(image_bytes: bytes) -> Optional[str]:
    """Ask Gemini to name the main object in the photo."""
    try:
        import google.generativeai as genai
    except ImportError:
        print("⚠️  google-generativeai not installed", flush=True)
        return None

    if not os.getenv("GEMINI_API_KEY"):
        print("⚠️  GEMINI_API_KEY not set — skipping vision describe", flush=True)
        return None

    for model_name in GEMINI_VISION_MODELS:
        try:
            def _sync():
                model = genai.GenerativeModel(model_name)
                return model.generate_content([
                    "What is the main physical object in this image? "
                    "Reply with ONLY the object name in 2-4 words, no punctuation, "
                    "no explanation. Examples: 'iPhone 14', 'leather handbag', "
                    "'wooden chair', 'cheeseburger', 'plate of rice'. "
                    "If nothing identifiable, reply with a single hyphen.",
                    {"mime_type": "image/jpeg", "data": image_bytes},
                ])

            response = await asyncio.to_thread(_sync)
            text = (getattr(response, "text", "") or "").strip()
            text = text.strip('"\u201c\u201d\u2018\u2019').strip()
            if text and text != "-":
                print(f"🔍 vision described as: {text!r}", flush=True)
                return text
        except Exception as e:
            print(f"⚠️  vision describe ({model_name}) failed: {e}", flush=True)
            continue

    return None


async def _text_fallback_search(
    description: str, lat: float, lng: float, radius_km: float
):
    tokens = [t for t in description.lower().split() if len(t) > 2][:5]
    if not tokens:
        return []

    box = _bbox(lat, lng, radius_km)
    or_parts = []
    params: dict = dict(box)
    for i, tok in enumerate(tokens):
        or_parts.append(
            f"LOWER(l.title) LIKE :w{i} "
            f"OR LOWER(COALESCE(l.category, '')) LIKE :w{i}"
        )
        params[f"w{i}"] = f"%{tok}%"

    where = " OR ".join(f"({p})" for p in or_parts)

    sql = f"""
        SELECT l.listing_id, l.store_id, l.title, l.price, l.image_url,
               l.lat, l.lng,
               s.name           AS store_name,
               s.store_image_url AS store_image_url
        FROM listings l
        LEFT JOIN stores s ON l.store_id = s.store_id
        WHERE ({where})
          AND (l.quantity_available IS NULL OR l.quantity_available > 0)
          AND l.lat BETWEEN :min_lat AND :max_lat
          AND l.lng BETWEEN :min_lng AND :max_lng
        LIMIT 20
    """
    try:
        rows = await database.fetch_all(sql, params)
    except Exception as e:
        print(f"⚠️  text fallback sql error: {e}", flush=True)
        return []

    return [dict(r) for r in rows]


def _build_hint(diagnostics: dict, embed_error: Optional[str]) -> str:
    if diagnostics.get("listings_total", 0) == 0:
        return "There are no listings on Admerce yet."
    if diagnostics.get("listings_with_embeddings", 0) == 0:
        return (
            "Photo search isn't ready yet — listings don't have photo "
            "embeddings. Try a text search while we finish setting this up."
        )
    if diagnostics.get("listings_in_radius", 0) == 0:
        return "No listings near you in the current radius. Try a wider area."
    if embed_error:
        return "Couldn't process that image. Try a different photo."
    return "Couldn't match that photo. Try a clearer image or a text search."


# Both decorators — /seai/lens and /seai/lens/ hit the same handler,
# so no 307 redirect risk.
@router.post("")
@router.post("/")
async def visual_search(
    image: UploadFile = File(...),
    lat: float = Form(...),
    lng: float = Form(...),
    radius_km: float = Form(10),
    current_user: dict = Depends(get_current_user),
):
    # ── 1. Read the upload once ─────────────────────────────────────
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image upload")

    # ── 2. Try to generate a visual embedding ───────────────────────
    os.makedirs("uploads/tmp", exist_ok=True)
    tmp_path = f"uploads/tmp/{uuid.uuid4().hex}.jpg"
    with open(tmp_path, "wb") as f:
        f.write(image_bytes)

    query_emb = None
    embed_error: Optional[str] = None
    try:
        query_emb = image_to_embedding(tmp_path)
    except Exception as e:
        embed_error = str(e)
        print(f"⚠️  image_to_embedding failed: {e}", flush=True)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # ── 3. Visual match attempt ─────────────────────────────────────
    visual_matches: list = []
    if query_emb is not None:
        visual_matches = await _visual_search(query_emb, lat, lng, radius_km)

    if visual_matches:
        return {
            "query_lat": lat,
            "query_lng": lng,
            "radius_km": radius_km,
            "method": "visual",
            "results": [
                {
                    "type": "item",
                    "listing_id": r["listing_id"],
                    "store_id": r["store_id"],
                    "title": r["title"],
                    "price": r["price"],
                    "image_url": r["image_url"],
                    "latitude": r["lat"],
                    "longitude": r["lng"],
                    "similarity": round(sim, 4),
                }
                for sim, r in visual_matches
            ],
        }

    # ── 4. Fallback: describe with vision LLM, then keyword search ──
    description = await _describe_image_with_llm(image_bytes)
    text_matches: list = []
    if description:
        text_matches = await _text_fallback_search(
            description, lat, lng, radius_km
        )

    if text_matches:
        return {
            "query_lat": lat,
            "query_lng": lng,
            "radius_km": radius_km,
            "method": "vision_llm",
            "description": description,
            "message": f"That looks like {description}. Here's what I found nearby:",
            "results": [
                {
                    "type": "item",
                    "listing_id": r["listing_id"],
                    "store_id": r["store_id"],
                    "title": r["title"],
                    "price": r["price"],
                    "image_url": r["image_url"],
                    "store_name": r.get("store_name"),
                    "store_image_url": r.get("store_image_url"),
                    "latitude": r["lat"],
                    "longitude": r["lng"],
                }
                for r in text_matches
            ],
        }

    # ── 5. Nothing matched — return diagnostics ─────────────────────
    diagnostics: dict = {}
    try:
        diagnostics["listings_total"] = (
            await database.fetch_val("SELECT COUNT(*) FROM listings") or 0
        )
        diagnostics["listings_with_embeddings"] = (
            await database.fetch_val(
                "SELECT COUNT(*) FROM listings WHERE embedding IS NOT NULL"
            )
            or 0
        )
        box = _bbox(lat, lng, radius_km)
        diagnostics["listings_in_radius"] = (
            await database.fetch_val(
                "SELECT COUNT(*) FROM listings WHERE "
                "lat BETWEEN :min_lat AND :max_lat AND "
                "lng BETWEEN :min_lng AND :max_lng",
                box,
            )
            or 0
        )
    except Exception as e:
        diagnostics["error"] = str(e)

    print(
        f"🔍 SEAI Lens: 0 results. embed_error={embed_error} "
        f"description={description!r} diagnostics={diagnostics}",
        flush=True,
    )

    return {
        "query_lat": lat,
        "query_lng": lng,
        "radius_km": radius_km,
        "method": "none",
        "results": [],
        "diagnostics": diagnostics,
        "hint": _build_hint(diagnostics, embed_error),
    }