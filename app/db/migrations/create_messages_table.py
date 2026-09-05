from app.db.database import database

async def create_messages_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            receiver_id TEXT NOT NULL,
            text TEXT,
            image_url TEXT,
            created_at TEXT NOT NULL,
            read INTEGER DEFAULT 0
        )
    """)
    print("✅ messages table ready")