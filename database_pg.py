"""PostgreSQL adapter for Vercel + Supabase deployment."""
import os
from contextlib import contextmanager
from datetime import date

import psycopg2
import psycopg2.extras

_DATABASE_URL = os.environ["DATABASE_URL"]
# Supabase requires SSL; ensure it's in the URL
if "sslmode" not in _DATABASE_URL:
    sep = "&" if "?" in _DATABASE_URL else "?"
    _DATABASE_URL += f"{sep}sslmode=require"


class Database:
    @contextmanager
    def get_conn(self):
        conn = psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
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

    def has_data_for_date(self, date_str):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM market_cap WHERE date=%s", (date_str,))
                return cur.fetchone()["count"] > 0

    def insert_batch(self, records):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO market_cap
                    (date, stock_id, stock_name, close_price, market_cap, shares, industry)
                    VALUES %s
                    ON CONFLICT (date, stock_id) DO UPDATE SET
                        close_price = EXCLUDED.close_price,
                        market_cap  = EXCLUDED.market_cap,
                        shares      = EXCLUDED.shares
                """, records)

    def update_ranks(self, date_str):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE market_cap mc
                    SET market_cap_rank = sub.rn
                    FROM (
                        SELECT id,
                            ROW_NUMBER() OVER (ORDER BY market_cap DESC) AS rn
                        FROM market_cap
                        WHERE date = %s AND market_cap > 0
                    ) sub
                    WHERE mc.id = sub.id AND mc.date = %s
                """, (date_str, date_str))

    def upsert_stocks(self, shares_data):
        today = date.today().isoformat()
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO stocks (stock_id, stock_name, shares, updated)
                    VALUES %s
                    ON CONFLICT (stock_id) DO UPDATE SET
                        stock_name = EXCLUDED.stock_name,
                        shares     = EXCLUDED.shares,
                        updated    = EXCLUDED.updated
                """, [
                    (sid, info.get("name", sid), info.get("shares", 0), today)
                    for sid, info in shares_data.items()
                ])

    def get_stocks_missing_shares(self, stock_ids):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stock_id FROM stocks WHERE shares > 0")
                cached = {r["stock_id"] for r in cur.fetchall()}
        return [sid for sid in stock_ids if sid not in cached]

    def get_all_shares(self):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stock_id, shares FROM stocks")
                return {r["stock_id"]: r["shares"] for r in cur.fetchall()}

    def get_dates(self):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT date FROM market_cap ORDER BY date DESC LIMIT 90"
                )
                return [r["date"] for r in cur.fetchall()]

    def get_data_for_date(self, date_str):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        t.stock_id,
                        t.stock_name,
                        t.close_price,
                        t.market_cap,
                        t.market_cap_rank,
                        t.industry,
                        COALESCE(p.market_cap,  t.market_cap)  AS prev_cap,
                        COALESCE(p.close_price, t.close_price) AS prev_close,
                        (t.market_cap - COALESCE(p.market_cap, t.market_cap)) AS cap_change,
                        CASE
                            WHEN COALESCE(p.market_cap, 0) > 0
                            THEN ROUND(CAST((t.market_cap - p.market_cap)*100.0/p.market_cap AS numeric), 2)
                            ELSE 0
                        END AS cap_change_pct,
                        (t.close_price - COALESCE(p.close_price, t.close_price)) AS price_change
                    FROM market_cap t
                    LEFT JOIN market_cap p ON p.stock_id = t.stock_id
                        AND p.date = (
                            SELECT MAX(date) FROM market_cap
                            WHERE date < %s AND stock_id = t.stock_id
                        )
                    WHERE t.date = %s AND t.market_cap > 0
                    ORDER BY t.market_cap DESC
                """, (date_str, date_str))
                return [dict(r) for r in cur.fetchall()]

    def get_stock_history(self, stock_id, days=60):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT date, stock_name, close_price, market_cap, market_cap_rank
                    FROM market_cap
                    WHERE stock_id = %s
                    ORDER BY date DESC LIMIT %s
                """, (stock_id, days))
                rows = cur.fetchall()
                return [dict(r) for r in reversed(rows)]

    def get_all_cap_history(self, days=120):
        """Return last N days of market cap for all stocks (for MA screener)."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT stock_id, stock_name, date, market_cap
                    FROM (
                        SELECT stock_id, stock_name, date, market_cap,
                               ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date DESC) AS rn
                        FROM market_cap
                        WHERE market_cap > 0
                    ) t
                    WHERE rn <= %s
                    ORDER BY stock_id, date DESC
                """, (days,))
                return [dict(r) for r in cur.fetchall()]

    def get_trillion_history(self):
        """Return all records where market_cap >= 5000億, ordered by date asc."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT date, stock_id, stock_name, market_cap, market_cap_rank
                    FROM market_cap
                    WHERE market_cap >= 50000000000
                    ORDER BY date ASC, market_cap_rank ASC
                """)
                return [dict(r) for r in cur.fetchall()]

    def get_summary(self, date_str):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(t.market_cap) AS total_market_cap,
                        SUM(CASE WHEN (t.market_cap - COALESCE(p.market_cap, t.market_cap)) > 0 THEN 1 ELSE 0 END) AS up_count,
                        SUM(CASE WHEN (t.market_cap - COALESCE(p.market_cap, t.market_cap)) < 0 THEN 1 ELSE 0 END) AS down_count,
                        SUM(CASE WHEN (t.market_cap - COALESCE(p.market_cap, t.market_cap)) = 0 THEN 1 ELSE 0 END) AS flat_count
                    FROM market_cap t
                    LEFT JOIN market_cap p ON p.stock_id = t.stock_id
                        AND p.date = (
                            SELECT MAX(date) FROM market_cap
                            WHERE date < %s AND stock_id = t.stock_id
                        )
                    WHERE t.date = %s AND t.market_cap > 0
                """, (date_str, date_str))
                row = cur.fetchone()
                return dict(row) if row else {}
