# app/services/market_service.py

import math
import os
from datetime import datetime, timezone

import requests
import yfinance as yf
from fastapi import HTTPException

from app.schemas.market import MarketCandleItem, MarketChartResponse
from typing import List, Dict, Any, Optional, Literal
from app.services.scanner_service import (
    get_leaderboard,
    get_opportunities,
    filter_symbols,
)
# Bybit API 基本網址
BYBIT_BASE_URL = "https://api.bybit.com"

# CoinMarketCap API Key（之後如果要抓 top100 可用）
CMC_API_KEY = os.getenv("CMC_API_KEY")
PoolSize = Literal["TOP100", "TOP800", "ALL"]

def safe_float(value):
    """安全轉 float，避免 None / NaN 造成錯誤"""
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
    """判斷是否為 USDT 交易對，例如 BTCUSDT"""
    s = str(symbol).strip().upper()
    return s.endswith("USDT")


def normalize_stock_symbol(symbol: str) -> str:
    """股票代號標準化：2330 -> 2330.TW，美股原樣保留"""
    s = str(symbol).strip().upper()

    if s.isdigit():
        return f"{s}.TW"

    if s.endswith(".TW"):
        return s

    return s


def normalize_crypto_symbol(symbol: str) -> str:
    """Crypto 代號標準化：BTC -> BTCUSDT"""
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


