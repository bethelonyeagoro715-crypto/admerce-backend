import asyncio
from app.db.database import database

async def main():
    await database.connect()
    await database.execute(
        "UPDATE listings SET quantity_available = 1 "
        "WHERE quantity_available IS NULL OR quantity_available = 0"
    )
    print("✅ All items now have quantity_available = 1")
    await database.disconnect()

asyncio.run(main())