from sqlalchemy import Column, String, Integer, DateTime, JSON
from app.db.database import Base

class EventModel(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    event_type = Column(String, index=True)
    user_id = Column(String, index=True)
    session_id = Column(String)
    listing_id = Column(String, nullable=True)
    store_id = Column(String, nullable=True)
    search_query = Column(String, nullable=True)
    user_location = Column(JSON)
    listing_location = Column(JSON, nullable=True)
    position = Column(Integer, nullable=True)
    timestamp = Column(DateTime)