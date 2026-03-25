"""
台股日線：FinMind API v4（TaiwanStockPrice）。

環境變數 FINMIND_API_TOKEN：Bearer token；未設定時由 stock_data_service 改走 Yahoo。
FINMIND_KLINE_CACHE_TTL：K 線結果記憶體 TTL（秒），預設 120。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

import pandas as pd
import requests
from cachetools import TTLCache

_log = logging.getLogger(__name__)

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_PROVIDER = "FINMIND"


def _kline_cache_ttl() -> int:
    try:
        return max(5, int(os.getenv("FINMIND_KLINE_CACHE_TTL", "120")))
    except ValueError:
        return 120


# 程序內 TTL：與 stock_data_service 快取互補，避免繞過或 worker 內重複 HTTP
_finmind_kline_cache: TTLCache[str, pd.DataFrame] = TTLCache(maxsize=500, ttl=_kline_cache_ttl())


def _get_verify():
    try:
        import certifi

        return certifi.where()
    except Exception:
        return True


def _to_float(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def fetch_tw_daily_history_finmind(stock_code: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """
    回傳 (df, None) 成功；(None, reason) 失敗。
    reason 為內部短碼，不應直接暴露給 API 使用者。
    """
    token = (os.getenv("FINMIND_API_TOKEN") or "").strip()
    if not token:
        return None, "finmind_no_token"

    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    if not code.isdigit():
        return None, "finmind_bad_code"

    cache_key = f"{code}_kline"
    if cache_key in _finmind_kline_cache:
        print(f"CACHE HIT: {code} (finmind kline)")
        try:
            return _finmind_kline_cache[cache_key].copy(), None
        except Exception:
            pass

    end = datetime.now().date()
    start = end - timedelta(days=400)

    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": code,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "StockPlatform/1.0",
    }
    try:
        r = requests.get(
            FINMIND_DATA_URL,
            params=params,
            headers=headers,
            timeout=25,
            verify=_get_verify(),
        )
    except requests.RequestException as e:
        _log.warning(
            "provider=%s symbol=%s request_error err=%s",
            FINMIND_PROVIDER,
            code,
            str(e)[:120],
        )
        return None, "finmind_request_error"

    ct_raw = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct_raw:
        _log.warning(
            "provider=%s symbol=%s html_content_type",
            FINMIND_PROVIDER,
            code,
        )
        return None, "finmind_html_response"

    if r.status_code == 429:
        _log.warning("provider=%s symbol=%s rate_limited", FINMIND_PROVIDER, code)
        return None, "finmind_rate_limited"

    if r.status_code != 200:
        _log.warning(
            "provider=%s symbol=%s http_%s body_head=%s",
            FINMIND_PROVIDER,
            code,
            r.status_code,
            (r.text or "")[:120],
        )
        return None, f"finmind_http_{r.status_code}"

    try:
        j = r.json()
    except ValueError:
        return None, "finmind_bad_json"

    if not isinstance(j, dict):
        return None, "finmind_bad_json"

    data = j.get("data")
    if not data or not isinstance(data, list):
        msg = j.get("msg")
        _log.warning(
            "provider=%s symbol=%s no_data api_msg=%s",
            FINMIND_PROVIDER,
            code,
            str(msg)[:120] if msg is not None else "(none)",
        )
        return None, "finmind_no_data"

    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        low = {str(k).lower(): v for k, v in item.items()}
        ds = low.get("date") or low.get("stock_date")
        if ds is None:
            continue
        try:
            ts = pd.Timestamp(str(ds))
        except Exception:
            continue

        o = _to_float(low.get("open"))
        hi = _to_float(low.get("max") or low.get("high"))
        lo = _to_float(low.get("min") or low.get("low"))
        c = _to_float(low.get("close"))
        vol = _to_float(
            low.get("trading_volume")
            or low.get("Trading_Volume")
            or low.get("volume")
        )

        if o is None or hi is None or lo is None or c is None:
            continue
        rows.append(
            {
                "Datetime": ts,
                "Open": float(o),
                "High": float(hi),
                "Low": float(lo),
                "Close": float(c),
                "Volume": float(vol) if vol is not None else 0.0,
            }
        )

    if len(rows) < 2:
        return None, "finmind_insufficient_rows"

    df = pd.DataFrame(rows)
    df = df.sort_values("Datetime").drop_duplicates(subset=["Datetime"])
    df = df.set_index("Datetime")
    df.index.name = None
    try:
        _finmind_kline_cache[cache_key] = df.copy()
    except Exception:
        pass
    return df, None


def finmind_hist_to_candles_list(df: pd.DataFrame) -> list[dict[str, Any]]:
    """將日線 DataFrame 轉成 API 友善的 OHLCV 列表（time 為 ISO 日期字串）。"""
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        t = idx
        if hasattr(t, "strftime"):
            time_s = t.strftime("%Y-%m-%d")
        else:
            time_s = str(t)[:10]
        out.append(
            {
                "time": time_s,
                "open": float(row.get("Open", row.get("open", 0))),
                "high": float(row.get("High", row.get("high", 0))),
                "low": float(row.get("Low", row.get("low", 0))),
                "close": float(row.get("Close", row.get("close", 0))),
                "volume": float(row.get("Volume", row.get("volume", 0))),
            }
        )
    return out


__all__ = [
    "FINMIND_DATA_URL",
    "FINMIND_PROVIDER",
    "fetch_tw_daily_history_finmind",
    "finmind_hist_to_candles_list",
]
