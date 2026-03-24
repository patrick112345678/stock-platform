from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.watchlist import Watchlist
from app.models.user import User
from app.schemas.watchlist import (
    WatchlistCreate,
    WatchlistDeleteBySymbol,
    WatchlistResponse,
    WatchlistOverviewItem,
    WatchlistOverviewResponse,
)
from app.core.security import get_current_user
from app.services.scanner_service import get_tw_symbol_to_chinese_only
from app.services.fundamental_provider import format_tw_display_name
from app.services.market_service import (
    get_quote_data,
    normalize_crypto_symbol,
    normalize_stock_symbol,
)
from app.services.stock_fundamental_service import get_tw_fundamental_bundle_cached

import math
import time

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def normalize_watchlist_symbol(symbol: str, market: str) -> str:
    """DB 與 API 統一：TW 存純代號 2330；CRYPTO→XXXUSDT；US→大寫代號。行情層會自行補 .TW。"""
    m = str(market).strip().upper()
    s = str(symbol).strip().upper()
    if m == "TW":
        sym = normalize_stock_symbol(s)
        return sym.replace(".TW", "").replace(".TWO", "").strip() or sym
    if m == "CRYPTO":
        return normalize_crypto_symbol(s)
    return s.replace(".TW", "").replace(".TWO", "").strip() or s


def _tw_list_display_name(symbol: str) -> str | None:
    """
    台股自選股顯示名稱：台積電（2330）。
    使用中文簡稱 + 代號單次組字，避免與 get_tw_symbol_to_name 重複括號邏輯。
    前端請只顯示此欄位，勿再串 symbol。
    """
    code = symbol.replace(".TW", "").replace(".TWO", "").strip()
    cn_map = get_tw_symbol_to_chinese_only()
    zh = cn_map.get(code)
    if not zh or zh == code:
        return code
    return format_tw_display_name(zh, code)


def _list_display_name(symbol: str, market: str) -> str:
    """列表／overview 必回傳 name：台股中文，其餘市場用代號。"""
    m = str(market).strip().upper()
    if m == "TW":
        return _tw_list_display_name(symbol) or symbol
    return symbol


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
    symbol = normalize_watchlist_symbol(data.symbol, data.market)

    existing = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id,
        Watchlist.market == data.market,
    ).all()
    for row in existing:
        if normalize_watchlist_symbol(row.symbol, row.market) == symbol:
            raise HTTPException(status_code=400, detail="Symbol already exists in watchlist")

    item = Watchlist(
        user_id=current_user.id,
        symbol=symbol,
        market=data.market
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return WatchlistResponse(
        id=item.id,
        user_id=item.user_id,
        symbol=item.symbol,
        market=item.market,
        name=_list_display_name(item.symbol, item.market),
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
    return [
        WatchlistResponse(
            id=w.id,
            user_id=w.user_id,
            symbol=normalize_watchlist_symbol(w.symbol, w.market),
            market=w.market,
            name=_list_display_name(w.symbol, w.market),
        )
        for w in items
    ]


@router.delete("/by-symbol")
def delete_watchlist_by_symbol(
    data: WatchlistDeleteBySymbol,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """以正規化後的 symbol + market 刪除（相容 DB 內 2330 / 2330.TW 等舊格式）。"""
    norm = normalize_watchlist_symbol(data.symbol, data.market)
    raw = str(data.symbol).strip().upper()
    item = (
        db.query(Watchlist)
        .filter(
            Watchlist.user_id == current_user.id,
            Watchlist.market == data.market,
            Watchlist.symbol.in_([norm, raw]),
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")

    deleted_id = item.id
    db.delete(item)
    db.commit()
    return {"message": "Deleted successfully", "id": deleted_id}


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
    return {"message": "Deleted successfully", "id": watchlist_id}


@router.get("/overview", response_model=WatchlistOverviewResponse)
def get_watchlist_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    items = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id
    ).all()

    result = []
    for idx, item in enumerate(items):
        # 行情層已有 60s 快取；保留少量間隔降低外部 API 瞬間壓力
        if idx > 0:
            time.sleep(0.08)
        mkt = item.market or "US"
        sym_out = normalize_watchlist_symbol(item.symbol, mkt)
        quote = _build_quote_data(sym_out, mkt)
        pb = eps = None
        if mkt == "TW":
            try:
                fund = get_tw_fundamental_bundle_cached(sym_out)
                pb = _safe_float(fund.get("pb"))
                eps = _safe_float(fund.get("eps"))
            except Exception:
                pass
        result.append(
            WatchlistOverviewItem(
                id=item.id,
                symbol=sym_out,
                market=item.market,
                name=_list_display_name(item.symbol, mkt),
                price=quote["price"],
                change=quote["change"],
                change_percent=quote["change_percent"],
                pb=pb,
                eps=eps,
            )
        )

    return WatchlistOverviewResponse(items=result)