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
HEADERS = {"User-Agent": "Mozilla/5.0 Taiwan-MarketCap/1.0"}
BATCH_SIZE = 200   # stocks per yfinance batch download
MAX_WORKERS = 10   # parallel threads for shares fetching


# ── Stock list ────────────────────────────────────────────────────────────────

def get_stock_list_from_twse():
    """Return list of dicts with stock_id and stock_name from TWSE."""
    try:
        r = requests.get(TWSE_STOCK_DAY, headers=HEADERS, timeout=20, verify=False)
        raw = r.content.decode("utf-8")
        data = json.loads(raw)
        result = []
        for item in data:
            sid = str(item.get("Code", "")).strip()
            name = str(item.get("Name", "")).strip()
            # Keep only numeric codes (exclude ETFs like 00400A)
            if sid.isdigit() and 4 <= len(sid) <= 6:
                result.append({"stock_id": sid, "stock_name": name})
        return result
    except Exception as e:
        logger.error(f"TWSE stock list error: {e}")
        return []


# ── Shares outstanding ────────────────────────────────────────────────────────

def _fetch_one_shares(stock_id):
    try:
        ticker = f"{stock_id}.TW"
        info = yf.Ticker(ticker).info
        shares = info.get("sharesOutstanding", 0) or 0
        name = info.get("longName", "") or info.get("shortName", "") or stock_id
        return stock_id, shares, name
    except Exception:
        return stock_id, 0, stock_id


def fetch_shares_parallel(stock_ids, max_workers=MAX_WORKERS):
    """Fetch shares outstanding for a list of stock_ids in parallel."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one_shares, sid): sid for sid in stock_ids}
        done = 0
        for fut in as_completed(futures):
            sid, shares, name = fut.result()
            results[sid] = {"shares": shares, "name": name}
            done += 1
            if done % 100 == 0:
                logger.info(f"  shares fetched: {done}/{len(stock_ids)}")
    return results


# ── Price download ────────────────────────────────────────────────────────────

def download_prices(stock_ids, period="5d"):
    """Batch download closing prices for all stocks. Returns dict date→{sid: price}."""
    tickers = [f"{sid}.TW" for sid in stock_ids]
    all_data = {}

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
                    if price and price == price:  # not NaN
                        ticker_str = str(col)
                        sid = ticker_str.replace(".TW", "")
                        all_data[day][sid] = float(price)
        except Exception as e:
            logger.error(f"Batch download error (batch {i}): {e}")

    return all_data


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_and_store(db, target_date=None, days_history=30):
    """
    Full data fetch and store.
    - First run: fetches shares for all stocks + historical prices.
    - Subsequent runs: uses cached shares + fetches recent prices.
    """
    # 1. Get stock list
    stock_list = get_stock_list_from_twse()
    if not stock_list:
        logger.error("Could not get stock list from TWSE")
        return 0

    all_ids = [s["stock_id"] for s in stock_list]
    id_to_name = {s["stock_id"]: s["stock_name"] for s in stock_list}
    logger.info(f"Stock list: {len(all_ids)} stocks")

    # 2. Ensure shares are cached
    missing_shares = db.get_stocks_missing_shares(all_ids)
    if missing_shares:
        logger.info(f"Fetching shares for {len(missing_shares)} stocks (parallel)…")
        shares_data = fetch_shares_parallel(missing_shares)
        db.upsert_stocks(shares_data)
        logger.info("Shares cached.")

    shares_map = db.get_all_shares()  # {stock_id: shares}

    # 3. Download prices (historical window)
    period = f"{days_history}d"
    logger.info(f"Downloading {period} of prices for {len(all_ids)} stocks…")
    price_data = download_prices(all_ids, period=period)
    logger.info(f"Price data: {len(price_data)} trading days")

    # 4. Build records and store
    total = 0
    for day, prices in sorted(price_data.items()):
        if db.has_data_for_date(day):
            continue  # skip already stored dates

        records = []
        for sid, close in prices.items():
            if close <= 0:
                continue
            shares = shares_map.get(sid, 0)
            market_cap = close * shares if shares > 0 else 0
            name = id_to_name.get(sid, sid)
            records.append((day, sid, name, close, market_cap, shares, ""))

        if records:
            db.insert_batch(records)
            db.update_ranks(day)
            total += len(records)
            logger.info(f"  {day}: stored {len(records)} records")

    return total


def fetch_today(db):
    """Lightweight daily update: download prices for today only."""
    stock_list = get_stock_list_from_twse()
    if not stock_list:
        return 0

    all_ids = [s["stock_id"] for s in stock_list]
    id_to_name = {s["stock_id"]: s["stock_name"] for s in stock_list}
    shares_map = db.get_all_shares()

    today = date.today().strftime("%Y-%m-%d")
    if db.has_data_for_date(today):
        logger.info(f"Data for {today} already exists")
        return 0

    price_data = download_prices(all_ids, period="3d")
    if today not in price_data:
        logger.warning(f"No price data for {today} (market may be closed)")
        # Try latest available day
        if price_data:
            today = max(price_data.keys())
            if db.has_data_for_date(today):
                return 0
        else:
            return 0

    prices = price_data[today]
    records = []
    for sid, close in prices.items():
        if close <= 0:
            continue
        shares = shares_map.get(sid, 0)
        market_cap = close * shares if shares > 0 else 0
        name = id_to_name.get(sid, sid)
        records.append((today, sid, name, close, market_cap, shares, ""))

    if records:
        db.insert_batch(records)
        db.update_ranks(today)
        logger.info(f"Today {today}: stored {len(records)} records")

    return len(records)
