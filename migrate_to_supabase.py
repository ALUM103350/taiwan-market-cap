"""
One-time migration: local SQLite → Supabase PostgreSQL.
Usage:
    set DATABASE_URL=postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres
    python migrate_to_supabase.py
"""
import os
import sqlite3
import psycopg2
import psycopg2.extras
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("Set DATABASE_URL first.\n"
                     "  set DATABASE_URL=postgresql://postgres:PASSWORD@HOST:5432/postgres")

SQLITE_PATH = "market_cap.db"
if not os.path.exists(SQLITE_PATH):
    raise SystemExit(f"{SQLITE_PATH} not found — run from the project directory")

# ── Connect to both ────────────────────────────────────────────────────────────
sqlite = sqlite3.connect(SQLITE_PATH)
sqlite.row_factory = sqlite3.Row
pg = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
pg.autocommit = False

# ── Init PG tables ─────────────────────────────────────────────────────────────
with pg.cursor() as cur:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stocks (
            stock_id TEXT PRIMARY KEY,
            stock_name TEXT DEFAULT '',
            shares REAL DEFAULT 0,
            updated TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_cap (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            close_price REAL DEFAULT 0,
            market_cap REAL DEFAULT 0,
            market_cap_rank INTEGER DEFAULT 0,
            shares REAL DEFAULT 0,
            industry TEXT DEFAULT '',
            UNIQUE(date, stock_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_date  ON market_cap(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock ON market_cap(stock_id)")
pg.commit()
logging.info("PG tables ready")

# ── Migrate stocks ─────────────────────────────────────────────────────────────
stocks = sqlite.execute("SELECT * FROM stocks").fetchall()
if stocks:
    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO stocks (stock_id, stock_name, shares, updated)
            VALUES %s
            ON CONFLICT (stock_id) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                shares     = EXCLUDED.shares,
                updated    = EXCLUDED.updated
        """, [(r["stock_id"], r["stock_name"], r["shares"], r["updated"]) for r in stocks])
    pg.commit()
    logging.info(f"Migrated {len(stocks)} stocks")

# ── Migrate market_cap (in batches by date) ────────────────────────────────────
dates = [r[0] for r in sqlite.execute(
    "SELECT DISTINCT date FROM market_cap ORDER BY date"
).fetchall()]

for day in dates:
    rows = sqlite.execute(
        "SELECT * FROM market_cap WHERE date=?", (day,)
    ).fetchall()
    records = [
        (r["date"], r["stock_id"], r["stock_name"], r["close_price"],
         r["market_cap"], r["market_cap_rank"], r["shares"], r["industry"])
        for r in rows
    ]
    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO market_cap
            (date, stock_id, stock_name, close_price, market_cap, market_cap_rank, shares, industry)
            VALUES %s
            ON CONFLICT (date, stock_id) DO NOTHING
        """, records)
    pg.commit()
    logging.info(f"  {day}: {len(records)} records")

logging.info("Migration complete!")
sqlite.close()
pg.close()
