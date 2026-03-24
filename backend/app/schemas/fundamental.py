"""
台股基本面統一結構（供 detail / market 層 JSON 使用）。
數值皆為「原始數字」；無資料為 null，前端可顯示為 "-"。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TaiwanStockFundamental(BaseModel):
    """FinMind 多 dataset 彙整後之台股基本面。"""

    pe: Optional[float] = Field(None, description="本益比")
    pb: Optional[float] = Field(None, description="股淨比")
    eps: Optional[float] = Field(None, description="每股盈餘（元）")
    roe: Optional[float] = Field(None, description="ROE（%，近四季稅後淨利／最新權益）")
    gross_margin: Optional[float] = Field(None, description="毛利率（%，單季）")
    revenue_growth_yoy: Optional[float] = Field(None, description="營收年增率（%，月營收 YoY）")
    debt_ratio: Optional[float] = Field(None, description="負債比（%，負債／資產）")
    valuation: Optional[str] = Field(None, description="估值評級（中文）")
    industry: Optional[str] = Field(None, description="產業別")
    stock_name_zh: Optional[str] = Field(None, description="中文簡稱（無括號）")
    display_name: str = Field("", description="頁面標題用：台積電（2330）")

    model_config = {"extra": "allow"}

    def as_dict_for_api(self) -> dict[str, Any]:
        return self.model_dump()


__all__ = ["TaiwanStockFundamental"]
