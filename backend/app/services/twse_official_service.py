"""
台股上市：TWSE OpenAPI 日線（STOCK_DAY）為主資料來源之一。
櫃買（上櫃）若 TWSE 無資料，請由上層改走備援（例如 Yahoo），勿在此強行混用不適合的來源。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

import pandas as pd
import requests

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockPlatform/1.0)",
    "Accept": "application/json",
}
EXTERNAL_REQUEST_TIMEOUT = 8.0

TWSE_STOCK_DAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY"


def _get_verify():
    try:
        import certifi

        return certifi.where()
    except Exception:
        return True


def _http_get_json(url: str, params: dict) -> dict | list | None:
    verify = _get_verify()
    try:
        r = requests.get(
            url,
            params=params,
            timeout=EXTERNAL_REQUEST_TIMEOUT,
            headers=REQUEST_HEADERS,
            verify=verify,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.SSLError as e:
        print("WARN TWSE SSL verify failed, retrying without verify:", repr(e))
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(
            url,
            params=params,
            timeout=EXTERNAL_REQUEST_TIMEOUT,
            headers=REQUEST_HEADERS,
            verify=False,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("WARN TWSE request failed:", url, params, repr(e))
        return None


def _parse_roc_or_gregorian_date(s: str) -> Optional[pd.Timestamp]:
    s = str(s).strip().replace(" ", "")
    parts = s.split("/")
    if len(parts) != 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    # TWSE 多為民國年 113/01/02；若年分很大視為西元
    if y < 1000:
        gy = 1911 + y
    else:
        gy = y
    try:
        return pd.Timestamp(datetime(gy, m, d))
    except Exception:
        return None


def _parse_num(val) -> Optional[float]:
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(str(val).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def _fetch_twse_stock_day_month(stock_no: str, year: int, month: int) -> List[List[Any]]:
    """單月 STOCK_DAY；無資料或非上市檔回傳空 list。"""
    date_str = f"{year}{month:02d}01"
    raw = _http_get_json(TWSE_STOCK_DAY_URL, {"date": date_str, "stockNo": stock_no})
    if raw is None:
        return []
    if isinstance(raw, dict):
        if raw.get("stat") and raw.get("stat") != "OK":
            return []
        data = raw.get("data")
        fields = raw.get("fields") or []
    else:
        return []

    if not isinstance(data, list) or not data:
        return []

    # 附帶欄位名供解析
    return [fields] + list(data)


def fetch_tw_daily_history_official(stock_no: str, months_back: int = 6) -> Optional[pd.DataFrame]:
    """
    自 TWSE OpenAPI 拉取最近數月日線，組成與 yfinance 相容的 OHLCV DataFrame（index: DatetimeIndex）。
    僅適用 **上市** 普通股；上櫃／興櫃若 TWSE 無檔會回傳 None。
    """
    code = str(stock_no).replace(".TW", "").replace(".TWO", "").strip()
    if not code.isdigit():
        return None

    now = datetime.now()
    y, m = now.year, now.month
    all_rows: List[List[Any]] = []
    fields: List[str] = []

    for _ in range(max(1, months_back)):
        chunk = _fetch_twse_stock_day_month(code, y, m)
        if not chunk:
            pass
        elif isinstance(chunk[0], list) and not fields:
            fields = [str(x) for x in chunk[0]]
        if len(chunk) > 1:
            all_rows.extend(chunk[1:])
        if m == 1:
            y, m = y - 1, 12
        else:
            m -= 1

    if not all_rows or not fields:
        return None

    try:
        i_date = fields.index("日期")
        i_vol = fields.index("成交股數") if "成交股數" in fields else 1
        i_open = fields.index("開盤價")
        i_high = fields.index("最高價")
        i_low = fields.index("最低價")
        i_close = fields.index("收盤價")
    except ValueError:
        print("WARN TWSE STOCK_DAY unexpected fields:", fields)
        return None

    records = []
    for row in all_rows:
        if not isinstance(row, (list, tuple)) or len(row) <= i_close:
            continue
        ts = _parse_roc_or_gregorian_date(str(row[i_date]))
        if ts is None:
            continue
        o = _parse_num(row[i_open])
        hi = _parse_num(row[i_high])
        lo = _parse_num(row[i_low])
        c = _parse_num(row[i_close])
        vol = _parse_num(row[i_vol]) if i_vol < len(row) else None
        if o is None or hi is None or lo is None or c is None:
            continue
        records.append(
            {
                "Datetime": ts,
                "Open": o,
                "High": hi,
                "Low": lo,
                "Close": c,
                "Volume": vol if vol is not None else 0.0,
            }
        )

    if len(records) < 2:
        return None

    df = pd.DataFrame(records)
    df = df.drop_duplicates(subset=["Datetime"]).sort_values("Datetime")
    df = df.set_index("Datetime")
    df.index.name = None
    # 欄位名對齊 yfinance
    return df


__all__ = ["fetch_tw_daily_history_official", "TWSE_STOCK_DAY_URL"]
