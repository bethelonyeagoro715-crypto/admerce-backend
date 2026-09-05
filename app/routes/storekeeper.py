from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from app.db.database import database
from .auth import get_current_user
from app.services.image_processor import process_image
from app.services.image_embedder import image_to_embedding, embedding_to_json
from app.services.auto_fill import suggest_from_barcode
from app.utils.category_utils import validate_product_category
from fastapi.responses import FileResponse
import uuid, os, json
from datetime import datetime
import traceback

router = APIRouter(prefix="/storekeeper", tags=["Storekeeper"])

# ---------- Pydantic models ----------
class StoreCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    category: List[str] = Field(..., example=["electronics", "phones"])
    address: str
    lat: float
    lng: float
    phone: str
    store_image_url: Optional[str] = None
    business_hours: dict = Field(default_factory=dict)
    contact_preference: str = "in-app"

class UpdateOrderRequest(BaseModel):
    listing_ids: List[str]   # ordered list of listing IDs

# ---------- helper ----------
def compute_title_quality(title: str) -> float:
    if not title or len(title.strip()) == 0:
        return 0.1
    title = title.strip()
    length = len(title)
    if length < 5: length_score = 0.2
    elif length < 15: length_score = 0.5
    elif length < 30: length_score = 0.7
    else: length_score = 0.9
    desc_words = ["new","used","vintage","original","authentic","brand","model","size","color","year","limited","rare","fresh","carton","pack","piece"]
    has_desc = any(w in title.lower().split() for w in desc_words)
    bonus = 0.2 if has_desc else 0.0
    score = min(max(length_score + bonus, 0.1), 0.95)
    return round(score, 4)

