from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
from app.db.database import database
from app.utils.security import get_current_user
from app.services.image_embedder import image_to_embedding, json_to_embedding, cosine_similarity
from app.services.auto_fill import suggest_from_barcode
import uuid, os, json
from datetime import datetime
import numpy as np

router = APIRouter(prefix="/flipper", tags=["Flipper"])

UPLOAD_DIR = "uploads/flipper"

class CreateFlipperListingRequest(BaseModel):
    title: str
    description: str = ""
    price: float
    source: str = "thrift"
    condition: str = "Used"
    image_url: str = None
    lat: float = 6.5244
    lng: float = 3.3792

# ---------- Existing: Create listing ----------
@router.post("/listings")
async def create_flipper_listing(req: CreateFlipperListingRequest, current_user: dict = Depends(get_current_user)):
    flipper_id = current_user["id"]
    listing_id = uuid.uuid4().hex[:8]
    now = datetime.utcnow().isoformat()
    query = """
    INSERT INTO flipper_listings (listing_id, flipper_id, title, description, price, source, condition, image_url, lat, lng, created_at)
    VALUES (:lid, :fid, :title, :desc, :price, :src, :cond, :img, :lat, :lng, :created)
    """
    await database.execute(query, {
        "lid": listing_id,
        "fid": flipper_id,
        "title": req.title,
        "desc": req.description,
        "price": req.price,
        "src": req.source,
        "cond": req.condition,
        "img": req.image_url,
        "lat": req.lat,
        "lng": req.lng,
        "created": now,
    })
    return {"listing_id": listing_id, "message": "Flipper listing created"}

# ---------- Existing: List own listings ----------
@router.get("/listings")
async def get_flipper_listings(current_user: dict = Depends(get_current_user)):
    rows = await database.fetch_all(
        "SELECT * FROM flipper_listings WHERE flipper_id = :fid ORDER BY created_at DESC",
        {"fid": current_user["id"]}
    )
    return [dict(row) for row in rows]

# ---------- Existing: Get single listing ----------
@router.get("/listings/{listing_id}")
async def get_flipper_listing(listing_id: str, current_user: dict = Depends(get_current_user)):
    row = await database.fetch_one("SELECT * FROM flipper_listings WHERE listing_id = :lid", {"lid": listing_id})
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    return dict(row)

# ==================== NEW: Scan & Resell ====================
@router.post("/scan-resell")
async def scan_resell(
    barcode: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    lat: float = Form(6.5244),
    lng: float = Form(3.3792),
    radius_km: float = Form(10),
    current_user: dict = Depends(get_current_user)
):
    if not barcode and not image:
        raise HTTPException(status_code=400, detail="Provide a barcode, an image, or both.")

    suggested_title = ""
    suggested_category = ""

    # 1. Barcode lookup
    if barcode:
        info = suggest_from_barcode(barcode)
        suggested_title = info.get("suggested_title", "")
        suggested_category = info.get("suggested_category", "")

    # 2. Image analysis – find similar items and suggest price
    similar_items = []
    suggested_price = None

    if image and image.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        tmp_filename = f"scan_{uuid.uuid4().hex}.jpg"
        tmp_path = os.path.join(UPLOAD_DIR, tmp_filename)
        with open(tmp_path, "wb") as f:
            f.write(await image.read())

        try:
            query_emb = image_to_embedding(tmp_path)
        except Exception as e:
            os.remove(tmp_path)
            raise HTTPException(status_code=400, detail=f"Image processing failed: {str(e)}")

        # Fetch nearby listings with embeddings (from main listings, not flipper's own)
        lat_diff = radius_km / 111.0
        lng_diff = radius_km / (111.0 * abs(np.cos(np.radians(lat))) + 1e-8)
        rows = await database.fetch_all(
            "SELECT listing_id, title, price, category, lat, lng, embedding FROM listings "
            "WHERE embedding IS NOT NULL AND "
            "lat BETWEEN :min_lat AND :max_lat AND lng BETWEEN :min_lng AND :max_lng "
            "ORDER BY created_at DESC LIMIT 100",
            {"min_lat": lat - lat_diff, "max_lat": lat + lat_diff,
             "min_lng": lng - lng_diff, "max_lng": lng + lng_diff}
        )

        # Compute similarity
        matches = []
        for row in rows:
            stored_emb = json_to_embedding(row["embedding"])
            sim = cosine_similarity(query_emb, stored_emb)
            matches.append((sim, dict(row)))

        matches.sort(key=lambda x: x[0], reverse=True)
        top = matches[:10]

        # Suggest price: median of top 5 similar items
        prices = [item["price"] for _, item in top if item["price"] > 0]
        if prices:
            suggested_price = float(np.median(prices))

        # Suggest category: most common among top 5
        categories = [item["category"] for _, item in top if item["category"]]
        if categories:
            suggested_category = max(set(categories), key=categories.count)

        # If no title from barcode, use title of the most similar item
        if not suggested_title and top:
            suggested_title = top[0][1]["title"]

        similar_items = [
            {
                "listing_id": item["listing_id"],
                "title": item["title"],
                "price": item["price"],
                "similarity": round(sim, 4)
            }
            for sim, item in top
        ]

        os.remove(tmp_path)

    # 3. Fallback price if nothing found
    if suggested_price is None:
        suggested_price = 0.0

    return {
        "suggested_title": suggested_title,
        "suggested_category": suggested_category,
        "suggested_price": round(suggested_price, 2),
        "similar_items": similar_items,
        "message": "Scan complete. Adjust and list."
    }