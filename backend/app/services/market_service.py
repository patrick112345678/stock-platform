# app/services/market_service.py
# 行情內部實作：
# - 台股：日線 FinMind→Yahoo→（可選）TWSE；即時 MIS；基本面 FinMind PER/PBR/EPS；股名 FinMind TaiwanStockInfo。
# - 美股：Yahoo（us_stock_provider，可替換為 Finnhub 等）。
# - 加密：Bybit → Binance，不使用 Yahoo。

import math
import os

import pandas as pd
import requests
from fastapi import HTTPException
from typing import List, Dict, Any, Optional, Literal

from app.schemas.market import MarketCandleItem, MarketChartResponse
from app.services.stock_data_service import get_cached_stock_data, get_cached_data, NEUTRAL_DATA_ERROR
from app.services.fundamental_provider import (
    fetch_tw_fundamentals_finmind,
    resolve_tw_display_name,
)
from app.services.technical_service import valuation_label
from app.services.scanner_service import (
    get_crypto_kline_with_fallback,
    get_tw_universe,
    get_us_universe,
    get_crypto_universe,
)

BYBIT_BASE_URL = "https://api.bybit.com"
BINANCE_API_BASE = "https://api.binance.com/api/v3"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockPlatform/1.0)",
    "Accept": "application/json",
}
EXTERNAL_REQUEST_TIMEOUT = 5.0

CMC_API_KEY = os.getenv("CMC_API_KEY")
PoolSize = Literal["TOP100", "TOP800", "ALL"]


def safe_float(value):
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
    s = str(symbol).strip().upper()
    return s.endswith("USDT")


def normalize_stock_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if s.isdigit():
        return f"{s}.TW"
    if s.endswith(".TW"):
        return s
    return s


def normalize_crypto_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if s.endswith("USDT"):
        return s
    return f"{s}USDT"


