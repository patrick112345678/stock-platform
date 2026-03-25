# app/services/market_service.py
# 行情內部實作：
# - 台股：日線 FinMind→Yahoo→（可選）TWSE；即時 MIS；基本面多 dataset（見 fundamental_provider）。
# - 美股：Yahoo（us_stock_provider，可替換為 Finnhub 等）。
# - 加密：預設 Binance 主來源，Bybit 可選備援（見 CRYPTO_* 環境變數）。

import math
import os
import time

import pandas as pd
import requests
from fastapi import HTTPException
from typing import List, Dict, Any, Optional, Literal, Tuple

from app.schemas.market import MarketCandleItem, MarketChartResponse
from app.services.stock_data_service import get_cached_stock_data, get_cached_data, NEUTRAL_DATA_ERROR
from app.services.fundamental_provider import (
    format_tw_display_name,
    resolve_tw_display_name,
    strip_tw_trailing_code_in_name,
    tw_percent_display_to_api_ratio,
)
from app.services.stock_fundamental_service import get_tw_fundamental_bundle_db_only
from app.services.technical_service import valuation_label
from app.services.market_data_cache_service import (
    get_or_refresh_crypto_kline_df,
    get_or_refresh_crypto_quote,
    get_or_refresh_stock_hist_df,
    get_or_refresh_stock_quote,
    try_read_fresh_stock_hist_from_chart_cache,
)
from app.services.scanner_service import (
    get_crypto_kline_with_fallback,
    get_tw_symbol_to_chinese_only,
    get_tw_universe,
    get_us_universe,
    get_crypto_universe,
)
from app.services.crypto_provider_health import (
    is_exchange_down,
    log_crypto_throttled,
    mark_exchange_down,
    note_exchange_success,
)

BYBIT_BASE_URL = "https://api.bybit.com"
BINANCE_API_BASE = "https://api.binance.com/api/v3"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockPlatform/1.0)",
    "Accept": "application/json",
}
EXTERNAL_REQUEST_TIMEOUT = 5.0

# 台股 MIS 即時報價：短 TTL，減少與 hist 同次請求重複打證交所
_mis_snap_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _tw_mis_cache_ttl() -> float:
    try:
        return max(1.0, float(os.getenv("TW_MIS_CACHE_TTL_SECONDS", "30")))
    except ValueError:
        return 30.0


CMC_API_KEY = os.getenv("CMC_API_KEY")
PoolSize = Literal["TOP100", "TOP800", "ALL"]


def _tw_fundamental_sections_api(
    fund: dict[str, Any], pe: Any, pb: Any, eps: Any
) -> tuple[dict[str, Any], float, dict[str, Any]]:
    """
    估值／獲利／成長／風險分類與完整度。
    ROE／毛利率／營收年增／負債比 以 tw_percent_display_to_api_ratio 輸出小數比例，避免前端再 *100 時放大 100 倍。
    """
    pr = {
        "roe": tw_percent_display_to_api_ratio(fund.get("roe")),
        "gross_margin": tw_percent_display_to_api_ratio(fund.get("gross_margin")),
        "revenue_growth_yoy": tw_percent_display_to_api_ratio(fund.get("revenue_growth_yoy")),
        "debt_ratio": tw_percent_display_to_api_ratio(fund.get("debt_ratio")),
    }
    sections = {
        "valuation": {"pe": pe, "pb": pb},
        "profitability": {"eps": eps, "roe": pr["roe"], "gross_margin": pr["gross_margin"]},
        "growth": {"revenue_growth_yoy": pr["revenue_growth_yoy"]},
        "risk": {"debt_ratio": pr["debt_ratio"]},
    }
    keys = [pe, pb, eps, pr["roe"], pr["gross_margin"], pr["revenue_growth_yoy"], pr["debt_ratio"]]
    n = sum(1 for x in keys if x is not None)
    completeness = round(100.0 * n / 7.0, 1)
    return sections, completeness, pr


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
    mis_key = f"{code}_mis_quote"
    now = time.monotonic()
    hit = _mis_snap_cache.get(mis_key)
    if hit is not None:
        ts, snap = hit
        if now - ts < _tw_mis_cache_ttl():
            print(f"CACHE HIT: {code} (mis)")
            return dict(snap)
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
            out = {
                "price": price,
                "previous_close": y,
                "name": str(name).strip() if name else None,
            }
            _mis_snap_cache[mis_key] = (now, out)
            return out
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
    if is_exchange_down("binance"):
        raise ValueError("binance_quote_cooldown")
    r = requests.get(
        f"{BINANCE_API_BASE}/ticker/24hr",
        params={"symbol": symbol},
        timeout=EXTERNAL_REQUEST_TIMEOUT,
        headers=REQUEST_HEADERS,
    )
    if r.status_code in (403, 451, 418):
        mark_exchange_down("binance")
        log_crypto_throttled(
            f"binance:ticker24h:{r.status_code}",
            f"[crypto] BINANCE HTTP {r.status_code} ticker/24hr symbol={symbol}",
        )
    r.raise_for_status()
    t = r.json()
    lp = safe_float(t.get("lastPrice"))
    op = safe_float(t.get("openPrice"))
    pcp = safe_float(t.get("priceChangePercent"))
    frac = (pcp / 100.0) if pcp is not None else None
    note_exchange_success("binance")
    return {
        "lastPrice": str(lp) if lp is not None else None,
        "prevPrice24h": str(op) if op is not None else None,
        "price24hPcnt": str(frac) if frac is not None else None,
        "_exchange": "BINANCE",
    }


