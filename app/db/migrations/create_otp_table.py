from app.db.database import database

async def create_otp_table():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT DEFAULT 'reset_password',
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        )
    """)
    print("✅ otp_codes table ready")