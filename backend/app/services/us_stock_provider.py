"""
美股日線：目前唯一實作為 Yahoo Finance（yfinance）。

後續若要「主源改 Finnhub / Alpha Vantage、Yahoo 降為備援」，請在此檔新增主源 fetch，
並讓 `fetch_us_history_yahoo_bounded` 僅在主源失敗時呼叫。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any

import pandas as pd
import yfinance as yf

try:
    from yfinance.exceptions import YFDataException
except Exception:
    YFDataException = Exception  # type: ignore[misc, assignment]

YFINANCE_FETCH_TIMEOUT = 5.0


def fetch_us_history_yahoo(yf_symbol: str) -> pd.DataFrame:
    """僅美股 ticker（如 AAPL），勿傳台股代號。"""
    ticker = yf.Ticker(str(yf_symbol).strip())
    return ticker.history(period="3mo", interval="1d", auto_adjust=False)


def fetch_us_history_yahoo_bounded(yf_symbol: str) -> tuple[pd.DataFrame | None, str | None]:
    """
    有執行緒逾時保護。成功回傳 (df, None)，失敗回傳 (None, error_code_or_msg)。
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
        err_msg = str(e)
        if "session" in err_msg.lower() or "curl_cffi" in err_msg.lower():
            err_msg = "yfinance_session_error"
        return None, err_msg

    if df is None or df.empty:
        return None, "empty_history"

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]
    return df, None


__all__ = ["fetch_us_history_yahoo_bounded", "fetch_us_history_yahoo"]
