from app.db.database import database

async def create_user_settings_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    print("✅ user_settings table ready")