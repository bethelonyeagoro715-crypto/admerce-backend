from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from app.services.image_embedder import (
    image_to_embedding,
    json_to_embedding,
    cosine_similarity,
    is_real_embedding,
)
from app.services.llm_agent import GEMINI_VISION_MODELS, GEMINI_MODEL
from app.db.database import database
from app.utils.security import get_current_user
import numpy as np
import os
import re
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
    """HF-based visual similarity. Only runs if embeddings exist."""
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
            if sim > 0.01:
                scored.append((sim, dict(row)))
        except Exception:
            continue

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
        print("⚠️  GEMINI_API_KEY not set", flush=True)
        return None

    for model_name in GEMINI_VISION_MODELS:
        try:
            def _sync():
                model = genai.GenerativeModel(model_name)
                return model.generate_content([
                    "What is the main physical object in this image? "
                    "Reply with ONLY the object name in 2-4 words, no punctuation, "
                    "no explanation. Examples: 'iPhone 14', 'leather handbag', "
                    "'wooden chair', 'cheeseburger', 'plate of jollof rice'. "
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


async def _fetch_candidates(lat: float, lng: float, radius_km: float, limit: int = 30):
    """Fetch listings in radius without embeddings — used for LLM re-rank."""
    box = _bbox(lat, lng, radius_km)
    try:
        rows = await database.fetch_all(
            """
            SELECT l.listing_id, l.store_id, l.title, l.price, l.image_url,
                   l.lat, l.lng,
                   s.name             AS store_name,
                   s.store_image_url  AS store_image_url
            FROM listings l
            LEFT JOIN stores s ON l.store_id = s.store_id
            WHERE (l.quantity_available IS NULL OR l.quantity_available > 0)
              AND l.lat BETWEEN :min_lat AND :max_lat
              AND l.lng BETWEEN :min_lng AND :max_lng
            LIMIT :lim
            """,
            {**box, "lim": limit},
        )
    except Exception as e:
        print(f"⚠️  candidates sql error: {e}", flush=True)
        return []
    return [dict(r) for r in rows]


async def _llm_rank_listings(
    description: str, listings: list[dict]
) -> list[dict]:
    """
    Ask Gemini to pick the best matches from the candidate listings.
    This is the fallback when visual embeddings are unavailable — it
    uses the description Gemini already produced plus the actual
    listings in radius, so matching is contextual rather than
    substring-based.
    """
    if not listings:
        return []

    try:
        import google.generativeai as genai
    except ImportError:
        return []

    if not os.getenv("GEMINI_API_KEY"):
        return []

    catalog = "\n".join(
        f"{i + 1}. {l['title']} — ₦{l.get('price', 0)}"
        for i, l in enumerate(listings[:20])
    )

    prompt = f"""A user uploaded a photo. It shows: "{description}".

Which of the following listings match the photo best? Reply with ONLY the index numbers of the best 1-3 matches, comma-separated. If none are a reasonable match, reply with a single hyphen.

Listings:
{catalog}

Reply with indices only (e.g. "1,3" or "-"):"""

    try:
        def _sync():
            model = genai.GenerativeModel(GEMINI_MODEL)
            return model.generate_content(prompt)

        response = await asyncio.to_thread(_sync)
        text = (getattr(response, "text", "") or "").strip()
        print(f"🎯 llm rank raw response: {text!r}", flush=True)

        if not text or text == "-":
            return []

        indices = [int(m) for m in re.findall(r"\d+", text)]
        selected: list[dict] = []
        seen: set[int] = set()
        for idx in indices[:3]:
            if 1 <= idx <= len(listings) and idx not in seen:
                selected.append(listings[idx - 1])
                seen.add(idx)
        return selected
    except Exception as e:
        print(f"⚠️  llm rank failed: {e}", flush=True)
        return []


def _build_hint(diagnostics: dict, embed_error: Optional[str]) -> str:
    if diagnostics.get("listings_total", 0) == 0:
        return "There are no listings on Admerce yet."
    if diagnostics.get("listings_in_radius", 0) == 0:
        return "No listings near you in the current radius. Try a wider area."
    if diagnostics.get("description"):
        return (
            f"That looks like {diagnostics['description']}, but I couldn't "
            "find a close match nearby."
        )
    return "Couldn't match that photo. Try a clearer image or a text search."


@router.post("")
@router.post("/")
async def visual_search(
    image: UploadFile = File(...),
    lat: float = Form(...),
    lng: float = Form(...),
    radius_km: float = Form(10),
    current_user: dict = Depends(get_current_user),
):
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image upload")

    # ── 1. Try HF visual embedding (non-fatal — HF is currently dead) ─
    query_emb = None
    embed_error: Optional[str] = None
    tmp_path = None
    try:
        os.makedirs("uploads/tmp", exist_ok=True)
        tmp_path = f"uploads/tmp/{uuid.uuid4().hex}.jpg"
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        query_emb = image_to_embedding(tmp_path)
        if not is_real_embedding(query_emb):
            query_emb = None
    except Exception as e:
        embed_error = str(e)
        print(f"⚠️  image_to_embedding failed: {e}", flush=True)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    # ── 2. Visual match attempt (only if we got a real embedding) ─────
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

    # ── 3. Vision LLM path — describe then rank ─────────────────────
    description = await _describe_image_with_llm(image_bytes)

    if description:
        candidates = await _fetch_candidates(lat, lng, radius_km, limit=30)

        if candidates:
            ranked = await _llm_rank_listings(description, candidates)
            if ranked:
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
                        for r in ranked
                    ],
                }

    # ── 4. Nothing matched — return diagnostics ─────────────────────
    diagnostics: dict = {"description": description}
    try:
        diagnostics["listings_total"] = (
            await database.fetch_val("SELECT COUNT(*) FROM listings") or 0
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