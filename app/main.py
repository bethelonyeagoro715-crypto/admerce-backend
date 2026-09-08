import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError

from dotenv import load_dotenv
load_dotenv()

from app.db.database import engine, sync_engine, Base, database

# ── Models – IMPORT ALL OF THEM so Base.metadata knows about them ────
from app.db.service_models import ServiceModel
from app.db.flipper_models import FlipperListingModel
from app.db.models import EventModel
from app.db.listing_models import ListingModel
from app.db.wallet_models import WalletModel, EscrowModel
from app.db.courier_models import CourierModel
from app.db.user_models import UserModel

# ── Routers (lightweight ones always imported) ─────────────────────────
from app.routes.payment import router as payment_router
from app.routes.storekeeper import router as storekeeper_router
from app.routes.courier import router as courier_router
from app.routes.wallet import router as wallet_router
from app.routes.order import router as order_router
from app.routes.profile import router as profile_router
from app.routes.events import router as events_router
from app.routes.flipper import router as flipper_router
from app.routes.service import router as services_router
from app.routes.auth import router as auth_router
from app.routes.map import router as map_router
from app.routes.auth_social import router as social_router
from app.routes.kyc import router as kyc_router
from app.routes.admin import router as admin_router
from app.routes.chat import router as chat_router
from app.routes.basket import router as basket_router
from app.routes import settings
from app.routes.notifications import router as notifications_router

# Conditionally import heavy AI routers
SKIP_MODELS = os.getenv("SKIP_MODELS") == "1"

if not SKIP_MODELS:
    from app.routes.shopper import router as shopper_router
    from app.routes.ai_tools import router as ai_tools_router
    from app.routes.seai_search import router as seai_search_router
    from app.routes.seai_ask import router as seai_ask_router
    from app.routes.seai_lens import router as lens_router
    from app.routes.seai_transcribe import router as transcribe_router
    from app.routes.businesses import router as businesses_router
else:
    shopper_router = None
    ai_tools_router = None
    seai_search_router = None
    seai_ask_router = None
    lens_router = None
    transcribe_router = None
    businesses_router = None
    print("⚠️ SKIP_MODELS=1 – Heavy AI models disabled")

