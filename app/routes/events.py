from fastapi import APIRouter, Request, Depends, HTTPException
from app.events import log_event, get_events, Event
from app.db.database import database
from app.routes.auth import get_current_user
from app.routes.seai_ask import _process_ask
from typing import Optional
import json
import traceback
from pydantic import ValidationError  # ✅ import this

router = APIRouter(prefix="/seai", tags=["Events"])

def _build_user_location(body: dict) -> Optional[dict]:
    """Convert user_lat/user_lng (if present) into a location dict."""
    lat = body.get("user_lat")
    lng = body.get("user_lng")
    if lat is not None and lng is not None:
        return {"lat": lat, "lng": lng}
    return body.get("user_location")  # fallback

@router.post("/events")
async def receive_event(
    request: Request,
    current_user: Optional[dict] = Depends(get_current_user)
):
    try:
        body = await request.json()
        print("📩 Received /events body:", json.dumps(body, indent=2))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # ✅ Detect AI query
    if "query" in body:
        print("🤖 AI query detected, delegating to _process_ask")
        query = body.get("query", "")
        lat = float(body.get("lat", 6.5244))
        lng = float(body.get("lng", 3.3792))
        radius_km = float(body.get("radius_km", 10.0))
        conversation_history = body.get("conversation_history", [])

        user_id = current_user["id"] if current_user else None

        return await _process_ask(
            query=query,
            lat=lat,
            lng=lng,
            radius_km=radius_km,
            conversation_history=conversation_history,
            user_id=user_id
        )

    # ✅ Build event data – do NOT include timestamp (default_factory handles it)
    event_data = {
        "event_type": body.get("event_type"),
        "user_id": body.get("user_id") or (current_user["id"] if current_user else None),
        "session_id": body.get("session_id"),
        "listing_id": body.get("listing_id"),
        "store_id": body.get("store_id"),
        "search_query": body.get("search_query"),
        "user_location": _build_user_location(body),
        "listing_location": body.get("listing_location"),
        "position": body.get("position"),
        # ❌ REMOVED: "timestamp": body.get("timestamp")
        #    The Event model's default_factory will generate it automatically.
    }

    print("📝 Logging event with data:", json.dumps(event_data, indent=2))

    try:
        event = Event(**event_data)
    except ValidationError as e:          # ✅ catch Pydantic validation errors
        traceback.print_exc()
        raise HTTPException(status_code=422, detail=e.errors())

    return await log_event(event, db=database)

@router.get("/events")
async def get_all_events():
    return await get_events(db=database)