def get_ticker_bybit(symbol: str) -> dict:
    """Bybit 現貨 ticker（備援用）。"""
    if is_exchange_down("bybit"):
        raise ValueError("bybit_quote_cooldown")
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "spot", "symbol": symbol}
    resp = requests.get(url, params=params, timeout=EXTERNAL_REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
    if resp.status_code == 403:
        mark_exchange_down("bybit")
        log_crypto_throttled(
            "bybit:ticker:403",
            f"[crypto] BYBIT HTTP 403 ticker symbol={symbol}",
        )
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise ValueError(f"Bybit retCode: {data.get('retCode')}")
    items = data.get("result", {}).get("list", [])
    if not items:
        raise ValueError("Bybit empty list")
    row = dict(items[0])
    row["_exchange"] = "BYBIT"
    note_exchange_success("bybit")
    return row


def get_crypto_ticker_row(symbol: str) -> dict:
    """預設 Binance 主來源，避免先打 Bybit 再 403；Bybit 可選備援。"""
    primary = os.getenv("CRYPTO_QUOTE_PRIMARY", "binance").strip().lower()
    fallback_on = os.getenv("CRYPTO_ENABLE_BYBIT_FALLBACK", "true").lower() in ("1", "true", "yes", "on")

    if primary == "bybit":
        if not is_exchange_down("bybit"):
            try:
                return get_ticker_bybit(symbol)
            except Exception as e:
                log_crypto_throttled(
                    "quote:bybit_primary",
                    f"[crypto] BYBIT quote primary fail {symbol} -> BINANCE: {type(e).__name__}",
                )
        if not is_exchange_down("binance"):
            try:
                row = _binance_ticker_row(symbol)
                row["_exchange"] = "BINANCE"
                return row
            except Exception as e2:
                raise HTTPException(
                    status_code=404,
                    detail="加密貨幣報價暫時無法取得（Bybit 與 Binance 皆失敗），請稍後再試。",
                ) from e2
        raise HTTPException(
            status_code=404,
            detail="加密貨幣報價暫時無法取得，請稍後再試。",
        )

    if not is_exchange_down("binance"):
        try:
            row = _binance_ticker_row(symbol)
            row["_exchange"] = "BINANCE"
            return row
        except Exception as e:
            log_crypto_throttled(
                "quote:binance_primary",
                f"[crypto] BINANCE quote primary fail {symbol}: {type(e).__name__}",
            )
    if fallback_on and not is_exchange_down("bybit"):
        try:
            return get_ticker_bybit(symbol)
        except Exception as e2:
            raise HTTPException(
                status_code=404,
                detail="加密貨幣報價暫時無法取得（Binance 與 Bybit 皆失敗），請稍後再試。",
            ) from e2
    raise HTTPException(
        status_code=404,
        detail="加密貨幣報價暫時無法取得，請稍後再試。",
    )


def build_crypto_quote_data(symbol: str):
    symbol = normalize_crypto_symbol(symbol)
    try:
        item = dict(get_crypto_ticker_row(symbol))
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
            "volume": None,
        }
    exch = item.pop("_exchange", "BYBIT")
    last_price = safe_float(item.get("lastPrice"))
    prev_price_24h = safe_float(item.get("prevPrice24h"))
    price_24h_pcnt = safe_float(item.get("price24hPcnt"))
    vol = safe_float(item.get("volume24h") or item.get("turnover24h"))
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
        "volume": vol,
    }


