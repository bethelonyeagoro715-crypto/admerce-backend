from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from app.services.image_embedder import image_to_embedding, json_to_embedding, cosine_similarity
from app.db.database import database
from app.utils.security import get_current_user
import numpy as np
import os
import uuid

router = APIRouter(prefix="/seai/lens", tags=["SEAI Lens"])

@router.post("/")
async def visual_search(
    image: UploadFile = File(...),
    lat: float = Form(...),
    lng: float = Form(...),
    radius_km: float = Form(10),
    current_user: dict = Depends(get_current_user)
):
    # 1. Save uploaded query image temporarily
    os.makedirs("uploads/tmp", exist_ok=True)
    tmp_path = f"uploads/tmp/{uuid.uuid4().hex}.jpg"
    with open(tmp_path, "wb") as f:
        f.write(await image.read())

    # 2. Generate query embedding
    try:
        query_emb = image_to_embedding(tmp_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(status_code=400, detail=f"Could not process image: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 3. Fetch nearby listings that have an embedding
    lat_diff = radius_km / 111.0
    lng_diff = radius_km / (111.0 * abs(np.cos(np.radians(lat))) + 1e-8)
    rows = await database.fetch_all(
        "SELECT listing_id, store_id, title, price, image_url, lat, lng, embedding "
        "FROM listings WHERE embedding IS NOT NULL AND "
        "lat BETWEEN :min_lat AND :max_lat AND lng BETWEEN :min_lng AND :max_lng",
        {"min_lat": lat - lat_diff, "max_lat": lat + lat_diff,
         "min_lng": lng - lng_diff, "max_lng": lng + lng_diff}
    )

    # 4. Compute similarity and rank
    results = []
    for row in rows:
        stored_emb = json_to_embedding(row["embedding"])
        sim = cosine_similarity(query_emb, stored_emb)
        results.append((sim, dict(row)))

    results.sort(key=lambda x: x[0], reverse=True)
    top = results[:20]  # top 20 visual matches

    return {
        "query_lat": lat,
        "query_lng": lng,
        "results": [
            {
                "listing_id": r["listing_id"],
                "store_id": r["store_id"],
                "title": r["title"],
                "price": r["price"],
                "image_url": r["image_url"],
                "lat": r["lat"],
                "lng": r["lng"],
                "similarity": round(sim, 4)
            }
            for sim, r in top
        ]
    }