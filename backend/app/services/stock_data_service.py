"""
股票資料統一入口：依市場分流資料源，並以記憶體快取 60 秒。

* 台股 TW：優先 TWSE OpenAPI 日線，失敗再備援 Yahoo Finance（yfinance）。
* 美股 US：目前主實作為 Yahoo Finance（見 us_stock_provider，可替換為 Finnhub 等）。
* Crypto：請勿使用本模組；請走 market_service / Bybit。

錯誤訊息經中性化處理，避免出現可能被誤解為下市的用語。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Tuple

import pandas as pd

from app.services.twse_official_service import fetch_tw_daily_history_official
from app.services.us_stock_provider import fetch_us_history_yahoo_bounded

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 60

# 給前端／日誌的中性說明（勿暗示下市）
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
    # 仍回傳簡短技術代碼時，改中性包裝
    if err in ("empty_history", "yfinance_timeout", "yfinance_session_error"):
        return NEUTRAL_DATA_ERROR
    return str(err)[:500]


def _is_tw_symbol(sym: str) -> bool:
    s = str(sym).strip().upper()
    if s.endswith(".TW") or s.endswith(".TWO"):
        return True
    return s.isdigit() and 2 <= len(s) <= 5


def normalize_tw_yf_symbol(symbol: str) -> str:
    """台股快取鍵：一律 2330.TW。"""
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
    """
    單次取得股票 history（約 3 個月日線），失敗時 ok=False。
    台股優先官方；美股使用 Yahoo（可替換層在 us_stock_provider）。
    """
    key = str(yf_symbol).strip().upper()

    if _is_tw_symbol(key):
        return _fetch_tw_stock_data(key)

    return _fetch_us_stock_data(key)


def _fetch_tw_stock_data(raw_key: str) -> Dict[str, Any]:
    canon = normalize_tw_yf_symbol(raw_key)
    code = canon.replace(".TW", "").replace(".TWO", "")

    # 1) TWSE 官方日線（上市）
    try:
        hist_off = fetch_tw_daily_history_official(code, months_back=6)
        if hist_off is not None and not hist_off.empty and len(hist_off) >= 2:
            hist_off = _normalize_hist_columns(hist_off)
            print(f"[market-data] TW provider=TWSE_OFFICIAL symbol={canon} rows={len(hist_off)}")
            return {
                "ok": True,
                "symbol": canon,
                "hist": hist_off,
                "error": None,
                "provider": "TWSE_OFFICIAL",
            }
    except Exception as e:
        print(f"[market-data] TWSE_OFFICIAL failed symbol={canon} err={e!r}")

    # 2) 備援：Yahoo Finance
    df, err = fetch_us_history_yahoo_bounded(canon)
    if df is not None and not df.empty:
        df = _normalize_hist_columns(df)
        print(f"[market-data] TW provider=YAHOO_FALLBACK symbol={canon} rows={len(df)}")
        return {
            "ok": True,
            "symbol": canon,
            "hist": df,
            "error": None,
            "provider": "YAHOO_FALLBACK",
        }

    pub = _sanitize_public_error(err)
    print(f"[market-data] TW failed symbol={canon} raw_err={err!r} public={pub}")
    return {
        "ok": False,
        "symbol": canon,
        "hist": None,
        "error": pub,
        "provider": None,
    }


def _fetch_us_stock_data(yf_symbol: str) -> Dict[str, Any]:
    """美股：目前僅 Yahoo 實作（見 us_stock_provider，可替換主源）。"""
    key = str(yf_symbol).strip().upper()
    df, err = fetch_us_history_yahoo_bounded(key)
    if df is not None and not df.empty:
        df = _normalize_hist_columns(df)
        print(f"[market-data] US provider=YAHOO symbol={key} rows={len(df)}")
        return {
            "ok": True,
            "symbol": key,
            "hist": df,
            "error": None,
            "provider": "YAHOO",
        }
    pub = _sanitize_public_error(err)
    print(f"[market-data] US failed symbol={key} raw_err={err!r} public={pub}")
    return {
        "ok": False,
        "symbol": key,
        "hist": None,
        "error": pub,
        "provider": None,
    }


def get_stock_data(yf_symbol: str) -> Dict[str, Any]:
    """別名：與規格「get_stock_data(symbol)」一致（無快取，單次抓取）。"""
    return fetch_stock_data(yf_symbol)


def get_cached_stock_data(yf_symbol: str) -> Dict[str, Any]:
    """
    帶 60 秒記憶體快取的資料取得；quote / detail / chart / 技術分析共用。
    台股快取鍵會正規化為 2330.TW。
    """
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
    """別名，與規格文件一致。"""
    return get_cached_stock_data(yf_symbol)


def clear_stock_cache(symbol: str | None = None) -> None:
    """測試或管理用：清除快取。"""
    global _CACHE
    if symbol is None:
        _CACHE = {}
    else:
        raw = str(symbol).strip().upper()
        k = normalize_tw_yf_symbol(raw) if _is_tw_symbol(raw) else raw
        _CACHE.pop(k, None)
