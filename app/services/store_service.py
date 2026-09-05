from app.db.database import get_db
from app.models.store import StoreCreate
from app.db.models import Store
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

async def create_store(owner_id: str, store_data: StoreCreate, db: AsyncSession = Depends(get_db)):
    store = Store(
        owner_id=owner_id,
        name=store_data.name,
        description=store_data.description,
        category=store_data.category,
        address=store_data.address,
        latitude=store_data.latitude,
        longitude=store_data.longitude,
        phone=store_data.phone,
        store_image_url=store_data.store_image_url,
        business_hours=store_data.business_hours,
        contact_preference=store_data.contact_preference,
        verified=False
    )
    db.add(store)
    await db.commit()
    await db.refresh(store)
    return store

async def get_store_by_owner(owner_id: str, db: AsyncSession):
    stmt = select(Store).where(Store.owner_id == owner_id)
    result = await db.execute(stmt)
    return result.scalars().first()