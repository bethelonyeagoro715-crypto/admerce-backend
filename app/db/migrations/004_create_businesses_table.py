import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../../seai.db")

def upgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Drop existing if you want a clean slate (optional, comment out if you want to keep old data)
    # cursor.execute("DROP TABLE IF EXISTS businesses")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS businesses (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            sub_category TEXT,
            description TEXT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            address TEXT,
            contact_phone TEXT,
            contact_email TEXT,
            website TEXT,
            business_type TEXT,  -- 'Store', 'Service', 'Freelancer', 'Contractor', 'Consultant', etc.
            price_range TEXT,    -- 'Low', 'Medium', 'High', 'Premium'
            rating REAL DEFAULT 0.0,
            reviews_count INTEGER DEFAULT 0,
            is_verified BOOLEAN DEFAULT 0,
            is_available_now BOOLEAN DEFAULT 0,
            operating_hours TEXT,  -- JSON string: {"mon": "9am-6pm", ...}
            tags TEXT,             -- JSON array: ["24hrs", "certified", "emergency"]
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Indexes for lightning-fast global queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_businesses_lat_lng ON businesses(lat, lng);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_businesses_category ON businesses(category);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_businesses_sub_category ON businesses(sub_category);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_businesses_available ON businesses(is_available_now);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_businesses_rating ON businesses(rating);")

    conn.commit()
    conn.close()
    print("✅ `businesses` table and indexes created successfully.")

def downgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS businesses")
    conn.commit()
    conn.close()
    print("❌ `businesses` table dropped.")

if __name__ == "__main__":
    upgrade()