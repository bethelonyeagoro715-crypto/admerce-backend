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

# ─── Async engine (used if you ever need SQLAlchemy async sessions) ──────
engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"statement_cache_size": 0},   # disable cache for engine
)

# ─── Sync engine (used only for Base.metadata.create_all + migrations) ───
sync_engine = create_engine(SYNC_DATABASE_URL, echo=False, pool_pre_ping=True)

# ─── Async session factory ───────────────────────────────────────────────
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# ─── Databases pool — the one every route uses ───────────────────────────
# IMPORTANT: statement_cache_size MUST be a direct kwarg here. Passing it
# via the URL query string does NOT work — asyncpg silently ignores it.
# Setting it on the engine alone does NOT propagate to this pool either.
# This kwarg is what stops InvalidCachedStatementError when ALTER TABLE
# runs during startup.
database = Database(
    ASYNC_DATABASE_URL,
    min_size=5,
    max_size=20,
    statement_cache_size=0,
)

# ─── Base ────────────────────────────────────────────────────────────────
Base = declarative_base()