# ==================== CREATE STORE ====================
@router.post("/store", status_code=201)
async def create_store(
    store_data: StoreCreateRequest,
    current_user: dict = Depends(get_current_user)
):
    try:
        existing = await database.fetch_one(
            "SELECT store_id FROM stores WHERE owner_id = :uid",
            {"uid": current_user["id"]}
        )
        if existing:
            raise HTTPException(status_code=400, detail="You already have a store")

        store_id = uuid.uuid4().hex[:12]
        now = datetime.utcnow().isoformat()

        category_json = json.dumps(store_data.category)
        hours_json = json.dumps(store_data.business_hours) if store_data.business_hours else "{}"

        query = """
        INSERT INTO stores (
            store_id, owner_id, name, description, category,
            address, latitude, longitude, phone, store_image_url,
            business_hours, contact_preference, verified, created_at, updated_at
        ) VALUES (
            :sid, :oid, :name, :desc, :cat,
            :addr, :lat, :lng, :phone, :img,
            :hours, :pref, false, :now, :now
        )
        """
        await database.execute(query, {
            "sid": store_id,
            "oid": current_user["id"],
            "name": store_data.name,
            "desc": store_data.description or "",
            "cat": category_json,
            "addr": store_data.address,
            "lat": store_data.lat,
            "lng": store_data.lng,
            "phone": store_data.phone,
            "img": store_data.store_image_url,
            "hours": hours_json,
            "pref": store_data.contact_preference,
            "now": now
        })

        return {
            "store_id": store_id,
            "owner_id": current_user["id"],
            "name": store_data.name,
            "message": "Store created successfully"
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Store creation failed: {str(e)}")

# ==================== CREATE LISTING ====================
@router.post("/listing")
async def create_listing(
    request: Request,
    store_id: str = Form(...),
    title: str = Form(""),
    price: float = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    category: str = Form(...),
    barcode: str = Form(""),
    image: UploadFile = File(None),
    style: str = Form("warm"),
    quantity: int = Form(1),
):
    # Validate category
    if not validate_product_category(category):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category: '{category}'. Valid categories are: tech_electronics, food_beverage, health_wellness, fashion_apparel, building_industrial, home_garden, kids_toys, sports_outdoors, automotive, media_office"
        )

    # Auto-fill from barcode
    suggested_title = title
    suggested_category = ""
    if barcode:
        info = suggest_from_barcode(barcode)
        if info["suggested_title"] and not title:
            suggested_title = info["suggested_title"]
        if info.get("suggested_category"):
            suggested_category = info["suggested_category"]

    # Process image
    image_url = None
    saved_filename = None
    if image and image.filename:
        image_bytes = await image.read()
        if image_bytes:
            try:
                processed = process_image(image_bytes, style=style)
                os.makedirs("uploads", exist_ok=True)
                saved_filename = f"{uuid.uuid4().hex}.png"
                fpath = os.path.join("uploads", saved_filename)
                with open(fpath, "wb") as f:
                    f.write(processed)
                image_url = f"{request.base_url}uploads/{saved_filename}"
            except Exception as e:
                print(f"Image processing error: {e}")
                os.makedirs("uploads", exist_ok=True)
                saved_filename = f"{uuid.uuid4().hex}_original.png"
                fpath = os.path.join("uploads", saved_filename)
                with open(fpath, "wb") as f:
                    f.write(image_bytes)
                image_url = f"{request.base_url}uploads/{saved_filename}"

    # Verify store exists
    existing_store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE store_id = :sid", {"sid": store_id}
    )
    if not existing_store:
        raise HTTPException(status_code=404, detail="Store not found. Please create a store first.")

    listing_id = uuid.uuid4().hex[:8]
    final_title = suggested_title if suggested_title else title
    title_quality = compute_title_quality(final_title)
    created_at = datetime.utcnow().isoformat()

    # Compute embedding (optional)
    embedding = None
    if saved_filename:
        try:
            base = r"C:\Users\Bethel\SEAI PROJECT"
            full_path = os.path.join(base, "uploads", saved_filename)
            emb = image_to_embedding(full_path)
            embedding = embedding_to_json(emb)
        except Exception as e:
            print(f"Embedding generation failed: {e}")

    # Insert listing with quantity
    query = """
    INSERT INTO listings (
        listing_id, store_id, title, price, lat, lng, category, created_at,
        title_quality, image_url, embedding,
        quantity_total, quantity_available
    ) VALUES (
        :lid, :sid, :t, :p, :lat, :lng, :cat, :ca,
        :tq, :img, :emb,
        :qty, :qty
    )
    """
    await database.execute(query, {
        "lid": listing_id,
        "sid": store_id,
        "t": final_title,
        "p": price,
        "lat": lat,
        "lng": lng,
        "cat": category,
        "ca": created_at,
        "tq": title_quality,
        "img": image_url,
        "emb": embedding,
        "qty": quantity,
    })

    return {
        "listing_id": listing_id,
        "store_id": store_id,
        "title": final_title,
        "category": category,
        "suggested_category": suggested_category,
        "title_quality": title_quality,
        "image_url": image_url,
        "embedding_computed": embedding is not None,
        "quantity": quantity,
        "created_at": created_at,
        "message": "Listing created"
    }

