from fastapi import APIRouter, HTTPException, Depends, File, UploadFile
from pydantic import BaseModel
from typing import Optional
from app.db.database import database
from app.utils.security import get_current_user
import uuid
import shutil
import os
from datetime import datetime, date, timedelta
from pathlib import Path

router = APIRouter(prefix="/services", tags=["Services"])

# ---------- Models ----------
class CreateServiceRequest(BaseModel):
    title: str
    category: str
    description: str = ""
    price: float
    duration_minutes: int = 60
    location_lat: float
    location_lng: float

class BookServiceRequest(BaseModel):
    scheduled_for: Optional[str] = None
    notes: Optional[str] = None
    location_lat: Optional[float] = None
    location_lng: Optional[float] = None

class ToggleAvailabilityRequest(BaseModel):
    is_available: bool

# ---------- Helper: save uploaded file ----------
UPLOAD_DIR = "uploads/services"
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

def save_uploaded_file(upload: UploadFile, service_id: str, file_type: str) -> str:
    ext = Path(upload.filename).suffix
    if not ext:
        ext = ".jpg" if file_type == "image" else ".mp4"
    filename = f"{service_id}_{file_type}{ext}"
    file_path = Path(UPLOAD_DIR) / filename
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(upload.file, buffer)
    return f"/uploads/services/{filename}"

# ============================================================
# STATIC ROUTES (MUST COME BEFORE DYNAMIC {service_id})
# ============================================================

# ---------- Get provider's own services ----------
@router.get("/provider")
async def get_provider_services(current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]
    rows = await database.fetch_all(
        "SELECT * FROM services WHERE provider_id = :pid ORDER BY created_at DESC",
        {"pid": provider_id}
    )
    return [dict(row) for row in rows]

# ---------- Get provider's bookings ----------
@router.get("/bookings/provider")
async def get_provider_bookings(current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]
    service_ids = await database.fetch_all(
        "SELECT service_id FROM services WHERE provider_id = :pid",
        {"pid": provider_id}
    )
    service_id_list = [row["service_id"] for row in service_ids]
    if not service_id_list:
        return []

    placeholders = ",".join([f":sid{i}" for i in range(len(service_id_list))])
    params = {f"sid{i}": sid for i, sid in enumerate(service_id_list)}
    query = f"""
        SELECT sb.*, s.title as service_title,
               COALESCE(
                   NULLIF(CONCAT(u.first_name, ' ', u.last_name), ' '),
                   u.nickname,
                   u.real_name,
                   u.email,
                   'Customer'
               ) AS user_name
        FROM service_bookings sb
        JOIN services s ON sb.service_id = s.service_id
        JOIN users u ON sb.client_id = u.id
        WHERE sb.service_id IN ({placeholders})
        ORDER BY sb.created_at DESC
    """
    rows = await database.fetch_all(query, params)
    return [dict(row) for row in rows]

# ---------- Get provider stats ----------
@router.get("/providers/stats")
async def get_provider_stats(current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]

    today = datetime.utcnow().date()
    start_str = datetime(today.year, today.month, today.day).isoformat()
    end_str = datetime(today.year, today.month, today.day + 1).isoformat()

    today_bookings = await database.fetch_val(
        """
        SELECT COUNT(*)
        FROM service_bookings sb
        JOIN services s ON sb.service_id = s.service_id
        WHERE s.provider_id = :pid
          AND sb.created_at >= :start
          AND sb.created_at < :end
        """,
        {"pid": provider_id, "start": start_str, "end": end_str}
    )

    total_earnings = await database.fetch_val(
        """
        SELECT COALESCE(SUM(sb.amount), 0)
        FROM service_bookings sb
        JOIN services s ON sb.service_id = s.service_id
        WHERE s.provider_id = :pid
          AND sb.status = 'completed'
        """,
        {"pid": provider_id}
    )

    rating = await database.fetch_val(
        """
        SELECT COALESCE(AVG(r.rating), 0)
        FROM reviews r
        JOIN services s ON r.store_id = s.service_id
        WHERE s.provider_id = :pid
        """,
        {"pid": provider_id}
    ) or 0.0

    return {
        "today_bookings": today_bookings or 0,
        "total_earnings": float(total_earnings or 0.0),
        "rating": float(rating)
    }

