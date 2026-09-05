import asyncio
from app.db.database import database

async def main():
    await database.connect()
    rows = await database.fetch_all("SELECT store_id, name, owner_id FROM stores")
    for r in rows:
        print(f'{r["store_id"]} – {r["name"]} (owner: {r["owner_id"]})')
    await database.disconnect()

asyncio.run(main())