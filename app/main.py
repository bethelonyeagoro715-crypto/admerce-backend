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

print(
    "🔑 Cloudinary env:",
    {
        "cloud_name":      bool(os.getenv("CLOUDINARY_CLOUD_NAME")),
        "api_key":         bool(os.getenv("CLOUDINARY_API_KEY")),
        "api_secret":      bool(os.getenv("CLOUDINARY_API_SECRET")),
        "CLOUDINARY_URL":  bool(os.getenv("CLOUDINARY_URL")),
    },
    flush=True,
)

import app.services.cloudinary_service  # noqa: F401

from app.db.database import engine, sync_engine, Base, database

from app.db.service_models import ServiceModel
from app.db.flipper_models import FlipperListingModel
from app.db.models import EventModel
from app.db.listing_models import ListingModel
from app.db.wallet_models import WalletModel, EscrowModel
from app.db.courier_models import CourierModel
from app.db.user_models import UserModel

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

from app.routes.shopper import router as shopper_router

from app.routes.seai_search import router as seai_search_router

SKIP_MODELS = os.getenv("SKIP_MODELS") == "1"

if not SKIP_MODELS:
    from app.routes.ai_tools import router as ai_tools_router
    from app.routes.seai_ask import router as seai_ask_router
    from app.routes.seai_lens import router as lens_router
    from app.routes.seai_transcribe import router as transcribe_router
    from app.routes.businesses import router as businesses_router
else:
    ai_tools_router = None
    seai_ask_router = None
    lens_router = None
    transcribe_router = None
    businesses_router = None
    print("⚠️ SKIP_MODELS=1 – Heavy AI models disabled")