def detect_stock_market(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if s.isdigit() or s.endswith(".TW"):
        return "TW"
    return "US"


def _parse_tw_mis_number(val) -> Optional[float]:
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_tw_mis_snapshot(raw_symbol: str) -> Optional[Dict[str, Any]]:
    code = str(raw_symbol).replace(".TW", "").replace(".TWO", "").strip()
    if not code.isdigit():
        return None
    for prefix in ("tse", "otc"):
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{code}.tw"
        try:
            r = requests.get(url, timeout=EXTERNAL_REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
            r.raise_for_status()
            j = r.json()
            arr = j.get("msgArray") or []
            if not arr:
                continue
            row = arr[0]
            z = _parse_tw_mis_number(row.get("z"))
            y = _parse_tw_mis_number(row.get("y"))
            price = z if z is not None else y
            if price is None:
                continue
            name = row.get("nf") or row.get("n") or row.get("c")
            return {
                "price": price,
                "previous_close": y,
                "name": str(name).strip() if name else None,
            }
        except Exception:
            continue
    return None


def get_bybit_spot_symbols():
    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    params = {"category": "spot"}
    resp = requests.get(url, params=params, timeout=EXTERNAL_REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise HTTPException(status_code=500, detail="取得 Bybit 幣種清單失敗")
    return data.get("result", {}).get("list", [])


def _binance_ticker_row(symbol: str) -> dict:
    r = requests.get(
        f"{BINANCE_API_BASE}/ticker/24hr",
        params={"symbol": symbol},
        timeout=EXTERNAL_REQUEST_TIMEOUT,
        headers=REQUEST_HEADERS,
    )
    r.raise_for_status()
    t = r.json()
    lp = safe_float(t.get("lastPrice"))
    op = safe_float(t.get("openPrice"))
    pcp = safe_float(t.get("priceChangePercent"))
    frac = (pcp / 100.0) if pcp is not None else None
    return {
        "lastPrice": str(lp) if lp is not None else None,
        "prevPrice24h": str(op) if op is not None else None,
        "price24hPcnt": str(frac) if frac is not None else None,
        "_exchange": "BINANCE",
    }


def get_ticker(symbol: str):
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "spot", "symbol": symbol}
    try:
        resp = requests.get(url, params=params, timeout=EXTERNAL_REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            raise ValueError(f"Bybit retCode: {data.get('retCode')}")
        items = data.get("result", {}).get("list", [])
        if not items:
            raise ValueError("Bybit empty list")
        row = dict(items[0])
        row["_exchange"] = "BYBIT"
        return row
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 403:
            print(f"[crypto] provider=BYBIT HTTP 403, fallback=BINANCE ticker symbol={symbol}")
        else:
            print(f"[crypto] provider=BYBIT HTTP error {code}, fallback=BINANCE ticker symbol={symbol}", repr(e))
        try:
            return _binance_ticker_row(symbol)
        except Exception as e2:
            raise HTTPException(
                status_code=404,
                detail=f"加密貨幣報價暫時無法取得（Bybit 與 Binance 皆失敗），請稍後再試。",
            ) from e2
    except Exception as e:
        print("[crypto] provider=BYBIT failed, fallback=BINANCE ticker", symbol, repr(e))
        try:
            return _binance_ticker_row(symbol)
        except Exception as e2:
            raise HTTPException(
                status_code=404,
                detail=f"加密貨幣報價暫時無法取得（Bybit 與 Binance 皆失敗），請稍後再試。",
            ) from e2


def build_crypto_quote_data(symbol: str):
    symbol = normalize_crypto_symbol(symbol)
    try:
        item = dict(get_ticker(symbol))
    except Exception:
        return {
            "symbol": symbol,
            "name": symbol,
            "currency": "USDT",
            "exchange": None,
            "price": None,
            "previous_close": None,
            "change": None,
            "change_percent": None,
        }
    exch = item.pop("_exchange", "BYBIT")
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
        "exchange": exch,
        "price": round(last_price, 8) if last_price is not None else None,
        "previous_close": round(prev_price_24h, 8) if prev_price_24h is not None else None,
        "change": change,
        "change_percent": change_percent,
    }


def _slice_hist_by_period(hist: pd.DataFrame, period: str) -> pd.DataFrame:
    days = {"1mo": 24, "3mo": 66, "6mo": 132, "1y": 252, "2y": 504}.get(period, 66)
    if hist is None or hist.empty:
        return hist
    return hist.tail(days) if len(hist) > days else hist


def _resample_to_4h(hist):
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


def _resample_daily_to_weekly(hist: pd.DataFrame) -> pd.DataFrame:
    if hist is None or hist.empty:
        return hist
    df = hist.copy()
    try:
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df = df.tz_convert(None)
    except Exception:
        pass
    return df.resample("1W").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])


def _resample_daily_to_4d(hist: pd.DataFrame) -> pd.DataFrame:
    """日線不足時以 4 日棒近似 4h 級別趨勢（僅供 UI，非真實盤中 4h）。"""
    if hist is None or hist.empty:
        return hist
    df = hist.copy()
    try:
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df = df.tz_convert(None)
    except Exception:
        pass
    return df.resample("4D").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])


