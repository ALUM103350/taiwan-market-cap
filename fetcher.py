import logging
import warnings
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import yfinance as yf

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

TWSE_STOCK_DAY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_LIST_URL  = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.tpex.org.tw/",
    "Accept": "application/json",
}
BATCH_SIZE = 200
MAX_WORKERS = 10


# ── Stock lists ───────────────────────────────────────────────────────────────

def get_stock_list_from_twse():
    """TWSE (上市) stocks → suffix .TW"""
    try:
        r = requests.get(TWSE_STOCK_DAY, headers=HEADERS, timeout=20, verify=False)
        data = json.loads(r.content.decode("utf-8"))
        result = []
        for item in data:
            sid = str(item.get("Code", "")).strip()
            name = str(item.get("Name", "")).strip()
            if sid.isdigit() and 4 <= len(sid) <= 6:
                result.append({"stock_id": sid, "stock_name": name, "suffix": ".TW"})
        return result
    except Exception as e:
        logger.error(f"TWSE stock list error: {e}")
        return []


def get_stock_list_from_tpex():
    """TPEx (上櫃) stocks → suffix .TWO"""
    try:
        r = requests.get(TPEX_LIST_URL, headers=HEADERS, timeout=20, verify=False)
        data = r.json()
        result = []
        for item in data:
            sid  = str(item.get("SecuritiesCompanyCode", "")).strip()
            name = str(item.get("CompanyName", "") or
                       item.get("CompanyAbbreviation", "")).strip()
            if sid.isdigit() and 4 <= len(sid) <= 6:
                result.append({"stock_id": sid, "stock_name": name, "suffix": ".TWO"})
        logger.info(f"TPEx list: {len(result)} stocks")
        return result
    except Exception as e:
        logger.error(f"TPEx stock list error: {e}")
        return []


def get_all_stocks():
    """Combined TWSE + TPEx stock list. TWSE takes priority on duplicate codes."""
    twse = get_stock_list_from_twse()
    tpex = get_stock_list_from_tpex()
    logger.info(f"Stock lists: TWSE={len(twse)}, TPEx={len(tpex)}")

    seen = {s["stock_id"] for s in twse}
    combined = twse + [s for s in tpex if s["stock_id"] not in seen]
    logger.info(f"Combined: {len(combined)} stocks")
    return combined


# ── Shares outstanding ────────────────────────────────────────────────────────

def _fetch_one_shares(stock_id, suffix=".TW"):
    for sfx in [suffix, ".TW", ".TWO"]:
        try:
            info = yf.Ticker(f"{stock_id}{sfx}").info
            shares = info.get("sharesOutstanding", 0) or 0
            name   = info.get("longName", "") or info.get("shortName", "") or stock_id
            if shares > 0:
                return stock_id, shares, name
        except Exception:
            pass
    return stock_id, 0, stock_id


def fetch_shares_parallel(stocks, max_workers=MAX_WORKERS):
    """stocks: list of {stock_id, suffix}"""
    results = {}
    suffix_map = {s["stock_id"]: s.get("suffix", ".TW") for s in stocks}
    ids = [s["stock_id"] for s in stocks]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_one_shares, sid, suffix_map[sid]): sid
            for sid in ids
        }
        done = 0
        for fut in as_completed(futures):
            sid, shares, name = fut.result()
            results[sid] = {"shares": shares, "name": name}
            done += 1
            if done % 100 == 0:
                logger.info(f"  shares fetched: {done}/{len(ids)}")
    return results


# ── Price download ────────────────────────────────────────────────────────────

