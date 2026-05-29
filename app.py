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


TIER_TRILLION  = 1_000_000_000_000   # 1兆   = 10,000億
TIER_5000B     =   500_000_000_000   # 5,000億
TIER_3000B     =   300_000_000_000   # 3,000億
TIER_2000B     =   200_000_000_000   # 2,000億
TIER_1000B     =   100_000_000_000   # 1,000億
TIER_500B      =    50_000_000_000   # 500億
TIER_200B      =    20_000_000_000   # 200億
TIER_100B      =    10_000_000_000   # 100億

def _tier_label(cap):
    if cap >= TIER_TRILLION: return "兆"
    if cap >= TIER_5000B:    return "五千億"
    if cap >= TIER_3000B:    return "三千億"
    if cap >= TIER_2000B:    return "二千億"
    if cap >= TIER_1000B:    return "千億"
    if cap >= TIER_500B:     return "五百億"
    if cap >= TIER_200B:     return "二百億"
    return "百億"

def _snapshot_dates(all_dates_asc):
    """Every 3rd trading date becomes a snapshot date. Returns list asc."""
    return [all_dates_asc[i] for i in range(2, len(all_dates_asc), 3)]


def _compute_daily_ma_screener(all_rows, snapshot_date):
    """
    Compute MA screener using DAILY market cap data up to snapshot_date.
    MAs are calculated on raw daily values (not period averages).
    """
    from collections import defaultdict

    cap_map = defaultdict(dict)
    names   = {}
    for r in all_rows:
        if r["date"] <= snapshot_date:
            cap_map[r["stock_id"]][r["date"]] = r["market_cap"]
            names[r["stock_id"]] = r["stock_name"]

    def davg(caps_desc, n):
        sub = [x for x in caps_desc[:n] if x > 0]
        return sum(sub) / len(sub) if sub else 0

    results = []
    for sid, date_caps in cap_map.items():
        caps = [v for _, v in sorted(date_caps.items(), reverse=True)]
        if len(caps) < 10:
            continue

        ma3   = davg(caps, 3)
        ma10  = davg(caps, 10)
        ma20  = davg(caps, 20)
        ma30  = davg(caps, 30)
        ma40  = davg(caps, 40)
        ma50  = davg(caps, 50)
        ma60  = davg(caps, 60)
        ma120 = davg(caps, 120)

        if not (ma3 > ma10 > ma30 > ma60 > 0):
            continue

        cap = caps[0]
        results.append({
            "stock_id":   sid,
            "stock_name": names.get(sid, sid),
            "market_cap": cap,
            "tier":       _tier_label(cap),
            "cap_yi":     round(cap    / 1e8, 1),
            "ma3":        round(ma3    / 1e8, 1),
            "ma10":       round(ma10   / 1e8, 1),
            "ma20":       round(ma20   / 1e8, 1),
            "ma30":       round(ma30   / 1e8, 1),
            "ma40":       round(ma40   / 1e8, 1),
            "ma50":       round(ma50   / 1e8, 1),
            "ma60":       round(ma60   / 1e8, 1),
            "ma120":      round(ma120  / 1e8, 1),
            "days_used":  len(caps),
        })

    results.sort(key=lambda x: x["market_cap"], reverse=True)
    return results


_screener_cache = {}

@app.route("/api/screener/ma/periods")
def screener_periods():
    """Return snapshot dates (every 3rd trading day), desc."""
    rows = db.get_all_cap_history(days=360)
    if not rows:
        return jsonify([])
    dates_asc = sorted(set(r["date"] for r in rows))
    snaps = _snapshot_dates(dates_asc)
    return jsonify(list(reversed(snaps)))


@app.route("/api/screener/ma")
def screener_ma():
    """Daily-MA screener for a specific snapshot date (every 3rd trading day)."""
    target = request.args.get("period")
    rows = db.get_all_cap_history(days=360)
    if not rows:
        return jsonify([])

    dates_asc = sorted(set(r["date"] for r in rows))
    snaps = _snapshot_dates(dates_asc)
    if not snaps:
        return jsonify([])

    snapshot = target if target and target in snaps else snaps[-1]
    snap_idx = snaps.index(snapshot)

    cache_key = f"dma_{snapshot}"
    if cache_key not in _screener_cache:
        _screener_cache[cache_key] = _compute_daily_ma_screener(rows, snapshot)

    return jsonify({
        "period_end":    snapshot,
        "period_dates":  [snapshot],
        "period_index":  snap_idx,
        "total_periods": len(snaps),
        "data":          _screener_cache[cache_key],
    })


@app.route("/api/screener/ma/frequency")
def screener_frequency():
    """Count how many snapshot periods each stock appeared in the aligned list."""
    from collections import Counter
    rows = db.get_all_cap_history(days=360)
    if not rows:
        return jsonify({"total_periods": 0, "data": []})

    dates_asc = sorted(set(r["date"] for r in rows))
    snaps     = _snapshot_dates(dates_asc)
    if not snaps:
        return jsonify({"total_periods": 0, "data": []})

    freq       = Counter()
    stock_info = {}

    for snap in snaps:
        key = f"dma_{snap}"
        if key not in _screener_cache:
            _screener_cache[key] = _compute_daily_ma_screener(rows, snap)
        for s in _screener_cache[key]:
            freq[s["stock_id"]] += 1
            stock_info[s["stock_id"]] = s   # keep latest info

    results = []
    for sid, cnt in freq.most_common():
        s = stock_info[sid]
        results.append({
            "stock_id":      sid,
            "stock_name":    s["stock_name"],
            "market_cap":    s["market_cap"],
            "tier":          s["tier"],
            "cap_yi":        s["cap_yi"],
            "count":         cnt,
            "total_periods": len(snaps),
            "pct":           round(cnt / len(snaps) * 100),
        })

    return jsonify({"total_periods": len(snaps), "data": results})


@app.route("/api/tier-growth")
def tier_growth():
    tier  = request.args.get("tier", "兆")
    days  = request.args.get("days",  60, type=int)
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_tier_growth(tier, limit, days))


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