# ---------- Toggle provider availability ----------
@router.put("/providers/availability")
async def update_provider_availability(
    req: ToggleAvailabilityRequest,
    current_user: dict = Depends(get_current_user)
):
    provider_id = current_user["id"]
    now = datetime.utcnow().isoformat()
    await database.execute("""
        INSERT INTO provider_availability (user_id, is_available, updated_at)
        VALUES (:uid, :avail, :now)
        ON CONFLICT (user_id) DO UPDATE SET
            is_available = :avail,
            updated_at = :now
    """, {"uid": provider_id, "avail": req.is_available, "now": now})
    return {"user_id": provider_id, "is_available": req.is_available}

# ---------- Toggle service active status ----------
@router.post("/{service_id}/toggle")
async def toggle_service_active(
    service_id: str,
    current_user: dict = Depends(get_current_user)
):
    service = await database.fetch_one(
        "SELECT provider_id, is_active FROM services WHERE service_id = :sid",
        {"sid": service_id}
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    new_state = not service["is_active"]
    await database.execute(
        "UPDATE services SET is_active = :state WHERE service_id = :sid",
        {"state": new_state, "sid": service_id}
    )
    return {"service_id": service_id, "is_active": new_state}

# ---------- Delete service (HARD DELETE) ----------
@router.delete("/{service_id}")
async def delete_service(service_id: str, current_user: dict = Depends(get_current_user)):
    service = await database.fetch_one(
        "SELECT provider_id FROM services WHERE service_id = :sid",
        {"sid": service_id}
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    await database.execute(
        "DELETE FROM services WHERE service_id = :sid",
        {"sid": service_id}
    )
    return {"message": "Service permanently deleted"}

# ---------- Get bookings for logged-in user ----------
@router.get("/bookings")
async def get_bookings(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = await database.fetch_all(
        "SELECT * FROM service_bookings WHERE client_id = :uid OR provider_id = :uid2 ORDER BY created_at DESC",
        {"uid": user_id, "uid2": user_id}
    )
    return [dict(row) for row in rows]

# ---------- Get single booking ----------
@router.get("/bookings/{booking_id}")
async def get_booking(booking_id: str, current_user: dict = Depends(get_current_user)):
    booking = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id}
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking["client_id"] != current_user["id"] and booking["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return dict(booking)

# ---------- Provider confirms job done ----------
@router.post("/bookings/{booking_id}/confirm")
async def confirm_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user)
):
    booking = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id}
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the service provider can confirm completion")
    if booking["status"] != "locked":
        raise HTTPException(status_code=400, detail="Booking already processed")

    await database.execute(
        "UPDATE service_bookings SET status = 'completed', completed_at = :now WHERE booking_id = :bid",
        {"now": datetime.utcnow().isoformat(), "bid": booking_id}
    )
    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": booking["amount"], "uid": booking["provider_id"]}
    )
    return {"booking_id": booking_id, "status": "completed", "message": "Job marked complete, funds released."}

