from app.db.database import database

async def create_courier_jobs_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS courier_jobs (
            job_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            courier_id TEXT NOT NULL,
            shopper_id TEXT NOT NULL,
            storekeeper_id TEXT NOT NULL,
            pickup_lat REAL NOT NULL,
            pickup_lng REAL NOT NULL,
            dropoff_lat REAL NOT NULL,
            dropoff_lng REAL NOT NULL,
            status TEXT DEFAULT 'pending',  -- pending, accepted, declined, arrived_store, picked_up, delivered
            vehicle_type TEXT,
            estimated_time REAL,
            delivery_fee REAL,
            created_at TEXT,
            accepted_at TEXT,
            completed_at TEXT
        )
    """)
    print("✅ courier_jobs table ready")