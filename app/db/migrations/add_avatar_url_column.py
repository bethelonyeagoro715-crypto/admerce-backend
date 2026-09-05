from app.db.database import database

async def add_avatar_url_column():
    existing = await database.fetch_all("PRAGMA table_info(users)")
    col_names = {row["name"] for row in existing}
    if "avatar_url" not in col_names:
        await database.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
        print("✅ added avatar_url column to users")
    else:
        print("⏭️ avatar_url column already exists")