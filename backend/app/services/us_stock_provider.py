"""
美股日線：Yahoo Finance（yfinance）僅允許在此檔呼叫，並抑制函式庫印到 stderr 的雜訊（如 possibly delisted）。
後續若要改主源為 Finnhub 等，請替換 fetch 實作並維持回傳 pandas DataFrame。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import pandas as pd
import yfinance as yf

try:
    from yfinance.exceptions import YFDataException
except Exception:
    YFDataException = Exception  # type: ignore[misc, assignment]

YFINANCE_FETCH_TIMEOUT = 5.0

_YF_LOGGERS = (
    "yfinance",
    "yfinance.base",
    "yfinance.ticker",
    "yfinance.scrapers",
    "yfinance.data",
    "peewee",
)


def _silence_yfinance_loggers() -> None:
    for name in _YF_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.CRITICAL)
        lg.disabled = True


def _sanitize_yahoo_exception_message(msg: str) -> str:
    """不將原生 Yahoo／yfinance 字串回傳給上層，避免 log 出現 possibly delisted 等。"""
    s = (msg or "").lower()
    if "delist" in s or "possibly delisted" in s:
        return "yahoo_quote_unavailable"
    if "quote not found" in s or "404" in s:
        return "yahoo_quote_unavailable"
    if "session" in s or "curl_cffi" in s:
        return "yfinance_session_error"
    return (msg or "")[:300]


def fetch_us_history_yahoo(yf_symbol: str) -> pd.DataFrame:
    """僅美股 ticker（如 AAPL）；台股備援時傳 2330.TW 亦走此函式，但 stderr 已抑制。"""
    _silence_yfinance_loggers()
    sym = str(yf_symbol).strip()
    if os.getenv("YFINANCE_DEBUG", "").lower() in ("1", "true", "yes"):
        ticker = yf.Ticker(sym)
        return ticker.history(period="3mo", interval="1d", auto_adjust=False)
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        ticker = yf.Ticker(sym)
        return ticker.history(period="3mo", interval="1d", auto_adjust=False)


def fetch_us_history_yahoo_bounded(yf_symbol: str) -> tuple[pd.DataFrame | None, str | None]:
    """
    有執行緒逾時保護。成功回傳 (df, None)，失敗回傳 (None, 簡短代碼) — 不含 yfinance 原文。
    """
    key = str(yf_symbol).strip().upper()
    try:

        def _run() -> pd.DataFrame:
            return fetch_us_history_yahoo(key)

        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run)
            df = fut.result(timeout=YFINANCE_FETCH_TIMEOUT)
    except FuturesTimeout:
        return None, "yfinance_timeout"
    except (YFDataException, Exception) as e:
        return None, _sanitize_yahoo_exception_message(str(e))

    if df is None or df.empty:
        return None, "empty_history"

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]
    return df, None


__all__ = ["fetch_us_history_yahoo_bounded", "fetch_us_history_yahoo"]
