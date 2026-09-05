from app.db.database import database

async def add_liveness_column():
    existing = await database.fetch_all("PRAGMA table_info(users)")
    col_names = {row["name"] for row in existing}
    if "liveness_verified" not in col_names:
        await database.execute("ALTER TABLE users ADD COLUMN liveness_verified INTEGER DEFAULT 0")
        print("✅ Added liveness_verified column to users")
    else:
        print("⏭️ liveness_verified already exists")