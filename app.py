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

def _build_periods(sorted_dates):
    """Group sorted trading dates into non-overlapping 3-day periods.
    Returns list of (period_end_date, [d1, d2, d3]).
    """
    periods = []
    for i in range(0, len(sorted_dates) - 2, 3):
        group = sorted_dates[i: i + 3]
        if len(group) == 3:
            periods.append((group[-1], group))
    return periods


def _compute_ma_screener(all_rows, up_to_period_idx, periods):
    """Compute MA screener using 3-day period averages.
    Returns list of stocks satisfying 3MA > 10MA > 30MA > 60MA.
    """
    from collections import defaultdict

    # Build {stock_id: {date: market_cap}}
    cap_map = defaultdict(dict)
    names   = {}
    for r in all_rows:
        cap_map[r["stock_id"]][r["date"]] = r["market_cap"]
        names[r["stock_id"]] = r["stock_name"]

    def pavg(lst, n):
        sub = [x for x in lst[-n:] if x > 0]
        return sum(sub) / len(sub) if sub else 0

    results = []
    for sid, date_caps in cap_map.items():
        # Build period-value series up to up_to_period_idx (inclusive)
        period_vals = []
        for _, dates in periods[: up_to_period_idx + 1]:
            vals = [date_caps[d] for d in dates if d in date_caps and date_caps[d] > 0]
            if vals:
                period_vals.append(sum(vals) / len(vals))

        if len(period_vals) < 3:
            continue

        ma3   = pavg(period_vals, 3)
        ma10  = pavg(period_vals, 10)
        ma20  = pavg(period_vals, 20)
        ma30  = pavg(period_vals, 30)
        ma40  = pavg(period_vals, 40)
        ma50  = pavg(period_vals, 50)
        ma60  = pavg(period_vals, 60)
        ma120 = pavg(period_vals, 120)

        if not (ma3 > ma10 > ma30 > ma60 > 0):
            continue

        # Use last day of target period as representative cap
        _, target_dates = periods[up_to_period_idx]
        last_caps = [date_caps[d] for d in reversed(target_dates) if d in date_caps and date_caps[d] > 0]
        cap = last_caps[0] if last_caps else period_vals[-1]

        results.append({
            "stock_id":    sid,
            "stock_name":  names.get(sid, sid),
            "market_cap":  cap,
            "tier":        _tier_label(cap),
            "cap_yi":      round(cap    / 1e8, 1),
            "ma3":         round(ma3    / 1e8, 1),
            "ma10":        round(ma10   / 1e8, 1),
            "ma20":        round(ma20   / 1e8, 1),
            "ma30":        round(ma30   / 1e8, 1),
            "ma40":        round(ma40   / 1e8, 1),
            "ma50":        round(ma50   / 1e8, 1),
            "ma60":        round(ma60   / 1e8, 1),
            "ma120":       round(ma120  / 1e8, 1),
            "num_periods": len(period_vals),
        })

    results.sort(key=lambda x: x["market_cap"], reverse=True)
    return results


# Cache to avoid recomputing on every request
_screener_cache = {}

@app.route("/api/screener/ma/periods")
def screener_periods():
    """Return available 3-day period end dates (desc)."""
    rows = db.get_all_cap_history(days=360)
    if not rows:
        return jsonify([])
    dates_asc = sorted(set(r["date"] for r in rows))
    periods = _build_periods(dates_asc)
    return jsonify([p[0] for p in reversed(periods)])


@app.route("/api/screener/ma")
def screener_ma():
    """Screener for a specific period (or latest). Uses 3-day period MAs."""
    target = request.args.get("period")
    rows = db.get_all_cap_history(days=360)
    if not rows:
        return jsonify([])

    dates_asc = sorted(set(r["date"] for r in rows))
    periods   = _build_periods(dates_asc)
    if not periods:
        return jsonify([])

    # Find target period index
    if target:
        idx = next((i for i, (end, _) in enumerate(periods) if end == target), None)
        if idx is None:
            return jsonify({"error": "period not found"}), 404
    else:
        idx = len(periods) - 1   # latest

    cache_key = f"ma_{idx}"
    if cache_key not in _screener_cache:
        _screener_cache[cache_key] = _compute_ma_screener(rows, idx, periods)

    period_end, period_dates = periods[idx]
    return jsonify({
        "period_end":   period_end,
        "period_dates": period_dates,
        "period_index": idx,
        "total_periods": len(periods),
        "data": _screener_cache[cache_key],
    })


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