def get_ticker(symbol: str):
    """取得 Bybit 單一 ticker"""
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {
        "category": "spot",
        "symbol": symbol
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("retCode") != 0:
        raise HTTPException(status_code=404, detail=f"Bybit 查無商品: {symbol}")

    items = data.get("result", {}).get("list", [])
    if not items:
        raise HTTPException(status_code=404, detail=f"Bybit 查無商品: {symbol}")

    return items[0]


def build_crypto_quote_data(symbol: str):
    """建立 Crypto 報價資料"""
    symbol = normalize_crypto_symbol(symbol)
    item = get_ticker(symbol)

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


def get_detail_data(symbol: str, market: str = "stock"):
    """取得股票或 crypto 的詳細資料（52週、市值、產業、基本面等）"""
    raw_symbol = str(symbol).strip().upper()
    market = str(market).strip().lower()

    if market == "crypto":
        quote = build_crypto_quote_data(raw_symbol)
        return {
            "symbol": quote["symbol"],
            "raw_symbol": raw_symbol,
            "name": quote.get("name") or raw_symbol,
            "market": "海外/其他",
            "industry": "Cryptocurrency",
            "sector": "Crypto",
            "price": quote.get("price"),
            "change": quote.get("change"),
            "change_percent": quote.get("change_percent"),
            "market_cap": None,
            "fifty_two_week_high": None,
            "fifty_two_week_low": None,
            "pe": None,
            "pb": None,
            "eps": None,
            "roe": None,
            "gross": None,
            "revenue": None,
            "debt": None,
            "valuation": None,
            "currency": quote.get("currency", "USDT"),
            "exchange": quote.get("exchange", "BYBIT"),
            "interval": "1d",
            "fetch_interval": "1d",
            "period": "1y",
            "data_quality": "完整",
            "errors": [],
        }

    stock_symbol = normalize_stock_symbol(raw_symbol)
    ticker = yf.Ticker(stock_symbol)
    info = safe_get_ticker_info(ticker)

    quote = get_quote_data(symbol, market)
    market_label = "台股/櫃買" if stock_symbol.endswith((".TW", ".TWO")) or raw_symbol.isdigit() else "海外/其他"
    sector = info.get("sector") or "N/A"
    industry = info.get("industry") or info.get("sector") or "N/A"

    # 資料品質
    quality = "完整"
    if not info.get("trailingPE") and not info.get("priceToBook"):
        quality = "部分"
    if not info.get("trailingPE") and not info.get("priceToBook") and not info.get("trailingEps"):
        quality = "基本"

    return {
        "symbol": stock_symbol,
        "raw_symbol": raw_symbol,
        "name": quote.get("name") or info.get("symbol"),
        "market": market_label,
        "industry": industry,
        "sector": sector,
        "display_industry": industry if industry != "N/A" else sector,
        "price": quote.get("price"),
        "change": quote.get("change"),
        "change_percent": quote.get("change_percent"),
        "market_cap": safe_float(info.get("marketCap")),
        "fifty_two_week_high": safe_float(info.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": safe_float(info.get("fiftyTwoWeekLow")),
        "pe": safe_float(info.get("trailingPE")),
        "pb": safe_float(info.get("priceToBook")),
        "eps": safe_float(info.get("trailingEps")),
        "roe": safe_float(info.get("returnOnEquity")),
        "gross": safe_float(info.get("grossMargins")),
        "revenue": safe_float(info.get("revenueGrowth")),
        "debt": safe_float(info.get("debtToEquity")),
        "valuation": None,
        "currency": quote.get("currency"),
        "exchange": quote.get("exchange"),
        "interval": "1d",
        "fetch_interval": "1d",
        "period": "3mo",
        "data_quality": quality,
        "errors": [],
    }


def get_peer_symbols(symbol: str, market: str, max_peers: int = 5) -> List[str]:
    """取得同業代號（簡化版：同市場優先）"""
    from app.services.scanner_service import get_tw_universe, get_us_universe

    raw = str(symbol).strip().upper()
    mkt = str(market).strip().upper()

    if mkt == "TW":
        universe = get_tw_universe("ALL")
    elif mkt == "US":
        universe = get_us_universe("ALL")
    else:
        return []

    symbols = [s if isinstance(s, str) else str(s.get("symbol", s)) for s in universe]
    base = raw.replace(".TW", "").replace(".TWO", "").split(".")[0]
    filtered = [s for s in symbols if str(s).replace(".TW", "").replace(".TWO", "").split(".")[0] != base]
    return filtered[:max_peers]


def map_chart_interval_to_bybit(interval: str) -> str:
    """把 chart interval 轉成 Bybit 格式"""
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
        "1wk": "W",
        "1mo": "M",
    }
    return mapping.get(interval, "D")


def build_crypto_chart_data(symbol: str, interval: str, period: str):
    """建立 Crypto K 線資料"""
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
def build_crypto_market_data(symbol: str, interval: str = "1d") -> dict:
    """提供 AI 分析用的 Crypto 資料（走 Bybit）"""
    import pandas as pd

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
        raise HTTPException(status_code=404, detail=f"Bybit 查無可分析資料: {symbol}")

    rows = data.get("result", {}).get("list", [])
    if not rows:
        raise HTTPException(status_code=404, detail=f"Bybit 查無可分析資料: {symbol}")

    df = pd.DataFrame(
        reversed(rows),
        columns=["timestamp", "Open", "High", "Low", "Close", "Volume", "Turnover"]
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)

    for col in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
        df[col] = df[col].apply(safe_float)

    df = df.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)

    if df.empty:
        raise HTTPException(status_code=404, detail=f"Bybit 查無有效可分析資料: {symbol}")

    # MA20
    df["MA20"] = df["Close"].rolling(20).mean()

    # MA60
    df["MA60"] = df["Close"].rolling(60).mean()

    # RSI
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()

    latest_close = safe_float(df["Close"].iloc[-1])

    recent = df.tail(20)
    support = safe_float(recent["Low"].min()) if not recent.empty else None
    resistance = safe_float(recent["High"].max()) if not recent.empty else None

    return {
        "raw_symbol": symbol,
        "name": symbol,
        "market": "CRYPTO",
        "price": latest_close,
        "support": support,
        "resistance": resistance,
        "hist": df,
    }

def _resample_to_4h(hist):
    """將 1h K 線重採樣為 4h（參考 v14）"""
    if hist is None or hist.empty or len(hist) < 2:
        return hist
    df = hist.copy()
    try:
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df = df.tz_convert(None)
    except Exception:
        pass
    out = df.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])
    return out


