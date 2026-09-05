from app.db.database import database
import asyncio

async def fill():
    await database.execute(
        "INSERT OR IGNORE INTO stores (store_id, store_name) SELECT DISTINCT store_id, store_id FROM listings"
    )
    print("Stores table populated.")

asyncio.run(fill())