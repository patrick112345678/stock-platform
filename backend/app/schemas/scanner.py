from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime 
class LeaderboardItem(BaseModel):
    id: Optional[int] = None
    symbol: str
    name: Optional[str] = None
    exchange: str
    market: str
    price: float
    change: Optional[float] = None
    change_percent: float
    volume: float = 0
    score: Optional[float] = None
    signals: Optional[List[str]] = None
    summary: Optional[str] = None
    trend: Optional[str] = None
    pattern: Optional[str] = None
    volume_ratio_30d: Optional[float] = None
    rsi: Optional[float] = None
    funding_rate: Optional[float] = None
    updated_at: Optional[datetime] = None


class ScannerFilterRequest(BaseModel):
    market: str = Field(default="US", pattern="^(TW|US|CRYPTO)$")
    pool: str = Field(default="TOP100", pattern="^(TOP30|TOP100|TOP800|ALL)$")
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_volume: Optional[float] = None
    min_change_percent: Optional[float] = None
    max_change_percent: Optional[float] = None
    above_ma20: bool = False
    above_ma60: bool = False
    macd_bullish: bool = False
    only_breakout: bool = False  # 只看接近突破（v14 對應）
    only_bull: bool = False      # 只看多頭排列（v14 對應，等同 above_ma60）
    rsi_min: Optional[float] = None
    rsi_max: Optional[float] = None
    limit: int = 20
    # 選股器 v14 條件（參考 ai_members_crypto）
    volume_ratio_30d_min: Optional[float] = None  # 30天內成交量爆量1.5倍以上
    breakout_30d: bool = False   # 突破30天最高價
    macd_golden: bool = False   # MACD 黃金交叉
    macd_death: bool = False    # MACD 死亡交叉
    sort_option: Optional[str] = None  # "5. 漲幅排行榜" | "6. 跌幅排行榜"


class OpportunityItem(BaseModel):
    symbol: str
    name: str = ""
    market: str
    exchange: str
    price: float = 0
    change: float = 0
    change_percent: float = 0
    volume: float = 0
    score: int = 0
    signals: List[str] = []
    summary: str = ""