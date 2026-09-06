import sqlite3

user_id = "70a2411bd3684b3b998907405f43b17a"

conn = sqlite3.connect("seai.db")
conn.execute("UPDATE stores SET owner_id = ? WHERE store_id = ?", (user_id, "store_A"))
conn.commit()
conn.close()
print("Store 'store_A' linked to user", user_id)
