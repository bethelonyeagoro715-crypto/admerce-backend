# app/db/migrations/recreate_stores_table.py
from app.db.database import database

async def recreate_stores_table():
    # Drop old table if it exists (cascading may require disabling foreign keys)
    await database.execute("DROP TABLE IF EXISTS stores")
    
    # Create new table with all required columns
    await database.execute("""
        CREATE TABLE stores (
            store_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            phone TEXT,
            store_image_url TEXT,
            business_hours TEXT,
            contact_preference TEXT DEFAULT 'in-app',
            verified INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    print("✅ stores table recreated successfully.")