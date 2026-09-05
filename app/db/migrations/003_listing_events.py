import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../../seai.db")

def upgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL,
            user_id TEXT,
            event_type TEXT NOT NULL,  -- 'view', 'click', 'reserve', 'purchase'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (listing_id) REFERENCES listings(listing_id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()
    print("✅ listing_events table created.")

def downgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS listing_events")
    conn.commit()
    conn.close()
    print("❌ listing_events table dropped.")

if __name__ == "__main__":
    upgrade()