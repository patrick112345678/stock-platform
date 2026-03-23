"""
股票資料統一入口：同一 symbol 只打一次 Yahoo（history），並以記憶體快取 60 秒。

* 不使用 requests.Session 傳入 yfinance（避免相容性問題）。
* 以 ThreadPoolExecutor 限制 yfinance 單次最長等待時間，避免 API 卡死。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Dict, Tuple

import pandas as pd
import yfinance as yf

try:
    from yfinance.exceptions import YFDataException
except Exception:
    YFDataException = Exception  # type: ignore[misc, assignment]

# 每個 symbol 快取 60 秒（key = yfinance 代號，如 2330.TW、AAPL）
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 60
YFINANCE_FETCH_TIMEOUT = 5.0


def _yf_history_only(yf_symbol: str) -> pd.DataFrame:
    """
    僅呼叫 yf.Ticker(symbol).history，禁止傳入 requests.Session / 自訂 session。
    """
    ticker = yf.Ticker(yf_symbol)
    return ticker.history(period="3mo", interval="1d", auto_adjust=False)


def get_stock_data(yf_symbol: str) -> Dict[str, Any]:
    """別名：與規格「get_stock_data(symbol)」一致（無快取，單次抓取）。"""
    return fetch_stock_data(yf_symbol)


def fetch_stock_data(yf_symbol: str) -> Dict[str, Any]:
    """
    單次取得股票 history（約 3 個月日線），失敗時 ok=False。
    不應在路由層直接重複呼叫；請改用 get_cached_stock_data。
    """
    key = str(yf_symbol).strip().upper()
    try:

        def _run() -> pd.DataFrame:
            return _yf_history_only(key)

        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run)
            df = fut.result(timeout=YFINANCE_FETCH_TIMEOUT)
    except FuturesTimeout:
        return {
            "ok": False,
            "symbol": key,
            "hist": None,
            "error": "yfinance_timeout",
        }
    except (YFDataException, Exception) as e:
        err_msg = str(e)
        if "session" in err_msg.lower() or "curl_cffi" in err_msg.lower():
            err_msg = "yfinance_session_error"
        return {
            "ok": False,
            "symbol": key,
            "hist": None,
            "error": err_msg,
        }

    if df is None or df.empty:
        return {
            "ok": False,
            "symbol": key,
            "hist": None,
            "error": "empty_history",
        }

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]

    return {
        "ok": True,
        "symbol": key,
        "hist": df,
        "error": None,
    }


def get_cached_stock_data(yf_symbol: str) -> Dict[str, Any]:
    """
    帶 60 秒記憶體快取的資料取得；quote / detail / chart / 技術分析共用。
    """
    key = str(yf_symbol).strip().upper()
    now = time.monotonic()
    if key in _CACHE:
        ts, payload = _CACHE[key]
        if now - ts < CACHE_TTL_SECONDS:
            out = dict(payload)
            out["from_cache"] = True
            return out

    payload = fetch_stock_data(key)
    payload["from_cache"] = False
    _CACHE[key] = (now, payload)
    return payload


def get_cached_data(yf_symbol: str) -> Dict[str, Any]:
    """別名，與規格文件一致。"""
    return get_cached_stock_data(yf_symbol)


def clear_stock_cache(symbol: str | None = None) -> None:
    """測試或管理用：清除快取。"""
    global _CACHE
    if symbol is None:
        _CACHE = {}
    else:
        _CACHE.pop(str(symbol).strip().upper(), None)
