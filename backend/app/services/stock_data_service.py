"""
股票資料統一入口：依市場分流資料源，並以記憶體快取 60 秒。

* 台股 TW 日線：Yahoo 主用（Render 上 TWSE OpenAPI 常回 HTML）；僅 ENABLE_TWSE_OPENAPI=true 時才在 Yahoo 失敗後嘗試 TWSE 備援。
* 台股報價：見 market_service（MIS 優先 + 日線補足）。
* 美股 US：Yahoo（us_stock_provider）。
* Crypto：勿使用本模組。

錯誤訊息經中性化處理，避免出現可能被誤解為下市的用語。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Tuple

import pandas as pd

from app.services.twse_official_service import (
    fetch_tw_daily_history_official,
    is_twse_openapi_enabled,
)
from app.services.us_stock_provider import fetch_us_history_yahoo_bounded

_log = logging.getLogger(__name__)

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 60

NEUTRAL_DATA_ERROR = (
    "資料來源暫時查無資料，可能為 symbol mapping、官方資料延遲或第三方資料源異常"
)


def _sanitize_public_error(err: str | None) -> str:
    if not err:
        return NEUTRAL_DATA_ERROR
    s = str(err).lower()
    if "delist" in s or "possibly delisted" in s or "delisted" in s:
        return NEUTRAL_DATA_ERROR
    if "no data" in s and "yahoo" in s:
        return NEUTRAL_DATA_ERROR
    if err in (
        "empty_history",
        "yfinance_timeout",
        "yfinance_session_error",
        "yahoo_quote_unavailable",
    ):
        return NEUTRAL_DATA_ERROR
    return str(err)[:500]


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
    台股日線策略（務實）：
    1) Yahoo 主用（chart/history）
    2) 僅當 Yahoo 失敗且 ENABLE_TWSE_OPENAPI=true 時，才嘗試 TWSE 備援（避免 Render 每請求先撞 TWSE）
    """
    canon = normalize_tw_yf_symbol(raw_key)
    code = canon.replace(".TW", "").replace(".TWO", "")

    df, err = fetch_us_history_yahoo_bounded(canon)
    if df is not None and not df.empty:
        df = _normalize_hist_columns(df)
        _log.info("market-data TW provider=YAHOO_PRIMARY symbol=%s rows=%s", canon, len(df))
        return {
            "ok": True,
            "symbol": canon,
            "hist": df,
            "error": None,
            "provider": "YAHOO_PRIMARY",
        }

    if is_twse_openapi_enabled():
        try:
            hist_off = fetch_tw_daily_history_official(code, months_back=6)
            if hist_off is not None and not hist_off.empty and len(hist_off) >= 2:
                hist_off = _normalize_hist_columns(hist_off)
                _log.info("market-data TW provider=TWSE_OFFICIAL_FALLBACK symbol=%s rows=%s", canon, len(hist_off))
                return {
                    "ok": True,
                    "symbol": canon,
                    "hist": hist_off,
                    "error": None,
                    "provider": "TWSE_OFFICIAL_FALLBACK",
                }
        except Exception as e:
            _log.warning("market-data TWSE fallback exception symbol=%s err=%s", canon, str(e)[:200])

    pub = _sanitize_public_error(err)
    _log.warning("market-data TW failed symbol=%s reason=%s", canon, err)
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
    return get_cached_stock_data(yf_symbol)


def clear_stock_cache(symbol: str | None = None) -> None:
    global _CACHE
    if symbol is None:
        _CACHE = {}
    else:
        raw = str(symbol).strip().upper()
        k = normalize_tw_yf_symbol(raw) if _is_tw_symbol(raw) else raw
        _CACHE.pop(k, None)
