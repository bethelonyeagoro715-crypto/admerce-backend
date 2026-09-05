from app.db.database import database

async def add_kyc_columns():
    desired = {
        "first_name": "TEXT",
        "last_name": "TEXT",
        "middle_name": "TEXT",
        "date_of_birth": "TEXT",
        "lga": "TEXT",
        "state_of_origin": "TEXT",
        "nationality": "TEXT",
        "residence_address": "TEXT",
        "national_id_number": "TEXT",
        "kyc_verified": "INTEGER DEFAULT 0",
        "id_document_url": "TEXT",
        "selfie_url": "TEXT"
    }
    existing = await database.fetch_all("PRAGMA table_info(users)")
    existing_names = {row["name"] for row in existing}
    for col, col_def in desired.items():
        if col not in existing_names:
            await database.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
            print(f"✅ Added column {col}")
    print("✅ KYC columns checked/added")