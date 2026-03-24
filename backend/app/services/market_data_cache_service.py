"""
行情 DB 快取：price_cache / chart_cache。
同一 (symbol, market) 以 threading.Lock 單飛，避免單次請求或並發重複打外部 API。
"""

from __future__ import annotations

import io
import os
import threading
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Callable, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.market_cache import ChartCache, PriceCache

_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()

CHART_CANONICAL_INTERVAL = "1d"
CHART_CANONICAL_PERIOD = "2y"
CRYPTO_BARS_PERIOD = "bars200"

# 程序內短期快取：同一 symbol 在短時間內多次 endpoint（quote/detail/chart/signal）共用，減少重複讀 DB／重算
_HOT_HIST: dict[str, tuple[float, pd.DataFrame]] = {}
_HOT_QUOTE: dict[str, tuple[float, dict[str, Any]]] = {}
_HOT_LOCK = threading.Lock()


def _hot_ttl_seconds() -> float:
    try:
        return float(os.getenv("MARKET_HOT_CACHE_SECONDS", "45"))
    except ValueError:
        return 45.0


def _hot_hist_key(symbol: str, market: str) -> str:
    return f"{market}:{symbol}:{CHART_CANONICAL_INTERVAL}:{CHART_CANONICAL_PERIOD}"


def _hot_quote_key(symbol: str, market: str) -> str:
    return f"Q:{market}:{symbol}"


def _hot_get_hist(key: str) -> Optional[pd.DataFrame]:
    t0 = monotonic()
    with _HOT_LOCK:
        ent = _HOT_HIST.get(key)
        if not ent:
            return None
        ts, df = ent
        if t0 - ts > _hot_ttl_seconds():
            del _HOT_HIST[key]
            return None
        try:
            return df.copy()
        except Exception:
            return None


def _hot_set_hist(key: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    with _HOT_LOCK:
        _HOT_HIST[key] = (monotonic(), df.copy())


def _hot_get_quote(key: str) -> Optional[dict[str, Any]]:
    t0 = monotonic()
    with _HOT_LOCK:
        ent = _HOT_QUOTE.get(key)
        if not ent:
            return None
        ts, d = ent
        if t0 - ts > _hot_ttl_seconds():
            del _HOT_QUOTE[key]
            return None
        return dict(d)


def _hot_set_quote(key: str, data: dict[str, Any]) -> None:
    if not data:
        return
    with _HOT_LOCK:
        _HOT_QUOTE[key] = (monotonic(), dict(data))


def try_read_fresh_stock_hist_from_chart_cache(symbol: str, market: str) -> Optional[pd.DataFrame]:
    """若 chart_cache 已有新鮮日線，供 quote 與 hist 共用，避免同一請求重複 get_cached_data。"""
    row = read_chart_row(symbol, market, CHART_CANONICAL_INTERVAL, CHART_CANONICAL_PERIOD)
    if row and is_fresh(row.updated_at, _chart_ttl_minutes()):
        try:
            return chart_row_to_df(row)
        except Exception:
            return None
    return None


def _price_ttl_minutes() -> int:
    try:
        return int(os.getenv("MARKET_PRICE_TTL_MINUTES", "10"))
    except ValueError:
        return 10


def _chart_ttl_minutes() -> int:
    try:
        return int(os.getenv("MARKET_CHART_TTL_MINUTES", "10"))
    except ValueError:
        return 10


def is_fresh(updated_at: Optional[datetime], minutes: int) -> bool:
    if updated_at is None:
        return False
    try:
        return datetime.utcnow() - updated_at < timedelta(minutes=minutes)
    except Exception:
        return False


def _lock_key(symbol: str, market: str) -> str:
    return f"{market}:{symbol}"


def _get_lock(k: str) -> threading.Lock:
    with _locks_lock:
        if k not in _locks:
            _locks[k] = threading.Lock()
        return _locks[k]


def _session() -> Session:
    return SessionLocal()


def _hist_df_to_json(df: pd.DataFrame) -> str:
    d = df.copy()
    d = d.reset_index()
    first = d.columns[0]
    d = d.rename(columns={first: "_idx"})
    return d.to_json(orient="records", date_format="iso")


def _hist_json_to_df(js: str) -> pd.DataFrame:
    df = pd.read_json(io.StringIO(js), orient="records")
    if "_idx" not in df.columns:
        return pd.DataFrame()
    df["_idx"] = pd.to_datetime(df["_idx"])
    df = df.set_index("_idx")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_index()


def read_price_row(symbol: str, market: str) -> Optional[PriceCache]:
    db = _session()
    try:
        return (
            db.query(PriceCache)
            .filter(PriceCache.symbol == symbol, PriceCache.market == market)
            .first()
        )
    finally:
        db.close()


def price_row_to_quote_dict(row: PriceCache) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.name or row.symbol,
        "currency": row.currency,
        "exchange": row.exchange,
        "price": row.price if row.price is not None else 0.0,
        "previous_close": row.previous_close,
        "change": row.change,
        "change_percent": row.change_percent,
    }


