from sqlalchemy import Column, String, Boolean, DateTime
from app.db.database import Base
import datetime

class UserModel(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)          # user_id (UUID)
    phone = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=True)
    hashed_password = Column(String, nullable=False)
    nickname = Column(String, nullable=True)        # for reservations
    real_name = Column(String, nullable=True)       # from KYC
    is_verified = Column(Boolean, default=False)    # KYC status
    created_at = Column(DateTime, default=datetime.datetime.utcnow)