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


TIER_TRILLION  = 1_000_000_000_000   # 1兆
TIER_500B      =   500_000_000_000   # 5000億
TIER_100B      =   100_000_000_000   # 1000億
TIER_50B       =    50_000_000_000   # 500億

def _tier_label(cap):
    if cap >= TIER_TRILLION:
        return "兆"
    if cap >= TIER_500B:
        return "五千億"
    if cap >= TIER_100B:
        return "千億"
    return "五百億"

@app.route("/api/screener/ma")
def screener_ma():
    """Stocks where market cap 3MA > 10MA > 30MA > 60MA."""
    from collections import defaultdict
    rows = db.get_all_cap_history(days=120)
    if not rows:
        return jsonify([])

    by_stock = defaultdict(list)
    for r in rows:
        by_stock[r["stock_id"]].append(r)

    def avg(lst, n):
        sub = [x for x in lst[:n] if x > 0]
        return sum(sub) / len(sub) if sub else 0

    results = []
    for sid, history in by_stock.items():
        caps = [h["market_cap"] for h in history]
        if len(caps) < 10:
            continue
        ma3   = avg(caps, 3)
        ma10  = avg(caps, 10)
        ma20  = avg(caps, 20)
        ma30  = avg(caps, 30)
        ma40  = avg(caps, 40)
        ma50  = avg(caps, 50)
        ma60  = avg(caps, 60)
        ma120 = avg(caps, 120)
        if ma3 > ma10 > ma30 > ma60 > 0:
            latest = history[0]
            cap = latest["market_cap"]
            results.append({
                "stock_id":   sid,
                "stock_name": latest["stock_name"],
                "market_cap": cap,
                "tier":       _tier_label(cap),
                "ma3":        round(ma3   / 1e8, 1),
                "ma10":       round(ma10  / 1e8, 1),
                "ma20":       round(ma20  / 1e8, 1),
                "ma30":       round(ma30  / 1e8, 1),
                "ma40":       round(ma40  / 1e8, 1),
                "ma50":       round(ma50  / 1e8, 1),
                "ma60":       round(ma60  / 1e8, 1),
                "ma120":      round(ma120 / 1e8, 1),
                "cap_yi":     round(cap   / 1e8, 1),
                "days_used":  len(caps),
            })

    results.sort(key=lambda x: x["market_cap"], reverse=True)
    return jsonify(results)


@app.route("/api/alerts")
def api_alerts():
    """偵測兆級（>=1兆）及五千億級（5000億~1兆）公司歷史排名交叉事件。"""
    rows = db.get_trillion_history()
    if not rows:
        return jsonify([])

    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], {})[r["stock_id"]] = r

    sorted_dates = sorted(by_date.keys())
    all_alerts = []

    for i in range(1, len(sorted_dates)):
        today_str = sorted_dates[i]
        yest_str  = sorted_dates[i - 1]
        today_map = by_date[today_str]
        yest_map  = by_date[yest_str]

        common = list(set(today_map) & set(yest_map))
        for j in range(len(common)):
            for k in range(j + 1, len(common)):
                a, b = common[j], common[k]
                cap_a = today_map[a]["market_cap"]
                cap_b = today_map[b]["market_cap"]

                # Only compare within the same tier
                if _tier_label(cap_a) != _tier_label(cap_b):
                    continue

                ra_t = today_map[a]["market_cap_rank"]
                rb_t = today_map[b]["market_cap_rank"]
                ra_y = yest_map[a]["market_cap_rank"]
                rb_y = yest_map[b]["market_cap_rank"]

                if (ra_t < rb_t) != (ra_y < rb_y):
                    winner, loser = (a, b) if ra_t < rb_t else (b, a)
                    all_alerts.append({
                        "date":        today_str,
                        "tier":        _tier_label(today_map[winner]["market_cap"]),
                        "winner_id":   winner,
                        "winner_name": today_map[winner]["stock_name"],
                        "winner_cap":  today_map[winner]["market_cap"],
                        "winner_rank": today_map[winner]["market_cap_rank"],
                        "loser_id":    loser,
                        "loser_name":  today_map[loser]["stock_name"],
                        "loser_cap":   today_map[loser]["market_cap"],
                        "loser_rank":  today_map[loser]["market_cap_rank"],
                    })

    all_alerts.sort(key=lambda x: (x["date"], x["winner_rank"]), reverse=True)
    return jsonify(all_alerts)


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
