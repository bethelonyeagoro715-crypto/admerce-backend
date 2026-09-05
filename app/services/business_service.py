import uuid
import json
from datetime import datetime
from typing import Optional, List, Dict
from app.db.database import database
from app.utils import haversine

class BusinessService:
    
    @staticmethod
    async def create_business(user_id: str, data: dict) -> str:
        business_id = uuid.uuid4().hex
        await database.execute(
            """
            INSERT INTO businesses (
                id, user_id, name, category, sub_category, description,
                lat, lng, address, contact_phone, contact_email, website,
                business_type, price_range, operating_hours, tags, is_verified
            ) VALUES (
                :id, :user_id, :name, :category, :sub_category, :description,
                :lat, :lng, :address, :contact_phone, :contact_email, :website,
                :business_type, :price_range, :operating_hours, :tags, :is_verified
            )
            """,
            {
                "id": business_id,
                "user_id": user_id,
                "name": data.get("name"),
                "category": data.get("category"),
                "sub_category": data.get("sub_category"),
                "description": data.get("description"),
                "lat": data.get("lat"),
                "lng": data.get("lng"),
                "address": data.get("address"),
                "contact_phone": data.get("contact_phone"),
                "contact_email": data.get("contact_email"),
                "website": data.get("website"),
                "business_type": data.get("business_type", "Service"),
                "price_range": data.get("price_range", "Medium"),
                "operating_hours": json.dumps(data.get("operating_hours")) if data.get("operating_hours") else None,
                "tags": json.dumps(data.get("tags")) if data.get("tags") else None,
                "is_verified": False,
            }
        )
        return business_id

    @staticmethod
    async def update_business(business_id: str, user_id: str, data: dict) -> bool:
        # Build dynamic update query
        fields = []
        values = {}
        for key, value in data.items():
            if value is not None:
                if key in ["operating_hours", "tags"]:
                    fields.append(f"{key} = :{key}")
                    values[key] = json.dumps(value)
                else:
                    fields.append(f"{key} = :{key}")
                    values[key] = value
        if not fields:
            return False
        values["id"] = business_id
        values["user_id"] = user_id
        query = f"""
            UPDATE businesses SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND user_id = :user_id
        """
        result = await database.execute(query, values)
        return result > 0

    @staticmethod
    async def search_businesses(
        lat: float,
        lng: float,
        radius_km: float = 10.0,
        category: Optional[str] = None,
        sub_category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        business_type: Optional[str] = None,
        price_range: Optional[str] = None,
        min_rating: Optional[float] = None,
        is_available_now: Optional[bool] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[dict]:
        # Base query with distance calculation
        query = """
            SELECT b.*,
                (
                    6371 * acos(
                        cos(radians(:lat)) * cos(radians(b.lat)) *
                        cos(radians(b.lng) - radians(:lng)) +
                        sin(radians(:lat)) * sin(radians(b.lat))
                    )
                ) AS distance_km
            FROM businesses b
            WHERE 1=1
        """
        params = {"lat": lat, "lng": lng}

        if category:
            query += " AND b.category = :category"
            params["category"] = category
        if sub_category:
            query += " AND b.sub_category = :sub_category"
            params["sub_category"] = sub_category
        if business_type:
            query += " AND b.business_type = :business_type"
            params["business_type"] = business_type
        if price_range:
            query += " AND b.price_range = :price_range"
            params["price_range"] = price_range
        if min_rating is not None:
            query += " AND b.rating >= :min_rating"
            params["min_rating"] = min_rating
        if is_available_now is not None:
            query += " AND b.is_available_now = :is_available_now"
            params["is_available_now"] = is_available_now

        # Tag filtering (JSON array contains)
        if tags:
            for tag in tags:
                query += " AND json_extract(b.tags, '$') LIKE :tag"
                params[f"tag_{tag}"] = f'%"{tag}"%'

        query += " HAVING distance_km < :radius_km"
        params["radius_km"] = radius_km

        query += " ORDER BY distance_km ASC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        rows = await database.fetch_all(query, params)
        
        result = []
        for row in rows:
            item = dict(row)
            # Parse JSON fields
            if item.get("operating_hours"):
                item["operating_hours"] = json.loads(item["operating_hours"])
            if item.get("tags"):
                item["tags"] = json.loads(item["tags"])
            result.append(item)
        return result

    @staticmethod
    async def get_business_by_id(business_id: str) -> Optional[dict]:
        row = await database.fetch_one(
            "SELECT * FROM businesses WHERE id = :id",
            {"id": business_id}
        )
        if not row:
            return None
        item = dict(row)
        if item.get("operating_hours"):
            item["operating_hours"] = json.loads(item["operating_hours"])
        if item.get("tags"):
            item["tags"] = json.loads(item["tags"])
        return item

    @staticmethod
    async def get_businesses_by_user(user_id: str) -> List[dict]:
        rows = await database.fetch_all(
            "SELECT * FROM businesses WHERE user_id = :user_id ORDER BY created_at DESC",
            {"user_id": user_id}
        )
        result = []
        for row in rows:
            item = dict(row)
            if item.get("operating_hours"):
                item["operating_hours"] = json.loads(item["operating_hours"])
            if item.get("tags"):
                item["tags"] = json.loads(item["tags"])
            result.append(item)
        return result