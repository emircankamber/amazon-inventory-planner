import sqlite3

DB_NAME = "amazon_planner.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        hashed_password TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Products (owner-specific SKU)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        sku TEXT NOT NULL,
        name TEXT,
        lead_time_days INTEGER NOT NULL,
        z_value REAL NOT NULL,
        fba_stock INTEGER NOT NULL DEFAULT 0,
        inbound_stock INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_id, sku),
        FOREIGN KEY(owner_id) REFERENCES users(id)
    );
    """)

    # Monthly sales
    cur.execute("""
    CREATE TABLE IF NOT EXISTS monthly_sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        sku TEXT NOT NULL,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
        units_sold INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_id, sku, year, month),
        FOREIGN KEY(owner_id) REFERENCES users(id)
    );
    """)

    conn.commit()
    conn.close()
