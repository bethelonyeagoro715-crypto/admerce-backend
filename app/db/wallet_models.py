# app/db/wallet_models.py
from sqlalchemy import Column, String, Float, Integer, DateTime, Boolean
from app.db.database import Base
import datetime

class WalletModel(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(String, unique=True, index=True)   # could be shopper, storekeeper, courier, etc.
    balance = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class EscrowModel(Base):
    __tablename__ = "escrow"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    order_id = Column(String, unique=True, index=True)
    shopper_id = Column(String)
    storekeeper_id = Column(String)
    courier_id = Column(String, nullable=True)   # null if pickup only
    item_amount = Column(Float)
    delivery_fee = Column(Float, default=0.0)
    total_amount = Column(Float)
    status = Column(String, default="locked")    # locked, released, refunded
    created_at = Column(DateTime, default=datetime.datetime.utcnow)