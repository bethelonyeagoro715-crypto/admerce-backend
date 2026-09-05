import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from databases import Database

DB_USER     = "Admerce2026"
DB_PASSWORD = "Bethel2026@"          # raw password
DB_HOST     = "127.0.0.1"
DB_PORT     = "5432"
DB_NAME     = "admerce_db"

# URL-encode the password so that special characters like @ are safe
encoded_password = quote_plus(DB_PASSWORD)

ASYNC_DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SYNC_DATABASE_URL  = f"postgresql+psycopg2://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_async_engine(ASYNC_DATABASE_URL, echo=False, pool_pre_ping=True)

sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    # connect_args={"host": "127.0.0.1"}   # not needed now
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

database = Database(ASYNC_DATABASE_URL, min_size=5, max_size=20)
Base = declarative_base()