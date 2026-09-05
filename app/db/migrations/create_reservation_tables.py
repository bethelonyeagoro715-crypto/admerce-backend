from app.db.database import database

async def create_reservation_tables():
    # 1. Baskets (now called reservations_list)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS baskets (
            basket_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # 2. Basket items
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

    # 3. Orders (parent order)
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

    # 4. Order items
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

    # 5. Order stores (sub-orders per store)
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

    # 6. Add listing_id and quantity to escrow if not exists (already added earlier, but ensure)
    # We'll add them if they don't exist – we assume they were added in a previous migration.
    # For safety, we can check existence.
    cols = await database.fetch_all("PRAGMA table_info(escrow)")
    existing_cols = [c['name'] for c in cols]
    if 'listing_id' not in existing_cols:
        await database.execute("ALTER TABLE escrow ADD COLUMN listing_id TEXT")
    if 'quantity' not in existing_cols:
        await database.execute("ALTER TABLE escrow ADD COLUMN quantity INTEGER DEFAULT 1")
    if 'order_store_id' not in existing_cols:
        await database.execute("ALTER TABLE escrow ADD COLUMN order_store_id INTEGER")

    print("✅ Reservation tables created/updated")