async def _refund_expired_escrows() -> None:
    while True:
        try:
            now = datetime.utcnow()
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
    Base.metadata.create_all(bind=sync_engine)
    print("✅ Tables created/verified (sync).")

    # ✅ FIX: `sync_engine.begin()` commits the DDL on block exit.
    #    `sync_engine.connect()` in SQLAlchemy 2.0 rolls back on exit, so every
    #    CREATE TABLE / ALTER TABLE in this block was being silently discarded.
    #    This is why message_deletions, orders, order_items, and order_stores
    #    never appeared in the database.
    with sync_engine.begin() as conn:
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

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id TEXT,
                store_id TEXT,
                created_at TIMESTAMP,
                PRIMARY KEY (user_id, store_id)
            )
        """)

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id TEXT,
                sender_id TEXT,
                receiver_id TEXT,
                sender_name TEXT,
                receiver_name TEXT,
                text TEXT,
                image_url TEXT,
                audio_url TEXT,
                read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP
            )
        """)
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS conversation_id TEXT")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_name TEXT")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS receiver_name TEXT")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_url TEXT")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS read BOOLEAN DEFAULT FALSE")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to_id INTEGER")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP")
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP")
        conn.exec_driver_sql(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_for_everyone BOOLEAN DEFAULT FALSE"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation_created "
            "ON messages(conversation_id, created_at DESC)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS message_deletions (
                user_id     TEXT NOT NULL,
                message_id  INTEGER NOT NULL,
                deleted_at  TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, message_id)
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_message_deletions_user "
            "ON message_deletions(user_id)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS listing_events (
                id SERIAL PRIMARY KEY,
                listing_id TEXT,
                event_type TEXT,
                created_at TIMESTAMP
            )
        """)

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS events (
                id                SERIAL PRIMARY KEY,
                event_type        TEXT NOT NULL,
                user_id           TEXT,
                session_id        TEXT,
                listing_id        TEXT,
                store_id          TEXT,
                search_query      TEXT,
                user_location     TEXT,
                listing_location  TEXT,
                position          INTEGER,
                timestamp         TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS timestamp TIMESTAMP DEFAULT NOW()"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_events_listing ON events(listing_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

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
            conn.exec_driver_sql(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {dtype}"
            )

        for col, dtype in [
            ("listing_id", "TEXT"),
            ("courier_id", "TEXT"),
            ("delivery_fee", "NUMERIC DEFAULT 0"),
            ("item_amount", "NUMERIC DEFAULT 0"),
            ("unit_price", "NUMERIC DEFAULT 0"),
            ("quantity", "INTEGER DEFAULT 1"),
            ("total_amount", "NUMERIC DEFAULT 0"),
            ("status", "TEXT DEFAULT 'locked'"),
            ("storekeeper_id", "TEXT"),
            ("shopper_id", "TEXT"),
            ("order_id", "TEXT"),
            ("created_at", "TIMESTAMP DEFAULT NOW()"),
            ("expires_at", "TIMESTAMP"),
            ("order_store_id", "INTEGER"),
        ]:
            conn.exec_driver_sql(
                f"ALTER TABLE escrow ADD COLUMN IF NOT EXISTS {col} {dtype}"
            )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS services (
                service_id TEXT PRIMARY KEY,
                provider_id TEXT,
                title TEXT,
                category TEXT,
                description TEXT,
                price DOUBLE PRECISION,
                duration_minutes INTEGER DEFAULT 60,
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                image_url TEXT,
                video_url TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        for col, dtype in [
            ("provider_id", "TEXT"),
            ("title", "TEXT"),
            ("category", "TEXT"),
            ("description", "TEXT"),
            ("price", "DOUBLE PRECISION"),
            ("duration_minutes", "INTEGER DEFAULT 60"),
            ("lat", "DOUBLE PRECISION"),
            ("lng", "DOUBLE PRECISION"),
            ("image_url", "TEXT"),
            ("video_url", "TEXT"),
            ("is_active", "BOOLEAN DEFAULT TRUE"),
            ("created_at", "TIMESTAMP DEFAULT NOW()"),
            ("updated_at", "TIMESTAMP DEFAULT NOW()"),
        ]:
            conn.exec_driver_sql(
                f"ALTER TABLE services ADD COLUMN IF NOT EXISTS {col} {dtype}"
            )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS role_onboardings (
                user_id TEXT NOT NULL,
                role    TEXT NOT NULL,
                PRIMARY KEY (user_id, role)
            )
        """)
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                role    TEXT NOT NULL,
                key     TEXT NOT NULL,
                value   TEXT,
                PRIMARY KEY (user_id, role, key)
            )
        """)
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS call_signals (
                id              SERIAL PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender_id       TEXT NOT NULL,
                receiver_id     TEXT NOT NULL,
                type            TEXT NOT NULL,
                data            TEXT,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS saved_items (
                user_id    TEXT NOT NULL,
                listing_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, listing_id)
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_saved_items_user ON saved_items(user_id)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS wanted_alerts (
                id          SERIAL PRIMARY KEY,
                user_id     TEXT NOT NULL,
                title       TEXT NOT NULL,
                notes       TEXT,
                category    TEXT,
                budget      NUMERIC,
                lat         DOUBLE PRECISION,
                lng         DOUBLE PRECISION,
                is_active   BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMP DEFAULT NOW(),
                expires_at  TIMESTAMP
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_wanted_alerts_user "
            "ON wanted_alerts(user_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_wanted_alerts_active "
            "ON wanted_alerts(is_active, expires_at)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS baskets (
                user_id    TEXT PRIMARY KEY,
                basket_id  TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "ALTER TABLE baskets ADD COLUMN IF NOT EXISTS basket_id TEXT"
        )
        conn.exec_driver_sql(
            "UPDATE baskets SET basket_id = user_id WHERE basket_id IS NULL"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_baskets_basket_id "
            "ON baskets(basket_id)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS basket_items (
                id         SERIAL PRIMARY KEY,
                user_id    TEXT NOT NULL,
                basket_id  TEXT,
                listing_id TEXT NOT NULL,
                store_id   TEXT NOT NULL,
                quantity   INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "ALTER TABLE basket_items ADD COLUMN IF NOT EXISTS basket_id TEXT"
        )
        conn.exec_driver_sql(
            "UPDATE basket_items SET basket_id = user_id WHERE basket_id IS NULL"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_basket_items_user "
            "ON basket_items(user_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_basket_items_basket_id "
            "ON basket_items(basket_id)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id      TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                total_amount  NUMERIC DEFAULT 0,
                status        TEXT DEFAULT 'pending',
                created_at    TIMESTAMP DEFAULT NOW(),
                updated_at    TIMESTAMP DEFAULT NOW(),
                expires_at    TIMESTAMP
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS order_stores (
                id                SERIAL PRIMARY KEY,
                order_id          TEXT NOT NULL,
                store_id          TEXT NOT NULL,
                escrow_id         TEXT,
                subtotal          NUMERIC DEFAULT 0,
                delivery_fee      NUMERIC DEFAULT 0,
                fulfillment_type  TEXT DEFAULT 'pickup',
                status            TEXT DEFAULT 'pending',
                created_at        TIMESTAMP DEFAULT NOW(),
                updated_at        TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_order_stores_order "
            "ON order_stores(order_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_order_stores_store "
            "ON order_stores(store_id)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS order_items (
                id          SERIAL PRIMARY KEY,
                order_id    TEXT NOT NULL,
                store_id    TEXT,
                listing_id  TEXT NOT NULL,
                quantity    INTEGER NOT NULL,
                price       NUMERIC NOT NULL,
                subtotal    NUMERIC NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_order_items_order "
            "ON order_items(order_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_order_items_listing "
            "ON order_items(listing_id)"
        )

    print("✅ Schema migrations complete.")

    await database.connect()
    print("✅ Async database pool connected.")

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
    await database.execute(
        "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS withdrawal_pin TEXT"
    )
    print("✅ wallets table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            amount      NUMERIC NOT NULL,
            type        TEXT NOT NULL,
            description TEXT,
            reference   TEXT NOT NULL,
            status      TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute(
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS description TEXT"
    )
    print("✅ wallet_transactions table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS service_bookings (
            id             SERIAL PRIMARY KEY,
            booking_id     TEXT UNIQUE NOT NULL,
            service_id     TEXT NOT NULL,
            provider_id    TEXT NOT NULL,
            customer_id    TEXT NOT NULL,
            scheduled_for  TIMESTAMP,
            notes          TEXT,
            location_lat   DOUBLE PRECISION,
            location_lng   DOUBLE PRECISION,
            status         TEXT DEFAULT 'pending',
            amount         NUMERIC DEFAULT 0,
            created_at     TIMESTAMP DEFAULT NOW(),
            updated_at     TIMESTAMP DEFAULT NOW()
        )
    """)
    for col, dtype in [
        ("customer_id", "TEXT"),
        ("client_id", "TEXT"),
        ("scheduled_for", "TIMESTAMP"),
        ("notes", "TEXT"),
        ("location_lat", "DOUBLE PRECISION"),
        ("location_lng", "DOUBLE PRECISION"),
        ("status", "TEXT DEFAULT 'pending'"),
        ("amount", "NUMERIC DEFAULT 0"),
        ("updated_at", "TIMESTAMP DEFAULT NOW()"),
    ]:
        await database.execute(
            f"ALTER TABLE service_bookings ADD COLUMN IF NOT EXISTS {col} {dtype}"
        )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_bookings_customer ON service_bookings(customer_id)"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_bookings_provider ON service_bookings(provider_id)"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_bookings_service ON service_bookings(service_id)"
    )
    print("✅ service_bookings table ready.")

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


app = FastAPI(title="SEAI - Admerce Backend (Multi-Role)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.getcwd()
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

app.include_router(payment_router)
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
app.include_router(seai_search_router)
if ai_tools_router:
    app.include_router(ai_tools_router)
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