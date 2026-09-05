from sqlalchemy import Column, String, Float, Integer, DateTime, Text
from app.db.database import Base
import datetime

class FlipperListingModel(Base):
    __tablename__ = "flipper_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    listing_id = Column(String, unique=True, index=True)
    flipper_id = Column(String, index=True)
    title = Column(String)
    description = Column(Text, default="")
    price = Column(Float)
    source = Column(String)
    condition = Column(String)
    image_url = Column(String, nullable=True)
    lat = Column(Float, default=6.5244)
    lng = Column(Float, default=3.3792)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)