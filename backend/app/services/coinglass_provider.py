"""
CoinGlass：規劃為「補強資料源」，用於 funding rate、open interest、liquidation、市場情緒等進階欄位。

⚠️ 請勿將 CoinGlass 接入基本 OHLC / quote / chart 主流程；主 K 線預設 Binance，Bybit 為可選備援。

整合步驟建議：
1. 設定環境變數（例如 COINGLASS_API_KEY）並在此檔實作 HTTP client。
2. 新增獨立 API 或欄位（例如 /market/crypto-metrics），由前端選用。
3. 與 scanner_cache / 現有 JSON 欄位分離，避免破壞既有契約。
"""

from __future__ import annotations

__all__: list[str] = []
