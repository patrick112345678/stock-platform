"""
台股基本面：FinMind API v4（PER / PBR / EPS、TaiwanStockInfo 股名）。

環境變數 FINMIND_API_TOKEN：與 finmind_provider 相同。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

_log = logging.getLogger(__name__)

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
PROVIDER = "FINMIND_FUNDAMENTAL"

# 快取（程序內）：減少 FinMind 請求次數
_STOCK_INFO_MAP: dict[str, str] | None = None
_STOCK_INFO_TS: float = 0.0
_STOCK_INFO_TTL = 86400.0

_FUND_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FUND_TTL = 3600.0


def _get_verify():
    try:
        import certifi

        return certifi.where()
    except Exception:
        return True


def _token() -> str:
    return (os.getenv("FINMIND_API_TOKEN") or "").strip()


def _finmind_get(params: dict) -> Optional[dict]:
    tok = _token()
    if not tok:
        return None
    headers = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
        "User-Agent": "StockPlatform/1.0",
    }
    try:
        r = requests.get(
            FINMIND_DATA_URL,
            params=params,
            headers=headers,
            timeout=30,
            verify=_get_verify(),
        )
    except requests.RequestException as e:
        _log.warning("provider=%s finmind_get err=%s", PROVIDER, str(e)[:120])
        return None

    ct = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct or r.status_code != 200:
        _log.warning(
            "provider=%s http=%s ct=%s head=%s",
            PROVIDER,
            r.status_code,
            ct[:40],
            (r.text or "")[:80],
        )
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _to_float(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def _parse_date_key(row: dict) -> str:
    low = {str(k).lower(): v for k, v in row.items()}
    return str(low.get("date") or low.get("stock_date") or "")


def _latest_row(rows: list[dict]) -> Optional[dict]:
    if not rows:
        return None
    try:
        return max(rows, key=lambda r: _parse_date_key(r))
    except Exception:
        return rows[-1]


def _extract_per(row: dict) -> Optional[float]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("per", "pe_ratio", "pe", "本益比"):
        if k in low:
            v = _to_float(low[k])
            if v is not None and v > 0:
                return v
    return None


def _extract_pbr(row: dict) -> Optional[float]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("pbr", "pb", "股淨比", "net_asset_value_ratio"):
        if k in low:
            v = _to_float(low[k])
            if v is not None and v > 0:
                return v
    return None


def _extract_eps(row: dict) -> Optional[float]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("eps", "每股盈餘", "revenue_per_share"):
        if k in low:
            v = _to_float(low[k])
            if v is not None:
                return v
    return None


def fetch_tw_fundamentals_finmind(stock_code: str) -> dict[str, Any]:
    """
    回傳 { pe, pb, eps }，缺漏為 None。
    以 TaiwanStockPER / TaiwanStockPB / TaiwanStockEPS 各取最新一筆（依 date）。
    """
    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    out: dict[str, Any] = {"pe": None, "pb": None, "eps": None}
    if not code.isdigit():
        return out

    now = time.monotonic()
    ck = f"fund::{code}"
    if ck in _FUND_CACHE:
        ts, cached = _FUND_CACHE[ck]
        if now - ts < _FUND_TTL:
            return dict(cached)

    end = datetime.now().date()
    start = end - timedelta(days=400)

    def load_dataset(dataset: str) -> list[dict]:
        j = _finmind_get(
            {
                "dataset": dataset,
                "data_id": code,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
            }
        )
        if not isinstance(j, dict):
            return []
        data = j.get("data")
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict)]

    per_rows = load_dataset("TaiwanStockPER")
    pb_rows = load_dataset("TaiwanStockPB")
    eps_rows = load_dataset("TaiwanStockEPS")

    if per_rows:
        r = _latest_row(per_rows)
        if r:
            out["pe"] = _extract_per(r)
    if pb_rows:
        r = _latest_row(pb_rows)
        if r:
            out["pb"] = _extract_pbr(r)
    if eps_rows:
        r = _latest_row(eps_rows)
        if r:
            out["eps"] = _extract_eps(r)

    _FUND_CACHE[ck] = (now, dict(out))
    return out


def _extract_stock_name(row: dict) -> Optional[str]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("stock_name", "stockname", "name", "公司名稱"):
        if k in low and low[k] not in (None, ""):
            s = str(low[k]).strip()
            if s:
                return s
    return None


def _extract_stock_id(row: dict) -> Optional[str]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("stock_id", "code", "symbol"):
        if k in low and low[k] not in (None, ""):
            s = str(low[k]).strip()
            if s.isdigit() and 2 <= len(s) <= 6:
                return s
    return None


def load_finmind_tw_stock_info_map(force: bool = False) -> dict[str, str]:
    """
    TaiwanStockInfo：stock_id -> stock_name（最新一筆 date 優先）。
    全量快取約每日更新一次。
    """
    global _STOCK_INFO_MAP, _STOCK_INFO_TS
    now = time.monotonic()
    if not force and _STOCK_INFO_MAP is not None and now - _STOCK_INFO_TS < _STOCK_INFO_TTL:
        return _STOCK_INFO_MAP

    if not _token():
        _STOCK_INFO_MAP = {}
        _STOCK_INFO_TS = now
        return _STOCK_INFO_MAP

    end = datetime.now().date()
    for days in (30, 400):
        start = end - timedelta(days=days)
        j = _finmind_get(
            {
                "dataset": "TaiwanStockInfo",
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
            }
        )
        if isinstance(j, dict) and isinstance(j.get("data"), list) and j["data"]:
            break
    else:
        j = None
    if not isinstance(j, dict):
        _STOCK_INFO_MAP = {}
        _STOCK_INFO_TS = now
        return _STOCK_INFO_MAP

    data = j.get("data")
    if not isinstance(data, list) or not data:
        _STOCK_INFO_MAP = {}
        _STOCK_INFO_TS = now
        return _STOCK_INFO_MAP

    # stock_id -> (best_date_str, name)
    best: dict[str, tuple[str, str]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sid = _extract_stock_id(row)
        nm = _extract_stock_name(row)
        if not sid or not nm:
            continue
        d = _parse_date_key(row)
        prev = best.get(sid)
        if prev is None or d >= prev[0]:
            best[sid] = (d, nm)

    _STOCK_INFO_MAP = {k: v[1] for k, v in best.items()}
    _STOCK_INFO_TS = now
    _log.info("provider=%s TaiwanStockInfo loaded count=%s", PROVIDER, len(_STOCK_INFO_MAP))
    return _STOCK_INFO_MAP


def _fetch_single_tw_stock_info_row(stock_code: str) -> Optional[dict]:
    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    if not code.isdigit():
        return None
    end = datetime.now().date()
    start = end - timedelta(days=400)
    j = _finmind_get(
        {
            "dataset": "TaiwanStockInfo",
            "data_id": code,
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
        }
    )
    if not isinstance(j, dict):
        return None
    data = j.get("data")
    if not isinstance(data, list) or not data:
        return None
    rows = [x for x in data if isinstance(x, dict)]
    return _latest_row(rows) if rows else None


def resolve_tw_stock_name_finmind(stock_code: str) -> Optional[str]:
    """單一 code -> 中文名（無則 None）。"""
    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    if not code.isdigit():
        return None
    m = load_finmind_tw_stock_info_map()
    if code in m:
        return m[code]
    row = _fetch_single_tw_stock_info_row(code)
    if row:
        nm = _extract_stock_name(row)
        if nm:
            m[code] = nm
            return nm
    return None


def format_tw_display_name(zh_name: Optional[str], code: str) -> str:
    """台股顯示：台積電（2330）；無中文則回傳 code。"""
    c = str(code).replace(".TW", "").replace(".TWO", "").strip()
    if zh_name and str(zh_name).strip():
        return f"{str(zh_name).strip()}（{c}）"
    return c


def resolve_tw_display_name(stock_code: str, fallback_zh: Optional[str] = None) -> str:
    """
    優先 FinMind 股名，其次 fallback（如 MIS），再組合成 中文（code）。
    """
    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    zh = resolve_tw_stock_name_finmind(code) or fallback_zh
    return format_tw_display_name(zh, code)


__all__ = [
    "fetch_tw_fundamentals_finmind",
    "load_finmind_tw_stock_info_map",
    "resolve_tw_stock_name_finmind",
    "format_tw_display_name",
    "resolve_tw_display_name",
]
