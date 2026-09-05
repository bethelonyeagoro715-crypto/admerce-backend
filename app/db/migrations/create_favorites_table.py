# app/db/migrations/create_favorites_table.py
from app.db.database import database

async def create_favorites_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, store_id)
        )
    """)
    print("✅ favorites table ready")