# ==================== AI IMAGE ANALYSIS (mock) ====================
@router.post("/analyze-image")
async def analyze_image(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    image_bytes = await image.read()
    mock_data = {
        "title": "Item from image",
        "category": "tech_electronics",
        "condition": "Used",
        "description": "This item was detected by the AI. Please update the details."
    }
    return mock_data

# ==================== UPLOAD STORE IMAGE ====================
@router.post("/upload-store-image")
async def upload_store_image(
    request: Request,
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    image_bytes = await image.read()
    os.makedirs("uploads/stores", exist_ok=True)
    filename = f"store_{uuid.uuid4().hex}.png"
    filepath = os.path.join("uploads/stores", filename)
    with open(filepath, "wb") as f:
        f.write(image_bytes)
    image_url = f"{request.base_url}uploads/stores/{filename}"
    return {"image_url": image_url}

# ==================== PREVIEW IMAGE ====================
@router.post("/preview-image")
async def preview_image(
    request: Request,
    image: UploadFile = File(...),
    style: str = Form("warm"),
):
    image_bytes = await image.read()
    processed = process_image(image_bytes, style=style)
    os.makedirs("uploads/previews", exist_ok=True)
    fname = f"{uuid.uuid4().hex}.png"
    fpath = os.path.join("uploads/previews", fname)
    with open(fpath, "wb") as f:
        f.write(processed)
    image_url = f"{request.base_url}uploads/previews/{fname}"
    return {"image_url": image_url}

# ==================== SERVE UPLOADED FILES ====================
@router.get("/uploads/{file_path:path}")
async def get_upload(file_path: str):
    base_dir = r"C:\Users\Bethel\SEAI PROJECT"
    filepath = os.path.join(base_dir, "uploads", file_path)
    if os.path.exists(filepath):
        return FileResponse(
            filepath,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET",
            },
        )
    raise HTTPException(status_code=404, detail="Image not found")

# ==================== GET STORE DETAILS ====================
@router.get("/store/{store_id}")
async def get_store(store_id: str):
    row = await database.fetch_one("SELECT * FROM stores WHERE store_id = :sid", {"sid": store_id})
    if not row:
        raise HTTPException(status_code=404, detail="Store not found")
    return dict(row)

# ==================== STORE LOCATIONS ====================
@router.get("/stores/locations")
async def get_store_locations():
    stores = await database.fetch_all(
        "SELECT s.store_id, s.name AS store_name, s.latitude, s.longitude, "
        "s.store_image_url AS image_url, "
        "CASE WHEN COUNT(l.listing_id) > 0 THEN true ELSE false END AS has_stock "
        "FROM stores s "
        "LEFT JOIN listings l ON s.store_id = l.store_id AND l.quantity_available > 0 "
        "GROUP BY s.store_id, s.name, s.latitude, s.longitude, s.store_image_url"
    )
    return [dict(store) for store in stores]

# ==================== GET ITEMS FOR A STORE ====================
@router.get("/items/{store_id}")
async def get_store_items(store_id: str):
    rows = await database.fetch_all(
        "SELECT * FROM listings WHERE store_id = :sid ORDER BY sort_order, created_at DESC",
        {"sid": store_id}
    )
    return [dict(row) for row in rows]

# ==================== UPDATE ITEM ORDER (visual shelf) ====================
@router.put("/items/order")
async def update_item_order(
    req: UpdateOrderRequest,
    current_user: dict = Depends(get_current_user)
):
    store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE owner_id = :uid", {"uid": current_user["id"]}
    )
    if not store:
        raise HTTPException(status_code=403, detail="No store found")

    for index, listing_id in enumerate(req.listing_ids):
        await database.execute(
            "UPDATE listings SET sort_order = :order WHERE listing_id = :lid AND store_id = :sid",
            {"order": index, "lid": listing_id, "sid": store["store_id"]}
        )
    return {"message": "Order updated"}

