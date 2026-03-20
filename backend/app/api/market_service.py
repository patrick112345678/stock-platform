# app/services/market_service.py

import math
import os
from datetime import datetime, timezone

import requests
import yfinance as yf
from fastapi import HTTPException

from app.schemas.market import MarketCandleItem, MarketChartResponse

BYBIT_BASE_URL = "https://api.bybit.com"
CMC_API_KEY = os.getenv("CMC_API_KEY")


def safe_float(value):
    """安全轉 float，避免 None 或 NaN 造成錯誤"""
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value):
            return None
        return value
    except Exception:
        return None


def is_crypto_symbol(symbol: str) -> bool:
    """判斷是否為 crypto 交易對，例如 BTCUSDT"""
    s = str(symbol).strip().upper()
    return s.endswith("USDT")


def normalize_stock_symbol(symbol: str) -> str:
    """標準化股票代號，台股數字補 .TW，美股維持原樣"""
    s = str(symbol).strip().upper()

    if s.isdigit():
        return f"{s}.TW"

    if s.endswith(".TW"):
        return s

    return s


def normalize_crypto_symbol(symbol: str) -> str:
    """標準化 crypto 交易對，若未帶 USDT 則自動補上"""
    s = str(symbol).strip().upper()

    if s.endswith("USDT"):
        return s

    return f"{s}USDT"


def detect_stock_market(symbol: str) -> str:
    """判斷股票市場，台股回 TW，其餘預設 US"""
    s = str(symbol).strip().upper()
    if s.isdigit() or s.endswith(".TW"):
        return "TW"
    return "US"


def get_bybit_spot_symbols():
    """取得 Bybit 現貨交易對清單"""
    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    params = {"category": "spot"}

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("retCode") != 0:
        raise HTTPException(status_code=500, detail="取得 Bybit 幣種清單失敗")

    return data.get("result", {}).get("list", [])


def build_crypto_quote_data(symbol: str):
    """建立 crypto 報價資料"""
    symbol = normalize_crypto_symbol(symbol)

    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {
        "category": "spot",
        "symbol": symbol,
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("retCode") != 0:
        raise HTTPException(status_code=404, detail=f"Bybit 查無商品: {symbol}")

    items = data.get("result", {}).get("list", [])
    if not items:
        raise HTTPException(status_code=404, detail=f"Bybit 查無商品: {symbol}")

    item = items[0]

    last_price = safe_float(item.get("lastPrice"))
    prev_price_24h = safe_float(item.get("prevPrice24h"))
    price_24h_pcnt = safe_float(item.get("price24hPcnt"))

    change = None
    if last_price is not None and prev_price_24h is not None:
        change = round(last_price - prev_price_24h, 8)

    change_percent = None
    if price_24h_pcnt is not None:
        change_percent = round(price_24h_pcnt * 100, 4)

    return {
        "symbol": symbol,
        "name": symbol,
        "currency": "USDT",
        "exchange": "BYBIT",
        "price": round(last_price, 8) if last_price is not None else None,
        "previous_close": round(prev_price_24h, 8) if prev_price_24h is not None else None,
        "change": change,
        "change_percent": change_percent,
    }
def safe_get_ticker_info(ticker):
    try:
        info = ticker.info
        return info or {}
    except Exception as e:
        print("WARN ticker.info failed:", repr(e))
        return {}

def safe_get_fast_info(ticker):
    try:
        fi = ticker.fast_info
        return dict(fi) if fi else {}
    except Exception as e:
        print("WARN ticker.fast_info failed:", repr(e))
        return {}
def get_quote_data(symbol: str, market: str = "stock"):
    """統一取得股票或 crypto 報價資料"""
    raw_symbol = str(symbol).strip().upper()
    market = str(market).strip().lower()

    if market == "crypto":
        return build_crypto_quote_data(raw_symbol)

    # stock
    stock_symbol = normalize_stock_symbol(raw_symbol)

    ticker = yf.Ticker(stock_symbol)
    info = safe_get_ticker_info(ticker)
    fast_info = safe_get_fast_info(ticker)

    current_price = safe_float(
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )
    previous_close = safe_float(
        info.get("previousClose")
        or info.get("regularMarketPreviousClose")
    )

    if current_price is None or previous_close is None:
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty:
            close_series = hist["Close"].dropna()

            if current_price is None and len(close_series) >= 1:
                current_price = safe_float(close_series.iloc[-1])

            if previous_close is None:
                if len(close_series) >= 2:
                    previous_close = safe_float(close_series.iloc[-2])
                elif len(close_series) == 1:
                    previous_close = safe_float(close_series.iloc[-1])

    if current_price is None:
        raise HTTPException(status_code=404, detail=f"查無商品或無法取得報價: {stock_symbol}")

    change = None
    change_percent = None
    if previous_close not in (None, 0):
        change = round(current_price - previous_close, 4)
        change_percent = round((change / previous_close) * 100, 4)

    return {
        "symbol": stock_symbol,
        "name": info.get("shortName") or info.get("longName"),
        "currency": info.get("currency"),
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "price": round(current_price, 4),
        "previous_close": round(previous_close, 4) if previous_close is not None else None,
        "change": change,
        "change_percent": change_percent,
    }
def map_chart_interval_to_bybit(interval: str) -> str:
    """將前端 chart interval 轉成 Bybit interval 格式"""
    mapping = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "6h": "360",
        "12h": "720",
        "1d": "D",
        "1w": "W",
        "1mo": "M",
    }
    return mapping.get(interval, "D")


