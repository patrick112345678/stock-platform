"""
台股上市：TWSE OpenAPI 日線（STOCK_DAY）為主資料來源之一。

嚴禁對回應直接呼叫 response.json()：一律先檢查 status、Content-Type、body，
再以 utf-8 解碼後 json.loads；失敗時寫結構化單行 log（含 url、body 前 200 字）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, List, Optional
from urllib.parse import urlencode

import pandas as pd
import requests

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
EXTERNAL_REQUEST_TIMEOUT = 15.0

TWSE_STOCK_DAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY"
TWSE_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

TWSE_PROVIDER = "TWSE_OPENAPI"


def _get_verify():
    try:
        import certifi

        return certifi.where()
    except Exception:
        return True


def _build_url_for_log(url: str, params: dict[str, Any]) -> str:
    if not params:
        return url
    q = urlencode(params, doseq=True)
    return f"{url}?{q}"


def _decode_response_body(raw_bytes: bytes) -> str:
    """TWSE 回傳應為 UTF-8；避免 requests 誤判 encoding 造成亂碼與 json.loads 失敗。"""
    if not raw_bytes:
        return ""
    for enc in ("utf-8-sig", "utf-8", "big5"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _body_looks_like_json(text: str) -> bool:
    s = text.lstrip("\ufeff \t\r\n")
    return s.startswith("{") or s.startswith("[")


def _content_type_allows_json_parsing(content_type: str, body: str) -> tuple[bool, str]:
    """
    若明確為 HTML 則拒絕。
    application/json、text/plain（且內容像 JSON）、空 content-type 但內容像 JSON → 允許。
    回傳 (allowed, reason_code)
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if "text/html" in content_type.lower():
        return False, "content_type_html"
    if "application/json" in content_type.lower() or "text/json" in content_type.lower():
        return True, "content_type_json"
    if "application/javascript" in content_type.lower():
        return True, "content_type_js"
    if ct in ("text/plain", "") or "charset" in content_type.lower():
        if _body_looks_like_json(body):
            return True, "content_type_plain_but_json_shape"
        return False, "content_type_plain_not_json"
    if _body_looks_like_json(body):
        return True, "content_type_unusual_but_json_shape"
    return False, f"content_type_rejected:{ct or 'empty'}"


