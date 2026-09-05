from app.db.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE stores ADD COLUMN lat REAL DEFAULT 6.5244"))
        print("✅ Added lat column.")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print("✅ lat column already exists.")
        else:
            print(f"Error: {e}")

    try:
        conn.execute(text("ALTER TABLE stores ADD COLUMN lng REAL DEFAULT 3.3792"))
        print("✅ Added lng column.")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print("✅ lng column already exists.")
        else:
            print(f"Error: {e}")
    conn.commit()