def _slice_hist_by_period(hist: pd.DataFrame, period: str) -> pd.DataFrame:
    days = {"1mo": 24, "3mo": 66, "6mo": 132, "1y": 252, "2y": 504}.get(period, 66)
    if hist is None or hist.empty:
        return hist
    return hist.tail(days) if len(hist) > days else hist


def _prepare_hist_for_interval(
    hist: pd.DataFrame,
    market_upper: str,
    interval: str,
    period: str | None,
) -> pd.DataFrame:
    """與 get_market_data 相同切片／重取樣邏輯，供 multi-timeframe 單次載入日線後重複使用。"""
    if hist is None or hist.empty:
        return hist
    if market_upper == "CRYPTO":
        return hist
    if interval == "1d":
        _period = period or "6mo"
        return _slice_hist_by_period(hist, _period if _period in ("1mo", "3mo", "6mo", "1y", "2y") else "6mo")
    if interval == "1wk":
        h = _slice_hist_by_period(hist, "2y")
        return _resample_daily_to_weekly(h)
    if interval == "4h":
        h = _slice_hist_by_period(hist, "3mo")
        return _resample_daily_to_4d(h)
    if interval == "1h":
        return hist.tail(40)
    return _slice_hist_by_period(hist, "6mo")


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
            sym = normalize_crypto_symbol(raw_symbol)
            return get_or_refresh_crypto_quote(sym, lambda: build_crypto_quote_data(raw_symbol))
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
        stock_symbol = normalize_stock_symbol(raw_symbol)
        mkt = detect_stock_market(stock_symbol)
        return get_or_refresh_stock_quote(
            stock_symbol,
            mkt,
            lambda: _fetch_quote_stock_live(raw_symbol, market),
        )
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


def _fetch_hist_bundle_for_symbol(yf_symbol: str) -> Optional[pd.DataFrame]:
    bundle = get_cached_data(yf_symbol)
    return bundle.get("hist") if bundle.get("ok") else None


def _fetch_quote_stock_live(
    raw_symbol: str,
    _market: str,
    hist_preload: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    stock_symbol = normalize_stock_symbol(raw_symbol)
    mkt = detect_stock_market(stock_symbol)
    hist = hist_preload
    bundle = None
    if hist is None:
        hist = try_read_fresh_stock_hist_from_chart_cache(stock_symbol, mkt)
    if hist is None:
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

    vol = None
    if hist is not None and not hist.empty and "Volume" in hist.columns:
        vol = safe_float(hist["Volume"].iloc[-1])

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
            "volume": vol,
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
        "volume": vol,
    }


