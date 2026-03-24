from pydantic import BaseModel, Field
from typing import Literal


class WatchlistBase(BaseModel):
    symbol: str
    market: Literal["TW", "US", "CRYPTO"]


class WatchlistCreate(WatchlistBase):
    pass


class WatchlistDeleteBySymbol(WatchlistBase):
    """與 POST 相同欄位；用於無法可靠傳遞數字 id 時，以正規化後的 symbol+market 刪除。"""


class WatchlistResponse(WatchlistBase):
    id: int
    user_id: int
    name: str = Field(..., description="顯示名稱；台股為中文簡稱，其餘市場為代號")

    class Config:
        from_attributes = True


class WatchlistOverviewItem(BaseModel):
    id: int
    symbol: str
    market: Literal["TW", "US", "CRYPTO"]
    name: str = Field(..., description="顯示名稱；台股為中文簡稱，其餘市場為代號")
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    pb: float | None = Field(None, description="股價淨值比（台股來自基本面快取；其他市場暫無）")
    eps: float | None = Field(None, description="每股盈餘（台股來自基本面快取；其他市場暫無）")


class WatchlistOverviewResponse(BaseModel):
    items: list[WatchlistOverviewItem]