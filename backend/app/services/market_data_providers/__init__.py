"""
統一行情資料入口（Facade）。

實作分散於：
- `app.services.stock_data_service`：台／股日線快取與來源分流
- `app.services.market_service`：quote / detail / chart / 技術指標組裝
- `app.services.scanner_service`：掃描用 K 線（加密 Bybit→Binance）

路由層請維持呼叫既有 `market` API；此模組僅供內部／測試清楚對應「依市場取數」。
"""

from app.services.market_service import (
    get_chart_data,
    get_detail_data,
    get_market_data,
    get_quote_data,
)

__all__ = [
    "get_quote_data",
    "get_chart_data",
    "get_detail_data",
    "get_market_data",
]