def twse_safe_get_json(
    url: str,
    params: dict[str, Any],
    *,
    stock_no: str,
) -> tuple[dict | list | None, Optional[str]]:
    """
    禁止 response.json()。成功 (parsed, None)；失敗 (None, short_reason)。

    short_reason: http_NNN, empty_body, provider_content_type, json_parse_failed, ssl_failed, request_error
    """
    url_preview = _build_url_for_log(url, params)

    for attempt_idx, use_verify in enumerate([_get_verify(), False]):
        if attempt_idx == 1:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        try:
            r = requests.get(
                url,
                params=params,
                timeout=EXTERNAL_REQUEST_TIMEOUT,
                headers=REQUEST_HEADERS,
                verify=use_verify,
            )
            status = r.status_code
            ct_raw = r.headers.get("Content-Type") or ""
            final_url = getattr(r, "url", None) or url_preview

            raw_bytes = r.content if r.content is not None else b""
            body_preview = _decode_response_body(raw_bytes)[:200].replace("\r\n", " ").replace("\n", " ")

            # TWSE 有時回 HTTP 200 但導向 404.html（或路徑含 /404）
            if "404.html" in final_url.lower() or final_url.rstrip("/").lower().endswith("/404"):
                logger.warning(
                    "provider=%s symbol=%s url=%s status=%s content_type=%s note=soft_404_url body_head=%s",
                    TWSE_PROVIDER,
                    stock_no,
                    final_url,
                    status,
                    ct_raw or "(none)",
                    body_preview,
                )
                return None, "twse_soft_404"

            body = _decode_response_body(raw_bytes)
            body = body.lstrip("\ufeff")
            snippet = body[:200].replace("\r\n", " ").replace("\n", " ")

            if status != 200:
                logger.warning(
                    "provider=%s symbol=%s url=%s status=%s content_type=%s body_head=%s",
                    TWSE_PROVIDER,
                    stock_no,
                    final_url,
                    status,
                    ct_raw or "(none)",
                    snippet,
                )
                return None, f"http_{status}"

            if not body.strip():
                logger.warning(
                    "provider=%s symbol=%s url=%s status=%s content_type=%s body_head=%s",
                    TWSE_PROVIDER,
                    stock_no,
                    final_url,
                    status,
                    ct_raw or "(none)",
                    "(empty)",
                )
                return None, "empty_body"

            allowed, ctype_reason = _content_type_allows_json_parsing(ct_raw, body)
            if not allowed:
                logger.warning(
                    "provider=%s symbol=%s url=%s status=%s content_type=%s ctype_check=%s body_head=%s",
                    TWSE_PROVIDER,
                    stock_no,
                    final_url,
                    status,
                    ct_raw or "(none)",
                    ctype_reason,
                    snippet,
                )
                return None, "provider_content_type"

            try:
                parsed: dict | list = json.loads(body)
            except json.JSONDecodeError as e:
                logger.warning(
                    "provider=%s symbol=%s url=%s status=%s content_type=%s "
                    "json_error=%s pos=%s body_head=%s",
                    TWSE_PROVIDER,
                    stock_no,
                    final_url,
                    status,
                    ct_raw or "(none)",
                    str(e).replace("\n", " ")[:120],
                    getattr(e, "pos", None),
                    snippet,
                )
                return None, "json_parse_failed"

            return parsed, None

        except requests.exceptions.SSLError as e:
            if attempt_idx == 0:
                logger.warning(
                    "provider=%s symbol=%s url=%s ssl_verify_failed err=%s (retry_insecure)",
                    TWSE_PROVIDER,
                    stock_no,
                    url_preview,
                    str(e)[:160],
                )
                continue
            logger.warning(
                "provider=%s symbol=%s url=%s ssl_failed err=%s",
                TWSE_PROVIDER,
                stock_no,
                url_preview,
                str(e)[:200],
            )
            return None, "ssl_failed"
        except requests.RequestException as e:
            logger.warning(
                "provider=%s symbol=%s url=%s request_error err=%s",
                TWSE_PROVIDER,
                stock_no,
                url_preview,
                str(e)[:200],
            )
            return None, "request_error"

    return None, "ssl_failed"


def _parse_roc_or_gregorian_date(s: str) -> Optional[pd.Timestamp]:
    s = str(s).strip().replace(" ", "")
    parts = s.split("/")
    if len(parts) != 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
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
    """單月 STOCK_DAY；失敗或非上市檔回傳空 list。"""
    date_str = f"{year}{month:02d}01"
    raw, err = twse_safe_get_json(
        TWSE_STOCK_DAY_URL,
        {"date": date_str, "stockNo": stock_no},
        stock_no=stock_no,
    )
    if err or raw is None:
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
        logger.warning(
            "provider=%s symbol=%s url=%s unexpected_fields=%s",
            TWSE_PROVIDER,
            code,
            TWSE_STOCK_DAY_URL,
            fields,
        )
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
    return df


def fetch_twse_stock_day_all_rows() -> tuple[List[Dict[str, Any]], Optional[str]]:
    """
    STOCK_DAY_ALL 全表；供 /market/search 與 scanner 搜尋清單。
    回傳 (rows, None) 或 ([], error_reason)。
    """
    raw, err = twse_safe_get_json(
        TWSE_STOCK_DAY_ALL_URL,
        {},
        stock_no="STOCK_DAY_ALL",
    )
    if err or raw is None:
        return [], err or "twse_failed"

    if isinstance(raw, dict):
        st = raw.get("stat")
        if st and st != "OK":
            logger.warning(
                "provider=%s symbol=STOCK_DAY_ALL url=%s stat=%s",
                TWSE_PROVIDER,
                TWSE_STOCK_DAY_ALL_URL,
                st,
            )
            return [], "stat_not_ok"
    data_raw: Any = raw
    if isinstance(raw, dict) and "data" in raw:
        data_raw = raw["data"]
    if not isinstance(data_raw, list):
        return [], "invalid_shape"

    out: List[Dict[str, Any]] = []
    for item in data_raw:
        if isinstance(item, dict):
            out.append(item)
    return out, None


__all__ = [
    "fetch_tw_daily_history_official",
    "TWSE_STOCK_DAY_URL",
    "TWSE_STOCK_DAY_ALL_URL",
    "TWSE_PROVIDER",
    "twse_safe_get_json",
    "fetch_twse_stock_day_all_rows",
]