def get_chart_data(symbol: str, interval: str, period: str):
    """統一取得股票 / Crypto K 線（支援 1h, 4h, 1d, 1wk）"""
    raw_symbol = str(symbol).strip().upper()

    if is_crypto_symbol(raw_symbol):
        return build_crypto_chart_data(raw_symbol, interval, period)

    symbol = normalize_stock_symbol(raw_symbol)
    ticker = yf.Ticker(symbol)
    if interval == "4h":
        hist = ticker.history(period="2mo", interval="1h", auto_adjust=False)
        if hist is not None and not hist.empty:
            hist = _resample_to_4h(hist)
        if hist is None or hist.empty:
            raise HTTPException(status_code=404, detail=f"查無 4h 圖表資料: {symbol}")
    else:
        _period = "1mo" if interval == "1h" else ("2y" if interval == "1wk" else period)
        hist = ticker.history(period=_period, interval=interval, auto_adjust=False)

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


def get_market_data(symbol: str, market: str = "US", interval: str = "1d", period: str | None = None) -> dict:
    """提供 AI 分析用的單一標的資料"""
    raw_symbol = str(symbol).strip().upper()
    market_upper = str(market).strip().upper()

    if market_upper == "CRYPTO":
        return build_crypto_market_data(raw_symbol, interval)

    if market_upper == "TW":
        yf_symbol = normalize_stock_symbol(raw_symbol)
    else:
        yf_symbol = raw_symbol

    ticker = yf.Ticker(yf_symbol)
    info = safe_get_ticker_info(ticker)
    # 估值用（yfinance 欄位可能缺漏，若取不到會是 None）
    pe = safe_float(info.get("trailingPE") or info.get("forwardPE"))
    pb = safe_float(info.get("priceToBook"))
    if interval == "4h":
        hist_1h = ticker.history(period="2mo", interval="1h", auto_adjust=False)
        if hist_1h is None or hist_1h.empty or len(hist_1h) < 2:
            raise HTTPException(status_code=404, detail=f"查無可分析資料: {yf_symbol}")
        hist_1h = hist_1h[~hist_1h.index.duplicated(keep="first")]
        hist = hist_1h.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum"
        }).dropna(subset=["Close"])
        if hist.empty or len(hist) < 2:
            raise HTTPException(status_code=404, detail=f"查無可分析資料: {yf_symbol}")
    else:
        _period = period or ("1mo" if interval == "1h" else ("2y" if interval == "1wk" else "6mo"))
        hist = ticker.history(period=_period, interval=interval, auto_adjust=False)

    if hist is None or hist.empty:
        raise HTTPException(status_code=404, detail=f"查無可分析資料: {yf_symbol}")

    # MA20
    hist["MA20"] = hist["Close"].rolling(20).mean()

    # MA60
    hist["MA60"] = hist["Close"].rolling(60).mean()

    # RSI
    delta = hist["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss
    hist["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = hist["Close"].ewm(span=12, adjust=False).mean()
    ema26 = hist["Close"].ewm(span=26, adjust=False).mean()
    hist["MACD"] = ema12 - ema26
    hist["MACD_SIGNAL"] = hist["MACD"].ewm(span=9, adjust=False).mean()

    latest_close = safe_float(hist["Close"].iloc[-1])

    recent = hist.tail(20)
    support = safe_float(recent["Low"].min()) if not recent.empty else None
    resistance = safe_float(recent["High"].max()) if not recent.empty else None

    return {
        "raw_symbol": raw_symbol,
        "name": info.get("shortName") or info.get("longName") or raw_symbol,
        "market": market_upper,
        "price": latest_close,
        "support": support,
        "resistance": resistance,
        "pe": pe,
        "pb": pb,
        "hist": hist,
    }


def get_multi_timeframe_summary(symbol: str, market: str = "US", lang: str = "zh") -> List[Dict[str, Any]]:
    """多時間框架總覽：1h, 1d, 1wk 的趨勢、價格、RSI、訊號分數"""
    from app.services.technical_service import trend_score, trend_label

    intervals = [("1h", "1mo"), ("4h", "2mo"), ("1d", "6mo"), ("1wk", "2y")]
    rows = []
    for iv, period in intervals:
        try:
            data = get_market_data(symbol=symbol, market=market, interval=iv)
            if data.get("hist") is None or data["hist"].empty:
                rows.append({"period": iv, "trend": "無資料", "price": "N/A", "rsi": "N/A", "score": "N/A"})
                continue
            hist = data["hist"]
            latest = hist.iloc[-1]
            score = trend_score(hist)
            price = safe_float(latest.get("Close"))
            rsi = safe_float(latest.get("RSI"))
            rows.append({
                "period": iv,
                "trend": trend_label(score, lang),
                "price": round(price, 2) if price is not None else "N/A",
                "rsi": round(rsi, 2) if rsi is not None else "N/A",
                "score": f"{score}/5",
            })
        except Exception:
            rows.append({"period": iv, "trend": "無資料", "price": "N/A", "rsi": "N/A", "score": "N/A"})
    return rows


def get_technical_signal_table(symbol: str, market: str = "US", lang: str = "zh") -> List[Dict[str, str]]:
    """技術訊號總表：均線、RSI、MACD、關鍵價位、成交量、型態"""
    from app.services.technical_service import generate_signal_table

    data = get_market_data(symbol=symbol, market=market, interval="1d")
    hist = data.get("hist")
    support = data.get("support")
    resistance = data.get("resistance")
    if hist is None or hist.empty:
        return []
    return generate_signal_table(hist, support, resistance, lang=lang)


def build_opportunity_candidates(
    market: str = "US",
    scan_mode: str = "core",
    limit: int = 8,
    lang: str = "zh",
) -> list[dict]:
    market_upper = str(market).strip().upper()

    if market_upper == "TW":
        symbols = get_tw_universe()
    elif market_upper == "US":
        symbols = get_us_universe()
    elif market_upper == "CRYPTO":
        symbols = get_crypto_universe(limit=max(limit * 3, 20))
    else:
        raise HTTPException(status_code=400, detail=f"不支援的 market: {market}")

    results = []

    for symbol in symbols:
        try:
            if market_upper == "CRYPTO":
                quote = build_crypto_quote_data(symbol)
                price = safe_float(quote.get("price"))
                change_pct = safe_float(quote.get("change_percent"))

                score = 50
                reasons = []

                if change_pct is not None and change_pct > 0:
                    score += 10
                    reasons.append("24h 漲幅為正")
                if change_pct is not None and change_pct >= 3:
                    score += 10
                    reasons.append("短線動能偏強")

                results.append({
                    "symbol": quote["symbol"],
                    "name": quote.get("name") or quote["symbol"],
                    "price": round(price, 4) if price is not None else None,
                    "change_pct": round(change_pct, 4) if change_pct is not None else None,
                    "score": score,
                    "reason": "、".join(reasons) if reasons else "幣價動能中性",
                })
                continue

            data = get_market_data(symbol=symbol, market=market_upper, interval="1d")
            hist = data["hist"]
            latest = hist.iloc[-1]

            price = safe_float(latest.get("Close"))
            ma20 = safe_float(latest.get("MA20"))
            ma60 = safe_float(latest.get("MA60"))
            rsi = safe_float(latest.get("RSI"))

            prev_close = safe_float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
            change_pct = None
            if price is not None and prev_close not in (None, 0):
                change_pct = round(((price - prev_close) / prev_close) * 100, 4)

            score = 50
            reasons = []

            if price is not None and ma20 is not None and price > ma20:
                score += 10
                reasons.append("站上 MA20")
            if ma20 is not None and ma60 is not None and ma20 > ma60:
                score += 10
                reasons.append("MA20 高於 MA60")
            if rsi is not None and 45 <= rsi <= 65:
                score += 10
                reasons.append("RSI 位於中性偏強區")
            if change_pct is not None and change_pct > 0:
                score += 5
                reasons.append("日內漲幅為正")

            results.append({
                "symbol": data["raw_symbol"],
                "name": data["name"],
                "price": round(price, 4) if price is not None else None,
                "change_pct": change_pct,
                "score": score,
                "reason": "、".join(reasons) if reasons else "技術面中性",
            })
        except Exception as e:
            print(f"skip {symbol}: {e}")
            continue

    results.sort(key=lambda x: (x.get("score") or 0, x.get("change_pct") or 0), reverse=True)
    return results[:limit]
   