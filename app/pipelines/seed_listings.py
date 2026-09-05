import sqlite3
import json
import random
import datetime

def run():
    conn = sqlite3.connect("seai.db")
    cursor = conn.cursor()

    # Delete existing listings to start fresh
    cursor.execute("DELETE FROM listings")

    # Sample listings around Yaba, Lagos
    sample_listings = [
        ("list_001", "store_A", "Indomie Noodles (carton)", 12000.0, 6.5250, 3.3800, 30, 0.9),
        ("list_002", "store_B", "Vintage Denim Jacket", 8000.0, 6.5100, 3.3700, 200, 0.3),
        ("list_003", "store_A", "Sony WH-1000XM5 Headphones", 250000.0, 6.5250, 3.3800, 5, 0.95),
        ("list_004", "store_C", "Yam tubers (5kg basket)", 15000.0, 6.5400, 3.3900, 45, 0.8),
        ("list_005", "store_B", "Retro Sneakers (used)", 12000.0, 6.5100, 3.3700, 300, 0.2),
    ]

    # Insert with random creation times to simulate recency
    now = datetime.datetime.utcnow()
    for (lid, store, title, price, lat, lng, mins_ago, quality) in sample_listings:
        created = now - datetime.timedelta(minutes=mins_ago)
        cursor.execute(
    "INSERT INTO listings (listing_id, store_id, title, price, lat, lng, created_at, title_quality, quantity_total, quantity_available) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (lid, store, title, price, lat, lng, created, quality, 10, 10)   # 10 units each
)

    conn.commit()
    print(f"✅ {len(sample_listings)} sample listings inserted.")
    conn.close()

if __name__ == "__main__":
    run()