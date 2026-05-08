"""Entry point for GitHub Actions daily data fetch."""
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if not os.environ.get("DATABASE_URL"):
    raise SystemExit("DATABASE_URL is not set")

from database_pg import Database
import fetcher

db = Database()
db.init_db()

dates = db.get_dates()
if not dates:
    # First run — full 30-day history
    logging.info("Empty DB detected, running full 30-day fetch…")
    count = fetcher.fetch_and_store(db, days_history=30)
else:
    count = fetcher.fetch_today(db)

logging.info(f"Done — {count} new records written")
