from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.db.database import database
from app.utils import haversine
from app.utils.security import get_current_user

router = APIRouter(prefix="/order", tags=["Orders"])

VEHICLE_SPEEDS = {"bike": 15.0, "car": 25.0, "truck": 20.0}
PICKUP_HANDLING_MIN = 5
BUFFER_FACTOR = 0.15
BASE_DELIVERY_FEE = 1.0
RATE_PER_MINUTE = 0.2

class DeliverRequest(BaseModel):
    order_id: str
    storekeeper_id: str
    item_amount: float
    listing_id: str
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float

@router.post("/deliver")
async def deliver(req: DeliverRequest, current_user: dict = Depends(get_current_user)):
    shopper_id = current_user["id"]
    rows = await database.fetch_all("SELECT * FROM couriers WHERE is_online = true")
    if not rows:
        raise HTTPException(status_code=404, detail="No couriers")
    d2 = haversine(req.pickup_lat, req.pickup_lng, req.dropoff_lat, req.dropoff_lng)
    best_c, best_pt = None, float('inf')
    for r in rows:
        d1 = haversine(r["lat"], r["lng"], req.pickup_lat, req.pickup_lng)
        speed = VEHICLE_SPEEDS.get(r["vehicle_type"], 20.0)
        travel = (d1+d2)/speed*60
        pt = travel + PICKUP_HANDLING_MIN + travel*BUFFER_FACTOR
        if pt < best_pt:
            best_pt = pt
            best_c = dict(r)
    if not best_c:
        raise HTTPException(status_code=500, detail="Matching failed")
    fee = BASE_DELIVERY_FEE + RATE_PER_MINUTE * best_pt
    total = req.item_amount + fee
    # wallet check
    w = await database.fetch_one("SELECT balance FROM wallets WHERE user_id = :uid", {"uid": shopper_id})
    if not w or w["balance"] < total:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    # reserve
    ex = await database.fetch_one("SELECT * FROM escrow WHERE order_id = :oid", {"oid": req.order_id})
    if ex:
        raise HTTPException(status_code=400, detail="Already reserved")
    await database.execute("UPDATE wallets SET balance = balance - :amt WHERE user_id = :uid", {"amt": total, "uid": shopper_id})
    await database.execute(
        "INSERT INTO escrow (order_id, shopper_id, storekeeper_id, courier_id, item_amount, delivery_fee, total_amount, status) VALUES (:oid,:sid,:stid,:cid,:ia,:df,:tot,'locked')",
        {"oid": req.order_id, "sid": shopper_id, "stid": req.storekeeper_id, "cid": best_c["courier_id"], "ia": req.item_amount, "df": fee, "tot": total}
    )
    return {
        "order_id": req.order_id,
        "status": "locked",
        "total": round(total,2),
        "delivery_fee": round(fee,2),
        "estimated_package_time_min": round(best_pt,1),
        "courier_name": best_c["name"],
        "courier_id": best_c["courier_id"]
    }