from app.db.database import engine, Base
from sqlalchemy import Column, Integer, String, Boolean

class UserDevice(Base):
    __tablename__ = "user_devices"
    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True)
    fcm_token = Column(String)
    is_active = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)
print("Table created.")