def download_prices(stocks, period="5d"):
    """
    stocks: list of {stock_id, suffix}
    Returns dict: date → {stock_id: price}
    """
    # Group by suffix
    by_suffix = {}
    for s in stocks:
        sfx = s.get("suffix", ".TW")
        by_suffix.setdefault(sfx, []).append(s["stock_id"])

    all_data = {}

    for suffix, ids in by_suffix.items():
        tickers = [f"{sid}{suffix}" for sid in ids]
        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i: i + BATCH_SIZE]
            try:
                df = yf.download(batch, period=period, auto_adjust=True, progress=False)
                if df.empty:
                    continue
                close = df["Close"]
                for ts, row in close.iterrows():
                    day = ts.strftime("%Y-%m-%d")
                    if day not in all_data:
                        all_data[day] = {}
                    for col, price in row.items():
                        if price and price == price:
                            sid = str(col).replace(".TWO", "").replace(".TW", "")
                            all_data[day][sid] = float(price)
            except Exception as e:
                logger.error(f"Batch download error ({suffix} batch {i}): {e}")

    return all_data


# ── Main entry points ─────────────────────────────────────────────────────────

def fetch_and_store(db, target_date=None, days_history=30):
    stock_list = get_all_stocks()
    if not stock_list:
        logger.error("Could not get any stock list")
        return 0

    all_ids    = [s["stock_id"]   for s in stock_list]
    id_to_name = {s["stock_id"]: s["stock_name"] for s in stock_list}
    logger.info(f"Total stocks: {len(all_ids)}")

    # Ensure shares cached
    missing = db.get_stocks_missing_shares(all_ids)
    if missing:
        missing_stocks = [s for s in stock_list if s["stock_id"] in set(missing)]
        logger.info(f"Fetching shares for {len(missing_stocks)} stocks…")
        shares_data = fetch_shares_parallel(missing_stocks)
        db.upsert_stocks(shares_data)

    shares_map = db.get_all_shares()

    period = f"{days_history}d"
    logger.info(f"Downloading {period} prices…")
    price_data = download_prices(stock_list, period=period)
    logger.info(f"Price data: {len(price_data)} trading days")

    total = 0
    for day, prices in sorted(price_data.items()):
        if db.has_data_for_date(day):
            continue
        records = []
        for sid, close in prices.items():
            if close <= 0:
                continue
            shares     = shares_map.get(sid, 0)
            market_cap = close * shares if shares > 0 else 0
            name       = id_to_name.get(sid, sid)
            records.append((day, sid, name, close, market_cap, shares, ""))
        if records:
            db.insert_batch(records)
            db.update_ranks(day)
            total += len(records)
            logger.info(f"  {day}: {len(records)} records")

    return total


def fetch_today(db):
    stock_list = get_all_stocks()
    if not stock_list:
        return 0

    all_ids    = [s["stock_id"]   for s in stock_list]
    id_to_name = {s["stock_id"]: s["stock_name"] for s in stock_list}
    shares_map = db.get_all_shares()

    today = date.today().strftime("%Y-%m-%d")
    if db.has_data_for_date(today):
        logger.info(f"Data for {today} already exists")
        return 0

    # Make sure new TPEx stocks have shares
    missing = db.get_stocks_missing_shares(all_ids)
    if missing:
        missing_stocks = [s for s in stock_list if s["stock_id"] in set(missing)]
        logger.info(f"Caching shares for {len(missing_stocks)} new stocks…")
        shares_data = fetch_shares_parallel(missing_stocks)
        db.upsert_stocks(shares_data)
        shares_map = db.get_all_shares()

    price_data = download_prices(stock_list, period="3d")
    if today not in price_data:
        logger.warning(f"No price data for {today}")
        if price_data:
            today = max(price_data.keys())
            if db.has_data_for_date(today):
                return 0
        else:
            return 0

    prices  = price_data[today]
    records = []
    for sid, close in prices.items():
        if close <= 0:
            continue
        shares     = shares_map.get(sid, 0)
        market_cap = close * shares if shares > 0 else 0
        name       = id_to_name.get(sid, sid)
        records.append((today, sid, name, close, market_cap, shares, ""))

    if records:
        db.insert_batch(records)
        db.update_ranks(today)
        logger.info(f"Today {today}: {len(records)} records")

    return len(records)
