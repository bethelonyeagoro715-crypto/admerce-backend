from app.db.database import engine, Base
from sqlalchemy import Column, String

class Store(Base):
    __tablename__ = "stores"
    store_id = Column(String, primary_key=True)
    store_name = Column(String)

Base.metadata.create_all(bind=engine)
print("Stores table created.")