import logging
import os
import threading
from datetime import date

from flask import Flask, jsonify, render_template, request

# Auto-select database backend based on environment
if os.environ.get("DATABASE_URL"):
    from database_pg import Database
else:
    from database import Database

import fetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
db = Database()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dates")
def api_dates():
    return jsonify(db.get_dates())


@app.route("/api/market-cap")
def api_market_cap():
    date_str = request.args.get("date")
    if not date_str:
        dates = db.get_dates()
        if not dates:
            return jsonify({"error": "no data — click 更新資料 to fetch"}), 404
        date_str = dates[0]

    rows = db.get_data_for_date(date_str)
    summary = db.get_summary(date_str)
    return jsonify({"date": date_str, "summary": summary, "data": rows})


@app.route("/api/stock/<stock_id>/history")
def api_stock_history(stock_id):
    days = request.args.get("days", 60, type=int)
    return jsonify(db.get_stock_history(stock_id, days))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    # On Vercel (serverless), background threads are not supported.
    # Data is updated daily by GitHub Actions.
    if os.environ.get("DATABASE_URL"):
        return jsonify({
            "message": "資料由 GitHub Actions 每天自動更新（台灣時間下午 5:30）。"
                       "如需立即更新，請至 GitHub → Actions → Run workflow。"
        })

    body = request.json or {}
    days = body.get("days", 30)

    def _run():
        count = fetcher.fetch_and_store(db, days_history=days)
        logger.info(f"Refresh complete: {count} new records")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"message": f"Fetching {days} days of data in background…"})


# ── Startup ───────────────────────────────────────────────────────────────────

def bootstrap():
    db.init_db()
    dates = db.get_dates()
    today = date.today().isoformat()

    if not dates:
        logger.info("Database empty — starting initial fetch (may take 2-3 min)…")
        fetcher.fetch_and_store(db, days_history=30)
    elif today not in dates:
        logger.info("Fetching today's prices…")
        fetcher.fetch_today(db)
    else:
        logger.info(f"Data already available ({len(dates)} dates in DB)")


if __name__ == "__main__":
    bootstrap()
    app.run(debug=False, port=5000, use_reloader=False)
