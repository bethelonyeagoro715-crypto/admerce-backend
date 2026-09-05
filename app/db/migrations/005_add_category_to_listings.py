import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../../seai.db")

def upgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Add category column if it doesn't exist
    cursor.execute("""
        ALTER TABLE listings ADD COLUMN category TEXT;
    """)

    conn.commit()
    conn.close()
    print("✅ Added 'category' column to listings table.")

def downgrade():
    # SQLite doesn't support dropping columns directly
    print("⚠️ Manual rollback required for SQLite: recreate table without category column.")
    pass

if __name__ == "__main__":
    upgrade()