def get_detail_data(symbol: str, market: str = "stock"):
    raw_symbol = str(symbol).strip().upper()
    market = str(market).strip().lower()

    if market == "crypto":
        try:
            sym_c = normalize_crypto_symbol(raw_symbol)
            quote = get_or_refresh_crypto_quote(sym_c, lambda: build_crypto_quote_data(raw_symbol))
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
        mkt = str(market).strip().upper()
        if mkt not in ("TW", "US"):
            mkt = detect_stock_market(stock_symbol)
        hist = get_or_refresh_stock_hist_df(
            stock_symbol,
            mkt,
            lambda: _fetch_hist_bundle_for_symbol(stock_symbol),
        )

        quote = get_or_refresh_stock_quote(
            stock_symbol,
            mkt,
            lambda: _fetch_quote_stock_live(raw_symbol, market, hist_preload=hist),
        )
        market_label = "台股/櫃買" if stock_symbol.endswith((".TW", ".TWO")) or raw_symbol.isdigit() else "海外/其他"

        hi = safe_float(hist["High"].max()) if hist is not None and not hist.empty and "High" in hist.columns else None
        lo = safe_float(hist["Low"].min()) if hist is not None and not hist.empty and "Low" in hist.columns else None

        quality = "基本"
        if hi is not None and lo is not None:
            quality = "部分"

        errors: List[str] = []
        if hist is None or hist.empty:
            errors.append(NEUTRAL_DATA_ERROR)

        code = stock_symbol.replace(".TW", "").replace(".TWO", "").strip()
        is_tw = stock_symbol.endswith((".TW", ".TWO")) or raw_symbol.isdigit()
        qn = quote.get("name") or stock_symbol
        fund = None
        fundamental: Optional[dict[str, Any]] = None
        fund_pct: Optional[float] = None
        page_title: Optional[str] = None
        pr: dict[str, Any] = {
            "roe": None,
            "gross_margin": None,
            "revenue_growth_yoy": None,
            "debt_ratio": None,
        }
        if is_tw and code.isdigit():
            fund = get_tw_fundamental_bundle_db_only(stock_symbol)
            tw_fb = qn if qn and qn != stock_symbol else None
            zh = fund.get("stock_name_zh")
            if zh:
                raw_disp = str(fund.get("display_name") or "").strip()
                base = raw_disp or str(zh).strip()
                detail_name = strip_tw_trailing_code_in_name(base, code)
            else:
                cn_map = get_tw_symbol_to_chinese_only()
                rn = cn_map.get(code) or tw_fb
                if rn and str(rn).strip() and str(rn).strip() != code:
                    detail_name = strip_tw_trailing_code_in_name(str(rn).strip(), code)
                else:
                    detail_name = code
            z_title = str(zh or detail_name or "").strip()
            page_title = format_tw_display_name(z_title if z_title else None, code)
            pe = fund.get("pe")
            pb = fund.get("pb")
            eps = fund.get("eps")
            valuation_val = valuation_label(
                pe=pe,
                pb=pb,
                eps=eps,
                price=quote.get("price"),
                lang="zh",
            )
            ind = fund.get("industry")
            sec, fund_pct, pr = _tw_fundamental_sections_api(fund, pe, pb, eps)
            fundamental = {
                "pe": fund.get("pe"),
                "pb": fund.get("pb"),
                "eps": fund.get("eps"),
                "roe": pr["roe"],
                "gross_margin": pr["gross_margin"],
                "revenue_growth_yoy": pr["revenue_growth_yoy"],
                "debt_ratio": pr["debt_ratio"],
                "valuation": valuation_val,
                "industry": ind,
                "stock_name_zh": zh,
                "display_name": detail_name,
                "sections": sec,
                "completeness_percent": fund_pct,
            }
        else:
            detail_name = qn
            valuation_val = None
            pe = pb = eps = None
            ind = None

        return {
            "symbol": code if is_tw else stock_symbol,
            "symbol_yf": stock_symbol,
            "code": code if is_tw else None,
            "raw_symbol": raw_symbol,
            "name": detail_name,
            "page_title": page_title if is_tw and code.isdigit() else None,
            "name_zh": fund.get("stock_name_zh") if fund and is_tw else None,
            "market": market_label,
            "industry": ind if is_tw else None,
            "sector": ind if is_tw else None,
            "display_industry": ind if is_tw else None,
            "price": quote.get("price"),
            "change": quote.get("change"),
            "change_percent": quote.get("change_percent"),
            "market_cap": None,
            "fifty_two_week_high": hi,
            "fifty_two_week_low": lo,
            "pe": pe,
            "pb": pb,
            "eps": eps,
            "roe": pr["roe"] if fund else None,
            "gross": pr["gross_margin"] if fund else None,
            "revenue": pr["revenue_growth_yoy"] if fund else None,
            "debt": pr["debt_ratio"] if fund else None,
            "valuation": valuation_val,
            "fundamental": fundamental,
            "fundamental_completeness_percent": fund_pct if is_tw and fundamental else None,
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
            "fundamental": None,
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

    def fetch_df():
        try:
            df, _exch = get_crypto_kline_with_fallback(symbol, bybit_interval, 200)
            return df
        except Exception:
            return pd.DataFrame()

    df = get_or_refresh_crypto_kline_df(symbol, interval, fetch_df)
    if df is None or df.empty:
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

    def fetch_df():
        try:
            df, _exch = get_crypto_kline_with_fallback(symbol, bybit_interval, 200)
            return df
        except Exception:
            return pd.DataFrame()

    df = get_or_refresh_crypto_kline_df(symbol, interval, fetch_df)
    if df is None or df.empty:
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
    mkt = detect_stock_market(sym)
    hist = get_or_refresh_stock_hist_df(
        sym,
        mkt,
        lambda: _fetch_hist_bundle_for_symbol(sym),
    )
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

        hist = get_or_refresh_stock_hist_df(
            yf_symbol,
            market_upper,
            lambda: _fetch_hist_bundle_for_symbol(yf_symbol),
        )

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
                "roe": None,
                "gross_margin": None,
                "revenue_growth_yoy": None,
                "debt_ratio": None,
                "industry": None,
                "fundamental": None,
                "hist": pd.DataFrame(),
            }

        hist = _prepare_hist_for_interval(hist, market_upper, interval, period)
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
                "roe": None,
                "gross_margin": None,
                "revenue_growth_yoy": None,
                "debt_ratio": None,
                "industry": None,
                "fundamental": None,
                "hist": pd.DataFrame(),
            }

        latest_close = safe_float(hist["Close"].iloc[-1])
        recent = hist.tail(20)
        support = safe_float(recent["Low"].min()) if not recent.empty else None
        resistance = safe_float(recent["High"].max()) if not recent.empty else None

        pe_v: Optional[float] = None
        pb_v: Optional[float] = None
        eps_v: Optional[float] = None
        roe_v: Optional[float] = None
        gm_v: Optional[float] = None
        rev_yoy_v: Optional[float] = None
        debt_v: Optional[float] = None
        ind_v: Optional[str] = None
        display_name: str = raw_symbol
        fundamental_mkt: Optional[dict[str, Any]] = None

        if market_upper == "TW":
            code = yf_symbol.replace(".TW", "").replace(".TWO", "").strip()
            b = get_tw_fundamental_bundle_db_only(yf_symbol)
            pe_v = b.get("pe")
            pb_v = b.get("pb")
            eps_v = b.get("eps")
            roe_v = b.get("roe")
            gm_v = b.get("gross_margin")
            rev_yoy_v = b.get("revenue_growth_yoy")
            debt_v = b.get("debt_ratio")
            ind_v = b.get("industry")
            snap_name = None
            if b.get("stock_name_zh"):
                display_name = str(b.get("display_name") or "").strip() or resolve_tw_display_name(
                    code, fallback_zh=snap_name
                )
            else:
                display_name = resolve_tw_display_name(code, fallback_zh=snap_name)
            val_l = valuation_label(
                pe=pe_v,
                pb=pb_v,
                eps=eps_v,
                price=latest_close,
                lang="zh",
            )
            fundamental_mkt = {
                "pe": pe_v,
                "pb": pb_v,
                "eps": eps_v,
                "roe": roe_v,
                "gross_margin": gm_v,
                "revenue_growth_yoy": rev_yoy_v,
                "debt_ratio": debt_v,
                "valuation": val_l,
                "industry": ind_v,
                "stock_name_zh": b.get("stock_name_zh"),
                "display_name": display_name,
            }

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
            "roe": roe_v,
            "gross_margin": gm_v,
            "revenue_growth_yoy": rev_yoy_v,
            "debt_ratio": debt_v,
            "industry": ind_v,
            "fundamental": fundamental_mkt,
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
            "roe": None,
            "gross_margin": None,
            "revenue_growth_yoy": None,
            "debt_ratio": None,
            "industry": None,
            "fundamental": None,
            "hist": pd.DataFrame(),
        }


