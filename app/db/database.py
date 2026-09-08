import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from databases import Database

# Try to get production DATABASE_URL (from Render/Neon)
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Convert standard postgresql:// to SQLAlchemy/asyncpg formats
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
    SYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")
else:
    DB_USER = "Admerce2026"
    DB_PASSWORD = "Bethel2026@"
    DB_HOST = "127.0.0.1"
    DB_PORT = "5432"
    DB_NAME = "admerce_db"
    encoded_password = quote_plus(DB_PASSWORD)
    ASYNC_DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    SYNC_DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Add statement_cache_size=0 to the async URL for the Database object
if "?" in ASYNC_DATABASE_URL:
    ASYNC_DATABASE_URL_FOR_POOL = ASYNC_DATABASE_URL + "&statement_cache_size=0"
else:
    ASYNC_DATABASE_URL_FOR_POOL = ASYNC_DATABASE_URL + "?statement_cache_size=0"

engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"statement_cache_size": 0},   # disable cache for engine
)
sync_engine = create_engine(SYNC_DATABASE_URL, echo=False, pool_pre_ping=True)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# Use the modified URL for Database (ensures statement_cache_size=0)
database = Database(ASYNC_DATABASE_URL_FOR_POOL, min_size=5, max_size=20)
Base = declarative_base()