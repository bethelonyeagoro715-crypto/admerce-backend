# app/db/courier_models.py
from sqlalchemy import Column, String, Float, Boolean
from app.db.database import Base

class CourierModel(Base):
    __tablename__ = "couriers"

    courier_id = Column(String, primary_key=True, index=True)
    name = Column(String)
    vehicle_type = Column(String)   # "bike", "car", "truck"
    lat = Column(Float)
    lng = Column(Float)
    is_online = Column(Boolean, default=False)