def get_multi_timeframe_summary(symbol: str, market: str = "US", lang: str = "zh") -> List[Dict[str, Any]]:
    from app.services.technical_service import trend_score, trend_label

    intervals = [("1h", "1mo"), ("4h", "2mo"), ("1d", "6mo"), ("1wk", "2y")]
    market_upper = str(market).strip().upper()
    raw_symbol = str(symbol).strip().upper()

    if market_upper == "CRYPTO":
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

    if market_upper == "TW":
        yf_symbol = normalize_stock_symbol(raw_symbol)
    else:
        yf_symbol = raw_symbol

    hist_raw = get_or_refresh_stock_hist_df(
        yf_symbol,
        market_upper,
        lambda: _fetch_hist_bundle_for_symbol(yf_symbol),
    )
    if hist_raw is None or hist_raw.empty:
        return [{"period": iv, "trend": "無資料", "price": "N/A", "rsi": "N/A", "score": "N/A"} for iv, _ in intervals]

    rows = []
    for iv, per in intervals:
        try:
            hist = _prepare_hist_for_interval(hist_raw.copy(), market_upper, iv, per)
            hist = _add_technical_columns(hist)
            if hist is None or hist.empty:
                rows.append({"period": iv, "trend": "無資料", "price": "N/A", "rsi": "N/A", "score": "N/A"})
                continue
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


