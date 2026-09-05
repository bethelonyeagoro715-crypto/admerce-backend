from app.db.database import database

async def create_service_bookings_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS service_bookings (
            booking_id TEXT PRIMARY KEY,
            service_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'locked',
            scheduled_for TEXT,
            location_lat REAL,
            location_lng REAL,
            notes TEXT,
            created_at TEXT,
            completed_at TEXT
        )
    """)
    print("✅ service_bookings table ready")