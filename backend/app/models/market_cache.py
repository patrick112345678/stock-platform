"""行情快取：price_cache（報價）、chart_cache（日線 OHLCV JSON）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, String, Text

from app.db.database import Base


class PriceCache(Base):
    __tablename__ = "price_cache"

    symbol = Column(String(64), primary_key=True, nullable=False)
    market = Column(String(16), primary_key=True, nullable=False)
    price = Column(Float, nullable=True)
    previous_close = Column(Float, nullable=True)
    change = Column(Float, nullable=True)
    change_percent = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    currency = Column(String(16), nullable=True)
    exchange = Column(String(64), nullable=True)
    name = Column(String(512), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChartCache(Base):
    __tablename__ = "chart_cache"

    symbol = Column(String(64), primary_key=True, nullable=False)
    market = Column(String(16), primary_key=True, nullable=False)
    interval = Column(String(32), primary_key=True, nullable=False)
    period = Column(String(32), primary_key=True, nullable=False)
    ohlcv_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
