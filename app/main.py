import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

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

print(
    "💳 Paystack env:",
    {
        "PAYSTACK_SECRET_KEY": bool(os.getenv("PAYSTACK_SECRET_KEY")),
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
from app.routes.presence import router as presence_router
from app.routes.community import router as community_router
from app.routes.maintenance import router as maintenance_router
from app.routes.funding import router as funding_router

# ── settings-tree routers ──────────────────────────────────────
from app.routes.auth_extras import router as auth_extras_router
from app.routes.profile_extras import router as profile_extras_router
from app.routes.feedback import router as feedback_router

# ── Wallet router + webhook (Paystack) ─────────
from app.routes.wallet import (
    router as wallet_router,
    webhook_router,
    _credit_ledger,
    _debit_ledger,
)

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


def _as_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _aware(dt):
    """Coerce a datetime to aware UTC. Returns None if not a datetime."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _refund_escrow(shopper_id: str, amount: float, order_id: str) -> None:
    if amount <= 0:
        return
    await _credit_ledger(shopper_id, "shopping", amount)
    await database.execute(
        """
        INSERT INTO wallet_transactions 
            (user_id, amount, type, description, reference, status, created_at)
        VALUES (:uid, :amt, 'credit', :desc, :ref, 'completed', NOW())
        """,
        {"uid": shopper_id, "amt": amount, "desc": f"Refund: escrow expired — order {order_id[:8]}", "ref": f"expire:{order_id}"}
    )


async def _refund_expired_escrows() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            expired = await database.fetch_all(
                "SELECT * FROM escrow WHERE status = 'locked' AND expires_at < :now",
                {"now": now},
            )
            for row in expired:
                order_id = row["order_id"]
                shopper_id = row["shopper_id"]
                refund_amount = float(row["total_amount"] or 0)

                await _refund_escrow(shopper_id, refund_amount, order_id)

                await database.execute(
                    "UPDATE escrow SET status = 'refunded' WHERE order_id = :oid",
                    {"oid": order_id},
                )
                print(
                    f"⏰ Refunded order {order_id} → {shopper_id} "
                    f"(₦{refund_amount:,.0f})",
                    flush=True,
                )
        except Exception as exc:
            print(f"⚠️  Escrow refund loop error: {exc}")
        await asyncio.sleep(60)


async def _refund_booking(customer_id: str, amount: float, booking_id: str) -> None:
    if amount <= 0:
        return
    await _credit_ledger(customer_id, "shopping", amount)
    await database.execute(
        """
        INSERT INTO wallet_transactions 
            (user_id, amount, type, description, reference, status, created_at)
        VALUES (:uid, :amt, 'credit', :desc, :ref, 'completed', NOW())
        """,
        {"uid": customer_id, "amt": amount, "desc": f"Refund: booking {booking_id[:8]} expired", "ref": f"booking-expire:{booking_id}"}
    )


async def _expire_stale_bookings() -> None:
    LOCKED_GRACE_HOURS = 24
    ACCEPTED_GRACE_HOURS = 72

    while True:
        try:
            now = datetime.now(timezone.utc)
            candidates = await database.fetch_all(
                """
                SELECT booking_id, customer_id, provider_id, amount,
                       status, scheduled_for, created_at
                FROM service_bookings
                WHERE status IN ('locked', 'accepted')
                """
            )
            for row in candidates:
                r = dict(row)
                status = (r.get("status") or "").lower()
                due = r.get("scheduled_for") or r.get("created_at")
                if due is None:
                    continue

                if isinstance(due, str):
                    try:
                        due = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                due = _aware(due)
                if due is None:
                    continue

                grace = (
                    LOCKED_GRACE_HOURS
                    if status == "locked"
                    else ACCEPTED_GRACE_HOURS
                )
                deadline = due + timedelta(hours=grace)
                if now < deadline:
                    continue

                booking_id = r["booking_id"]
                customer_id = r["customer_id"]
                amount = _as_float(r.get("amount"))

                if amount > 0:
                    await _refund_booking(customer_id, amount, booking_id)

                await database.execute(
                    """
                    UPDATE service_bookings
                    SET status = 'expired', updated_at = :now
                    WHERE booking_id = :bid
                      AND status IN ('locked', 'accepted')
                    """,
                    {"now": now, "bid": booking_id},
                )
                print(
                    f"⌛ Booking {booking_id} expired (was {status}) — "
                    f"refunded ₦{amount:,.0f} → {customer_id}",
                    flush=True,
                )
        except Exception as exc:
            print(f"⚠️  Booking expiry loop error: {exc}")
        await asyncio.sleep(15 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=sync_engine)
    print("✅ Tables created/verified (sync).")

    with sync_engine.begin() as conn:
        # ═══════════════════════════════════════════════════════════
        # Core tables
        # ═══════════════════════════════════════════════════════════
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

        # ═══════════════════════════════════════════════════════════
        # users table columns (Paystack version)
        # ═══════════════════════════════════════════════════════════
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
            ("last_seen_at", "TIMESTAMPTZ"),
            ("bio", "TEXT"),
            ("phone_verified", "BOOLEAN DEFAULT FALSE"),
            ("email_verified", "BOOLEAN DEFAULT FALSE"),
            ("two_factor_enabled", "BOOLEAN DEFAULT FALSE"),
            ("two_factor_method", "TEXT"),
            ("two_factor_target", "TEXT"),
            ("avatar_width", "INTEGER"),
            ("avatar_height", "INTEGER"),
            ("business_image_width", "INTEGER"),
            ("business_image_height", "INTEGER"),
            # ── Paystack virtual account columns ─────────────────
            ("paystack_customer_code", "TEXT"),
            ("virtual_account_number", "TEXT"),
            ("virtual_bank_name", "TEXT"),
            ("paystack_recipient_code", "TEXT"),
            ("paystack_subaccount_code", "TEXT"),
        ]:
            conn.exec_driver_sql(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {dtype}"
            )

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_users_virtual_account "
            "ON users(virtual_account_number)"
        )
        
        conn.exec_driver_sql(
            "ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_width INTEGER"
        )
        conn.exec_driver_sql(
            "ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_height INTEGER"
        )
        conn.exec_driver_sql(
            "ALTER TABLE stores ADD COLUMN IF NOT EXISTS image_width INTEGER"
        )
        conn.exec_driver_sql(
            "ALTER TABLE stores ADD COLUMN IF NOT EXISTS image_height INTEGER"
        )

        # ═══════════════════════════════════════════════════════════
        # escrow table columns
        # ═══════════════════════════════════════════════════════════
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

        # ═══════════════════════════════════════════════════════════
        # services table
        # ═══════════════════════════════════════════════════════════
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
            ("image_width", "INTEGER"),
            ("image_height", "INTEGER"),
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

        # ═══════════════════════════════════════════════════════════
        # Store verification
        # ═══════════════════════════════════════════════════════════
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS store_verifications (
                id               UUID PRIMARY KEY,
                store_id         TEXT NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
                submitted_by     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status           TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
                legal_name       TEXT NOT NULL,
                business_type    TEXT NOT NULL,
                cac_number       TEXT,
                business_address TEXT NOT NULL,
                contact_phone    TEXT NOT NULL,
                evidence         JSONB NOT NULL DEFAULT '[]'::jsonb,
                reference_code   TEXT NOT NULL UNIQUE,
                submitted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_by      TEXT REFERENCES users(id),
                reviewed_at      TIMESTAMPTZ,
                review_reason    TEXT
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_store_verif_store "
            "ON store_verifications(store_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_store_verif_status "
            "ON store_verifications(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_store_verif_ref "
            "ON store_verifications(reference_code)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS store_verification_events (
                id           BIGSERIAL PRIMARY KEY,
                store_id     TEXT NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
                actor_id     TEXT,
                from_status  TEXT,
                to_status    TEXT NOT NULL,
                reason       TEXT,
                reference    TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_store_verif_events_store "
            "ON store_verification_events(store_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_store_verif_events_created "
            "ON store_verification_events(created_at DESC)"
        )

        conn.exec_driver_sql(
            "ALTER TABLE stores ADD COLUMN IF NOT EXISTS "
            "verification_status TEXT NOT NULL DEFAULT 'unverified'"
        )
        conn.exec_driver_sql(
            "ALTER TABLE stores ADD COLUMN IF NOT EXISTS "
            "verified_at TIMESTAMPTZ"
        )

        conn.exec_driver_sql("""
            UPDATE stores
            SET verification_status = 'verified'
            WHERE verified = TRUE AND verification_status = 'unverified'
        """)

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_users_last_seen "
            "ON users(last_seen_at DESC)"
        )

        # ═══════════════════════════════════════════════════════════
        # Community
        # ═══════════════════════════════════════════════════════════
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS community_messages (
                id              SERIAL PRIMARY KEY,
                sender_id       TEXT NOT NULL,
                sender_name     TEXT,
                sender_avatar   TEXT,
                sender_role     TEXT,
                room            TEXT NOT NULL DEFAULT 'global',
                text            TEXT NOT NULL,
                image_url       TEXT,
                reply_to_id     INTEGER,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                edited_at       TIMESTAMPTZ,
                deleted_at      TIMESTAMPTZ
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_community_room_created "
            "ON community_messages(room, created_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_community_sender "
            "ON community_messages(sender_id)"
        )

        # ═══════════════════════════════════════════════════════════
        # Settings-tree tables
        # ═══════════════════════════════════════════════════════════
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS addresses (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id         TEXT NOT NULL,
                label           TEXT,
                recipient_name  TEXT,
                phone           TEXT,
                line1           TEXT NOT NULL,
                line2           TEXT,
                city            TEXT,
                state           TEXT,
                country         TEXT DEFAULT 'Nigeria',
                postal_code     TEXT,
                is_default      BOOLEAN DEFAULT FALSE,
                kind            TEXT DEFAULT 'home',
                latitude        DOUBLE PRECISION,
                longitude       DOUBLE PRECISION,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_addresses_user "
            "ON addresses (user_id, created_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_default_address "
            "ON addresses (user_id) WHERE is_default = TRUE"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_blocks (
                id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                blocker_id   TEXT NOT NULL,
                blocked_id   TEXT NOT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (blocker_id, blocked_id)
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_user_blocks_blocker "
            "ON user_blocks (blocker_id, created_at DESC)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS login_activity (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id       TEXT NOT NULL,
                action        TEXT NOT NULL,
                device_kind   TEXT,
                device_name   TEXT,
                browser       TEXT,
                os            TEXT,
                ip            TEXT,
                city          TEXT,
                country       TEXT,
                success       BOOLEAN DEFAULT TRUE,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_login_activity_user "
            "ON login_activity (user_id, created_at DESC)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS feedback (
                id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id      TEXT NOT NULL,
                type         TEXT NOT NULL,
                category     TEXT,
                area         TEXT,
                description  TEXT,
                image_url    TEXT,
                rating       INT,
                kind         TEXT,
                comment      TEXT,
                status       TEXT DEFAULT 'open',
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_feedback_user "
            "ON feedback (user_id, created_at DESC)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS pending_contact_changes (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id     TEXT NOT NULL,
                kind        TEXT NOT NULL,
                target      TEXT NOT NULL,
                code        TEXT NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_pending_contact_changes_user "
            "ON pending_contact_changes (user_id, kind, created_at DESC)"
        )

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id         TEXT NOT NULL,
                token_hash      TEXT,
                device_kind     TEXT,
                device_name     TEXT,
                browser         TEXT,
                os              TEXT,
                ip              TEXT,
                city            TEXT,
                country         TEXT,
                last_active_at  TIMESTAMPTZ DEFAULT NOW(),
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_user_sessions_user "
            "ON user_sessions (user_id, last_active_at DESC)"
        )

        # ═══════════════════════════════════════════════════════════
        # Ledger (display buckets)
        # ═══════════════════════════════════════════════════════════
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_ledger (
                user_id     TEXT NOT NULL,
                bucket      TEXT NOT NULL CHECK (bucket IN ('shopping','selling','services')),
                amount      NUMERIC NOT NULL DEFAULT 0,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, bucket)
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_ledger_user ON user_ledger(user_id)"
        )

        conn.exec_driver_sql("""
            INSERT INTO user_ledger (user_id, bucket, amount)
            SELECT u.id, b.bucket, 0
            FROM users u
            CROSS JOIN (VALUES ('shopping'),('selling'),('services')) AS b(bucket)
            ON CONFLICT DO NOTHING
        """)

        # ═══════════════════════════════════════════════════════════
        # Social: user_follows (asymmetric follow graph)
        # ═══════════════════════════════════════════════════════════
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_follows (
                follower_id  TEXT NOT NULL,
                following_id TEXT NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (follower_id, following_id)
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_user_follows_following "
            "ON user_follows (following_id, created_at DESC)"
        )

        # ═══════════════════════════════════════════════════════════
        # Social: profile_highlights (user-curated shortcut strip)
        # ═══════════════════════════════════════════════════════════
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS profile_highlights (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id     TEXT NOT NULL,
                emoji       TEXT NOT NULL,
                label       TEXT NOT NULL,
                route       TEXT NOT NULL,
                sort_order  INTEGER DEFAULT 0,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_profile_highlights_user "
            "ON profile_highlights (user_id, sort_order, created_at)"
        )

    print("✅ Schema migrations complete.")

    await database.connect()
    print("✅ Async database pool connected.")

    # ═══════════════════════════════════════════════════════════
    # Wallet + booking tables (async — after connect)
    # ═══════════════════════════════════════════════════════════
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
    await database.execute(
        "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS pin_failed_count INTEGER DEFAULT 0"
    )
    await database.execute(
        "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS pin_locked_until TIMESTAMPTZ"
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
    await database.execute(
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS anchor_transfer_id TEXT"
    )
    await database.execute(
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS anchor_status TEXT"
    )
    await database.execute(
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS anchor_reference TEXT"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_txn_anchor_transfer "
        "ON wallet_transactions(anchor_transfer_id)"
    )
    print("✅ wallet_transactions table ready.")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS withdraw_idempotency (
            idempotency_key TEXT NOT NULL,
            user_id         TEXT NOT NULL,
            response_json   TEXT NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (idempotency_key, user_id)
        )
    """)
    print("✅ withdraw_idempotency table ready.")

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
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_bookings_status_due "
        "ON service_bookings(status, scheduled_for)"
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
    await database.execute(
        "ALTER TABLE user_notifications ADD COLUMN IF NOT EXISTS kind TEXT"
    )
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

    # ═══════════════════════════════════════════════════════════
    # Background tasks
    # ═══════════════════════════════════════════════════════════
    task_escrow   = asyncio.create_task(_refund_expired_escrows())
    task_bookings = asyncio.create_task(_expire_stale_bookings())
    print("✅ Server is ready.")
    yield

    task_escrow.cancel()
    task_bookings.cancel()
    for t in (task_escrow, task_bookings):
        try:
            await t
        except asyncio.CancelledError:
            pass
    await database.disconnect()
    print("🛑 Server shut down cleanly.")


app = FastAPI(title="SEAI - Admerce Backend (Multi-Role)", lifespan=lifespan)

# 🚨 BULLETPROOF CORS: Do not use ["*"] with credentials in production.
# This reads from your .env file. Add FRONTEND_URL=https://your-app.vercel.app to .env
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
origins = [
    FRONTEND_URL,
    "http://localhost:3000",
    "http://localhost:5173",
    "https://admerce.vercel.app", # Fallback if env fails
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
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

# ═══════════════════════════════════════════════════════════════
# Routers
# ═══════════════════════════════════════════════════════════════
app.include_router(payment_router)
app.include_router(shopper_router)
app.include_router(storekeeper_router)
app.include_router(courier_router)
app.include_router(wallet_router)
app.include_router(webhook_router)
app.include_router(order_router)
app.include_router(profile_router)
app.include_router(profile_extras_router)
app.include_router(events_router)
app.include_router(flipper_router)
app.include_router(services_router)
app.include_router(auth_router)
app.include_router(auth_extras_router)
app.include_router(seai_search_router)
app.include_router(presence_router)
app.include_router(community_router)
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
app.include_router(maintenance_router)
app.include_router(settings.router)
app.include_router(notifications_router)
app.include_router(feedback_router)
app.include_router(funding_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("❌ 422 validation errors:", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# 🚨 BULLETPROOF EXCEPTION HANDLER: Prevents leaking server traces to hackers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"🔥 UNHANDLED SERVER ERROR: {exc}", flush=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again later."}
    )

# 🚨 HEALTH CHECK: Pings the DB to ensure Render doesn't route traffic to a dead server
@app.get("/health")
async def health_check():
    try:
        await database.fetch_one("SELECT 1")
        return {"status": "healthy"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "error": str(e)})

@app.get("/")
async def root():
    return {"message": "SEAI is alive with multi-role API"}