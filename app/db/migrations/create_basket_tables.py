import os
from app.db.database import database

async def create_basket_tables():
    # 1. Baskets table
    await database.execute("""
        CREATE TABLE IF NOT EXISTS baskets (
            basket_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # 2. Basket Items table
    await database.execute("""
        CREATE TABLE IF NOT EXISTS basket_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            price REAL NOT NULL,
            added_at TEXT NOT NULL,
            FOREIGN KEY (basket_id) REFERENCES baskets (basket_id)
        )
    """)

    # 3. Orders table (parent order)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        )
    """)

    # 4. Order Items table
    await database.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            subtotal REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (order_id)
        )
    """)

    # 5. Order Stores (sub-orders per store)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS order_stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            subtotal REAL NOT NULL,
            delivery_fee REAL DEFAULT 0,
            fulfillment_type TEXT DEFAULT 'pickup',
            courier_id TEXT NULL,
            status TEXT DEFAULT 'pending',
            escrow_id TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (order_id)
        )
    """)

    print("✅ Basket tables created")