def _add_technical_columns(hist: pd.DataFrame) -> pd.DataFrame:
    if hist is None or hist.empty:
        return hist
    h = hist.copy()
    h["MA20"] = h["Close"].rolling(20).mean()
    h["MA60"] = h["Close"].rolling(60).mean()
    delta = h["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, math.nan)
    h["RSI"] = 100 - (100 / (1 + rs))
    ema12 = h["Close"].ewm(span=12, adjust=False).mean()
    ema26 = h["Close"].ewm(span=26, adjust=False).mean()
    h["MACD"] = ema12 - ema26
    h["MACD_SIGNAL"] = h["MACD"].ewm(span=9, adjust=False).mean()
    return h


def get_quote_data(symbol: str, market: str = "stock"):
    raw_symbol = str(symbol).strip().upper()
    market = str(market).strip().lower()

    if market == "crypto":
        try:
            return build_crypto_quote_data(raw_symbol)
        except Exception as e:
            print("WARN get_quote_data crypto:", repr(e))
            sym = normalize_crypto_symbol(raw_symbol)
            return {
                "symbol": sym,
                "name": sym,
                "currency": "USDT",
                "exchange": None,
                "price": 0.0,
                "previous_close": None,
                "change": None,
                "change_percent": None,
            }

    try:
        return _get_quote_data_stock(raw_symbol, market)
    except Exception as e:
        print("WARN get_quote_data:", repr(e))
        stock_symbol = normalize_stock_symbol(raw_symbol)
        return {
            "symbol": stock_symbol,
            "name": stock_symbol,
            "currency": "TWD" if stock_symbol.endswith(".TW") else None,
            "exchange": None,
            "price": 0.0,
            "previous_close": None,
            "change": None,
            "change_percent": None,
        }


def _get_quote_data_stock(raw_symbol: str, _market: str) -> Dict[str, Any]:
    stock_symbol = normalize_stock_symbol(raw_symbol)
    bundle = get_cached_data(stock_symbol)
    prov = bundle.get("provider")
    if bundle.get("ok"):
        print(f"[quote] hist provider={prov} symbol={stock_symbol}")
    else:
        print(f"[quote] hist failed symbol={stock_symbol} err={bundle.get('error')!r}")

    hist = bundle.get("hist") if bundle.get("ok") else None

    current_price = None
    previous_close = None
    tw_name = None
    is_tw = stock_symbol.endswith((".TW", ".TWO"))

    # 台股：優先 MIS（證交所官方即時），再以日線收盤補足
    if is_tw:
        try:
            snap = fetch_tw_mis_snapshot(stock_symbol)
            if snap:
                current_price = safe_float(snap.get("price"))
                previous_close = safe_float(snap.get("previous_close"))
                tw_name = snap.get("name")
        except Exception:
            pass

    if hist is not None and not hist.empty and "Close" in hist.columns:
        cs = hist["Close"].dropna()
        if len(cs) >= 1 and current_price is None:
            current_price = safe_float(cs.iloc[-1])
        if len(cs) >= 2 and previous_close is None:
            previous_close = safe_float(cs.iloc[-2])
        elif len(cs) == 1 and previous_close is None:
            previous_close = current_price

    code = stock_symbol.replace(".TW", "").replace(".TWO", "").strip()
    if is_tw and code.isdigit():
        display_name = resolve_tw_display_name(code, fallback_zh=tw_name)
    else:
        display_name = tw_name or stock_symbol

    if current_price is None:
        return {
            "symbol": stock_symbol,
            "name": display_name,
            "currency": "TWD" if is_tw else None,
            "exchange": None,
            "price": 0.0,
            "previous_close": previous_close,
            "change": None,
            "change_percent": None,
        }

    change = None
    change_percent = None
    if previous_close not in (None, 0):
        change = round(current_price - previous_close, 4)
        change_percent = round((change / previous_close) * 100, 4)

    return {
        "symbol": stock_symbol,
        "name": display_name,
        "currency": "TWD" if is_tw else None,
        "exchange": None,
        "price": round(current_price, 4),
        "previous_close": round(previous_close, 4) if previous_close is not None else None,
        "change": change,
        "change_percent": change_percent,
    }


def get_detail_data(symbol: str, market: str = "stock"):
    raw_symbol = str(symbol).strip().upper()
    market = str(market).strip().lower()

    if market == "crypto":
        try:
            quote = build_crypto_quote_data(raw_symbol)
        except Exception as e:
            print("WARN get_detail_data crypto:", repr(e))
            sym = normalize_crypto_symbol(raw_symbol)
            return {
                "symbol": sym,
                "raw_symbol": raw_symbol,
                "name": raw_symbol,
                "market": "海外/其他",
                "industry": "Cryptocurrency",
                "sector": "Crypto",
                "price": None,
                "change": None,
                "change_percent": None,
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
                "currency": "USDT",
                "exchange": None,
                "interval": "1d",
                "fetch_interval": "1d",
                "period": "1y",
                "data_quality": "無資料",
                "errors": [],
            }
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

    try:
        stock_symbol = normalize_stock_symbol(raw_symbol)
        bundle = get_cached_data(stock_symbol)
        hist = bundle.get("hist") if bundle.get("ok") else None

        quote = get_quote_data(symbol, market)
        market_label = "台股/櫃買" if stock_symbol.endswith((".TW", ".TWO")) or raw_symbol.isdigit() else "海外/其他"

        hi = safe_float(hist["High"].max()) if hist is not None and not hist.empty and "High" in hist.columns else None
        lo = safe_float(hist["Low"].min()) if hist is not None and not hist.empty and "Low" in hist.columns else None

        quality = "基本"
        if hi is not None and lo is not None:
            quality = "部分"

        errors: List[str] = []
        if not bundle.get("ok"):
            err = bundle.get("error")
            if err:
                errors.append(str(err))

        code = stock_symbol.replace(".TW", "").replace(".TWO", "").strip()
        is_tw = stock_symbol.endswith((".TW", ".TWO")) or raw_symbol.isdigit()
        fund = fetch_tw_fundamentals_finmind(code) if is_tw and code.isdigit() else {"pe": None, "pb": None, "eps": None}
        pe = fund.get("pe")
        pb = fund.get("pb")
        eps = fund.get("eps")
        qn = quote.get("name") or stock_symbol
        if is_tw and code.isdigit():
            tw_fb = qn if qn and qn != stock_symbol else None
            detail_name = resolve_tw_display_name(code, fallback_zh=tw_fb)
            valuation_val: Optional[str] = valuation_label(
                pe=pe,
                pb=pb,
                eps=eps,
                price=quote.get("price"),
                lang="zh",
            )
        else:
            detail_name = qn
            valuation_val = None

        return {
            "symbol": stock_symbol,
            "raw_symbol": raw_symbol,
            "name": detail_name,
            "market": market_label,
            "industry": "N/A",
            "sector": "N/A",
            "display_industry": "N/A",
            "price": quote.get("price"),
            "change": quote.get("change"),
            "change_percent": quote.get("change_percent"),
            "market_cap": None,
            "fifty_two_week_high": hi,
            "fifty_two_week_low": lo,
            "pe": pe,
            "pb": pb,
            "eps": eps,
            "roe": None,
            "gross": None,
            "revenue": None,
            "debt": None,
            "valuation": valuation_val,
            "currency": quote.get("currency"),
            "exchange": quote.get("exchange"),
            "interval": "1d",
            "fetch_interval": "1d",
            "period": "3mo",
            "data_quality": quality,
            "errors": errors,
        }
    except Exception as e:
        print("WARN get_detail_data:", repr(e))
        sym = normalize_stock_symbol(raw_symbol)
        mlabel = "台股/櫃買" if sym.endswith((".TW", ".TWO")) or raw_symbol.isdigit() else "海外/其他"
        return {
            "symbol": sym,
            "raw_symbol": raw_symbol,
            "name": sym,
            "market": mlabel,
            "industry": "N/A",
            "sector": "N/A",
            "display_industry": "N/A",
            "price": None,
            "change": None,
            "change_percent": None,
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
            "currency": None,
            "exchange": None,
            "interval": "1d",
            "fetch_interval": "1d",
            "period": "3mo",
            "data_quality": "無資料",
            "errors": [NEUTRAL_DATA_ERROR],
        }


def get_peer_symbols(symbol: str, market: str, max_peers: int = 5) -> List[str]:
    raw = str(symbol).strip().upper()
    mkt = str(market).strip().upper()

    if mkt == "TW":
        universe = get_tw_universe("TOP30")
    elif mkt == "US":
        universe = get_us_universe("TOP30")
    else:
        return []

    symbols = [s if isinstance(s, str) else str(s.get("symbol", s)) for s in universe]
    base = raw.replace(".TW", "").replace(".TWO", "").split(".")[0]
    filtered = [s for s in symbols if str(s).replace(".TW", "").replace(".TWO", "").split(".")[0] != base]
    return filtered[:max_peers]


def map_chart_interval_to_bybit(interval: str) -> str:
    mapping = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
        "1d": "D", "1w": "W", "1wk": "W", "1mo": "M",
    }
    return mapping.get(interval, "D")


