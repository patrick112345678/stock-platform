from typing import List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.database import SessionLocal
from app.models.user import User
from app.models.watchlist import Watchlist
from app.schemas.scanner import (
    LeaderboardItem,
    OpportunityItem,
    ScannerFilterRequest,
)
from app.services.scanner_service import (
    get_leaderboard,
    get_opportunities,
    get_watchlist_opportunities,
    filter_symbols,
    is_tw_market_hours,
    refresh_tw_cache,
)

router = APIRouter(prefix="/scanner", tags=["scanner"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/leaderboard", response_model=List[LeaderboardItem])
def scanner_leaderboard(
    background_tasks: BackgroundTasks,
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
    pool: str = Query("TOP100"),
    sort: str = Query("change_percent", pattern="^(change_percent|volume)$"),
    limit: int = Query(20, ge=1, le=100),
    sort_direction: str = Query("gainers", pattern="^(gainers|losers)$"),
):
    try:
        # 台股 9:00-13:30：先回快取，背景更新
        if market == "TW" and is_tw_market_hours():
            background_tasks.add_task(refresh_tw_cache)  # 背景更新快取，下次請求會拿到新資料
        return get_leaderboard(market=market, pool=pool, sort_by=sort, limit=limit, sort_direction=sort_direction)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"排行榜取得失敗: {str(e)}")
        
@router.get("/opportunities")
def scanner_opportunities(
    market: str = Query("US"),
    pool: str = Query("TOP100"),
    limit: int = 20
):
    try:
        return get_opportunities(market=market, pool=pool, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"今日機會取得失敗: {str(e)}")

@router.post("/filter")
def scanner_filter(req: ScannerFilterRequest):
    """選股器：回傳 {items: [...], source: 'live'|'cache'}"""
    try:
        result = filter_symbols(req)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"選股器執行失敗: {str(e)}")


@router.get("/watchlist")
def scanner_watchlist(
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取得使用者自選股的技術分析結果（需登入）"""
    try:
        items = (
            db.query(Watchlist)
            .filter(
                Watchlist.user_id == current_user.id,
                Watchlist.market == market,
            )
            .all()
        )
        watchlist_items = [
            {"symbol": w.symbol, "market": w.market} for w in items
        ]
        return get_watchlist_opportunities(
            watchlist_items=watchlist_items,
            market=market,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"自選股分析失敗: {str(e)}")