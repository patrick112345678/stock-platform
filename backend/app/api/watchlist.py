from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.watchlist import Watchlist
from app.models.user import User
from app.schemas.watchlist import (
    WatchlistCreate,
    WatchlistResponse,
    WatchlistOverviewItem,
    WatchlistOverviewResponse,
)
from app.core.security import get_current_user
from app.services.scanner_service import get_tw_symbol_to_name

import yfinance as yf
import math
import time

import pandas as pd

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:  # 舊版 yfinance
    YFRateLimitError = type("YFRateLimitError", (Exception,), {})

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _safe_float(value):
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value):
            return None
        return value
    except Exception:
        return None
def safe_get_ticker_info(ticker):
    try:
        info = ticker.info
        return info or {}
    except Exception as e:
        print("WARN ticker.info failed:", repr(e))
        return {}

def safe_get_fast_info(ticker):
    try:
        fi = ticker.fast_info
        return dict(fi) if fi else {}
    except Exception as e:
        print("WARN ticker.fast_info failed:", repr(e))
        return {}


def safe_get_history(ticker, **kwargs):
    """避免 Yahoo 限流時未捕獲導致 API 500。"""
    try:
        return ticker.history(**kwargs)
    except YFRateLimitError as e:
        print("WARN ticker.history rate limited:", repr(e))
        return pd.DataFrame()
    except Exception as e:
        print("WARN ticker.history failed:", repr(e))
        return pd.DataFrame()


def _build_quote_data(symbol: str, market: str = "US"):
    symbol = symbol.strip().upper()
    # 台股 yfinance 需要 2330.TW 格式
    if market == "TW" and not symbol.endswith(".TW"):
        symbol = symbol + ".TW"
    ticker = yf.Ticker(symbol)
    info = safe_get_ticker_info(ticker)
    fast_info = safe_get_fast_info(ticker)

    # fast_info 常能在 info 被限流時仍補到價格，且可避免多打 history()
    current_price = _safe_float(
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
        or fast_info.get("last_price")
        or fast_info.get("lastPrice")
        or fast_info.get("regular_market_price")
    )
    previous_close = _safe_float(
        info.get("previousClose")
        or info.get("regularMarketPreviousClose")
        or fast_info.get("previous_close")
        or fast_info.get("previousClose")
    )

    if current_price is None or previous_close is None:
        hist = safe_get_history(ticker, period="5d", interval="1d", auto_adjust=False)

        if hist is not None and not hist.empty:
            close_series = hist["Close"].dropna()

            if current_price is None and len(close_series) >= 1:
                current_price = _safe_float(close_series.iloc[-1])

            if previous_close is None:
                if len(close_series) >= 2:
                    previous_close = _safe_float(close_series.iloc[-2])
                elif len(close_series) == 1:
                    previous_close = _safe_float(close_series.iloc[-1])

    if current_price is None:
        return {
            "symbol": symbol,
            "price": None,
            "change": None,
            "change_percent": None,
        }

    change = None
    change_percent = None
    if previous_close not in (None, 0):
        change = round(current_price - previous_close, 4)
        change_percent = round((change / previous_close) * 100, 4)

    return {
        "symbol": symbol,
        "price": round(current_price, 4),
        "change": change,
        "change_percent": change_percent,
    }


@router.post("/", response_model=WatchlistResponse)
def add_watchlist(
    data: WatchlistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    symbol = data.symbol.strip().upper()

    existing = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id,
        Watchlist.symbol == symbol,
        Watchlist.market == data.market
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Symbol already exists in watchlist")

    item = Watchlist(
        user_id=current_user.id,
        symbol=symbol,
        market=data.market
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    tw_names = get_tw_symbol_to_name() if data.market == "TW" else {}
    name = tw_names.get(symbol.replace(".TW", "")) if data.market == "TW" else None
    return WatchlistResponse(
        id=item.id,
        user_id=item.user_id,
        symbol=item.symbol,
        market=item.market,
        name=name,
    )

from typing import Literal

@router.get("/", response_model=list[WatchlistResponse])
def get_watchlist(
    market: Literal["TW", "US", "CRYPTO"] | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id
    )

    if market:
        query = query.filter(Watchlist.market == market)

    items = query.all()
    tw_names = get_tw_symbol_to_name() if any(w.market == "TW" for w in items) else {}

    return [
        WatchlistResponse(
            id=w.id,
            user_id=w.user_id,
            symbol=w.symbol,
            market=w.market,
            name=tw_names.get(w.symbol.replace(".TW", "")) if w.market == "TW" else None,
        )
        for w in items
    ]

@router.delete("/{watchlist_id}")
def delete_watchlist(
    watchlist_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(Watchlist).filter(
        Watchlist.id == watchlist_id,
        Watchlist.user_id == current_user.id
    ).first()

    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")

    db.delete(item)
    db.commit()
    return {"message": "Deleted successfully"}


@router.get("/overview", response_model=WatchlistOverviewResponse)
def get_watchlist_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    items = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id
    ).all()

    tw_names = get_tw_symbol_to_name() if any(w.market == "TW" for w in items) else {}
    result = []
    for idx, item in enumerate(items):
        # 連續打 Yahoo 易觸發限流，略為間隔（第一筆不延遲）
        if idx > 0:
            time.sleep(0.35)
        quote = _build_quote_data(item.symbol, item.market or "US")
        name = tw_names.get(item.symbol.replace(".TW", "")) if item.market == "TW" else None
        result.append(
            WatchlistOverviewItem(
                id=item.id,
                symbol=item.symbol,
                market=item.market,
                name=name,
                price=quote["price"],
                change=quote["change"],
                change_percent=quote["change_percent"],
            )
        )

    return WatchlistOverviewResponse(items=result)