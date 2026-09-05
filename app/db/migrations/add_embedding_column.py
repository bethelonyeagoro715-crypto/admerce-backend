from app.db.database import database

async def add_embedding_column():
    existing = await database.fetch_all("PRAGMA table_info(listings)")
    col_names = {row["name"] for row in existing}
    if "embedding" not in col_names:
        await database.execute("ALTER TABLE listings ADD COLUMN embedding TEXT")   # store JSON array as text
        print("✅ Added embedding column to listings")
    else:
        print("⏭️ embedding column already exists")