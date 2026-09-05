import sqlite3
import os
DB_PATH = os.path.join(os.path.dirname(__file__), "../../../seai.db")

def upgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS saved_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (listing_id) REFERENCES listings(listing_id) ON DELETE CASCADE,
            UNIQUE(user_id, listing_id)
        )
    """)
    conn.commit()
    conn.close()
    print("✅ saved_items table created.")

if __name__ == "__main__":
    upgrade()