def upsert_price_row(
    symbol: str,
    market: str,
    *,
    price: Optional[float],
    previous_close: Optional[float],
    change: Optional[float],
    change_percent: Optional[float],
    volume: Optional[float],
    currency: Optional[str],
    exchange: Optional[str],
    name: Optional[str],
) -> None:
    db = _session()
    try:
        row = (
            db.query(PriceCache)
            .filter(PriceCache.symbol == symbol, PriceCache.market == market)
            .first()
        )
        now = datetime.utcnow()
        if row is None:
            row = PriceCache(symbol=symbol, market=market, updated_at=now)
            db.add(row)
        row.price = price
        row.previous_close = previous_close
        row.change = change
        row.change_percent = change_percent
        row.volume = volume
        row.currency = currency
        row.exchange = exchange
        row.name = name
        row.updated_at = now
        db.commit()
    finally:
        db.close()


def upsert_price_from_scanner_item(item: dict[str, Any], market_upper: str) -> None:
    """排程寫入 scanner_cache 時同步寫入 price_cache。"""
    sym = str(item.get("symbol", "")).strip()
    if not sym:
        return
    mkt = market_upper.upper()
    price = item.get("price")
    cp = item.get("change_percent")
    chg = item.get("change")
    vol = item.get("volume")
    name = item.get("name")
    exch = item.get("exchange")
    prev = None
    try:
        if price is not None and chg is not None:
            prev = float(price) - float(chg)
    except (TypeError, ValueError):
        prev = None
    upsert_price_row(
        sym,
        mkt,
        price=float(price) if price is not None else None,
        previous_close=prev,
        change=float(chg) if chg is not None else None,
        change_percent=float(cp) if cp is not None else None,
        volume=float(vol) if vol is not None else None,
        currency="TWD" if mkt == "TW" else ("USDT" if mkt == "CRYPTO" else None),
        exchange=str(exch) if exch else None,
        name=str(name) if name else None,
    )


def read_chart_row(symbol: str, market: str, interval: str, period: str) -> Optional[ChartCache]:
    db = _session()
    try:
        return (
            db.query(ChartCache)
            .filter(
                ChartCache.symbol == symbol,
                ChartCache.market == market,
                ChartCache.interval == interval,
                ChartCache.period == period,
            )
            .first()
        )
    finally:
        db.close()


def upsert_chart_df(
    symbol: str,
    market: str,
    interval: str,
    period: str,
    df: pd.DataFrame,
) -> None:
    if df is None or df.empty:
        return
    db = _session()
    try:
        row = (
            db.query(ChartCache)
            .filter(
                ChartCache.symbol == symbol,
                ChartCache.market == market,
                ChartCache.interval == interval,
                ChartCache.period == period,
            )
            .first()
        )
        now = datetime.utcnow()
        payload = _hist_df_to_json(df)
        if row is None:
            row = ChartCache(
                symbol=symbol,
                market=market,
                interval=interval,
                period=period,
                ohlcv_json=payload,
                updated_at=now,
            )
            db.add(row)
        else:
            row.ohlcv_json = payload
            row.updated_at = now
        db.commit()
    finally:
        db.close()


