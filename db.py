import sqlite3

DB_NAME = "amazon_planner.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def _col_exists(cur, table: str, col: str) -> bool:
    cols = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == col for c in cols)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # --- USERS table: create if missing (new schema) ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        identifier TEXT NOT NULL UNIQUE,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # --- MIGRATION: if old users table exists without 'identifier' ---
    # Some older versions used 'email' column.
    if not _col_exists(cur, "users", "identifier"):
        # Add identifier column
        cur.execute("ALTER TABLE users ADD COLUMN identifier TEXT;")

        # If old column 'email' exists, copy it into identifier
        if _col_exists(cur, "users", "email"):
            cur.execute("UPDATE users SET identifier = lower(trim(email)) WHERE identifier IS NULL;")
        else:
            # Fallback: fill missing identifier with id
            cur.execute("UPDATE users SET identifier = 'user_' || id WHERE identifier IS NULL;")

        # Make sure no NULL left
        cur.execute("UPDATE users SET identifier = 'user_' || id WHERE identifier IS NULL;")

        # Create unique index (SQLite can't easily add UNIQUE constraint after ALTER)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_identifier ON users(identifier);")

    # --- PRODUCTS ---
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

    # --- MONTHLY SALES ---
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
