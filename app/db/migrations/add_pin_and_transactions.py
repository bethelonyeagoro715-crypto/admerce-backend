from app.db.database import database

async def add_pin_and_transactions():
    # Add pin column to wallets if it doesn't exist
    await database.execute("ALTER TABLE wallets ADD COLUMN IF NOT EXISTS pin TEXT")

    # Create transactions table if it doesn't exist
    await database.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            amount DECIMAL,
            type TEXT,
            description TEXT,
            reference TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)