# ---------- Client releases funds ----------
@router.post("/bookings/{booking_id}/complete")
async def complete_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user)
):
    booking = await database.fetch_one(
        "SELECT * FROM service_bookings WHERE booking_id = :bid",
        {"bid": booking_id}
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking["client_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the client can release funds")
    if booking["status"] != "locked":
        raise HTTPException(status_code=400, detail="Booking already processed")

    await database.execute(
        "UPDATE service_bookings SET status = 'completed', completed_at = :now WHERE booking_id = :bid",
        {"now": datetime.utcnow().isoformat(), "bid": booking_id}
    )
    await database.execute(
        "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
        {"amt": booking["amount"], "uid": booking["provider_id"]}
    )
    return {"booking_id": booking_id, "status": "completed", "message": "Funds released to provider."}

# ============================================================
# DYNAMIC ROUTES (must come AFTER all static paths)
# ============================================================

# ---------- Create service ----------
@router.post("/")
async def create_service(req: CreateServiceRequest, current_user: dict = Depends(get_current_user)):
    provider_id = current_user["id"]
    service_id = uuid.uuid4().hex[:8]
    now = datetime.utcnow()   # ✅ datetime object, not string
    query = """
    INSERT INTO services (service_id, provider_id, title, category, description, price, duration_minutes, lat, lng, is_active, created_at)
    VALUES (:sid, :pid, :title, :cat, :desc, :price, :dur, :lat, :lng, TRUE, :created)
    """
    await database.execute(query, {
        "sid": service_id,
        "pid": provider_id,
        "title": req.title,
        "cat": req.category,
        "desc": req.description,
        "price": req.price,
        "dur": req.duration_minutes,
        "lat": req.location_lat,
        "lng": req.location_lng,
        "created": now,
    })
    return {"service_id": service_id, "message": "Service created"}

# ---------- List all services (public) with provider business info ----------
@router.get("/")
async def list_services():
    rows = await database.fetch_all("""
        SELECT s.*, u.business_name, u.business_image_url
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.is_active = TRUE
        ORDER BY s.created_at DESC
    """)
    return [dict(row) for row in rows]

# ---------- Upload service image ----------
@router.post("/{service_id}/image")
async def upload_service_image(
    service_id: str,
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    service = await database.fetch_one(
        "SELECT provider_id FROM services WHERE service_id = :sid",
        {"sid": service_id}
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    image_url = save_uploaded_file(image, service_id, "image")
    await database.execute(
        "UPDATE services SET image_url = :url WHERE service_id = :sid",
        {"url": image_url, "sid": service_id}
    )
    return {"image_url": image_url}

# ---------- Upload service video ----------
@router.post("/{service_id}/video")
async def upload_service_video(
    service_id: str,
    video: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    service = await database.fetch_one(
        "SELECT provider_id FROM services WHERE service_id = :sid",
        {"sid": service_id}
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service["provider_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    video_url = save_uploaded_file(video, service_id, "video")
    await database.execute(
        "UPDATE services SET video_url = :url WHERE service_id = :sid",
        {"url": video_url, "sid": service_id}
    )
    return {"video_url": video_url}

# ---------- Get single service (dynamic) ----------
@router.get("/{service_id}")
async def get_service(service_id: str):
    row = await database.fetch_one(
        """
        SELECT s.*, u.business_name, u.business_image_url
        FROM services s
        JOIN users u ON s.provider_id = u.id
        WHERE s.service_id = :sid AND s.is_active = TRUE
        """,
        {"sid": service_id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)

# ---------- Book a service ----------
@router.post("/{service_id}/book")
async def book_service(
    service_id: str,
    req: BookServiceRequest,
    current_user: dict = Depends(get_current_user)
):
    service = await database.fetch_one(
        "SELECT * FROM services WHERE service_id = :sid AND is_active = TRUE",
        {"sid": service_id}
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found or inactive")
    client_id = current_user["id"]
    if client_id == service["provider_id"]:
        raise HTTPException(status_code=400, detail="You cannot book your own service")

    provider_available = await database.fetch_val(
        "SELECT is_available FROM provider_availability WHERE user_id = :pid",
        {"pid": service["provider_id"]}
    )
    if provider_available is False:
        raise HTTPException(status_code=400, detail="Provider is currently unavailable")

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id = :uid",
        {"uid": client_id}
    )
    if not wallet or wallet["balance"] < service["price"]:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    await database.execute(
        "UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid",
        {"amt": service["price"], "uid": client_id}
    )

    booking_id = uuid.uuid4().hex[:8]
    now = datetime.utcnow().isoformat()
    await database.execute(
        """
        INSERT INTO service_bookings (booking_id, service_id, client_id, provider_id, amount,
                                      status, scheduled_for, location_lat, location_lng, notes, created_at)
        VALUES (:bid, :sid, :cid, :pid, :amt, 'locked', :sch, :lat, :lng, :notes, :now)
        """,
        {
            "bid": booking_id,
            "sid": service_id,
            "cid": client_id,
            "pid": service["provider_id"],
            "amt": service["price"],
            "sch": req.scheduled_for,
            "lat": req.location_lat,
            "lng": req.location_lng,
            "notes": req.notes,
            "now": now
        }
    )
    return {
        "booking_id": booking_id,
        "service_id": service_id,
        "status": "locked",
        "message": "Service booked. Funds held in escrow."
    }

# ---------- Get services by provider (public) ----------
@router.get("/provider/{provider_id}")
async def get_provider_services_by_user(provider_id: str):
    rows = await database.fetch_all(
        "SELECT * FROM services WHERE provider_id = :pid AND is_active = TRUE ORDER BY created_at DESC",
        {"pid": provider_id}
    )
    return [dict(row) for row in rows]