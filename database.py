import sqlite3
from contextlib import contextmanager
from datetime import date


class Database:
    def __init__(self, db_path="market_cap.db"):
        self.db_path = db_path

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self):
        with self.get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS stocks (
                    stock_id TEXT PRIMARY KEY,
                    stock_name TEXT DEFAULT '',
                    shares REAL DEFAULT 0,
                    updated TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS market_cap (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    stock_id TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    close_price REAL DEFAULT 0,
                    market_cap REAL DEFAULT 0,
                    market_cap_rank INTEGER DEFAULT 0,
                    shares REAL DEFAULT 0,
                    industry TEXT DEFAULT '',
                    UNIQUE(date, stock_id)
                );
                CREATE INDEX IF NOT EXISTS idx_date  ON market_cap(date);
                CREATE INDEX IF NOT EXISTS idx_stock ON market_cap(stock_id);
            """)

    # ── Stocks (shares cache) ──────────────────────────────────────────────────

    def upsert_stocks(self, shares_data):
        """shares_data: {stock_id: {shares, name}}"""
        today = date.today().isoformat()
        with self.get_conn() as conn:
            conn.executemany("""
                INSERT INTO stocks (stock_id, stock_name, shares, updated)
                VALUES (?,?,?,?)
                ON CONFLICT(stock_id) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    shares = excluded.shares,
                    updated = excluded.updated
            """, [
                (sid, info.get("name", sid), info.get("shares", 0), today)
                for sid, info in shares_data.items()
            ])

    def get_stocks_missing_shares(self, stock_ids):
        """Return stock_ids that aren't cached in stocks table."""
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT stock_id FROM stocks WHERE shares > 0"
            )
            cached = {r[0] for r in cur.fetchall()}
        return [sid for sid in stock_ids if sid not in cached]

    def get_all_shares(self):
        """Return {stock_id: shares}"""
        with self.get_conn() as conn:
            cur = conn.execute("SELECT stock_id, shares FROM stocks")
            return {r[0]: r[1] for r in cur.fetchall()}

    # ── Market cap data ────────────────────────────────────────────────────────

    def has_data_for_date(self, date_str):
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM market_cap WHERE date=?", (date_str,)
            )
            return cur.fetchone()[0] > 0

    def insert_batch(self, records):
        with self.get_conn() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO market_cap
                (date, stock_id, stock_name, close_price, market_cap, shares, industry)
                VALUES (?,?,?,?,?,?,?)
            """, records)

    def update_ranks(self, date_str):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE market_cap SET market_cap_rank = (
                    SELECT COUNT(*) + 1 FROM market_cap m2
                    WHERE m2.date = market_cap.date
                      AND m2.market_cap > market_cap.market_cap
                )
                WHERE date = ? AND market_cap > 0
            """, (date_str,))

    def get_dates(self):
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT DISTINCT date FROM market_cap ORDER BY date DESC LIMIT 90"
            )
            return [r[0] for r in cur.fetchall()]

    def get_data_for_date(self, date_str):
        with self.get_conn() as conn:
            cur = conn.execute("""
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
                        THEN ROUND((t.market_cap - p.market_cap)*100.0/p.market_cap, 2)
                        ELSE 0
                    END AS cap_change_pct,
                    (t.close_price - COALESCE(p.close_price, t.close_price)) AS price_change
                FROM market_cap t
                LEFT JOIN market_cap p ON p.stock_id = t.stock_id
                    AND p.date = (
                        SELECT MAX(date) FROM market_cap
                        WHERE date < ? AND stock_id = t.stock_id
                    )
                WHERE t.date = ? AND t.market_cap > 0
                ORDER BY t.market_cap DESC
            """, (date_str, date_str))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_stock_history(self, stock_id, days=60):
        with self.get_conn() as conn:
            cur = conn.execute("""
                SELECT date, stock_name, close_price, market_cap, market_cap_rank
                FROM market_cap
                WHERE stock_id = ?
                ORDER BY date DESC LIMIT ?
            """, (stock_id, days))
            rows = cur.fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_all_cap_history(self, days=120):
        with self.get_conn() as conn:
            cur = conn.execute("""
                SELECT stock_id, stock_name, date, market_cap
                FROM (
                    SELECT stock_id, stock_name, date, market_cap,
                           ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date DESC) AS rn
                    FROM market_cap
                    WHERE market_cap > 0
                ) t
                WHERE rn <= ?
                ORDER BY stock_id, date DESC
            """, (days,))
            return [dict(r) for r in cur.fetchall()]

    def get_trillion_history(self):
        with self.get_conn() as conn:
            cur = conn.execute("""
                SELECT date, stock_id, stock_name, market_cap, market_cap_rank
                FROM market_cap
                WHERE market_cap >= 10000000000
                ORDER BY date ASC, market_cap_rank ASC
            """)
            return [dict(r) for r in cur.fetchall()]

    def get_tier_growth(self, tier, limit=50, days=60):
        tier_floor = {
            "兆":    1_000_000_000_000,
            "五千億":   500_000_000_000,
            "三千億":   300_000_000_000,
            "二千億":   200_000_000_000,
            "千億":     100_000_000_000,
            "五百億":    50_000_000_000,
            "二百億":    20_000_000_000,
            "百億":      10_000_000_000,
        }
        lo = tier_floor.get(tier, 10_000_000_000)
        with self.get_conn() as conn:
            cur = conn.execute("SELECT MAX(date) FROM market_cap WHERE market_cap > 0")
            latest = cur.fetchone()[0]
            if not latest:
                return {"tier": tier, "latest_date": "", "labels": [], "datasets": []}

            cur = conn.execute("""
                SELECT stock_id, stock_name, market_cap FROM market_cap
                WHERE date=? AND market_cap>=?
                ORDER BY market_cap DESC LIMIT ?
            """, (latest, lo, limit))
            top_stocks = [dict(r) for r in cur.fetchall()]
            if not top_stocks:
                return {"tier": tier, "latest_date": latest, "labels": [], "datasets": []}

            stock_ids = [s["stock_id"] for s in top_stocks]
            ph = ",".join("?" * len(stock_ids))
            cur = conn.execute(f"""
                SELECT stock_id, date, market_cap
                FROM (
                    SELECT stock_id, date, market_cap,
                           ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date DESC) AS rn
                    FROM market_cap WHERE stock_id IN ({ph}) AND market_cap > 0
                ) t WHERE rn <= ?
                ORDER BY date ASC
            """, (*stock_ids, days))
            hist_rows = cur.fetchall()
            labels = sorted(set(r["date"] for r in hist_rows))
            by_stock = {}
            for r in hist_rows:
                by_stock.setdefault(r["stock_id"], {})[r["date"]] = r["market_cap"]

            datasets = []
            for s in top_stocks:
                sid = s["stock_id"]
                hist = by_stock.get(sid, {})
                datasets.append({
                    "stock_id":   sid,
                    "stock_name": s["stock_name"],
                    "market_cap": s["market_cap"],
                    "data": [hist[d] / 1e8 if d in hist else None for d in labels],
                })
            return {"tier": tier, "latest_date": latest, "labels": labels, "datasets": datasets}

    def get_summary(self, date_str):
        with self.get_conn() as conn:
            cur = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(t.market_cap) as total_market_cap,
                    SUM(CASE WHEN (t.market_cap - COALESCE(p.market_cap, t.market_cap)) > 0 THEN 1 ELSE 0 END) as up_count,
                    SUM(CASE WHEN (t.market_cap - COALESCE(p.market_cap, t.market_cap)) < 0 THEN 1 ELSE 0 END) as down_count,
                    SUM(CASE WHEN (t.market_cap - COALESCE(p.market_cap, t.market_cap)) = 0 THEN 1 ELSE 0 END) as flat_count
                FROM market_cap t
                LEFT JOIN market_cap p ON p.stock_id = t.stock_id
                    AND p.date = (
                        SELECT MAX(date) FROM market_cap
                        WHERE date < ? AND stock_id = t.stock_id
                    )
                WHERE t.date = ? AND t.market_cap > 0
            """, (date_str, date_str))
            row = cur.fetchone()
            return dict(row) if row else {}
