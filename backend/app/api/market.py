from fastapi import APIRouter, HTTPException, Query

from app.schemas.market import (
    MarketQuoteResponse,
    MarketOverviewItem,
    MarketOverviewResponse,
    MarketChartResponse,
)

from app.services.market_service import (
    get_quote_data,
    get_detail_data,
    get_chart_data,
    get_multi_timeframe_summary,
    get_technical_signal_table,
    get_peer_symbols,
)
from app.services.scanner_service import (
    get_tw_universe,
    get_tw_search_items,
    get_us_universe,
    get_us_search_items,
    get_crypto_universe,
)
router = APIRouter(prefix="/market", tags=["market"])


TW_MASTER = [
    {"symbol": "2330", "name": "台積電", "market": "TW", "exchange": "TWSE"},
    {"symbol": "2317", "name": "鴻海", "market": "TW", "exchange": "TWSE"},
    {"symbol": "2454", "name": "聯發科", "market": "TW", "exchange": "TWSE"},
]

US_MASTER = [
    {"symbol": "NVDA", "name": "NVIDIA Corporation", "market": "US", "exchange": "NASDAQ"},
    {"symbol": "MSFT", "name": "Microsoft Corporation", "market": "US", "exchange": "NASDAQ"},
    {"symbol": "AAPL", "name": "Apple Inc.", "market": "US", "exchange": "NASDAQ"},
]

CRYPTO_MASTER = [
    {"symbol": "BTCUSDT", "name": "Bitcoin", "market": "CRYPTO", "exchange": "BYBIT"},
    {"symbol": "ETHUSDT", "name": "Ethereum", "market": "CRYPTO", "exchange": "BYBIT"},
    {"symbol": "SOLUSDT", "name": "Solana", "market": "CRYPTO", "exchange": "BYBIT"},
]

@router.get("/quote", response_model=MarketQuoteResponse)
def get_market_quote(
    symbol: str = Query(..., min_length=1),
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
):
    try:
        data = get_quote_data(symbol, market)
        return MarketQuoteResponse(**data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"取得報價失敗: {str(e)}")


@router.get("/overview", response_model=MarketOverviewResponse)
def get_market_overview(
    symbols: str = Query(...),
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
):
    try:
        raw_symbols = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not raw_symbols:
            raise HTTPException(status_code=400, detail="請提供至少一個 symbol")

        items = []
        for symbol in raw_symbols:
            try:
                data = get_quote_data(symbol, market)
                items.append(
                    MarketOverviewItem(
                        symbol=data["symbol"],
                        name=data["name"],
                        price=data["price"],
                        change=data["change"],
                        change_percent=data["change_percent"],
                    )
                )
            except Exception:
                items.append(
                    MarketOverviewItem(
                        symbol=symbol,
                        name=None,
                        price=None,
                        change=None,
                        change_percent=None,
                    )
                )

        return MarketOverviewResponse(items=items)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"取得 overview 失敗: {str(e)}")


@router.get("/chart", response_model=MarketChartResponse)
def get_market_chart(
    symbol: str = Query(..., min_length=1),
    interval: str = Query("1d"),
    period: str = Query("3mo"),
):
    try:
        return get_chart_data(symbol=symbol, interval=interval, period=period)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"取得 chart 失敗: {str(e)}")


@router.get("/multi-timeframe")
def get_multi_timeframe(
    symbol: str = Query(..., min_length=1),
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
    lang: str = Query("zh"),
):
    """多時間框架總覽：1h, 4h, 1d, 1wk 的趨勢、價格、RSI、訊號分數"""
    try:
        return get_multi_timeframe_summary(symbol=symbol, market=market, lang=lang)
    except HTTPException:
        raise
    except Exception as e:
        print("WARN multi-timeframe failed:", repr(e))
        return [{"period": "1h", "trend": "無資料", "price": "N/A", "rsi": "N/A", "score": "N/A"}]


@router.get("/signal-table")
def get_signal_table(
    symbol: str = Query(..., min_length=1),
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
    lang: str = Query("zh"),
):
    """技術訊號總表：均線、RSI、MACD、關鍵價位、成交量、型態"""
    try:
        return get_technical_signal_table(symbol=symbol, market=market, lang=lang)
    except HTTPException:
        raise
    except Exception as e:
        print("WARN signal-table failed:", repr(e))
        return []