# ==================== GET ORDERS FOR A STORE (FIXED) ====================
@router.get("/orders/{store_id}")
async def get_store_orders(store_id: str):
    # Get the owner_id of the store
    store = await database.fetch_one(
        "SELECT owner_id FROM stores WHERE store_id = :sid",
        {"sid": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    owner_id = store["owner_id"]

    # Join escrow with users to get shopper's nickname (customer name).
    # If your users table uses a different column (e.g., name, full_name), replace u.nickname.
    query = """
        SELECT e.*, 
               COALESCE(u.nickname, 'Customer') AS customer_name
        FROM escrow e
        LEFT JOIN users u ON e.shopper_id = u.id
        WHERE e.storekeeper_id = :owner_id
        ORDER BY e.created_at DESC
    """
    rows = await database.fetch_all(query, {"owner_id": owner_id})
    return [dict(row) for row in rows]

# ==================== GET MY OWN STORE ====================
@router.get("/my-store")
async def get_my_store(current_user: dict = Depends(get_current_user)):
    store = await database.fetch_one(
        "SELECT * FROM stores WHERE owner_id = :uid",
        {"uid": current_user["id"]}
    )
    if not store:
        raise HTTPException(
            status_code=404,
            detail=f"No store found for user {current_user['id']}. Please create a store first."
        )
    return dict(store)

# ==================== SEARCH STORE ITEMS ====================
@router.get("/items/{store_id}/search")
async def search_store_items(store_id: str, q: str):
    rows = await database.fetch_all(
        "SELECT * FROM listings WHERE store_id = :sid AND title ILIKE :q ORDER BY created_at DESC LIMIT 20",
        {"sid": store_id, "q": f"%{q}%"}
    )
    return [dict(row) for row in rows]

# ==================== FOLLOW / UNFOLLOW ====================
@router.post("/{store_id}/follow")
async def follow_store(store_id: str, current_user: dict = Depends(get_current_user)):
    exists = await database.fetch_one(
        "SELECT * FROM stores WHERE store_id = :sid", {"sid": store_id}
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Store not found")
    await database.execute(
        "INSERT INTO favorites (user_id, store_id, created_at) VALUES (:uid, :sid, :now) "
        "ON CONFLICT DO NOTHING",
        {"uid": current_user["id"], "sid": store_id, "now": datetime.utcnow().isoformat()}
    )
    return {"message": "Store followed"}

@router.get("/{store_id}/follow-status")
async def get_follow_status(
    store_id: str,
    current_user: dict = Depends(get_current_user)
):
    record = await database.fetch_one(
        "SELECT * FROM favorites WHERE user_id = :uid AND store_id = :sid",
        {"uid": current_user["id"], "sid": store_id}
    )
    return {"is_following": record is not None}

@router.delete("/{store_id}/unfollow")
async def unfollow_store(store_id: str, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "DELETE FROM favorites WHERE user_id = :uid AND store_id = :sid",
        {"uid": current_user["id"], "sid": store_id}
    )
    return {"message": "Store unfollowed"}

# ==================== UPDATE STORE IMAGE ====================
@router.post("/update-store-image")
async def update_store_image(
    request: Request,
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE owner_id = :uid",
        {"uid": current_user["id"]}
    )
    if not store:
        raise HTTPException(status_code=404, detail="No store found")

    store_id = store["store_id"]

    image_bytes = await image.read()
    os.makedirs("uploads/stores", exist_ok=True)
    filename = f"store_{store_id}_{uuid.uuid4().hex[:8]}.png"
    filepath = os.path.join("uploads/stores", filename)
    with open(filepath, "wb") as f:
        f.write(image_bytes)

    image_url = f"{request.base_url}uploads/stores/{filename}"

    await database.execute(
        "UPDATE stores SET store_image_url = :url WHERE store_id = :sid",
        {"url": image_url, "sid": store_id}
    )

    return {
        "store_image_url": image_url,
        "message": "Store image updated successfully"
    }

# ==================== STORE STATISTICS (FIXED) ====================
@router.get("/stats")
async def get_store_stats(current_user: dict = Depends(get_current_user)):
    store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE owner_id = :uid",
        {"uid": current_user["id"]}
    )
    if not store:
        raise HTTPException(status_code=404, detail="No store found")

    store_id = store["store_id"]
    owner_id = current_user["id"]

    # Completed statuses: orders that have been picked up or dispatched
    completed_statuses = ('picked_up', 'dispatched', 'completed')

    # Views (from listing_events if table exists)
    views = 0
    try:
        views = await database.fetch_val(
            """
            SELECT COUNT(*) FROM listing_events le
            JOIN listings l ON le.listing_id = l.listing_id
            WHERE l.store_id = :sid AND le.event_type = 'view'
            """,
            {"sid": store_id}
        ) or 0
    except Exception as e:
        print(f"Could not fetch views: {e}")

    # Inquiries – count messages received by the storekeeper
    inquiries = 0
    try:
        inquiries = await database.fetch_val(
            "SELECT COUNT(*) FROM messages WHERE receiver_id = :uid",
            {"uid": owner_id}
        ) or 0
    except Exception as e:
        print(f"Could not fetch inquiries: {e}")

    # Sold items – count completed escrow orders
    sold = 0
    try:
        sold = await database.fetch_val(
            "SELECT COUNT(*) FROM escrow WHERE storekeeper_id = :uid AND status = ANY(:statuses)",
            {"uid": owner_id, "statuses": completed_statuses}
        ) or 0
    except Exception as e:
        print(f"Could not fetch sold: {e}")

    # Revenue – sum of total_amount for completed orders
    revenue = 0
    try:
        revenue = await database.fetch_val(
            "SELECT COALESCE(SUM(total_amount::numeric), 0) FROM escrow "
            "WHERE storekeeper_id = :uid AND status = ANY(:statuses)",
            {"uid": owner_id, "statuses": completed_statuses}
        ) or 0
    except Exception as e:
        print(f"Could not fetch revenue: {e}")

    return {
        "views": views,
        "inquiries": inquiries,
        "sold": sold,
        "revenue": revenue,
    }