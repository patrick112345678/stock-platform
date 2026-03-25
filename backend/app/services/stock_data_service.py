"""
股票資料統一入口：依市場分流資料源，並以記憶體快取（預設 60 秒，STOCK_DATA_CACHE_TTL_SECONDS）。

* 台股 TW 日線：FinMind 主用 → Yahoo 備援 → 僅 ENABLE_TWSE_OPENAPI=true 時才嘗試 TWSE（預設關閉，避免 Render SSL/HTML/rate limit）。
* FinMind HTTP 另見 finmind_provider 之 TTLCache（FINMIND_KLINE_CACHE_TTL，預設 120 秒）。
* 台股報價：見 market_service（MIS 優先 + 日線補足）。
* 美股 US：Yahoo（us_stock_provider）。
* Crypto：勿使用本模組。

錯誤訊息統一為中性「資料來源暫時不可用」，不暴露下市相關字樣。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Tuple

import pandas as pd

from app.services.finmind_provider import fetch_tw_daily_history_finmind
from app.services.twse_official_service import (
    fetch_tw_daily_history_official,
    is_twse_openapi_enabled,
)
from app.services.us_stock_provider import fetch_us_history_yahoo_bounded

_log = logging.getLogger(__name__)

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _stock_data_cache_ttl() -> float:
    try:
        return max(5.0, float(os.getenv("STOCK_DATA_CACHE_TTL_SECONDS", "60")))
    except ValueError:
        return 60.0

NEUTRAL_DATA_ERROR = "資料來源暫時不可用"


def _sanitize_public_error(_err: str | None) -> str:
    """對外一律中性訊息，不暴露內部短碼或 Yahoo／yfinance 原文。"""
    return NEUTRAL_DATA_ERROR


def _is_tw_symbol(sym: str) -> bool:
    s = str(sym).strip().upper()
    if s.endswith(".TW") or s.endswith(".TWO"):
        return True
    return s.isdigit() and 2 <= len(s) <= 5


def normalize_tw_yf_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper().replace(".TWO", ".TW")
    if s.endswith(".TW"):
        return s
    if s.isdigit():
        return f"{s}.TW"
    return s


def _normalize_hist_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def fetch_stock_data(yf_symbol: str) -> Dict[str, Any]:
    key = str(yf_symbol).strip().upper()
    if _is_tw_symbol(key):
        return _fetch_tw_stock_data(key)
    return _fetch_us_stock_data(key)


def _fetch_tw_stock_data(raw_key: str) -> Dict[str, Any]:
    """
    台股日線：FinMind → Yahoo →（可選）TWSE OpenAPI。
    """
    canon = normalize_tw_yf_symbol(raw_key)
    code = canon.replace(".TW", "").replace(".TWO", "")

    df_fm, err_fm = fetch_tw_daily_history_finmind(code)
    if df_fm is not None and not df_fm.empty and len(df_fm) >= 2:
        df_fm = _normalize_hist_columns(df_fm)
        _log.info("TW provider=FINMIND symbol=%s rows=%s", canon, len(df_fm))
        return {
            "ok": True,
            "symbol": canon,
            "hist": df_fm,
            "error": None,
            "provider": "FINMIND",
        }

    df_y, err_y = fetch_us_history_yahoo_bounded(canon)
    if df_y is not None and not df_y.empty:
        df_y = _normalize_hist_columns(df_y)
        _log.info(
            "TW provider=YAHOO fallback=YAHOO symbol=%s rows=%s finmind_err=%s",
            canon,
            len(df_y),
            err_fm or "none",
        )
        return {
            "ok": True,
            "symbol": canon,
            "hist": df_y,
            "error": None,
            "provider": "YAHOO_PRIMARY",
        }

    if is_twse_openapi_enabled():
        try:
            hist_off = fetch_tw_daily_history_official(code, months_back=6)
            if hist_off is not None and not hist_off.empty and len(hist_off) >= 2:
                hist_off = _normalize_hist_columns(hist_off)
                _log.info(
                    "TW provider=TWSE_OFFICIAL_FALLBACK symbol=%s rows=%s",
                    canon,
                    len(hist_off),
                )
                return {
                    "ok": True,
                    "symbol": canon,
                    "hist": hist_off,
                    "error": None,
                    "provider": "TWSE_OFFICIAL_FALLBACK",
                }
        except Exception as e:
            _log.warning("market-data TWSE fallback exception symbol=%s err=%s", canon, str(e)[:200])

    internal = err_y or err_fm
    pub = _sanitize_public_error(internal)
    _log.warning(
        "market-data TW failed symbol=%s finmind=%s yahoo=%s",
        canon,
        err_fm,
        err_y,
    )
    return {
        "ok": False,
        "symbol": canon,
        "hist": None,
        "error": pub,
        "provider": None,
    }


def _fetch_us_stock_data(yf_symbol: str) -> Dict[str, Any]:
    key = str(yf_symbol).strip().upper()
    df, err = fetch_us_history_yahoo_bounded(key)
    if df is not None and not df.empty:
        df = _normalize_hist_columns(df)
        _log.info("market-data US provider=YAHOO symbol=%s rows=%s", key, len(df))
        return {
            "ok": True,
            "symbol": key,
            "hist": df,
            "error": None,
            "provider": "YAHOO",
        }
    pub = _sanitize_public_error(err)
    _log.warning("market-data US failed symbol=%s reason=%s", key, err)
    return {
        "ok": False,
        "symbol": key,
        "hist": None,
        "error": pub,
        "provider": None,
    }


def get_stock_data(yf_symbol: str) -> Dict[str, Any]:
    return fetch_stock_data(yf_symbol)


def get_cached_stock_data(yf_symbol: str) -> Dict[str, Any]:
    raw = str(yf_symbol).strip().upper()
    key = normalize_tw_yf_symbol(raw) if _is_tw_symbol(raw) else raw

    now = time.monotonic()
    ttl = _stock_data_cache_ttl()
    if key in _CACHE:
        ts, payload = _CACHE[key]
        if now - ts < ttl:
            print(f"CACHE HIT: {key} (stock_data hist bundle)")
            out = dict(payload)
            out["from_cache"] = True
            return out

    payload = fetch_stock_data(key)
    payload["from_cache"] = False
    _CACHE[key] = (now, payload)
    return payload


def get_cached_data(yf_symbol: str) -> Dict[str, Any]:
    return get_cached_stock_data(yf_symbol)


def clear_stock_cache(symbol: str | None = None) -> None:
    global _CACHE
    if symbol is None:
        _CACHE = {}
    else:
        raw = str(symbol).strip().upper()
        k = normalize_tw_yf_symbol(raw) if _is_tw_symbol(raw) else raw
        _CACHE.pop(k, None)
