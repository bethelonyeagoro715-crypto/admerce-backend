import asyncio
from app.db.database import database

async def main():
    await database.connect()
    await database.execute('DELETE FROM users')
    await database.execute('DELETE FROM wallets')
    await database.execute('DELETE FROM otp_codes')
    print('✅ All test users cleared')
    await database.disconnect()

asyncio.run(main())
