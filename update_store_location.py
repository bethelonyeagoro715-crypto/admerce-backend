import asyncio
from app.db.database import database

async def main():
    await database.connect()
    await database.execute(
        "UPDATE stores SET latitude = :lat, longitude = :lng",
        {"lat": 5.383187, "lng": 7.008897}
    )
    print("✅ Store moved to FUTO, Owerri")
    await database.disconnect()

asyncio.run(main())