# ── Background task: auto‑refund expired escrow ──────────────────────────
async def _refund_expired_escrows() -> None:
    while True:
        try:
            now = datetime.utcnow()   # ✅ datetime object
            expired = await database.fetch_all(
                "SELECT * FROM escrow WHERE status = 'locked' AND expires_at < :now",
                {"now": now},
            )
            for row in expired:
                refund_amount = float(row["total_amount"])
                await database.execute(
                    "UPDATE wallets SET balance = balance + :amt WHERE user_id = :uid",
                    {"amt": refund_amount, "uid": row["shopper_id"]},
                )
                await database.execute(
                    "UPDATE escrow SET status = 'refunded' WHERE order_id = :oid",
                    {"oid": row["order_id"]},
                )
                print(f"⏰ Refunded order {row['order_id']} → {row['shopper_id']}")
        except Exception as exc:
            print(f"⚠️  Escrow refund loop error: {exc}")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create tables from models
    Base.metadata.create_all(bind=sync_engine)
    print("✅ Tables created/verified (sync).")

    # 2. Run schema migrations (raw SQL)
    with sync_engine.connect() as conn:
        # otp_codes
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                code TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER DEFAULT 0
            )
        """)

        # stores
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stores (
                store_id TEXT PRIMARY KEY,
                owner_id TEXT,
                name TEXT,
                description TEXT,
                category TEXT,
                address TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                phone TEXT,
                store_image_url TEXT,
                business_hours TEXT,
                contact_preference TEXT,
                verified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)

        # listings
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS listings (
                listing_id TEXT PRIMARY KEY,
                store_id TEXT,
                title TEXT,
                price DOUBLE PRECISION,
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                category TEXT,
                sort_order INTEGER,
                created_at TIMESTAMP,
                title_quality DOUBLE PRECISION,
                image_url TEXT,
                embedding TEXT,
                quantity_total INTEGER,
                quantity_available INTEGER
            )
        """)

        # favorites
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id TEXT,
                store_id TEXT,
                created_at TIMESTAMP,
                PRIMARY KEY (user_id, store_id)
            )
        """)

        # messages
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender_id TEXT,
                receiver_id TEXT,
                text TEXT,
                image_url TEXT,
                created_at TIMESTAMP
            )
        """)

        # listing_events (for stats)
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS listing_events (
                id SERIAL PRIMARY KEY,
                listing_id TEXT,
                event_type TEXT,
                created_at TIMESTAMP
            )
        """)

        # users columns (ensure existence)
        for col, dtype in [
            ("verified", "BOOLEAN DEFAULT FALSE"),
            ("nickname", "TEXT"),
            ("real_name", "TEXT"),
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
            ("middle_name", "TEXT"),
            ("date_of_birth", "DATE"),
            ("lga", "TEXT"),
            ("state_of_origin", "TEXT"),
            ("nationality", "TEXT"),
            ("residence_address", "TEXT"),
            ("national_id_number", "TEXT"),
            ("kyc_verified", "BOOLEAN DEFAULT FALSE"),
            ("id_document_url", "TEXT"),
            ("selfie_url", "TEXT"),
            ("liveness_verified", "BOOLEAN DEFAULT FALSE"),
            ("avatar_url", "TEXT"),
            ("role", "TEXT"),
            ("suspended", "BOOLEAN DEFAULT FALSE"),
            ("business_image_url", "TEXT"),
            ("business_name", "TEXT"),
        ]:
            conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {dtype}")

        # escrow expires_at
        conn.exec_driver_sql("ALTER TABLE escrow ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")

    print("✅ Schema migrations complete.")

    # 3. Connect async database pool
    await database.connect()
    print("✅ Async database pool connected.")

    # 4. Create other necessary tables (async)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS provider_availability (
            user_id      TEXT    PRIMARY KEY,
            is_available BOOLEAN DEFAULT TRUE,
            updated_at   TEXT
        )
    """)
    print("✅ provider_availability table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id        TEXT PRIMARY KEY,
            balance        NUMERIC DEFAULT 0,
            withdrawal_pin TEXT,
            created_at     TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ wallets table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            amount     NUMERIC NOT NULL,
            type       TEXT NOT NULL,
            reference  TEXT NOT NULL,
            status     TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ wallet_transactions table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS user_devices (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT,
            fcm_token  TEXT NOT NULL,
            is_active  BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ user_devices table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS user_notifications (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            title      TEXT,
            body       TEXT,
            data       TEXT,
            is_read    BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ user_notifications table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id              SERIAL PRIMARY KEY,
            user_id         TEXT NOT NULL,
            card_token      TEXT NOT NULL,
            last4           TEXT NOT NULL,
            expiry_month    TEXT NOT NULL,
            expiry_year     TEXT NOT NULL,
            brand           TEXT NOT NULL,
            cardholder_name TEXT,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ cards table ready.")

    # 5. Start background task
    task = asyncio.create_task(_refund_expired_escrows())
    print("✅ Server is ready.")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await database.disconnect()
    print("🛑 Server shut down cleanly.")


# ── FastAPI app ──────────────────────────────────────────────────────────
app = FastAPI(title="SEAI - Admerce Backend (Multi-Role)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static file serving (uploads) ──────────────────────────────────────
BASE_DIR = os.getcwd()  # Use current working directory on Render
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/uploads/services/{filename}")
async def serve_service_media(filename: str):
    filepath = os.path.join(BASE_DIR, "uploads", "services", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "video/mp4" if filename.lower().endswith(".mp4") else "image/jpeg"
    return FileResponse(filepath, media_type=media_type)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ── Include all routers ──────────────────────────────────────────────────
app.include_router(payment_router)
if shopper_router:
    app.include_router(shopper_router)
app.include_router(storekeeper_router)
app.include_router(courier_router)
app.include_router(wallet_router)
app.include_router(order_router)
app.include_router(profile_router)
app.include_router(events_router)
app.include_router(flipper_router)
app.include_router(services_router)
app.include_router(auth_router)
if ai_tools_router:
    app.include_router(ai_tools_router)
if seai_search_router:
    app.include_router(seai_search_router)
if seai_ask_router:
    app.include_router(seai_ask_router)
app.include_router(map_router)
app.include_router(social_router)
app.include_router(kyc_router)
if lens_router:
    app.include_router(lens_router)
app.include_router(admin_router)
app.include_router(chat_router)
if transcribe_router:
    app.include_router(transcribe_router)
if businesses_router:
    app.include_router(businesses_router)
app.include_router(basket_router)
app.include_router(settings.router)
app.include_router(notifications_router)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("❌ 422 validation errors:", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

@app.get("/")
async def root():
    return {"message": "SEAI is alive with multi-role API"}