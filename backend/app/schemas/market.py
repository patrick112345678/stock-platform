from pydantic import BaseModel, Field


class MarketQuoteResponse(BaseModel):
    symbol: str = Field(..., description="查詢標的代號")
    name: str | None = Field(None, description="公司/標的名稱")
    currency: str | None = Field(None, description="幣別")
    exchange: str | None = Field(None, description="交易所")
    price: float = Field(..., description="最新價格")
    previous_close: float | None = Field(None, description="前一交易日收盤價")
    change: float | None = Field(None, description="漲跌")
    change_percent: float | None = Field(None, description="漲跌幅 (%)")


class MarketOverviewItem(BaseModel):
    symbol: str = Field(..., description="標的代號")
    name: str | None = Field(None, description="公司/標的名稱")
    price: float | None = Field(None, description="最新價格")
    change: float | None = Field(None, description="漲跌")
    change_percent: float | None = Field(None, description="漲跌幅 (%)")


class MarketOverviewResponse(BaseModel):
    items: list[MarketOverviewItem]


class MarketCandleItem(BaseModel):
    time: str = Field(..., description="K線時間，ISO格式")
    open: float = Field(..., description="開盤價")
    high: float = Field(..., description="最高價")
    low: float = Field(..., description="最低價")
    close: float = Field(..., description="收盤價")
    volume: float | None = Field(None, description="成交量")


class MarketChartResponse(BaseModel):
    symbol: str
    interval: str
    period: str
    candles: list[MarketCandleItem]