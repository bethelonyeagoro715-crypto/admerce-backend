import sqlite3
import bcrypt
import hashlib

def hash_password(password: str) -> str:
    prehash = hashlib.sha256(password.encode("utf-8")).digest()
    pwhash = bcrypt.hashpw(prehash, bcrypt.gensalt())
    return pwhash.decode("utf-8")

conn = sqlite3.connect("seai.db")
cursor = conn.cursor()

phone = "08127906512"           # <- CHANGE TO YOUR PHONE
new_password = "Bethel2026@"     # <- CHANGE TO YOUR DESIRED PASSWORD

hashed = hash_password(new_password)

cursor.execute(
    "UPDATE users SET hashed_password = ? WHERE phone = ?",
    (hashed, phone)
)

conn.commit()
conn.close()

print(f"✅ Password reset for {phone}")