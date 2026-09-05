import sqlite3

conn = sqlite3.connect("seai.db")
rows = conn.execute("SELECT id, phone FROM users").fetchall()
for u in rows:
    print(u)
conn.close()