def build_crypto_chart_data(symbol: str, interval: str, period: str):
    symbol = normalize_crypto_symbol(symbol)
    bybit_interval = map_chart_interval_to_bybit(interval)
    try:
        df, _exch = get_crypto_kline_with_fallback(symbol, bybit_interval, 200)
    except Exception:
        return MarketChartResponse(symbol=symbol, interval=interval, period=period, candles=[])

    candles = []
    for idx, row in df.iterrows():
        t = pd.Timestamp(idx)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        time_str = t.isoformat()
        open_price = safe_float(row.get("Open"))
        high_price = safe_float(row.get("High"))
        low_price = safe_float(row.get("Low"))
        close_price = safe_float(row.get("Close"))
        volume = safe_float(row.get("Volume"))
        if None in (open_price, high_price, low_price, close_price):
            continue
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
    return MarketChartResponse(symbol=symbol, interval=interval, period=period, candles=candles)


def build_crypto_market_data(symbol: str, interval: str = "1d") -> dict:
    symbol = normalize_crypto_symbol(symbol)
    bybit_interval = map_chart_interval_to_bybit(interval)
    try:
        df, _exch = get_crypto_kline_with_fallback(symbol, bybit_interval, 200)
    except Exception:
        return {
            "raw_symbol": symbol,
            "name": symbol,
            "market": "CRYPTO",
            "price": None,
            "support": None,
            "resistance": None,
            "hist": pd.DataFrame(),
        }
    df = df.reset_index(drop=True)
    if df.empty:
        return {
            "raw_symbol": symbol,
            "name": symbol,
            "market": "CRYPTO",
            "price": None,
            "support": None,
            "resistance": None,
            "hist": pd.DataFrame(),
        }
    df = _add_technical_columns(df)
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
        "pe": None,
        "pb": None,
        "hist": df,
    }


