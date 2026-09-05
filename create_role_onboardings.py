import sqlite3
conn = sqlite3.connect("seai.db")
conn.execute("""
    CREATE TABLE IF NOT EXISTS role_onboardings (
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        onboarded_at TEXT NOT NULL,
        PRIMARY KEY (user_id, role)
    )
""")
conn.commit()
conn.close()
print("role_onboardings table created")