@router.get("/detail")
def get_market_detail(
    symbol: str = Query(..., min_length=1),
    market: str = Query("US", pattern="^(TW|US|CRYPTO)$"),
):
    """取得標的詳細資料：52週、市值、產業、基本面等"""
    try:
        return get_detail_data(symbol, market)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"取得詳細資料失敗: {str(e)}")


@router.get("/peers")
def get_market_peers(
    symbol: str = Query(..., min_length=1),
    market: str = Query("US", pattern="^(TW|US)$"),
    max_peers: int = Query(6, ge=1, le=20),
):
    """取得同業代號清單"""
    try:
        symbols = get_peer_symbols(symbol, market, max_peers)
        items = []
        for sym in symbols:
            try:
                data = get_quote_data(sym, market)
                items.append({
                    "symbol": data["symbol"],
                    "name": data.get("name"),
                    "price": data.get("price"),
                    "change": data.get("change"),
                    "change_percent": data.get("change_percent"),
                })
            except Exception:
                items.append({"symbol": sym, "name": None, "price": None, "change": None, "change_percent": None})
        return {"peers": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"取得同業資料失敗: {str(e)}")


@router.get("/search")
def search_market(
    q: str = Query("", min_length=0),
    market: str | None = Query(None, pattern="^(TW|US|CRYPTO)$"),
    limit: int = Query(10, ge=1, le=50),
):
    keyword = q.strip()
    if not keyword:
        return []

    data = []
    keyword_lower = keyword.lower()

    def append_search_item(symbol: str, name: str, market_name: str, exchange: str):
        if not symbol:
            return
        data.append({
            "symbol": symbol,
            "name": name or symbol,
            "market": market_name,
            "exchange": exchange,
        })

    try:
        if market in (None, "", "TW"):
            tw_items = get_tw_search_items()
            for item in tw_items:
                raw_symbol = str(item.get("symbol", "")).strip().upper()
                name = str(item.get("name", raw_symbol)).strip()
                if raw_symbol:
                    append_search_item(raw_symbol, name, "TW", "TWSE")

        if market in (None, "", "US"):
            us_items = get_us_search_items()
            for item in us_items:
                raw_symbol = str(item.get("symbol", "")).strip().upper()
                name = str(item.get("name", raw_symbol)).strip()
                if raw_symbol:
                    append_search_item(raw_symbol, name, "US", "US")

        if market in (None, "", "CRYPTO"):
            crypto_items = get_crypto_universe("ALL")[:800]

            for item in crypto_items:
                if isinstance(item, str):
                    raw_symbol = item.strip().upper()
                    append_search_item(raw_symbol, raw_symbol, "CRYPTO", "BYBIT")

                elif isinstance(item, dict):
                    raw_symbol = str(
                        item.get("symbol")
                        or item.get("Symbol")
                        or ""
                    ).strip().upper()

                    name = str(
                        item.get("name")
                        or item.get("Name")
                        or raw_symbol
                    ).strip()

                    append_search_item(raw_symbol, name, "CRYPTO", "BYBIT")

        # 支援中文關鍵字匹配：keyword 在 symbol 或 name 中
        results = []
        for item in data:
            sym = item["symbol"]
            name = item.get("name") or sym
            # 比對：symbol 或 name 包含 keyword（不分大小寫對 symbol，name 支援中文）
            if keyword_lower in sym.lower() or keyword in name:
                results.append(item)

        # 排序：1. 精確 symbol 匹配 2. 精確 name 匹配 3. 部分匹配；同級按 symbol 排序
        def sort_key(x):
            sym = x["symbol"].lower()
            name = (x.get("name") or "").strip()
            if keyword_lower == sym:
                return (0, x["symbol"])  # exact symbol match
            if name and keyword == name:
                return (1, x["symbol"])  # exact name match
            return (2, x["symbol"])  # partial match

        results.sort(key=sort_key)

        return results[:limit]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜尋失敗: {str(e)}")