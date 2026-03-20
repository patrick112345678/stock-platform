import traceback
from fastapi import APIRouter, HTTPException, Depends
from app.schemas.ai import (
    AIAnalyzeRequest,
    AIAnalyzeResponse,
    AIOpportunitiesRequest,
    AIOpportunitiesResponse,
    AIOpportunityItem,
    AIWatchlistDailyRequest,
    AIWatchlistDailyResponse,
    AIWatchlistDailyItem,
)
from app.services.ai_service import AIService
from app.core.security import get_current_user
from app.core.ai_access import assert_ai_access
from app.models.user import User
from sqlalchemy.orm import Session
from app.db.database import SessionLocal
from app.db.database import get_db
router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/analyze", response_model=AIAnalyzeResponse)
def analyze_symbol(
    payload: AIAnalyzeRequest,
    current_user: User = Depends(get_current_user),
):
    # quick_only=True 僅規則型技術摘要，不呼叫 Gemini；完整報告需付費（或管理員／總帳號）
    if not payload.quick_only:
        assert_ai_access(current_user)
    try:
        result = AIService.analyze_symbol(
            symbol=payload.symbol,
            market=payload.market,
            interval=payload.interval,
            lang=payload.lang,
            quick_only=payload.quick_only,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print("ROUTER /ai/analyze ERROR:", repr(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"AI 分析失敗: {e}")
@router.post("/opportunities", response_model=AIOpportunitiesResponse)
def analyze_opportunities(
    payload: AIOpportunitiesRequest,
    current_user: User = Depends(get_current_user),
):
    assert_ai_access(current_user)
    try:
        result = AIService.get_ai_opportunities(
            market=payload.market,
            lang=payload.lang,
            limit=payload.limit,
            force_refresh=payload.force_refresh,
        )
        return {
            "market": result["market"],
            "updated_at": result.get("updated_at"),
            "source": result.get("source", "cache"),
            "items": [AIOpportunityItem(**item) for item in result.get("items", [])],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 機會分析失敗: {e}")
@router.post("/watchlist-daily", response_model=AIWatchlistDailyResponse)
def analyze_watchlist_daily(
    payload: AIWatchlistDailyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not payload.quick_only:
        assert_ai_access(current_user)
    try:
        results = AIService.analyze_watchlist_daily(
            db=db,
            user_id=current_user.id,
            market=payload.market,
            interval=payload.interval,
            lang=payload.lang,
            quick_only=payload.quick_only,
            limit=payload.limit,
        )
        return {
            "items": [AIWatchlistDailyItem(**item) for item in results]
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print("ROUTER /ai/watchlist-daily ERROR:", repr(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"自選股每日分析失敗: {e}")