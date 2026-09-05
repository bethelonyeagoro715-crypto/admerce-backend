from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime

class BusinessCreate(BaseModel):
    name: str
    category: str
    sub_category: Optional[str] = None
    description: Optional[str] = None
    lat: float
    lng: float
    address: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    website: Optional[str] = None
    business_type: str = "Service"  # Store, Service, Freelancer, Contractor, Consultant
    price_range: str = "Medium"     # Low, Medium, High, Premium
    operating_hours: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None

class BusinessUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    description: Optional[str] = None
    address: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    website: Optional[str] = None
    price_range: Optional[str] = None
    operating_hours: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None
    is_available_now: Optional[bool] = None

class BusinessResponse(BaseModel):
    id: str
    user_id: str
    name: str
    category: str
    sub_category: Optional[str]
    description: Optional[str]
    lat: float
    lng: float
    address: Optional[str]
    contact_phone: Optional[str]
    contact_email: Optional[str]
    website: Optional[str]
    business_type: str
    price_range: str
    rating: float
    reviews_count: int
    is_verified: bool
    is_available_now: bool
    operating_hours: Optional[Dict[str, str]]
    tags: Optional[List[str]]
    created_at: datetime
    updated_at: datetime
    distance_km: Optional[float] = None  # Calculated at query time

class BusinessSearchRequest(BaseModel):
    query: Optional[str] = None
    lat: float
    lng: float
    radius_km: float = 10.0
    category: Optional[str] = None
    sub_category: Optional[str] = None
    tags: Optional[List[str]] = None
    business_type: Optional[str] = None
    price_range: Optional[str] = None
    min_rating: Optional[float] = None
    is_available_now: Optional[bool] = None
    limit: int = 20
    offset: int = 0

class MarketGapRequest(BaseModel):
    lat: float
    lng: float
    category: str
    radius_km: float = 10.0

class MarketGapResponse(BaseModel):
    category: str
    existing_businesses: int
    demand_score: int  # 0-100
    recommendation: str  # "High", "Medium", "Low"
    top_competitors: List[dict]
    insight: str