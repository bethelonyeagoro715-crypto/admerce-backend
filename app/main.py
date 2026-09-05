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

# ── Routers ──────────────────────────────────────────────────────────────
from app.routes.payment import router as payment_router
from app.routes.shopper import router as shopper_router
from app.routes.storekeeper import router as storekeeper_router
from app.routes.courier import router as courier_router
from app.routes.wallet import router as wallet_router
from app.routes.order import router as order_router
from app.routes.profile import router as profile_router
from app.routes.events import router as events_router
from app.routes.flipper import router as flipper_router
from app.routes.service import router as services_router
from app.routes.auth import router as auth_router
from app.routes.ai_tools import router as ai_tools_router
from app.routes.seai_search import router as seai_search_router
from app.routes.seai_ask import router as seai_ask_router
from app.routes.map import router as map_router
from app.routes.auth_social import router as social_router
from app.routes.kyc import router as kyc_router
from app.routes.seai_lens import router as lens_router
from app.routes.admin import router as admin_router
from app.routes.chat import router as chat_router
from app.routes.seai_transcribe import router as transcribe_router
from app.routes.businesses import router as businesses_router
from app.routes.basket import router as basket_router
from app.routes import settings
from app.routes.notifications import router as notifications_router

# ── Background task: auto‑refund expired escrow ──────────────────────────
async def _refund_expired_escrows() -> None:
    while True:
        try:
            now = datetime.utcnow().isoformat()
            expired = await database.fetch_all(
                "SELECT * FROM escrow WHERE status = 'locked' AND expires_at < :now",
                {"now": now},
            )
            for row in expired:
                # Convert total_amount from TEXT to float before updating wallet
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


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create tables (synchronous, using sync_engine)
    Base.metadata.create_all(bind=sync_engine)
    print("✅ Tables created/verified (sync).")

    # 2. Connect the async database pool
    await database.connect()
    print("✅ Async database pool connected.")

    # 3. Create provider_availability table
    await database.execute("""
        CREATE TABLE IF NOT EXISTS provider_availability (
            user_id      TEXT    PRIMARY KEY,
            is_available BOOLEAN DEFAULT TRUE,
            updated_at   TEXT
        )
    """)
    print("✅ provider_availability table ready.")

    # 4. Create wallets table
    await database.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id        TEXT PRIMARY KEY,
            balance        NUMERIC DEFAULT 0,
            withdrawal_pin TEXT,
            created_at     TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ wallets table ready.")

    # 5. Create wallet_transactions table
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

    # 6. Create user_devices table (for FCM tokens)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS user_devices (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            fcm_token  TEXT NOT NULL,
            is_active  BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ user_devices table ready.")

    # 7. Create user_notifications table (notification history)
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

    # 8. Create cards table
    await database.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id              SERIAL PRIMARY KEY,
            user_id         TEXT NOT NULL,
            card_token      TEXT NOT NULL,           -- Paystack authorization code
            last4           TEXT NOT NULL,
            expiry_month    TEXT NOT NULL,
            expiry_year     TEXT NOT NULL,
            brand           TEXT NOT NULL,
            cardholder_name TEXT,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    print("✅ cards table ready.")

    # 9. Start background escrow refund worker
    task = asyncio.create_task(_refund_expired_escrows())
    print("✅ Server is ready.")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 10. Shutdown: disconnect database pool
    await database.disconnect()
    print("🛑 Server shut down cleanly.")


# ── FastAPI app ──────────────────────────────────────────────────────────
app = FastAPI(title="SEAI - Admerce Backend (Multi-Role)", lifespan=lifespan)

# ── CORS – allow all origins (dev) with credentials ─────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static file serving (uploads) ──────────────────────────────────────
BASE_DIR = r"C:\Users\Bethel\SEAI PROJECT"
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Dedicated media route for services (videos/images) to ensure correct MIME type
@app.get("/uploads/services/{filename}")
async def serve_service_media(filename: str):
    filepath = os.path.join(BASE_DIR, "uploads", "services", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "video/mp4" if filename.lower().endswith(".mp4") else "image/jpeg"
    return FileResponse(filepath, media_type=media_type)

# General static mount for all other uploads (must be after the dedicated route)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ── Include all routers ──────────────────────────────────────────────────
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
app.include_router(ai_tools_router)
app.include_router(seai_search_router)
app.include_router(seai_ask_router)
app.include_router(map_router)
app.include_router(social_router)
app.include_router(kyc_router)
app.include_router(lens_router)
app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(transcribe_router)
app.include_router(businesses_router)
app.include_router(basket_router)
app.include_router(settings.router)
app.include_router(notifications_router)

# ── Exception handler (fixed – no body re‑read) ─────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("❌ 422 validation errors:", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/")
async def root():
    return {"message": "SEAI is alive with multi-role API"}