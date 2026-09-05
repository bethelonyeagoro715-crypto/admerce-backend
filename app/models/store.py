from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

class StoreCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    category: List[str] = Field(..., description="List of category keys like 'tech', 'fashion'")
    address: str = Field(..., description="Pickup address")
    latitude: float
    longitude: float
    phone: str = Field(..., description="Contact phone")
    store_image_url: Optional[str] = None
    business_hours: dict = Field(default_factory=dict, example={"Monday":"9-5","Tuesday":"9-5"})
    contact_preference: str = "in-app"  # in-app or phone

class StoreResponse(BaseModel):
    id: str
    owner_id: str
    name: str
    description: Optional[str]
    category: List[str]
    address: str
    latitude: float
    longitude: float
    phone: str
    store_image_url: Optional[str]
    business_hours: dict
    contact_preference: str
    created_at: datetime
    updated_at: datetime
    verified: bool = False