def _chart_model_to_dict(chart: Any) -> dict[str, Any]:
    if isinstance(chart, dict):
        return chart
    if hasattr(chart, "model_dump"):
        return chart.model_dump()
    if hasattr(chart, "dict"):
        return chart.dict()
    return {"symbol": "", "interval": "", "period": "", "candles": []}


def quote_dict_from_detail(detail: dict[str, Any], raw_symbol: str) -> dict[str, Any]:
    """get_detail_data 已含報價欄位，避免 bundle 再打一輪 get_quote_data。"""
    price = detail.get("price")
    chg = detail.get("change")
    prev = None
    if price is not None and chg is not None:
        try:
            prev = float(price) - float(chg)
        except (TypeError, ValueError):
            prev = None
    sym = str(detail.get("symbol") or raw_symbol).strip().upper()
    name = detail.get("name") or sym
    cp = detail.get("change_percent")
    return {
        "symbol": sym,
        "name": name,
        "currency": detail.get("currency"),
        "exchange": detail.get("exchange"),
        "price": float(price) if price is not None else 0.0,
        "previous_close": prev,
        "change": float(chg) if chg is not None else None,
        "change_percent": float(cp) if cp is not None else None,
    }


def build_selection_bundle(
    symbol: str,
    market: str,
    interval: str,
    period: str,
    lang: str,
) -> dict[str, Any]:
    """
    自選切換用：先 get_detail_data（單次 hist/鎖與報價），再依序 chart / mtf / signal。
    避免 asyncio.to_thread 多執行緒同時搶同一 (symbol,market) 的 hist 鎖造成實質排隊，
    也避免 get_quote + get_detail 重複打行情。
    """
    errors: dict[str, str] = {}
    sym = str(symbol).strip()
    mkt = str(market).strip()

    detail = get_detail_data(sym, mkt)
    quote = quote_dict_from_detail(detail, sym)

    try:
        chart = get_chart_data(sym, interval, period)
        chart_d = _chart_model_to_dict(chart)
    except Exception as e:
        errors["chart"] = str(e)[:200]
        chart_d = None

    try:
        mtf = get_multi_timeframe_summary(symbol=sym, market=mkt, lang=lang)
    except Exception as e:
        errors["multi_timeframe"] = str(e)[:200]
        mtf = None

    try:
        sig = get_technical_signal_table(symbol=sym, market=mkt, lang=lang)
    except Exception as e:
        errors["signal_table"] = str(e)[:200]
        sig = None

    return {
        "quote": quote,
        "detail": detail,
        "chart": chart_d,
        "multi_timeframe": mtf if isinstance(mtf, list) else None,
        "signal_table": sig if isinstance(sig, list) else None,
        "errors": errors or None,
    }
