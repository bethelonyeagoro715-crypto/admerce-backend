import sqlite3
import random
import datetime

# Categories for title generation
CATEGORIES = [
    ("Fashion", ["Vintage Denim Jacket", "Summer Floral Dress", "Leather Boots", "Silk Scarf", "Wool Sweater", "Cargo Pants", "Graphic T‑Shirt", "Plaid Skirt", "Denim Shorts", "Puffer Coat"]),
    ("Electronics", ["Wireless Earbuds", "Bluetooth Speaker", "USB‑C Hub", "Phone Charger", "Webcam HD", "Mechanical Keyboard", "Gaming Mouse", "Smart Watch", "Tablet Stand", "Portable Monitor"]),
    ("Groceries", ["Indomie Noodles Carton", "Basmati Rice 5kg", "Vegetable Oil 3L", "Sugar 2kg", "Flour 5kg", "Pasta Pack", "Cooking Spices Set", "Canned Tomatoes", "Frozen Chicken", "Bread Loaf"]),
    ("Eateries", ["Jollof Rice Plate", "Grilled Chicken Wings", "Shawarma Wrap", "Burger Combo", "Pizza Slice", "Fried Yam & Sauce", "Suya Skewers", "Pounded Yam & Egusi", "Smoothie Bowl", "Sandwich"]),
    ("Services", ["Phone Screen Repair", "Laptop Cleanup", "Home Cleaning", "Math Tutoring", "Makeup Service", "Hair Styling", "Plumbing Fix", "AC Maintenance", "Car Wash", "Electrician"]),
    ("Provisions", ["Cement Bag 50kg", "Roofing Sheets Pack", "Door Lock Set", "Electric Cable Roll", "Light Bulbs Pack", "Plumbing Pipe 3m", "Tile Adhesive", "Paint Bucket", "Nails & Screws Kit", "Sandpaper Roll"]),
]

def run():
    conn = sqlite3.connect("seai.db")
    cursor = conn.cursor()

    # Clear existing listings
    cursor.execute("DELETE FROM listings")
    print("🗑️  Old listings cleared.")

    now = datetime.datetime.utcnow()
    store_ids = [f"store_{i}" for i in range(1, 6)]  # 5 fake stores

    total = 0
    for cat_name, titles in CATEGORIES:
        for _ in range(50):  # 50 items per category = 300 total
            title = random.choice(titles)
            price = round(random.uniform(500, 50000), 2)
            lat = 6.5244 + random.uniform(-0.03, 0.03)   # Yaba area
            lng = 3.3792 + random.uniform(-0.03, 0.03)
            store_id = random.choice(store_ids)
            created_at = now - datetime.timedelta(minutes=random.randint(1, 1440))
            title_quality = round(random.uniform(0.2, 0.95), 2)
            quantity = random.randint(1, 50)

            cursor.execute(
                """INSERT INTO listings
                   (listing_id, store_id, title, price, lat, lng, created_at, title_quality, quantity_total, quantity_available)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"seed_{total}",
                    store_id,
                    title,
                    price,
                    lat,
                    lng,
                    created_at.isoformat(),
                    title_quality,
                    quantity,
                    quantity,
                )
            )
            total += 1

    conn.commit()
    conn.close()
    print(f"✅ {total} listings inserted.")

if __name__ == "__main__":
    run()