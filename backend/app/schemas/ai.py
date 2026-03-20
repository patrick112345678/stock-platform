from pydantic import BaseModel, Field
from typing import Optional, List
from typing import Optional, Any
from typing import Optional, List, Literal, Any

class AIReportItem(BaseModel):
    model_config = {"extra": "allow"}  # 允許額外欄位向下相容

    trend: Optional[str] = None
    valuation: Optional[str] = None
    risk: Optional[str] = None
    summary: Optional[str] = None
    action: Optional[str] = None
    action_short: Optional[str] = None
    confidence: Optional[int] = None
    confidence_detail: Optional[dict] = None  # { overall, fundamental, technical, industry }
    fundamental: Optional[str] = None
    technical: Optional[Any] = None
    industry: Optional[str] = None
    risk_opportunity: Optional[str] = None
    strategy: Optional[str] = None
    rating: Optional[dict] = None
    action_detail: Optional[dict] = None
    fundamental_detail: Optional[dict] = None

class AIAnalyzeResponse(BaseModel):
    symbol: str
    name: Optional[str] = None
    market: str
    interval: str
    quick_summary: dict
    ai_report: Optional[AIReportItem] = None

class AIWatchlistDailyRequest(BaseModel):
    market: Optional[Literal["TW", "US", "CRYPTO"]] = None
    interval: str = Field(default="1d")
    lang: str = Field(default="zh")
    quick_only: bool = Field(default=False)
    limit: int = Field(default=20)


class AIWatchlistDailyItem(BaseModel):
    watchlist_id: int
    symbol: str
    market: str
    name: Optional[str] = None
    interval: str
    quick_summary: dict
    ai_report: Optional[AIReportItem] = None
    error: Optional[str] = None


class AIWatchlistDailyResponse(BaseModel):
    items: List[AIWatchlistDailyItem]
    
class AIAnalyzeRequest(BaseModel):
    symbol: str
    market: str = Field(default="US")   # TW / US / Crypto
    interval: str = Field(default="1d")
    lang: str = Field(default="zh")
    quick_only: bool = Field(default=True)



class AIOpportunitiesRequest(BaseModel):
    market: str = Field(default="TW")   # TW / US / Crypto
    scan_mode: str = Field(default="core")
    limit: int = Field(default=8)
    lang: str = Field(default="zh")
    force_refresh: bool = Field(default=False)


class AIOpportunityItem(BaseModel):
    symbol: str
    name: Optional[str] = None
    score: Optional[int] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    reason: Optional[str] = None
    risk: Optional[str] = None


class AIOpportunitiesResponse(BaseModel):
    market: str
    items: List[AIOpportunityItem]