from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from app.db.database import database
from .auth import get_current_user
from app.services.image_processor import process_image
from app.services.image_embedder import image_to_embedding, embedding_to_json
from app.services.auto_fill import suggest_from_barcode
from app.utils.category_utils import validate_product_category
from app.services.cloudinary_service import upload_image   # ✅ Cloudinary helper
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

# ✅ NEW — store verification submission. Evidence is a list of URLs; the
#    storekeeper uploads files through the existing /upload-store-image
#    endpoint (or any future media endpoint) and passes the returned URLs.
class VerificationSubmitRequest(BaseModel):
    legal_name: str = Field(..., min_length=2, max_length=200)
    business_type: str = Field(..., min_length=2, max_length=100)
    cac_number: Optional[str] = Field(None, max_length=50)
    business_address: str = Field(..., min_length=4, max_length=400)
    contact_phone: str = Field(..., min_length=7, max_length=30)
    evidence: List[str] = Field(default_factory=list)

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

# ✅ NEW — the single place that writes to store_verification_events.
#    Append-only; nothing here can UPDATE or DELETE existing events.
async def _log_store_verification_event(
    store_id: str,
    from_status: Optional[str],
    to_status: str,
    actor_id: Optional[str],
    reason: Optional[str] = None,
    reference: Optional[str] = None,
):
    await database.execute(
        """
        INSERT INTO store_verification_events
            (store_id, actor_id, from_status, to_status, reason, reference, created_at)
        VALUES
            (:sid, :actor, :from_s, :to_s, :reason, :ref, NOW())
        """,
        {
            "sid": store_id,
            "actor": actor_id,
            "from_s": from_status,
            "to_s": to_status,
            "reason": reason,
            "ref": reference,
        },
    )

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
        now = datetime.utcnow()   # ✅ datetime object

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
            "now": now,
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

# ==================== STORE VERIFICATION ====================
# Admin-reviewed two-step flow:
#   1. Storekeeper submits a request (this section).
#   2. Admin approves / rejects (admin.py).
#
# Rules enforced here:
#   - Only the store's owner can submit or cancel.
#   - Store must be 'unverified' or 'rejected' to submit a new request.
#   - A store with a pending request cannot submit a second one.
#   - Cancelling reverts the store to 'unverified'; the storekeeper can
#     then fix the submission and try again.
#
# Every transition writes an append-only row to store_verification_events.

