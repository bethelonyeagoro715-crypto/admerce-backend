import sqlite3
import os
DB_PATH = os.path.join(os.path.dirname(__file__), "../../../seai.db")

def upgrade():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE stores ADD COLUMN store_image_url TEXT;")
        conn.commit()
        print("✅ Added store_image_url column.")
    except sqlite3.OperationalError:
        print("⚠️ Column already exists.")
    conn.close()

if __name__ == "__main__":
    upgrade()