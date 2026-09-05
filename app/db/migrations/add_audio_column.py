from app.db.database import database

async def add_audio_column():
    existing = await database.fetch_all("PRAGMA table_info(messages)")
    col_names = {row["name"] for row in existing}
    if "audio_url" not in col_names:
        await database.execute("ALTER TABLE messages ADD COLUMN audio_url TEXT")
        print("✅ added audio_url column to messages")
    else:
        print("⏭️ audio_url column already exists")