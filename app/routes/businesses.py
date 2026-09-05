import os

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
from app.routes.auth import get_current_user
from app.models.business_models import (
    BusinessCreate, BusinessUpdate, BusinessResponse,
    BusinessSearchRequest, MarketGapRequest, MarketGapResponse
)
from app.services.business_service import BusinessService
from app.services.market_analysis_service import MarketGapService
import uuid

# ✅ This is the router we import in main.py
router = APIRouter(prefix="/businesses", tags=["Businesses"])

# ============================================================
# CRUD OPERATIONS
# ============================================================

@router.post("/", response_model=dict)
async def create_business(
    data: BusinessCreate,
    current_user: dict = Depends(get_current_user)
):
    business_id = await BusinessService.create_business(current_user["id"], data.dict())
    return {"id": business_id, "message": "Business created successfully"}

@router.put("/{business_id}", response_model=dict)
async def update_business(
    business_id: str,
    data: BusinessUpdate,
    current_user: dict = Depends(get_current_user)
):
    updated = await BusinessService.update_business(business_id, current_user["id"], data.dict(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Business not found or not owned by you")
    return {"message": "Business updated successfully"}

@router.get("/me", response_model=List[BusinessResponse])
async def get_my_businesses(current_user: dict = Depends(get_current_user)):
    businesses = await BusinessService.get_businesses_by_user(current_user["id"])
    return businesses

@router.get("/{business_id}", response_model=BusinessResponse)
async def get_business(business_id: str):
    business = await BusinessService.get_business_by_id(business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return business

# ============================================================
# UNIVERSAL SEARCH & DISCOVERY
# ============================================================

@router.post("/search", response_model=List[BusinessResponse])
async def search_businesses(req: BusinessSearchRequest):
    businesses = await BusinessService.search_businesses(
        lat=req.lat,
        lng=req.lng,
        radius_km=req.radius_km,
        category=req.category,
        sub_category=req.sub_category,
        tags=req.tags,
        business_type=req.business_type,
        price_range=req.price_range,
        min_rating=req.min_rating,
        is_available_now=req.is_available_now,
        limit=req.limit,
        offset=req.offset,
    )
    return businesses

# ============================================================
# MARKET GAP ANALYSIS
# ============================================================

@router.post("/market-gap", response_model=MarketGapResponse)
async def analyze_market_gap(req: MarketGapRequest):
    if not req.category:
        raise HTTPException(status_code=400, detail="Category is required")
    analysis = await MarketGapService.analyze(
        lat=req.lat,
        lng=req.lng,
        category=req.category,
        radius_km=req.radius_km
    )
    return analysis

# ============================================================
# AI-POWERED INTENT SEARCH
# ============================================================

@router.post("/discover")
async def discover_with_ai(query: str, lat: float, lng: float, radius_km: float = 10.0):
    from groq import Groq
    import json
    groq = os.getenv("GROQ_API_KEY")
    prompt = f"""
    Parse this user query into search filters for a business database.
    Query: "{query}"
    Output JSON only with these keys: category, sub_category, tags, business_type, price_range, min_rating, is_available_now.
    Example: {{"category": "Plumbing", "tags": ["emergency"], "is_available_now": true}}
    """
    response = groq.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    try:
        filters = json.loads(response.choices[0].message.content)
    except:
        filters = {}
    results = await BusinessService.search_businesses(
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        category=filters.get("category"),
        sub_category=filters.get("sub_category"),
        tags=filters.get("tags"),
        business_type=filters.get("business_type"),
        price_range=filters.get("price_range"),
        min_rating=filters.get("min_rating"),
        is_available_now=filters.get("is_available_now"),
        limit=20,
    )
    return {"query": query, "filters": filters, "results": results}