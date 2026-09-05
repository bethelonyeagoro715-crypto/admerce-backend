from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.db.database import database
from app.utils.security import get_current_user
from app.utils import haversine
import uuid
from datetime import datetime

router = APIRouter(prefix="/courier", tags=["Courier"])

VEHICLE_SPEEDS = {"bike": 15.0, "car": 25.0, "truck": 20.0}
PICKUP_HANDLING_MIN = 5
BUFFER_FACTOR = 0.15
BASE_DELIVERY_FEE = 1.0
RATE_PER_MINUTE = 0.2

# ---------- Models ----------
class DeliveryRequest(BaseModel):
    order_id: str               # the reservation order from wallet
    storekeeper_id: str
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float

class AcceptJobRequest(BaseModel):
    job_id: str

class DeclineJobRequest(BaseModel):
    job_id: str

class LocationUpdate(BaseModel):
    lat: float
    lng: float

class StatusUpdate(BaseModel):
    job_id: str
    status: str   # 'arrived_store', 'picked_up', 'delivered'

# ---------- Existing: Register ----------
@router.post("/register")
async def register(courier_id: str, name: str, vehicle_type: str, lat: float, lng: float, current_user: dict = Depends(get_current_user)):
    if courier_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="You can only register yourself")
    exist = await database.fetch_one("SELECT * FROM couriers WHERE courier_id = :cid", {"cid": courier_id})
    if exist:
        raise HTTPException(status_code=400, detail="Already registered")
    await database.execute(
        "INSERT INTO couriers (courier_id, name, vehicle_type, lat, lng, is_online) VALUES (:cid, :n, :v, :lat, :lng, false)",
        {"cid": courier_id, "n": name, "v": vehicle_type, "lat": lat, "lng": lng}
    )
    w = await database.fetch_one("SELECT * FROM wallets WHERE user_id = :uid", {"uid": courier_id})
    if not w:
        await database.execute("INSERT INTO wallets (user_id, balance) VALUES (:uid, 0.0)", {"uid": courier_id})
    return {"courier_id": courier_id, "message": "Registered"}

# ---------- Existing: Set Online ----------
@router.post("/online")
async def set_online(courier_id: str, online: bool = True, current_user: dict = Depends(get_current_user)):
    if courier_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="You can only change your own status")
    await database.execute("UPDATE couriers SET is_online = :on WHERE courier_id = :cid", {"on": online, "cid": courier_id})
    return {"courier_id": courier_id, "status": "online" if online else "offline"}