def build_crypto_chart_data(symbol: str, interval: str, period: str):
    """建立 crypto K 線資料"""
    symbol = normalize_crypto_symbol(symbol)
    bybit_interval = map_chart_interval_to_bybit(interval)

    url = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": bybit_interval,
        "limit": 200,
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("retCode") != 0:
        raise HTTPException(status_code=404, detail=f"Bybit 查無圖表資料: {symbol}")

    rows = data.get("result", {}).get("list", [])
    if not rows:
        raise HTTPException(status_code=404, detail=f"Bybit 查無圖表資料: {symbol}")

    candles = []
    for row in reversed(rows):
        ts = int(row[0])
        open_price = safe_float(row[1])
        high_price = safe_float(row[2])
        low_price = safe_float(row[3])
        close_price = safe_float(row[4])
        volume = safe_float(row[5])

        if None in (open_price, high_price, low_price, close_price):
            continue

        time_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()

        candles.append(
            MarketCandleItem(
                time=time_str,
                open=round(open_price, 4),
                high=round(high_price, 4),
                low=round(low_price, 4),
                close=round(close_price, 4),
                volume=volume,
            )
        )

    return MarketChartResponse(
        symbol=symbol,
        interval=interval,
        period=period,
        candles=candles,
    )


def get_chart_data(symbol: str, interval: str, period: str):
    """統一取得股票或 crypto K 線資料"""
    raw_symbol = str(symbol).strip().upper()

    if is_crypto_symbol(raw_symbol):
        symbol = normalize_crypto_symbol(raw_symbol)
        return build_crypto_chart_data(symbol, interval, period)

    symbol = normalize_stock_symbol(raw_symbol)
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, interval=interval, auto_adjust=False)

    if hist is None or hist.empty:
        raise HTTPException(status_code=404, detail=f"查無圖表資料: {symbol}")

    candles = []
    for idx, row in hist.iterrows():
        open_price = safe_float(row.get("Open"))
        high_price = safe_float(row.get("High"))
        low_price = safe_float(row.get("Low"))
        close_price = safe_float(row.get("Close"))
        volume = safe_float(row.get("Volume"))

        if None in (open_price, high_price, low_price, close_price):
            continue

        time_str = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)

        candles.append(
            MarketCandleItem(
                time=time_str,
                open=round(open_price, 4),
                high=round(high_price, 4),
                low=round(low_price, 4),
                close=round(close_price, 4),
                volume=volume,
            )
        )

    if not candles:
        raise HTTPException(status_code=404, detail=f"查無有效K線資料: {symbol}")

    return MarketChartResponse(
        symbol=symbol,
        interval=interval,
        period=period,
        candles=candles,
    )