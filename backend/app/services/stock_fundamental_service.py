"""
台股基本面：優先讀 DB（stock_fundamentals），過期或無資料才打 FinMind 並寫回。
同一 symbol 以 threading.Lock 單飛，避免並發重複打外部 API。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from app.db.database import SessionLocal
from app.models.stock_fundamental import StockFundamental
from app.services.fundamental_provider import (
    fetch_tw_fundamental_bundle,
    format_tw_display_name,
)

_log = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}


def _ttl_hours() -> float:
    try:
        return float(os.getenv("FUNDAMENTAL_TTL_HOURS", "24"))
    except ValueError:
        return 24.0


def _normalize_tw_symbol(raw: str) -> str:
    s = str(raw).strip().upper().replace(".TWO", ".TW")
    if s.endswith(".TW"):
        return s
    if s.isdigit():
        return f"{s}.TW"
    return s


def _code_from_symbol(sym: str) -> str:
    return str(sym).replace(".TW", "").replace(".TWO", "").strip()


def _is_fresh(updated_at: Optional[datetime]) -> bool:
    if updated_at is None:
        return False
    try:
        return datetime.utcnow() - updated_at < timedelta(hours=_ttl_hours())
    except Exception:
        return False


def _row_to_bundle(row: StockFundamental, code: str) -> dict[str, Any]:
    zh = row.name_zh
    return {
        "pe": row.pe,
        "pb": row.pb,
        "eps": row.eps,
        "roe": row.roe,
        "gross_margin": row.gross_margin,
        "revenue_growth_yoy": row.revenue_growth,
        "debt_ratio": row.debt_ratio,
        "industry": row.industry,
        "stock_name_zh": zh,
        "display_name": format_tw_display_name(zh, code) if zh else code,
        "valuation": None,
        "market_cap": row.market_cap,
    }


def _upsert_bundle(db, symbol: str, market: str, bundle: dict[str, Any]) -> StockFundamental:
    row = (
        db.query(StockFundamental)
        .filter(
            StockFundamental.symbol == symbol,
            StockFundamental.market == market,
        )
        .first()
    )
    if row is None:
        row = StockFundamental(symbol=symbol, market=market)
        db.add(row)

    row.name_zh = bundle.get("stock_name_zh")
    row.industry = bundle.get("industry")
    row.pe = bundle.get("pe")
    row.pb = bundle.get("pb")
    row.eps = bundle.get("eps")
    row.roe = bundle.get("roe")
    row.gross_margin = bundle.get("gross_margin")
    row.revenue_growth = bundle.get("revenue_growth_yoy")
    row.debt_ratio = bundle.get("debt_ratio")
    if bundle.get("market_cap") is not None:
        row.market_cap = bundle.get("market_cap")
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def get_tw_fundamental_bundle_cached(raw_symbol: str) -> dict[str, Any]:
    """
    與 fetch_tw_fundamental_bundle 相同欄位；優先 DB，過期／無列才 FinMind。
    """
    sym = _normalize_tw_symbol(raw_symbol)
    code = _code_from_symbol(sym)
    if not code.isdigit():
        return _empty_bundle(code)

    db = SessionLocal()
    try:
        row = (
            db.query(StockFundamental)
            .filter(
                StockFundamental.symbol == sym,
                StockFundamental.market == "TW",
            )
            .first()
        )
        if row is not None and _is_fresh(row.updated_at):
            return _row_to_bundle(row, code)
    finally:
        db.close()

    lock = _locks.setdefault(code, threading.Lock())
    with lock:
        db = SessionLocal()
        try:
            row = (
                db.query(StockFundamental)
                .filter(
                    StockFundamental.symbol == sym,
                    StockFundamental.market == "TW",
                )
                .first()
            )
            if row is not None and _is_fresh(row.updated_at):
                return _row_to_bundle(row, code)

            bundle = fetch_tw_fundamental_bundle(code)
            _upsert_bundle(db, sym, "TW", bundle)
            return bundle
        except Exception as e:
            _log.warning("fundamental refresh failed symbol=%s err=%s", sym, str(e)[:200])
            db.rollback()
            row = (
                db.query(StockFundamental)
                .filter(
                    StockFundamental.symbol == sym,
                    StockFundamental.market == "TW",
                )
                .first()
            )
            if row is not None:
                return _row_to_bundle(row, code)
            return fetch_tw_fundamental_bundle(code)
        finally:
            db.close()


def _empty_bundle(code: str) -> dict[str, Any]:
    return {
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
        "market_cap": None,
    }


def run_tw_fundamentals_daily_sync() -> None:
    """每日批次：更新台股基本面至 stock_fundamentals（受 FUNDAMENTAL_DAILY_SYNC_MAX 限制）。"""
    if os.getenv("ENABLE_FUNDAMENTAL_DAILY_SYNC", "true").lower() not in ("1", "true", "yes", "on"):
        _log.info("ENABLE_FUNDAMENTAL_DAILY_SYNC 已關閉，跳過基本面日同步")
        return

    try:
        max_n = int(os.getenv("FUNDAMENTAL_DAILY_SYNC_MAX", "500"))
    except ValueError:
        max_n = 500

    from app.services.scanner_service import get_tw_universe

    syms = get_tw_universe("ALL")
    n_ok = 0
    for i, yf_sym in enumerate(syms):
        if i >= max_n:
            break
        code = _code_from_symbol(str(yf_sym))
        if not code.isdigit():
            continue
        sym = _normalize_tw_symbol(str(yf_sym))
        lock = _locks.setdefault(code, threading.Lock())
        with lock:
            db = SessionLocal()
            try:
                bundle = fetch_tw_fundamental_bundle(code)
                _upsert_bundle(db, sym, "TW", bundle)
                n_ok += 1
            except Exception as e:
                _log.warning("daily sync fail symbol=%s err=%s", sym, str(e)[:160])
                db.rollback()
            finally:
                db.close()

    _log.info("tw_fundamentals daily sync done processed=%s ok=%s cap=%s", min(len(syms), max_n), n_ok, max_n)


__all__ = [
    "get_tw_fundamental_bundle_cached",
    "run_tw_fundamentals_daily_sync",
    "_normalize_tw_symbol",
]