def get_chart_data(symbol: str, interval: str, period: str):
    raw_symbol = str(symbol).strip().upper()
    if is_crypto_symbol(raw_symbol):
        return build_crypto_chart_data(raw_symbol, interval, period)

    try:
        return _get_chart_data_stock(raw_symbol, interval, period)
    except Exception as e:
        print("WARN get_chart_data:", repr(e))
        sym = normalize_stock_symbol(raw_symbol)
        return MarketChartResponse(symbol=sym, interval=interval, period=period, candles=[])


def _get_chart_data_stock(raw_symbol: str, interval: str, period: str) -> MarketChartResponse:
    sym = normalize_stock_symbol(raw_symbol)
    bundle = get_cached_data(sym)
    hist = bundle.get("hist") if bundle.get("ok") else None
    if hist is None or hist.empty:
        return MarketChartResponse(symbol=sym, interval=interval, period=period, candles=[])

    hist = _slice_hist_by_period(hist, period)

    if interval == "4h":
        # 僅使用快取日線衍生，不再額外呼叫 yfinance（避免 rate limit）
        hist = _resample_daily_to_4d(hist)
    elif interval == "1wk":
        hist = _resample_daily_to_weekly(hist)
    elif interval == "1h":
        hist = hist.tail(30)

    if hist is None or hist.empty:
        return MarketChartResponse(symbol=sym, interval=interval, period=period, candles=[])

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
    return MarketChartResponse(symbol=sym, interval=interval, period=period, candles=candles)


