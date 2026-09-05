import sqlite3
import psycopg2
from sqlalchemy import create_engine, text
from app.db.database import Base

from app.db import user_models, listing_models, wallet_models, service_models
from app.db import flipper_models, models, courier_models

PG_DSN = "postgresql://Admerce2026:Bethel2026%40@127.0.0.1:5432/admerce_db"

sync_engine = create_engine(PG_DSN)

print("🗑️  Dropping problematic tables...")
with sync_engine.connect() as conn:
    conn.execute(text("DROP TABLE IF EXISTS listings CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS services CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS user_settings CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS app_settings CASCADE"))
    conn.commit()
print("✅ Dropped.")

print("🔧 Creating all tables with correct schema...")
Base.metadata.create_all(bind=sync_engine)
print("✅ Tables created.")

print("🔧 Adding missing columns/tables...")
with sync_engine.connect() as conn:
    conn.execute(text("ALTER TABLE listings ADD COLUMN IF NOT EXISTS quantity_available INTEGER"))
    conn.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS is_active BOOLEAN"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT,
            key TEXT,
            value TEXT,
            role TEXT
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT,
            value TEXT,
            updated_at TEXT
        )
    """))
    conn.commit()
print("✅ Schema fixed.")

SQLITE_PATH = "seai.db"

def copy_data():
    print("🔌 Connecting to SQLite...")
    sq = sqlite3.connect(SQLITE_PATH)
    cur = sq.cursor()

    print("🔌 Connecting to PostgreSQL...")
    pg = psycopg2.connect(PG_DSN)
    pg.autocommit = False

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [r[0] for r in cur.fetchall()]

    for table in tables:
        print(f"📦 Copying {table}...")

        cur.execute(f"PRAGMA table_info('{table}')")
        cols_info = cur.fetchall()
        columns = [c[1] for c in cols_info]

        date_cols = {c[1] for c in cols_info if c[1].endswith('_at') or c[1].lower() in ('created_at', 'updated_at', 'expires_at')}
        text_value_cols = {'value'} if table in ('user_settings', 'app_settings') else set()
        bool_cols = {c[1] for c in cols_info if "BOOL" in (c[2] or "").upper() and c[1] not in text_value_cols}

        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        if not rows:
            print(f"   ⏭️  {table} is empty.")
            continue

        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

        ok = 0
        pg_cursor = pg.cursor()
        for row in rows:
            coerced = []
            for i, v in enumerate(row):
                col = columns[i]
                if col in date_cols and isinstance(v, str):
                    coerced.append(v)
                elif col in text_value_cols and isinstance(v, bool):
                    coerced.append(str(v))
                elif col in bool_cols and isinstance(v, int):
                    coerced.append(bool(v))
                elif isinstance(v, float) and v.is_integer():
                    coerced.append(int(v))
                elif isinstance(v, str) and v.lower() in ('true', 'false'):
                    coerced.append(v.lower() == 'true')
                else:
                    coerced.append(v)
            try:
                pg_cursor.execute(insert_sql, coerced)
                ok += 1
            except Exception as e:
                print(f"   ⚠️  Row error in {table}: {e}")
                if ok == 0:
                    print(f"       First row data: {dict(zip(columns, row))}")
        pg.commit()
        pg_cursor.close()

        print(f"   ✅ Inserted {ok} rows into {table}.")

    pg.close()
    sq.close()
    print("🎉 Migration complete!")

if __name__ == "__main__":
    copy_data()
