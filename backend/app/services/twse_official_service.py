"""
TWSE OpenAPI：可選備援（Render 等環境常回 HTML，預設關閉）。

環境變數 ENABLE_TWSE_OPENAPI=true 才會發送 HTTP；否則立即跳過（程序生命週期內僅記錄一次 TWSE skipped (disabled)）。
硬性規則：Content-Type 須含 application/json，且 body 不得為 HTML，否則視為 provider_unavailable，不嘗試 json.loads。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, List, Optional
from urllib.parse import urlencode

import pandas as pd
import requests

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
EXTERNAL_REQUEST_TIMEOUT = 15.0

TWSE_STOCK_DAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY"
TWSE_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

TWSE_PROVIDER = "TWSE_OPENAPI"

_twse_disabled_logged = False


def is_twse_openapi_enabled() -> bool:
    """預設 false；設為 1/true/yes/on 才啟用 TWSE HTTP。"""
    return os.getenv("ENABLE_TWSE_OPENAPI", "false").lower() in ("1", "true", "yes", "on")


def _get_verify():
    try:
        import certifi

        return certifi.where()
    except Exception:
        return True


def _build_url_for_log(url: str, params: dict[str, Any]) -> str:
    if not params:
        return url
    return f"{url}?{urlencode(params, doseq=True)}"


def _decode_response_body(raw_bytes: bytes) -> str:
    if not raw_bytes:
        return ""
    for enc in ("utf-8-sig", "utf-8", "big5"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _body_head(text: str, n: int = 120) -> str:
    return text[:n].replace("\r\n", " ").replace("\n", " ")


def _twse_json_eligible(ct_raw: str, body: str) -> tuple[bool, str]:
    """
    硬性檢查：非 application/json → 不可用。
    HTML 開頭 → 不可用（不再做 json parsing）。
    """
    ct = (ct_raw or "").lower()
    if "application/json" not in ct:
        return False, "not_application_json"
    b = body.lstrip("\ufeff \t\r\n")
    bl = b.lower()
    if bl.startswith("<!doctype html") or bl.startswith("<html"):
        return False, "html_body"
    if "<html" in bl[:50]:
        return False, "html_body"
    return True, "ok"


def _twse_log_unavailable(symbol: str, status: int, ct_raw: str, body: str, reason: str) -> None:
    logger.warning(
        "provider=%s symbol=%s status=%s content_type=%s body_head=%s reason=%s",
        TWSE_PROVIDER,
        symbol,
        status,
        (ct_raw or "(none)")[:80],
        _body_head(body),
        reason,
    )


def twse_safe_get_json(
    url: str,
    params: dict[str, Any],
    *,
    stock_no: str,
) -> tuple[dict | list | None, Optional[str]]:
    """
    禁止 response.json()。未通過硬性檢查前不呼叫 json.loads。
    失敗碼：twse_disabled, provider_unavailable, http_NNN, empty_body, json_parse_failed, ssl_failed, request_error
    """
    if not is_twse_openapi_enabled():
        global _twse_disabled_logged
        if not _twse_disabled_logged:
            logger.info("provider=%s TWSE skipped (disabled)", TWSE_PROVIDER)
            _twse_disabled_logged = True
        return None, "twse_disabled"

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
            body = _decode_response_body(raw_bytes).lstrip("\ufeff")

            if status != 200:
                _twse_log_unavailable(stock_no, status, ct_raw, body, "http_not_200")
                return None, f"http_{status}"

            if not body.strip():
                _twse_log_unavailable(stock_no, status, ct_raw, body, "empty_body")
                return None, "empty_body"

            if "404.html" in final_url.lower() or final_url.rstrip("/").lower().endswith("/404"):
                _twse_log_unavailable(stock_no, status, ct_raw, body, "soft_404_url")
                return None, "twse_soft_404"

            ok_elig, elig_reason = _twse_json_eligible(ct_raw, body)
            if not ok_elig:
                _twse_log_unavailable(stock_no, status, ct_raw, body, elig_reason)
                return None, "provider_unavailable"

            try:
                parsed: dict | list = json.loads(body)
            except json.JSONDecodeError:
                # 僅在已宣告 JSON 且非 HTML 時仍解析失敗
                _twse_log_unavailable(stock_no, status, ct_raw, body, "json_parse_failed")
                return None, "json_parse_failed"

            return parsed, None

        except requests.exceptions.SSLError as e:
            if attempt_idx == 0:
                logger.warning(
                    "provider=%s symbol=%s ssl_retry_insecure err=%s",
                    TWSE_PROVIDER,
                    stock_no,
                    str(e)[:100],
                )
                continue
            logger.warning(
                "provider=%s symbol=%s ssl_failed err=%s",
                TWSE_PROVIDER,
                stock_no,
                str(e)[:120],
            )
            return None, "ssl_failed"
        except requests.RequestException as e:
            logger.warning(
                "provider=%s symbol=%s request_err=%s",
                TWSE_PROVIDER,
                stock_no,
                str(e)[:120],
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
    if not is_twse_openapi_enabled():
        return None

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
            "provider=%s symbol=%s body_head=%s reason=unexpected_fields",
            TWSE_PROVIDER,
            code,
            str(fields)[:120],
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
    if not is_twse_openapi_enabled():
        return [], "twse_disabled"

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
                "provider=%s symbol=STOCK_DAY_ALL status=200 reason=stat_%s",
                TWSE_PROVIDER,
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
    "is_twse_openapi_enabled",
]
