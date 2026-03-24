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
from app.services.market_service import get_quote_data

import math
import time

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


def _build_quote_data(symbol: str, market: str = "US"):
    """
    與 /market/quote 相同資料層：台股 TWSE 官方 + MIS、美股 Yahoo、加密 Bybit→Binance。
    不再使用 yfinance 直連，避免與行情 API 重複且策略不一致。
    """
    m = str(market or "US").strip().upper()
    if m not in ("TW", "US", "CRYPTO"):
        m = "US"
    try:
        q = get_quote_data(symbol, m)
        return {
            "symbol": q.get("symbol") or str(symbol).strip().upper(),
            "price": _safe_float(q.get("price")),
            "change": _safe_float(q.get("change")),
            "change_percent": _safe_float(q.get("change_percent")),
        }
    except Exception as e:
        print("WARN watchlist _build_quote_data:", symbol, m, repr(e))
        return {
            "symbol": str(symbol).strip().upper(),
            "price": None,
            "change": None,
            "change_percent": None,
        }


@router.post("", response_model=WatchlistResponse)
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

@router.get("", response_model=list[WatchlistResponse])
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
        # 行情層已有 60s 快取；保留少量間隔降低外部 API 瞬間壓力
        if idx > 0:
            time.sleep(0.08)
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