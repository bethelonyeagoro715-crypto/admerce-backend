from app.db.database import database

async def add_role_to_settings():
    existing = await database.fetch_all("PRAGMA table_info(user_settings)")
    cols = {row["name"] for row in existing}
    if "role" not in cols:
        # Add the column with a default value
        await database.execute("ALTER TABLE user_settings ADD COLUMN role TEXT DEFAULT 'shopper'")
        # Remove the old primary key and recreate (SQLite limitation; we'll handle by not enforcing duplicate check)
        # Actually, we can just add the column and later ensure upsert uses role.
        print("✅ added role column to user_settings")
    else:
        print("⏭️ role column already exists")