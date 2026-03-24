"""
台股基本面：FinMind API v4 — 多 dataset 彙整（PER/PBR/EPS、綜合損益、資產負債、月營收、TaiwanStockInfo）。

環境變數 FINMIND_API_TOKEN：與 finmind_provider 相同。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

_log = logging.getLogger(__name__)

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
PROVIDER = "FINMIND_FUNDAMENTAL"

_STOCK_INFO_MAP: dict[str, str] | None = None
_STOCK_INFO_TS: float = 0.0
_STOCK_INFO_TTL = 86400.0

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
            timeout=35,
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


def normalize_percent_ratio(v: Optional[float]) -> Optional[float]:
    """
    統一為「31.41 代表 31.41%」的數值（API／前端顯示時只加 % 符號，不再乘 100）。
    - |x| > 1：視為已是百分數（例如 31.41）
    - |x| ≤ 1：視為小數比例（例如 0.3141 → 31.41）
    """
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if abs(x) > 1:
        return round(x, 4)
    return round(x * 100.0, 4)


def normalize_tw_percent_fields(bundle: dict[str, Any]) -> None:
    """就地修正 roe / gross_margin / revenue_growth_yoy / debt_ratio。"""
    for k in ("roe", "gross_margin", "revenue_growth_yoy", "debt_ratio"):
        if k in bundle and bundle[k] is not None:
            bundle[k] = normalize_percent_ratio(bundle.get(k))


def tw_percent_display_to_api_ratio(v: Optional[float]) -> Optional[float]:
    """
    將內部儲存的「百分數字」（如 22.56 代表 22.56%）轉成 API 用的小數比例（0.2256）。
    若前端誤用 (value * 100) 再顯示 %，會把 22.56 變成 2256%；改傳 0.2256 則 *100 後為 22.56 正確。

    若 |v|≤1 則視為已是小數比例（如 0.2256），原樣回傳。
    """
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if abs(x) <= 1:
        return round(x, 6)
    return round(x / 100.0, 6)


def apply_tw_fundamental_sanity(bundle: dict[str, Any]) -> None:
    """排除明顯錯誤（如 ROE>100%、毛利率>100%）。"""
    roe = bundle.get("roe")
    if roe is not None and abs(float(roe)) > 100:
        bundle["roe"] = None
    gm = bundle.get("gross_margin")
    if gm is not None:
        g = float(gm)
        if g < 0 or g > 100:
            bundle["gross_margin"] = None
    dr = bundle.get("debt_ratio")
    if dr is not None:
        d = float(dr)
        if d < 0 or d > 100:
            bundle["debt_ratio"] = None


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
    for k in (
        "eps",
        "每股盈餘",
        "revenue_per_share",
        "earning_per_share",
        "reference_eps",
        "近四季每股盈餘",
    ):
        if k in low:
            v = _to_float(low[k])
            if v is not None:
                return v
    return None


def _load_dataset_rows(
    dataset: str,
    code: str,
    start: datetime.date,
    end: datetime.date,
) -> list[dict]:
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


def _pivot_long_by_date(rows: list[dict]) -> dict[str, dict[str, float]]:
    """FinMind 財報長表：依 date 聚合 type -> value。"""
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        ds = _parse_date_key(r)
        if not ds:
            continue
        typ = r.get("type")
        if typ is None:
            continue
        v = _to_float(r.get("value"))
        if v is None:
            continue
        if ds not in by_date:
            by_date[ds] = {}
        by_date[ds][str(typ)] = v
    return by_date


def _pick_metric(
    metrics: dict[str, float],
    keys: list[str],
) -> Optional[float]:
    for k in keys:
        if k in metrics:
            v = _to_float(metrics[k])
            if v is not None:
                return v
    return None


def _find_metric_substr(metrics: dict[str, float], must: tuple[str, ...]) -> Optional[float]:
    for k, v in metrics.items():
        kl = k.lower()
        if all(s in kl for s in must):
            return _to_float(v)
    return None


def _ttm_income_after_tax(fin_by_date: dict[str, dict[str, float]]) -> Optional[float]:
    """近四季本期淨利（稅後）加總。"""
    dates = sorted(fin_by_date.keys(), reverse=True)
    total = 0.0
    n = 0
    for d in dates:
        m = fin_by_date[d]
        ni = _pick_metric(m, ["IncomeAfterTaxes", "IncomeAfterTax"])
        if ni is not None:
            total += ni
            n += 1
        if n >= 4:
            break
    if n == 0:
        return None
    return total


def _latest_quarter_eps(fin_by_date: dict[str, dict[str, float]]) -> Optional[float]:
    """綜合損益表最新一季 EPS（FinMind v4 已無 TaiwanStockEPS dataset）。"""
    if not fin_by_date:
        return None
    latest = max(fin_by_date.keys())
    m = fin_by_date[latest]
    v = _pick_metric(
        m,
        [
            "BasicEarningsPerShare",
            "EarningsPerShare",
            "EPS",
            "EpsPerShare",
            "EarningPerShare",
        ],
    )
    if v is not None:
        return v
    v = _find_metric_substr(m, ("basic", "earning", "share"))
    if v is not None:
        return v
    return _find_metric_substr(m, ("eps",))


def _latest_quarter_gross_margin_ratio(fin_by_date: dict[str, dict[str, float]]) -> Optional[float]:
    """回傳毛利率小數比例（例如 0.3141），由 normalize_percent_ratio 轉成 31.41。"""
    if not fin_by_date:
        return None
    latest = max(fin_by_date.keys())
    m = fin_by_date[latest]
    gp = _pick_metric(m, ["GrossProfit", "GrossProfitLoss"])
    if gp is None:
        gp = _find_metric_substr(m, ("gross", "profit"))
    rev = _pick_metric(
        m,
        [
            "OperatingRevenue",
            "Revenue",
            "NetOperatingRevenue",
        ],
    )
    if rev is None:
        rev = _find_metric_substr(m, ("operating", "revenue"))
    if gp is None or rev is None or rev == 0:
        return None
    return gp / rev


def _latest_balance_ratios(bs_by_date: dict[str, dict[str, float]]) -> tuple[
    Optional[float],
    Optional[float],
    Optional[float],
]:
    """回傳 (total_assets, total_liabilities, total_equity)。"""
    if not bs_by_date:
        return None, None, None
    latest = max(bs_by_date.keys())
    m = bs_by_date[latest]
    ta = _pick_metric(m, ["TotalAssets", "Assets"])
    if ta is None:
        ta = _find_metric_substr(m, ("total", "asset"))
    tl = _pick_metric(m, ["TotalLiabilities", "Liabilities"])
    if tl is None:
        tl = _find_metric_substr(m, ("total", "liabilit"))
    te = _pick_metric(
        m,
        [
            "TotalEquity",
            "TotalStockholdersEquity",
            "StockholdersEquity",
            "Equity",
        ],
    )
    if te is None:
        te = _find_metric_substr(m, ("total", "equity"))
    return ta, tl, te


def _roe_ratio(ttm_net: Optional[float], equity: Optional[float]) -> Optional[float]:
    """稅後淨利／權益，為小數比例；normalize_percent_ratio 轉成百分數。"""
    if ttm_net is None or equity is None or equity == 0:
        return None
    return ttm_net / equity


def _debt_ratio_raw(liab: Optional[float], assets: Optional[float]) -> Optional[float]:
    """負債／資產，為小數比例。"""
    if liab is None or assets is None or assets == 0:
        return None
    return liab / assets


def _month_revenue_yoy_ratio(rows: list[dict]) -> Optional[float]:
    pts: list[tuple[str, float]] = []
    for r in rows:
        ds = _parse_date_key(r)
        rev = _to_float(r.get("revenue"))
        if ds and rev is not None:
            pts.append((ds[:10], rev))
    pts.sort(key=lambda x: x[0])
    if len(pts) < 13:
        return None
    last_ds, last_rev = pts[-1]
    try:
        y, mth, _ = [int(x) for x in last_ds.split("-")]
    except Exception:
        return None
    prev_y = y - 1
    prev_key = f"{prev_y:04d}-{mth:02d}"
    prev_rev = None
    for ds, rev in pts:
        if ds[:7] == prev_key[:7]:
            prev_rev = rev
    if prev_rev is None or prev_rev == 0:
        return None
    return (last_rev - prev_rev) / prev_rev


def fetch_tw_fundamental_bundle(stock_code: str) -> dict[str, Any]:
    """
    統一基本面結構（台股）：
    pe, pb, eps, roe, gross_margin, revenue_growth_yoy, debt_ratio,
    industry, stock_name_zh, display_name, valuation（由呼叫端填入）
    """
    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    empty: dict[str, Any] = {
        "pe": None,
        "pb": None,
        "eps": None,
        "roe": None,
        "gross_margin": None,
        "revenue_growth_yoy": None,
        "debt_ratio": None,
        "industry": None,
        "stock_name_zh": None,
        "display_name": code,
        "valuation": None,
    }
    if not code.isdigit():
        return empty

    end = datetime.now().date()
    start_market = end - timedelta(days=400)
    start_fin = end - timedelta(days=1200)
    start_month = end - timedelta(days=800)

    out = dict(empty)

    # --- 市價指標：PER + PBR（同一 dataset TaiwanStockPER；v4 已廢除 TaiwanStockPB）---
    per_rows = _load_dataset_rows("TaiwanStockPER", code, start_market, end)
    eps_from_per: Optional[float] = None
    if per_rows:
        r = _latest_row(per_rows)
        if r:
            out["pe"] = _extract_per(r)
            out["pb"] = _extract_pbr(r)
            eps_from_per = _extract_eps(r)

    # --- TaiwanStockInfo：股名、產業 ---
    info_row = _fetch_single_tw_stock_info_row(code)
    if info_row:
        nm = _extract_stock_name(info_row)
        if nm:
            out["stock_name_zh"] = nm
        ind = _extract_industry(info_row)
        if ind:
            out["industry"] = ind
        out["display_name"] = format_tw_display_name(nm, code) if nm else code
    else:
        zh = resolve_tw_stock_name_finmind(code)
        if zh:
            out["stock_name_zh"] = zh
            out["display_name"] = format_tw_display_name(zh, code)
        else:
            out["display_name"] = code

    # --- 綜合損益：毛利率（單季）、EPS（v4 已廢除 TaiwanStockEPS，改由財報長表）---
    fin_rows = _load_dataset_rows("TaiwanStockFinancialStatements", code, start_fin, end)
    fin_by = _pivot_long_by_date(fin_rows)
    out["gross_margin"] = _latest_quarter_gross_margin_ratio(fin_by)
    fin_eps = _latest_quarter_eps(fin_by)
    # PER 表若含 EPS 優先（與本益比口徑一致）；否則用綜合損益表單季 EPS
    out["eps"] = eps_from_per if eps_from_per is not None else fin_eps
    ttm_ni = _ttm_income_after_tax(fin_by)

    # --- 資產負債表：負債比、ROE 分母 ---
    bs_rows = _load_dataset_rows("TaiwanStockBalanceSheet", code, start_fin, end)
    bs_by = _pivot_long_by_date(bs_rows)
    ta, tl, te = _latest_balance_ratios(bs_by)
    out["debt_ratio"] = _debt_ratio_raw(tl, ta)
    out["roe"] = _roe_ratio(ttm_ni, te)

    # --- 月營收：年增率 ---
    rev_rows = _load_dataset_rows("TaiwanStockMonthRevenue", code, start_month, end)
    out["revenue_growth_yoy"] = _month_revenue_yoy_ratio(rev_rows)

    normalize_tw_percent_fields(out)
    apply_tw_fundamental_sanity(out)

    return out


def fetch_tw_fundamentals_finmind(stock_code: str) -> dict[str, Any]:
    """向下相容：僅 pe / pb / eps。"""
    b = fetch_tw_fundamental_bundle(stock_code)
    return {"pe": b.get("pe"), "pb": b.get("pb"), "eps": b.get("eps")}


def _extract_stock_name(row: dict) -> Optional[str]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("stock_name", "stockname", "name", "公司名稱"):
        if k in low and low[k] not in (None, ""):
            s = str(low[k]).strip()
            if s:
                return s
    return None


def _extract_industry(row: dict) -> Optional[str]:
    low = {str(k).lower(): v for k, v in row.items()}
    for k in ("industry_category", "industry", "sector", "產業"):
        if k in low and low[k] not in (None, ""):
            return str(low[k]).strip()
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
    j = None
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


def strip_tw_trailing_code_in_name(name: str, code: str) -> str:
    """若字串尾端已有 (代號) 或 （代號），剝除，避免重複組字。"""
    s = (name or "").strip()
    c = str(code).replace(".TW", "").replace(".TWO", "").strip()
    if not s or not c:
        return s
    pat = re.compile(rf"\s*[（(]\s*{re.escape(c)}\s*[)）]\s*$")
    t = pat.sub("", s).strip()
    return t if t else s


def format_tw_display_name(zh_name: Optional[str], code: str) -> str:
    """台股顯示：台積電（2330）；無中文或名稱等於代號則只回傳 code。主標題用全形括號。"""
    c = str(code).replace(".TW", "").replace(".TWO", "").strip()
    if not zh_name or not str(zh_name).strip():
        return c
    z = strip_tw_trailing_code_in_name(str(zh_name).strip(), c)
    if not z or z == c:
        return c
    return f"{z}（{c}）"


def resolve_tw_display_name(stock_code: str, fallback_zh: Optional[str] = None) -> str:
    """
    優先 FinMind 股名，其次 fallback（如 MIS），再組合成 中文（code）。
    """
    code = str(stock_code).replace(".TW", "").replace(".TWO", "").strip()
    zh = resolve_tw_stock_name_finmind(code) or fallback_zh
    return format_tw_display_name(zh, code)


__all__ = [
    "fetch_tw_fundamentals_finmind",
    "fetch_tw_fundamental_bundle",
    "load_finmind_tw_stock_info_map",
    "resolve_tw_stock_name_finmind",
    "strip_tw_trailing_code_in_name",
    "format_tw_display_name",
    "resolve_tw_display_name",
    "normalize_percent_ratio",
    "normalize_tw_percent_fields",
    "apply_tw_fundamental_sanity",
    "tw_percent_display_to_api_ratio",
]
