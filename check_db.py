import sqlite3

conn = sqlite3.connect("seai.db")
cursor = conn.cursor()

print("=== TABLES ===")
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
for t in tables:
    print(f"  {t[0]}")

print("\n=== STORES ===")
cursor.execute("SELECT * FROM stores;")
rows = cursor.fetchall()
for row in rows:
    print(row)

conn.close()