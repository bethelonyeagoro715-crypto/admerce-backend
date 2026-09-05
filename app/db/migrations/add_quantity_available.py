# app/db/migrations/add_quantity_available.py
from app.db.database import database

async def add_quantity_available():
    existing = await database.fetch_all("PRAGMA table_info(listings)")
    col_names = {row["name"] for row in existing}
    if "quantity_available" not in col_names:
        await database.execute("ALTER TABLE listings ADD COLUMN quantity_available INTEGER DEFAULT 1")
        print("✅ added quantity_available to listings")
    else:
        print("⏭️ quantity_available already exists")