@router.get("/{store_id}/verification")
async def get_store_verification_status(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Owner-side status: current state, latest request, full event history.
    """
    store = await database.fetch_one(
        "SELECT store_id, owner_id, verification_status, verified, verified_at "
        "FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store["owner_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your store")

    latest_request = await database.fetch_one(
        """
        SELECT * FROM store_verifications
        WHERE store_id = :sid
        ORDER BY submitted_at DESC
        LIMIT 1
        """,
        {"sid": store_id},
    )
    events = await database.fetch_all(
        """
        SELECT * FROM store_verification_events
        WHERE store_id = :sid
        ORDER BY created_at DESC
        LIMIT 20
        """,
        {"sid": store_id},
    )

    return {
        "store_id": store_id,
        "verification_status": store["verification_status"] or "unverified",
        "verified": bool(store["verified"]),
        "verified_at": store["verified_at"],
        "latest_request": dict(latest_request) if latest_request else None,
        "events": [dict(e) for e in events],
    }

@router.post("/{store_id}/verification/request", status_code=201)
async def submit_store_verification(
    store_id: str,
    req: VerificationSubmitRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Submit a verification request. Only the owner can call this.
    Fails 409 if a pending request already exists for the store.
    """
    store = await database.fetch_one(
        "SELECT store_id, owner_id, name, verification_status FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store["owner_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your store")

    current_status = store["verification_status"] or "unverified"
    if current_status not in ("unverified", "rejected"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot submit a verification request from status '{current_status}'. "
                "Only unverified or rejected stores can submit."
            ),
        )

    existing_pending = await database.fetch_one(
        "SELECT id FROM store_verifications WHERE store_id = :sid AND status = 'pending'",
        {"sid": store_id},
    )
    if existing_pending:
        raise HTTPException(
            status_code=409,
            detail="A verification request is already pending for this store",
        )

    request_id = uuid.uuid4()
    reference_code = uuid.uuid4().hex
    now = datetime.utcnow()

    await database.execute(
        """
        INSERT INTO store_verifications (
            id, store_id, submitted_by, status,
            legal_name, business_type, cac_number,
            business_address, contact_phone, evidence,
            reference_code, submitted_at
        ) VALUES (
            :id, :sid, :uid, 'pending',
            :legal_name, :btype, :cac,
            :addr, :phone, :evidence,
            :ref, :now
        )
        """,
        {
            "id": request_id,
            "sid": store_id,
            "uid": current_user["id"],
            "legal_name": req.legal_name.strip(),
            "btype": req.business_type.strip(),
            "cac": (req.cac_number or "").strip() or None,
            "addr": req.business_address.strip(),
            "phone": req.contact_phone.strip(),
            "evidence": json.dumps(req.evidence or []),
            "ref": reference_code,
            "now": now,
        },
    )

    await database.execute(
        """
        UPDATE stores
        SET verification_status = 'pending',
            verified = FALSE,
            updated_at = :now
        WHERE store_id = :sid
          AND verification_status IN ('unverified', 'rejected')
        """,
        {"now": now, "sid": store_id},
    )

    await _log_store_verification_event(
        store_id=store_id,
        from_status=current_status,
        to_status="pending",
        actor_id=current_user["id"],
        reason=None,
        reference=reference_code,
    )

    return {
        "store_id": store_id,
        "status": "pending",
        "reference_code": reference_code,
        "message": "Verification request submitted. An admin will review it shortly.",
    }

@router.delete("/{store_id}/verification/request")
async def cancel_store_verification(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Cancel a pending verification request. Reverts store to 'unverified'.
    Only the owner can cancel; only works when status is currently 'pending'.
    """
    store = await database.fetch_one(
        "SELECT store_id, owner_id, verification_status FROM stores WHERE store_id = :sid",
        {"sid": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store["owner_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your store")

    current_status = store["verification_status"] or "unverified"
    if current_status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"No pending request to cancel (current status: '{current_status}')",
        )

    now = datetime.utcnow()

    pending = await database.fetch_one(
        """
        SELECT id, reference_code FROM store_verifications
        WHERE store_id = :sid AND status = 'pending'
        ORDER BY submitted_at DESC
        LIMIT 1
        """,
        {"sid": store_id},
    )
    if not pending:
        raise HTTPException(status_code=400, detail="No pending request found")

    await database.execute(
        """
        UPDATE store_verifications
        SET status = 'cancelled',
            reviewed_by = :uid,
            reviewed_at = :now,
            review_reason = 'Cancelled by storekeeper'
        WHERE id = :rid AND status = 'pending'
        """,
        {"uid": current_user["id"], "now": now, "rid": pending["id"]},
    )

    await database.execute(
        """
        UPDATE stores
        SET verification_status = 'unverified',
            verified = FALSE,
            updated_at = :now
        WHERE store_id = :sid AND verification_status = 'pending'
        """,
        {"now": now, "sid": store_id},
    )

    await _log_store_verification_event(
        store_id=store_id,
        from_status="pending",
        to_status="unverified",
        actor_id=current_user["id"],
        reason="Cancelled by storekeeper",
        reference=pending["reference_code"],
    )

    return {
        "store_id": store_id,
        "status": "unverified",
        "message": "Verification request cancelled",
    }

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
    if not validate_product_category(category):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category: '{category}'. Valid categories are: tech_electronics, food_beverage, health_wellness, fashion_apparel, building_industrial, home_garden, kids_toys, sports_outdoors, automotive, media_office"
        )

    suggested_title = title
    suggested_category = ""
    if barcode:
        info = suggest_from_barcode(barcode)
        if info["suggested_title"] and not title:
            suggested_title = info["suggested_title"]
        if info.get("suggested_category"):
            suggested_category = info["suggested_category"]

    # Process and upload image to Cloudinary
    image_url = None
    if image and image.filename:
        image_bytes = await image.read()
        if image_bytes:
            try:
                processed = process_image(image_bytes, style=style)
                image_url = upload_image(processed, folder="listings")   # ✅ Cloudinary
            except Exception as e:
                print(f"Image processing error: {e}")
                try:
                    image_url = upload_image(image_bytes, folder="listings")
                except Exception as e2:
                    print(f"Upload error: {e2}")

    existing_store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE store_id = :sid", {"sid": store_id}
    )
    if not existing_store:
        raise HTTPException(status_code=404, detail="Store not found. Please create a store first.")

    listing_id = uuid.uuid4().hex[:8]
    final_title = suggested_title if suggested_title else title
    title_quality = compute_title_quality(final_title)
    created_at = datetime.utcnow()   # ✅ datetime object

    # Compute embedding (optional) – still may use local file? we can skip or use image_url
    embedding = None
    # embedding generation currently expects a local file path; we'll skip for now
    # if you need embeddings, you can download from Cloudinary URL or process in memory

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
    image_url = upload_image(image_bytes, folder="store_images")   # ✅ Cloudinary
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
    image_url = upload_image(processed, folder="previews")   # ✅ Cloudinary
    return {"image_url": image_url}

# ==================== SERVE UPLOADED FILES (kept for legacy) ====================
@router.get("/uploads/{file_path:path}")
async def get_upload(file_path: str):
    # Since we now use Cloudinary, this may not be needed, but keep for compatibility.
    base_dir = os.getcwd()
    filepath = os.path.join(base_dir, "uploads", file_path)
    if os.path.exists(filepath):
        return FileResponse(filepath)
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
    store = await database.fetch_one(
        "SELECT owner_id FROM stores WHERE store_id = :sid",
        {"sid": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    owner_id = store["owner_id"]

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
        {"uid": current_user["id"], "sid": store_id, "now": datetime.utcnow()}
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
    image_url = upload_image(image_bytes, folder="store_images")   # ✅ Cloudinary

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

    completed_statuses = ('picked_up', 'dispatched', 'completed')

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

    inquiries = 0
    try:
        inquiries = await database.fetch_val(
            "SELECT COUNT(*) FROM messages WHERE receiver_id = :uid",
            {"uid": owner_id}
        ) or 0
    except Exception as e:
        print(f"Could not fetch inquiries: {e}")

    sold = 0
    try:
        sold = await database.fetch_val(
            "SELECT COUNT(*) FROM escrow WHERE storekeeper_id = :uid AND status = ANY(:statuses)",
            {"uid": owner_id, "statuses": completed_statuses}
        ) or 0
    except Exception as e:
        print(f"Could not fetch sold: {e}")

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
# ==================== DELETE LISTING ====================
@router.delete("/listing/{listing_id}")
async def delete_listing(
    listing_id: str,
    current_user: dict = Depends(get_current_user)
):
    # 1. Confirm the listing exists
    listing = await database.fetch_one(
        "SELECT listing_id, store_id, image_url FROM listings WHERE listing_id = :lid",
        {"lid": listing_id}
    )
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    # 2. Confirm it belongs to this storekeeper's store
    store = await database.fetch_one(
        "SELECT store_id FROM stores WHERE store_id = :sid AND owner_id = :uid",
        {"sid": listing["store_id"], "uid": current_user["id"]}
    )
    if not store:
        raise HTTPException(status_code=403, detail="You can only delete your own listings")

    # 3. Delete dependent rows (ignore failures for optional tables)
    try:
        await database.execute(
            "DELETE FROM listing_events WHERE listing_id = :lid",
            {"lid": listing_id}
        )
    except Exception as e:
        print(f"⚠️  listing_events cleanup skipped: {e}")

    # 4. Delete the listing itself
    await database.execute(
        "DELETE FROM listings WHERE listing_id = :lid",
        {"lid": listing_id}
    )

    return {"success": True, "deleted": listing_id}