def chart_row_to_df(row: ChartCache) -> pd.DataFrame:
    return _hist_json_to_df(row.ohlcv_json)


def get_or_refresh_stock_quote(
    symbol: str,
    market: str,
    fetch_live: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    hq = _hot_get_quote(_hot_quote_key(symbol, market))
    if hq is not None:
        return hq

    lk = _get_lock(_lock_key(symbol, market))
    with lk:
        row = read_price_row(symbol, market)
        if row and is_fresh(row.updated_at, _price_ttl_minutes()):
            out = price_row_to_quote_dict(row)
            _hot_set_quote(_hot_quote_key(symbol, market), out)
            return out
        data = fetch_live()
        upsert_price_row(
            symbol,
            market,
            price=float(data.get("price")) if data.get("price") is not None else None,
            previous_close=float(data.get("previous_close")) if data.get("previous_close") is not None else None,
            change=float(data.get("change")) if data.get("change") is not None else None,
            change_percent=float(data.get("change_percent")) if data.get("change_percent") is not None else None,
            volume=float(data.get("volume")) if data.get("volume") is not None else None,
            currency=data.get("currency"),
            exchange=data.get("exchange"),
            name=data.get("name"),
        )
        _hot_set_quote(_hot_quote_key(symbol, market), data)
        return data


def get_or_refresh_crypto_quote(
    symbol: str,
    fetch_live: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    return get_or_refresh_stock_quote(symbol, "CRYPTO", fetch_live)


def get_or_refresh_stock_hist_df(
    symbol: str,
    market: str,
    fetch_live: Callable[[], Optional[pd.DataFrame]],
) -> Optional[pd.DataFrame]:
    hk = _hot_hist_key(symbol, market)
    mem = _hot_get_hist(hk)
    if mem is not None:
        return mem

    lk = _get_lock(_lock_key(symbol, market))
    with lk:
        row = read_chart_row(symbol, market, CHART_CANONICAL_INTERVAL, CHART_CANONICAL_PERIOD)
        if row and is_fresh(row.updated_at, _chart_ttl_minutes()):
            try:
                df = chart_row_to_df(row)
                _hot_set_hist(hk, df)
                return df.copy()
            except Exception:
                pass
        df = fetch_live()
        if df is None or df.empty:
            return None
        upsert_chart_df(symbol, market, CHART_CANONICAL_INTERVAL, CHART_CANONICAL_PERIOD, df)
        _hot_set_hist(hk, df)
        return df.copy()


def get_or_refresh_crypto_kline_df(
    symbol: str,
    interval: str,
    fetch_live: Callable[[], pd.DataFrame],
) -> pd.DataFrame:
    ck = f"CRYPTO:{symbol}:{interval}:{CRYPTO_BARS_PERIOD}"
    mem = _hot_get_hist(ck)
    if mem is not None:
        return mem

    lk = _get_lock(_lock_key(symbol, "CRYPTO"))
    with lk:
        row = read_chart_row(symbol, "CRYPTO", interval, CRYPTO_BARS_PERIOD)
        if row and is_fresh(row.updated_at, _chart_ttl_minutes()):
            try:
                df = chart_row_to_df(row)
                _hot_set_hist(ck, df)
                return df.copy()
            except Exception:
                pass
        df = fetch_live()
        if df is None or df.empty:
            return pd.DataFrame()
        upsert_chart_df(symbol, "CRYPTO", interval, CRYPTO_BARS_PERIOD, df)
        _hot_set_hist(ck, df)
        return df.copy()


def upsert_chart_from_scanner_df(df: pd.DataFrame, symbol: str, market: str) -> None:
    """排程寫入 scanner 時同步寫入日線 OHLCV（鍵 1d/2y）。"""
    if df is None or df.empty:
        return
    try:
        upsert_chart_df(symbol, market.upper(), CHART_CANONICAL_INTERVAL, CHART_CANONICAL_PERIOD, df)
    except Exception:
        pass
