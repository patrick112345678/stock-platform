from pydantic import BaseModel
from typing import Literal


class WatchlistBase(BaseModel):
    symbol: str
    market: Literal["TW", "US", "CRYPTO"]


class WatchlistCreate(WatchlistBase):
    pass


class WatchlistResponse(WatchlistBase):
    id: int
    user_id: int
    name: str | None = None

    class Config:
        from_attributes = True


class WatchlistOverviewItem(BaseModel):
    id: int
    symbol: str
    market: Literal["TW", "US", "CRYPTO"]
    name: str | None = None
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None


class WatchlistOverviewResponse(BaseModel):
    items: list[WatchlistOverviewItem]