def get_market_data(symbol: str, market: str = "US", interval: str = "1d", period: str | None = None) -> dict:
    raw_symbol = str(symbol).strip().upper()
    market_upper = str(market).strip().upper()

    try:
        if market_upper == "CRYPTO":
            return build_crypto_market_data(raw_symbol, interval)

        if market_upper == "TW":
            yf_symbol = normalize_stock_symbol(raw_symbol)
        else:
            yf_symbol = raw_symbol

        bundle = get_cached_data(yf_symbol)
        hist = bundle.get("hist") if bundle.get("ok") else None

        if hist is None or hist.empty:
            return {
                "raw_symbol": raw_symbol,
                "name": raw_symbol,
                "market": market_upper,
                "price": None,
                "support": None,
                "resistance": None,
                "pe": None,
                "pb": None,
                "eps": None,
                "hist": pd.DataFrame(),
            }

        if interval == "1d":
            _period = period or "6mo"
            hist = _slice_hist_by_period(hist, _period if _period in ("1mo", "3mo", "6mo", "1y", "2y") else "6mo")
        elif interval == "1wk":
            hist = _slice_hist_by_period(hist, "2y")
            hist = _resample_daily_to_weekly(hist)
        elif interval == "4h":
            hist = _slice_hist_by_period(hist, "3mo")
            hist = _resample_daily_to_4d(hist)
        elif interval == "1h":
            hist = hist.tail(40)
        else:
            hist = _slice_hist_by_period(hist, "6mo")

        hist = _add_technical_columns(hist)

        if hist is None or hist.empty:
            return {
                "raw_symbol": raw_symbol,
                "name": raw_symbol,
                "market": market_upper,
                "price": None,
                "support": None,
                "resistance": None,
                "pe": None,
                "pb": None,
                "eps": None,
                "hist": pd.DataFrame(),
            }

        latest_close = safe_float(hist["Close"].iloc[-1])
        recent = hist.tail(20)
        support = safe_float(recent["Low"].min()) if not recent.empty else None
        resistance = safe_float(recent["High"].max()) if not recent.empty else None

        pe_v: Optional[float] = None
        pb_v: Optional[float] = None
        eps_v: Optional[float] = None
        display_name: str = raw_symbol

        if market_upper == "TW":
            code = yf_symbol.replace(".TW", "").replace(".TWO", "").strip()
            fund = fetch_tw_fundamentals_finmind(code)
            pe_v = fund.get("pe")
            pb_v = fund.get("pb")
            eps_v = fund.get("eps")
            snap_name = None
            try:
                snap = fetch_tw_mis_snapshot(yf_symbol)
                if snap:
                    snap_name = snap.get("name")
            except Exception:
                pass
            display_name = resolve_tw_display_name(code, fallback_zh=snap_name)

        return {
            "raw_symbol": raw_symbol,
            "name": display_name,
            "market": market_upper,
            "price": latest_close,
            "support": support,
            "resistance": resistance,
            "pe": pe_v,
            "pb": pb_v,
            "eps": eps_v,
            "hist": hist,
        }
    except Exception as e:
        print("WARN get_market_data:", repr(e))
        return {
            "raw_symbol": raw_symbol,
            "name": raw_symbol,
            "market": market_upper,
            "price": None,
            "support": None,
            "resistance": None,
            "pe": None,
            "pb": None,
            "eps": None,
            "hist": pd.DataFrame(),
        }


def get_multi_timeframe_summary(symbol: str, market: str = "US", lang: str = "zh") -> List[Dict[str, Any]]:
    from app.services.technical_service import trend_score, trend_label

    intervals = [("1h", "1mo"), ("4h", "2mo"), ("1d", "6mo"), ("1wk", "2y")]
    rows = []
    for iv, _per in intervals:
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
    from app.services.technical_service import generate_signal_table

    data = get_market_data(symbol=symbol, market=market, interval="1d")
    hist = data.get("hist")
    support = data.get("support")
    resistance = data.get("resistance")
    if hist is None or hist.empty:
        return []
    try:
        return generate_signal_table(hist, support, resistance, lang=lang)
    except Exception:
        return []


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
        symbols = get_crypto_universe()
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
            if hist is None or hist.empty:
                continue
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
