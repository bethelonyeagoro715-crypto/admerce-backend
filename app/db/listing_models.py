from sqlalchemy import Column, String, Integer, Float, DateTime
from app.db.database import Base
import datetime

class ListingModel(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    listing_id = Column(String, unique=True, index=True)
    store_id = Column(String, index=True)
    title = Column(String)
    price = Column(Float)
    lat = Column(Float)
    lng = Column(Float)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    title_quality = Column(Float, default=1.0)   # 0-1, lower = poorly described
    quantity_total = Column(Integer, default=1)
quantity_available = Column(Integer, default=1)