# ---------- UPDATED: Match (now with real order & storekeeper) ----------
@router.post("/match")
async def match(req: DeliveryRequest, current_user: dict = Depends(get_current_user)):
    # 1. Verify the reservation exists and belongs to this shopper
    order = await database.fetch_one(
        "SELECT * FROM escrow WHERE order_id = :oid AND shopper_id = :sid AND status = 'locked'",
        {"oid": req.order_id, "sid": current_user["id"]}
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found or not available for delivery")

    # 2. Prevent duplicate courier assignment
    existing_job = await database.fetch_one(
        "SELECT * FROM courier_jobs WHERE order_id = :oid AND status != 'declined'",
        {"oid": req.order_id}
    )
    if existing_job:
        raise HTTPException(status_code=400, detail="A courier is already assigned to this order")

    # 3. Find online couriers
    rows = await database.fetch_all("SELECT * FROM couriers WHERE is_online = true")
    if not rows:
        raise HTTPException(status_code=404, detail="No couriers available")

    d2 = haversine(req.pickup_lat, req.pickup_lng, req.dropoff_lat, req.dropoff_lng)
    best_courier = None
    best_pt = float('inf')
    best_d1 = 0.0
    for row in rows:
        d1 = haversine(row["lat"], row["lng"], req.pickup_lat, req.pickup_lng)
        speed = VEHICLE_SPEEDS.get(row["vehicle_type"], 20.0)
        travel = (d1 + d2) / speed * 60.0
        pt = travel + PICKUP_HANDLING_MIN + travel * BUFFER_FACTOR
        if pt < best_pt:
            best_pt = pt
            best_courier = dict(row)
            best_d1 = d1

    if not best_courier:
        raise HTTPException(status_code=500, detail="Matching failed")

    fee = BASE_DELIVERY_FEE + RATE_PER_MINUTE * best_pt

    # 4. Create the courier job with real order & storekeeper
    job_id = uuid.uuid4().hex[:10]
    now = datetime.utcnow().isoformat()
    await database.execute(
        """
        INSERT INTO courier_jobs (job_id, order_id, courier_id, shopper_id, storekeeper_id,
                                 pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                                 status, vehicle_type, estimated_time, delivery_fee, created_at)
        VALUES (:jid, :oid, :cid, :sid, :skid, :plat, :plng, :dlat, :dlng, 'pending', :vtype, :etime, :fee, :now)
        """,
        {
            "jid": job_id,
            "oid": req.order_id,
            "cid": best_courier["courier_id"],
            "sid": current_user["id"],
            "skid": req.storekeeper_id,
            "plat": req.pickup_lat,
            "plng": req.pickup_lng,
            "dlat": req.dropoff_lat,
            "dlng": req.dropoff_lng,
            "vtype": best_courier["vehicle_type"],
            "etime": round(best_pt, 1),
            "fee": round(fee, 2),
            "now": now
        }
    )

    # 5. Update the escrow record with delivery fee & courier so it can be released later
    await database.execute(
        "UPDATE escrow SET delivery_fee = :fee, courier_id = :cid WHERE order_id = :oid",
        {"fee": round(fee, 2), "cid": best_courier["courier_id"], "oid": req.order_id}
    )

    return {
        "job_id": job_id,
        "order_id": req.order_id,
        "best_courier_id": best_courier["courier_id"],
        "courier_name": best_courier["name"],
        "vehicle_type": best_courier["vehicle_type"],
        "estimated_package_time_min": round(best_pt, 1),
        "delivery_fee": round(fee, 2),
        "store_to_shopper_km": round(d2, 2),
        "courier_to_store_km": round(best_d1, 2),
        "message": "Courier assigned. Awaiting acceptance."
    }

# ---------- Accept Job ----------
@router.post("/accept-job")
async def accept_job(req: AcceptJobRequest, current_user: dict = Depends(get_current_user)):
    job = await database.fetch_one("SELECT * FROM courier_jobs WHERE job_id = :jid", {"jid": req.job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["courier_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="This job is not assigned to you")
    if job["status"] != "pending":
        raise HTTPException(status_code=400, detail="Job already accepted or declined")

    now = datetime.utcnow().isoformat()
    await database.execute(
        "UPDATE courier_jobs SET status = 'accepted', accepted_at = :now WHERE job_id = :jid",
        {"now": now, "jid": req.job_id}
    )
    return {"job_id": req.job_id, "status": "accepted", "message": "Job accepted"}

# ---------- Decline Job ----------
@router.post("/decline-job")
async def decline_job(req: DeclineJobRequest, current_user: dict = Depends(get_current_user)):
    job = await database.fetch_one("SELECT * FROM courier_jobs WHERE job_id = :jid", {"jid": req.job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["courier_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="This job is not assigned to you")
    if job["status"] != "pending":
        raise HTTPException(status_code=400, detail="Job already processed")

    await database.execute("UPDATE courier_jobs SET status = 'declined' WHERE job_id = :jid", {"jid": req.job_id})
    return {"job_id": req.job_id, "status": "declined", "message": "Job declined. System will reassign."}

# ---------- Update Location (GPS ping) ----------
@router.post("/location")
async def update_location(req: LocationUpdate, current_user: dict = Depends(get_current_user)):
    await database.execute(
        "UPDATE couriers SET lat = :lat, lng = :lng WHERE courier_id = :cid",
        {"lat": req.lat, "lng": req.lng, "cid": current_user["id"]}
    )
    return {"courier_id": current_user["id"], "lat": req.lat, "lng": req.lng, "message": "Location updated"}

# ---------- Status Update (arrived_store, picked_up, delivered) ----------
@router.post("/status")
async def update_status(req: StatusUpdate, current_user: dict = Depends(get_current_user)):
    valid_statuses = ["arrived_store", "picked_up", "delivered"]
    if req.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid_statuses}")

    job = await database.fetch_one("SELECT * FROM courier_jobs WHERE job_id = :jid", {"jid": req.job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["courier_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="You are not assigned to this job")

    now = datetime.utcnow().isoformat()
    await database.execute(
        "UPDATE courier_jobs SET status = :status WHERE job_id = :jid",
        {"status": req.status, "jid": req.job_id}
    )

    if req.status == "delivered":
        await database.execute(
            "UPDATE courier_jobs SET completed_at = :now WHERE job_id = :jid",
            {"now": now, "jid": req.job_id}
        )

    return {"job_id": req.job_id, "status": req.status, "message": f"Job status updated to {req.status}"}

# ---------- Get Jobs (for courier) ----------
@router.get("/jobs")
async def get_jobs(current_user: dict = Depends(get_current_user)):
    jobs = await database.fetch_all(
        "SELECT * FROM courier_jobs WHERE courier_id = :cid ORDER BY created_at DESC",
        {"cid": current_user["id"]}
    )
    return [dict(job) for job in jobs]