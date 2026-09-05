import sqlite3
import os
DB_PATH = os.path.join(os.path.dirname(__file__), "../../../seai.db")

def upgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promotions (
            id TEXT PRIMARY KEY,
            image_url TEXT NOT NULL,
            title TEXT,
            subtitle TEXT,
            target_url TEXT,
            is_active BOOLEAN DEFAULT 1,
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Promotions table created.")