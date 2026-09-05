from sqlalchemy import Column, String, Float, Integer, DateTime
from app.db.database import Base
import datetime

class ServiceModel(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(String, unique=True, index=True)
    provider_id = Column(String, index=True)
    title = Column(String)
    category = Column(String)
    description = Column(String, default="")
    price = Column(Float)
    duration_minutes = Column(Integer, default=60)
    lat = Column(Float, default=6.5244)
    lng = Column(Float, default=3.3792)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)