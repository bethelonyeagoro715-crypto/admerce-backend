from app.db.database import database

async def create_call_signals_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS call_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            receiver_id TEXT NOT NULL,
            type TEXT NOT NULL,   -- 'offer', 'answer', 'ice-candidate'
            data TEXT NOT NULL,   -- JSON string
            created_at TEXT NOT NULL
        )
    """)
    print("✅ call_signals table ready")