"""台股基本面快取（低頻同步 FinMind，查詢優先讀 DB）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint

from app.db.database import Base


class StockFundamental(Base):
    __tablename__ = "stock_fundamentals"
    __table_args__ = (UniqueConstraint("symbol", "market", name="uq_stock_fundamentals_symbol_market"),)

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    market = Column(String(8), nullable=False, index=True, default="TW")
    name_zh = Column(String(128), nullable=True)
    industry = Column(String(256), nullable=True)
    pe = Column(Float, nullable=True)
    pb = Column(Float, nullable=True)
    eps = Column(Float, nullable=True)
    roe = Column(Float, nullable=True)
    gross_margin = Column(Float, nullable=True)
    revenue_growth = Column(Float, nullable=True)
    debt_ratio = Column(Float, nullable=True)
    market_cap = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
