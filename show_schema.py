import sqlite3
import os

DB_PATH = "seai.db"  # change this if your database is elsewhere

# Check if the database file exists
if not os.path.exists(DB_PATH):
    print(f"❌ Database file '{DB_PATH}' not found!")
    print("Please update DB_PATH to the correct path.")
    exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Get all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()

if not tables:
    print("⚠️ No tables found.")
else:
    for table in tables:
        table_name = table[0]
        print(f"\n📋 Table: {table_name}")
        
        # Get column info
        cursor.execute(f"PRAGMA table_info({table_name});")
        columns = cursor.fetchall()
        for col in columns:
            print(f"  {col[1]} ({col[2]})")
        
        # Show row count
        cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
        count = cursor.fetchone()[0]
        print(f"  Row count: {count}")
        
        # Show first 3 rows as sample (if any)
        if count > 0:
            print("  Sample rows (first 3):")
            cursor.execute(f"SELECT * FROM {table_name} LIMIT 3;")
            rows = cursor.fetchall()
            # Get column names for better output
            col_names = [description[0] for description in cursor.description]
            for row in rows:
                # Print each row as a dict for readability
                row_dict = dict(zip(col_names, row))
                print(f"    {row_dict}")
        else:
            print("  (empty)")

conn.close()
print("\n✅ Done.")