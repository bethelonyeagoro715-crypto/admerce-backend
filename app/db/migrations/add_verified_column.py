from app.db.database import database

async def add_verified_column():
    cols = await database.fetch_all("PRAGMA table_info(users)")
    col_names = {row["name"] for row in cols}
    if "verified" not in col_names:
        await database.execute("ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0")
        print("✅ added verified column to users")
    else:
        print("⏭️ verified column already exists")