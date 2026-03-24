import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from typing import List, Dict, Any

import pandas as pd
import requests
from app.services.stock_data_service import get_cached_stock_data
from datetime import datetime, timedelta, time
import json
from app.db.database import SessionLocal
from sqlalchemy import text

BYBIT_BASE_URL = "https://api.bybit.com"
BINANCE_API_BASE = "https://api.binance.com/api/v3"

# 雲端 IP 若無 User-Agent，Bybit/Binance 常回 403；仍無法解決 Binance 對美國 IP 的 451 地區封鎖
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockPlatform/1.0; +https://example.com)",
    "Accept": "application/json",
}

DEFAULT_TW_SYMBOLS = [
    "2330.TW", "2317.TW", "2454.TW", "2303.TW", "2882.TW",
    "6505.TW", "2308.TW", "2881.TW", "2886.TW", "1303.TW",
    "1301.TW", "2002.TW", "3711.TW", "2884.TW"
]

DEFAULT_US_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "NFLX", "AVGO", "TSM", "INTC", "QCOM", "PLTR"
]

DEFAULT_CRYPTO_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT"
]

# 掃描 / universe：寫死最多 30，避免全市場請求與 Yahoo 爆炸
SCANNER_UNIVERSE_MAX = 30

US_SYMBOLS_CACHE = None
US_SEARCH_CACHE: List[Dict[str, str]] | None = None
def get_cached_results(market, limit):
    db = SessionLocal()
    try:
        rows = db.execute(
            text("""
                SELECT *
                FROM scanner_cache
                WHERE market = :market
                ORDER BY score DESC
                LIMIT :limit
            """),
            {"market": market, "limit": limit}
        ).fetchall()

        results = []
        for row in rows:
            item = dict(row._mapping)

            if item.get("signals"):
                try:
                    item["signals"] = json.loads(item["signals"])
                except Exception:
                    item["signals"] = []
            else:
                item["signals"] = []

            if item.get("extra"):
                try:
                    extra = json.loads(item["extra"])
                    item["volume_ratio_30d"] = extra.get("volume_ratio_30d")
                    item["breakout_30d"] = extra.get("breakout_30d")
                    item["macd_golden"] = extra.get("macd_golden")
                    item["macd_death"] = extra.get("macd_death")
                    item["rsi"] = extra.get("rsi")
                    item["support"] = extra.get("support")
                    item["resistance"] = extra.get("resistance")
                    item["trend"] = extra.get("trend")
                    item["pattern"] = extra.get("pattern")
                except Exception:
                    pass
            item.pop("extra", None)

            if item.get("change") is None and item.get("price") is not None and item.get("change_percent") is not None:
                cp = item["change_percent"]
                item["change"] = round(item["price"] * cp / (100 + cp), 4) if (100 + cp) != 0 else 0
            item.setdefault("trend", None)
            item.setdefault("pattern", "暫無明確型態")
            item.setdefault("funding_rate", None)
            results.append(item)

        return enrich_tw_names(results, market)

    finally:
        db.close()
def is_scanner_cache_recent(hours=6):
    """檢查 scanner_cache 是否有近期資料（任一市場），用於啟動時跳過重跑"""
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT MAX(updated_at) as last_update FROM scanner_cache")
        ).fetchone()
        if not row or not row[0]:
            return False
        from datetime import datetime, timedelta
        return datetime.utcnow() - row[0] < timedelta(hours=hours)
    finally:
        db.close()


def is_cache_fresh(market, pool, minutes=60):
    db = SessionLocal()
    try:
        row = db.execute(
            text("""
                SELECT MAX(updated_at) as last_update
                FROM scanner_cache
                WHERE market = :market AND pool = :pool
            """),
            {"market": market, "pool": pool}
        ).fetchone()

        if not row or not row[0]:
            return False

        from datetime import datetime, timedelta
        return datetime.utcnow() - row[0] < timedelta(minutes=minutes)

    finally:
        db.close()
def _ensure_scanner_cache_table(db):
    """確保 scanner_cache 表存在且含 extra 欄位"""
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS scanner_cache (
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            exchange TEXT,
            price REAL,
            change_percent REAL,
            volume REAL,
            score REAL,
            signals TEXT,
            summary TEXT,
            updated_at TIMESTAMP,
            extra TEXT,
            PRIMARY KEY (symbol, market)
        )
    """))
    db.commit()


def _ensure_scanner_extra_column(db):
    """確保 scanner_cache 有 extra 欄位（若表已存在但無此欄）"""
    try:
        db.execute(text("ALTER TABLE scanner_cache ADD COLUMN extra TEXT"))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def save_scanner_results(items, market):
    db = SessionLocal()
    try:
        _ensure_scanner_cache_table(db)
        _ensure_scanner_extra_column(db)
        for item in items:
            extra = {
                "volume_ratio_30d": item.get("volume_ratio_30d"),
                "breakout_30d": item.get("breakout_30d"),
                "macd_golden": item.get("macd_golden"),
                "macd_death": item.get("macd_death"),
                "rsi": item.get("rsi"),
                "support": item.get("support"),
                "resistance": item.get("resistance"),
                "trend": item.get("trend"),
                "pattern": item.get("pattern"),
            }
            db.execute(
                text("""
                    INSERT INTO scanner_cache
                    (symbol, market, exchange, price, change_percent, volume, score, signals, summary, updated_at, extra)
                    VALUES
                    (:symbol, :market, :exchange, :price, :change_percent, :volume, :score, :signals, :summary, :updated_at, :extra)
                    ON CONFLICT (symbol, market)
                    DO UPDATE SET
                        exchange = EXCLUDED.exchange,
                        price = EXCLUDED.price,
                        change_percent = EXCLUDED.change_percent,
                        volume = EXCLUDED.volume,
                        score = EXCLUDED.score,
                        signals = EXCLUDED.signals,
                        summary = EXCLUDED.summary,
                        updated_at = EXCLUDED.updated_at,
                        extra = EXCLUDED.extra
                """),
                {
                    "symbol": item["symbol"],
                    "market": market,
                    "exchange": item["exchange"],
                    "price": item["price"],
                    "change_percent": item["change_percent"],
                    "volume": item["volume"],
                    "score": item["score"],
                    "signals": json.dumps(item["signals"], ensure_ascii=False),
                    "summary": item["summary"],
                    "updated_at": datetime.utcnow(),
                    "extra": json.dumps(extra, ensure_ascii=False),
                },
            )

        db.commit()

    finally:
        db.close()
def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, math.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def calc_macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return macd, signal, hist

def build_signals_from_df(df: pd.DataFrame, min_bars: int = 60) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < min_bars:
        raise ValueError(f"歷史資料不足，至少需要 {min_bars} 根 K 線")

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.loc[:, ~df.columns.duplicated()]

    required = ["Close", "High", "Low", "Volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"缺少必要欄位: {col}")

        if isinstance(df[col], pd.DataFrame):
            df[col] = df[col].iloc[:, 0]

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)

    if len(df) < min_bars:
        raise ValueError(f"歷史資料不足，至少需要 {min_bars} 根 K 線")

    ma20_window = min(20, len(df))
    ma60_window = min(60, len(df))
    high20_window = min(20, len(df))
    vol20_window = min(20, len(df))

    df["ma20"] = df["Close"].rolling(ma20_window).mean()
    df["ma60"] = df["Close"].rolling(ma60_window).mean()
    df["rsi14"] = calc_rsi(df["Close"], 14)
    macd_line, macd_signal, hist = calc_macd(df["Close"])
    df["macd_hist"] = hist
    df["MACD"] = macd_line
    df["MACD_SIGNAL"] = macd_signal
    df["vol_ma20"] = df["Volume"].rolling(vol20_window).mean()
    df["vol_ma30"] = df["Volume"].rolling(30).mean()
    df["high30_prev"] = df["High"].shift(1).rolling(30).max()
    df["high20"] = df["High"].rolling(high20_window).max()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    signals: List[str] = []
    score = 0

    close = safe_float(last["Close"])
    ma20 = safe_float(last["ma20"])
    ma60 = safe_float(last["ma60"])
    rsi14 = safe_float(last["rsi14"])
    macd_hist_now = safe_float(last["macd_hist"])
    macd_hist_prev = safe_float(prev["macd_hist"])
    volume = safe_float(last["Volume"])
    vol_ma20 = safe_float(last["vol_ma20"])
    high20 = safe_float(last["high20"])

    if close > ma20:
        score += 20
        signals.append("站上 MA20")

    if ma20 > ma60 and ma60 > 0:
        score += 20
        signals.append("均線多頭排列")

    if 45 <= rsi14 <= 70:
        score += 15
        signals.append("RSI 健康區間")

    if macd_hist_now > 0:
        score += 15
        signals.append("MACD 柱體翻正")

    if macd_hist_prev <= 0 < macd_hist_now:
        score += 10
        signals.append("MACD 動能轉強")

    if vol_ma20 > 0 and volume > vol_ma20:
        score += 10
        signals.append("成交量高於 20 日均量")

    if high20 > 0 and close >= high20:
        score += 10
        signals.append("突破近 20 日高點")

    vol_ma30 = safe_float(last.get("vol_ma30"))
    volume_ratio_30d = (volume / vol_ma30) if vol_ma30 and vol_ma30 > 0 else None
    prev_high_30 = safe_float(last.get("high30_prev"))
    breakout_30d = prev_high_30 is not None and close > prev_high_30
    macd_now = safe_float(last.get("MACD"))
    macd_sig_now = safe_float(last.get("MACD_SIGNAL"))
    macd_prev = safe_float(prev.get("MACD"))
    macd_sig_prev = safe_float(prev.get("MACD_SIGNAL"))
    macd_golden = all(x is not None for x in [macd_now, macd_sig_now, macd_prev, macd_sig_prev]) and macd_now > macd_sig_now and macd_prev <= macd_sig_prev
    macd_death = all(x is not None for x in [macd_now, macd_sig_now, macd_prev, macd_sig_prev]) and macd_now < macd_sig_now and macd_prev >= macd_sig_prev

    summary = "技術面偏中性"
    if score >= 70:
        summary = "技術面偏多，短線動能轉強"
    elif score >= 50:
        summary = "技術面轉強，可列入觀察"
    elif score < 30:
        summary = "技術條件偏弱，暫時觀望"

    return {
        "score": score,
        "signals": signals,
        "summary": summary,
        "price": close,
        "volume": volume,
        "rsi14": rsi14,
        "ma20": ma20,
        "ma60": ma60,
        "macd_hist": macd_hist_now,
        "volume_ratio_30d": round(volume_ratio_30d, 2) if volume_ratio_30d is not None else None,
        "breakout_30d": breakout_30d,
        "macd_golden": macd_golden,
        "macd_death": macd_death,
    }

def normalize_us_symbol(symbol: str) -> str:
    return str(symbol).upper().replace(".", "-")


def get_stock_hist(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """
    與行情 API 共用 get_cached_stock_data，避免重複打 Yahoo。
    僅支援日線匯總（快取為 3mo 日線）；忽略 period/interval 差異以維持單一來源。
    """
    if symbol.endswith(".TW"):
        yf_symbol = symbol
    else:
        yf_symbol = normalize_us_symbol(symbol)

    bundle = get_cached_stock_data(yf_symbol)
    if not bundle.get("ok") or bundle.get("hist") is None:
        raise ValueError(f"抓不到股票資料: {yf_symbol}")

    df = bundle["hist"]
    if df is None or df.empty:
        raise ValueError(f"抓不到股票資料: {yf_symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]

    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"缺少必要欄位: {yf_symbol} / {col}")

    df = df.dropna()

    if len(df) < 2:
        raise ValueError(f"股票資料不足: {yf_symbol}")

    return df

def get_bybit_spot_tickers() -> List[Dict[str, Any]]:
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "spot"}
    r = requests.get(url, params=params, timeout=5, headers=REQUEST_HEADERS)
    r.raise_for_status()
    data = r.json()

    if data.get("retCode") != 0:
        raise ValueError(f"Bybit API error: {data}")

    return data["result"]["list"]


def map_bybit_interval_to_binance(interval: str) -> str:
    """Bybit interval 字元 → Binance klines interval"""
    m = {
        "D": "1d",
        "W": "1w",
        "M": "1M",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "360": "6h",
        "720": "12h",
        "1": "1m",
        "3": "3m",
        "5": "5m",
        "15": "15m",
        "30": "30m",
    }
    return m.get(interval, "1d")


def get_bybit_kline(symbol: str, interval: str = "D", limit: int = 120) -> pd.DataFrame:
    url = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    r = requests.get(url, params=params, timeout=5, headers=REQUEST_HEADERS)
    if r.status_code == 403:
        print(f"[crypto] provider=BYBIT HTTP 403 kline symbol={symbol} interval={interval} -> fallback BINANCE")
    r.raise_for_status()
    data = r.json()

    if data.get("retCode") != 0:
        raise ValueError(f"Bybit Kline API error: {data}")

    rows = data["result"]["list"]
    if not rows:
        raise ValueError(f"抓不到 crypto K 線: {symbol}")

    rows = list(reversed(rows))

    df = pd.DataFrame(
        rows,
        columns=["startTime", "Open", "High", "Low", "Close", "Volume", "Turnover"]
    )

    for col in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Datetime"] = pd.to_datetime(df["startTime"].astype("int64"), unit="ms")
    df = df.set_index("Datetime").dropna()

    if len(df) < 2:
        raise ValueError(f"crypto K 線不足: {symbol}")

    return df


def get_binance_kline(symbol: str, interval: str = "D", limit: int = 120) -> pd.DataFrame:
    """Binance 現貨 K 線，輸出欄位結構與 get_bybit_kline 一致"""
    bi = map_bybit_interval_to_binance(interval)
    url = f"{BINANCE_API_BASE}/klines"
    r = requests.get(
        url,
        params={"symbol": symbol, "interval": bi, "limit": limit},
        timeout=5,
        headers=REQUEST_HEADERS,
    )
    r.raise_for_status()
    raw = r.json()
    if not raw:
        raise ValueError(f"Binance K 線為空: {symbol}")

    rows = []
    for k in raw:
        qv = float(k[7]) if len(k) > 7 else 0.0
        rows.append([int(k[0]), k[1], k[2], k[3], k[4], k[5], qv])

    rows = list(rows)

    df = pd.DataFrame(
        rows,
        columns=["startTime", "Open", "High", "Low", "Close", "Volume", "Turnover"],
    )

    for col in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Datetime"] = pd.to_datetime(df["startTime"].astype("int64"), unit="ms")
    df = df.set_index("Datetime").dropna()

    if len(df) < 2:
        raise ValueError(f"Binance crypto K 線不足: {symbol}")

    return df


def get_crypto_kline_with_fallback(symbol: str, interval: str = "D", limit: int = 120) -> tuple[pd.DataFrame, str]:
    """先 Bybit，失敗則 Binance。不使用 Yahoo。回傳 (DataFrame, 'BYBIT'|'BINANCE')"""
    try:
        return get_bybit_kline(symbol, interval, limit), "BYBIT"
    except Exception as e:
        print("[crypto] provider=BYBIT kline failed, fallback=BINANCE", symbol, repr(e))
        try:
            return get_binance_kline(symbol, interval, limit), "BINANCE"
        except Exception as e2:
            raise ValueError(
                f"加密 K 線暫時無法取得（Bybit 與 Binance 皆失敗）: {symbol}"
            ) from e2


def get_binance_spot_tickers_normalized() -> List[Dict[str, Any]]:
    """Binance 全現貨 24h，格式對齊 Bybit tickers 列表以利後續排序／排行榜"""
    r = requests.get(
        f"{BINANCE_API_BASE}/ticker/24hr",
        timeout=5,
        headers=REQUEST_HEADERS,
    )
    r.raise_for_status()
    data = r.json()
    out: List[Dict[str, Any]] = []
    for t in data:
        sym = t.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        lp = safe_float(t.get("lastPrice"))
        op = safe_float(t.get("openPrice"))
        qv = safe_float(t.get("quoteVolume")) or 0.0
        pcp = safe_float(t.get("priceChangePercent"))
        # Bybit price24hPcnt 為小數（例如 0.0123 = 1.23%）
        pfrac = (pcp / 100.0) if pcp is not None else None
        out.append(
            {
                "symbol": sym,
                "lastPrice": str(lp) if lp is not None else "0",
                "prevPrice24h": str(op) if op is not None else "0",
                "price24hPcnt": str(pfrac) if pfrac is not None else "0",
                "turnover24h": str(qv),
            }
        )
    return out


def get_spot_tickers_with_fallback() -> List[Dict[str, Any]]:
    try:
        return get_bybit_spot_tickers()
    except Exception as e:
        print("WARN Bybit spot tickers failed, using Binance:", repr(e))
        return get_binance_spot_tickers_normalized()


def _fetch_twse_stock_day_all() -> List[Dict[str, Any]]:
    """
    拉取 TWSE OpenAPI 每日收盤報表。部分環境（OpenSSL 3）對 TWSE 憑證鏈會報 Missing SKI；
    先以 certifi 驗證，若仍 SSLError 則對該官方網址再試 verify=False。
    """
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    headers = {
        "User-Agent": REQUEST_HEADERS["User-Agent"],
        "Accept": "application/json",
    }
    try:
        import certifi

        verify = certifi.where()
    except Exception:
        verify = True
    try:
        resp = requests.get(url, timeout=5, headers=headers, verify=verify)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.SSLError as e:
        print("WARN TWSE SSL verify failed, retrying without verify:", repr(e))
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, timeout=5, headers=headers, verify=False)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list):
        return []
    return data


def get_tw_universe(pool="TOP30"):
    """
    掃描用 universe：固定最多 SCANNER_UNIVERSE_MAX，不抓 TWSE 全量（避免大量請求）。
    資料來源：tw_stock_master.json 前綴，失敗則 DEFAULT_TW_SYMBOLS。
    """
    try:
        fb = _load_tw_stock_master_fallback()
        symbols = [str(x["symbol"]).strip() + ".TW" for x in fb if x.get("symbol")]
        if not symbols:
            symbols = list(DEFAULT_TW_SYMBOLS)
    except Exception as e:
        print("❌ get_tw_universe failed:", repr(e))
        symbols = list(DEFAULT_TW_SYMBOLS)

    return symbols[:SCANNER_UNIVERSE_MAX]


TW_SEARCH_CACHE: List[Dict[str, str]] | None = None


def _load_tw_stock_master_fallback() -> List[Dict[str, str]]:
    """從 tw_stock_master.json 載入台股清單（TWSE API 失敗時使用）"""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "tw_stock_master.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [{"symbol": str(x.get("symbol", "")).strip(), "name": str(x.get("name", "")).strip() or str(x.get("symbol", "")).strip()} for x in data if x.get("symbol")]
        return []
    except Exception as e:
        print("❌ _load_tw_stock_master_fallback failed:", repr(e))
        return [
            {"symbol": "2330", "name": "台積電"},
            {"symbol": "2317", "name": "鴻海"},
            {"symbol": "2454", "name": "聯發科"},
            {"symbol": "2303", "name": "聯電"},
            {"symbol": "2882", "name": "國泰金"},
            {"symbol": "6505", "name": "台塑化"},
            {"symbol": "2308", "name": "台達電"},
            {"symbol": "2881", "name": "富邦金"},
            {"symbol": "2886", "name": "兆豐金"},
            {"symbol": "1301", "name": "台塑"},
            {"symbol": "2002", "name": "中鋼"},
        ]


def get_tw_search_items() -> List[Dict[str, str]]:
    """取得台股搜尋用清單（含代號與公司名稱），供 /market/search 使用。優先 TWSE API，失敗則載入 tw_stock_master.json"""
    global TW_SEARCH_CACHE
    if TW_SEARCH_CACHE is not None:
        return TW_SEARCH_CACHE
    try:
        data = _fetch_twse_stock_day_all()
        items = []
        for item in data:
            code = item.get("Code", "")
            name = item.get("Name", "")
            if code:
                display_symbol = str(code).strip()
                items.append({"symbol": display_symbol, "name": (name or display_symbol).strip()})
        TW_SEARCH_CACHE = items
        return items
    except Exception as e:
        print("❌ get_tw_search_items TWSE API failed:", repr(e), "-> loading from tw_stock_master.json")
        TW_SEARCH_CACHE = _load_tw_stock_master_fallback()
        return TW_SEARCH_CACHE


def get_tw_symbol_to_name() -> Dict[str, str]:
    """取得台股代號 -> 中文名稱對照表"""
    items = get_tw_search_items()
    return {x["symbol"]: x["name"] for x in items}


def enrich_tw_names(items: List[Dict[str, Any]], market: str = "TW") -> List[Dict[str, Any]]:
    """為台股項目加入中文名稱（有對照表時覆寫，確保顯示中文股名）"""
    if market != "TW":
        return items
    name_map = get_tw_symbol_to_name()
    for item in items:
        sym = str(item.get("symbol", "")).replace(".TW", "").strip()
        if sym and name_map.get(sym):
            item["name"] = name_map.get(sym)
        elif sym and not item.get("name"):
            item["name"] = sym
    return items


def get_us_universe(pool="TOP30"):
    """掃描用：寫死 DEFAULT_US_SYMBOLS 前 30，不抓 Wikipedia。"""
    return list(DEFAULT_US_SYMBOLS)[:SCANNER_UNIVERSE_MAX]


def get_us_search_items() -> List[Dict[str, str]]:
    """取得美股搜尋用清單（含代號與公司名稱），供 /market/search 使用"""
    global US_SEARCH_CACHE
    if US_SEARCH_CACHE is not None:
        return US_SEARCH_CACHE
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        df = pd.read_html(StringIO(resp.text))[0]
        # 欄位可能是 Symbol/Security 或略有不同
        sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        name_col = "Security" if "Security" in df.columns else ("Name" if "Name" in df.columns else sym_col)
        items = []
        for _, row in df.iterrows():
            sym = str(row.get(sym_col, "")).strip()
            name = str(row.get(name_col, sym)).strip()
            if sym and sym != "nan":
                items.append({"symbol": sym, "name": name or sym})
        US_SEARCH_CACHE = items
        return items
    except Exception as e:
        print("❌ get_us_search_items failed:", repr(e))
        return [{"symbol": s, "name": s} for s in DEFAULT_US_SYMBOLS]


def get_crypto_universe(pool: str = "TOP30") -> List[str]:
    """掃描用：寫死 DEFAULT_CRYPTO_SYMBOLS 前 30，不在此請求 Bybit/Binance。"""
    return list(DEFAULT_CRYPTO_SYMBOLS)[:SCANNER_UNIVERSE_MAX]


def get_crypto_search_pool(limit: int = 200) -> List[str]:
    """搜尋用：可嘗試即時交易所清單，失敗則靜態清單。"""
    try:
        tickers = get_spot_tickers_with_fallback()
        symbols = sorted(
            [x for x in tickers if x["symbol"].endswith("USDT")],
            key=lambda x: float(x.get("turnover24h") or 0),
            reverse=True,
        )
        out = [x["symbol"] for x in symbols]
        if out:
            return out[:limit]
    except Exception as e:
        print("WARN get_crypto_search_pool:", repr(e))
    return list(DEFAULT_CRYPTO_SYMBOLS)


def build_opportunity_from_df(
    df: pd.DataFrame,
    symbol: str,
    market: str,
    exchange: str,
    display_symbol: str | None = None,
    min_bars: int = 60,
) -> Dict[str, Any]:
    if df is None or len(df) < 2:
        raise ValueError(f"歷史資料不足: {symbol}")

    tech = build_signals_from_df(df, min_bars=min_bars)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = safe_float(last["Close"])
    prev_close = safe_float(prev["Close"])
    change = price - prev_close if (price is not None and prev_close is not None) else 0
    change_percent = (change / prev_close * 100) if prev_close else 0

    tail = min(20, len(df))
    support = float(df["Low"].tail(tail).min()) if tail else None
    resistance = float(df["High"].tail(tail).max()) if tail else None

    score = tech["score"]
    signals = tech["signals"] or []
    breakout_30d = tech.get("breakout_30d", False)

    # 整體趨勢 (偏多/中性偏多/中性/中性偏空/偏空)
    if score >= 70:
        trend = "偏多"
    elif score >= 50:
        trend = "中性偏多"
    elif score >= 30:
        trend = "中性"
    elif score >= 15:
        trend = "中性偏空"
    else:
        trend = "偏空"

    # 型態 (多頭排列/接近壓力/接近支撐/暫無明確型態)
    pattern = "暫無明確型態"
    if "均線多頭排列" in signals:
        pattern = "多頭排列"
    elif breakout_30d:
        pattern = "突破 30 日高"
    elif resistance and price and resistance > 0 and (resistance - price) / resistance < 0.02:
        pattern = "接近壓力 / 強勢整理"
    elif support and price and support > 0 and (price - support) / support < 0.02:
        pattern = "接近支撐"

    return {
        "symbol": display_symbol or symbol,
        "name": (display_symbol or symbol),
        "market": market,
        "exchange": exchange,
        "price": price or 0,
        "change": round(change, 4),
        "change_percent": change_percent,
        "score": score,
        "signals": signals,
        "summary": tech["summary"] or "",
        "volume": safe_float(last["Volume"]) or 0,
        "volume_ratio_30d": tech.get("volume_ratio_30d"),
        "breakout_30d": breakout_30d,
        "macd_golden": tech.get("macd_golden", False),
        "macd_death": tech.get("macd_death", False),
        "rsi": tech.get("rsi14"),
        "support": support,
        "resistance": resistance,
        "trend": trend,
        "pattern": pattern,
    }

def process_us_symbol(symbol: str):
    try:
        df = get_stock_hist(symbol)
        return build_opportunity_from_df(
            df=df,
            symbol=symbol,
            market="US",
            exchange="US",
            display_symbol=symbol,
            min_bars=60,
        )
    except Exception as e:
        return None

def process_tw_symbol(yf_symbol: str):
    try:
        df = get_stock_hist(yf_symbol)
        return build_opportunity_from_df(
            df=df,
            symbol=yf_symbol,
            market="TW",
            exchange="TW",
            display_symbol=yf_symbol.replace(".TW", ""),
            min_bars=60,
        )
    except Exception as e:
        return None

def process_crypto_symbol(symbol: str):
    try:
        df, exch = get_crypto_kline_with_fallback(symbol)
        return build_opportunity_from_df(
            df=df,
            symbol=symbol,
            market="CRYPTO",
            exchange=exch,
            display_symbol=symbol,
            min_bars=40,
        )
    except Exception as e:
        return None

def run_parallel(symbols: List[str], processor, max_workers: int = 8) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(processor, s) for s in symbols]

        for idx, future in enumerate(as_completed(futures), 1):
            item = future.result()
            if item:
                results.append(item)

            if idx % 100 == 0 and idx > 0:
                print(f"  progress: {idx}/{len(symbols)}")

    return results



def get_us_opportunities(limit=20):

    print("🔥 using cache US")
    return get_cached_results("US", limit)

def get_tw_opportunities(pool="TOP100", limit=20):
     return get_cached_results("TW", limit)

def get_crypto_opportunities(limit=20):
    """僅讀 DB 快取；不在 API 請求時全市場掃描（排程預留給外部 cron）。"""
    return enrich_tw_names(get_cached_results("CRYPTO", limit), "CRYPTO")

def get_stock_leaderboard(sort: str = "change_percent", limit: int = 20) -> List[Dict[str, Any]]:
    results = []

    for symbol in DEFAULT_US_SYMBOLS:
        try:
            df = get_stock_hist(symbol, period="10d", interval="1d")
            if len(df) < 2:
                continue

            last = df.iloc[-1]
            prev = df.iloc[-2]

            price = safe_float(last["Close"])
            prev_close = safe_float(prev["Close"])
            change = price - prev_close
            change_percent = (change / prev_close * 100) if prev_close else 0
            volume = safe_float(last["Volume"])

            results.append({
                "symbol": symbol,
                "name": symbol,
                "exchange": "US",
                "market": "stock",
                "price": round(price, 4),
                "change": round(change, 4),
                "change_percent": round(change_percent, 2),
                "volume": volume,
                "score": 0,
                "signals": [],
                "summary": "",
            })
        except Exception:
            continue

    if sort == "volume":
        results.sort(key=lambda x: x["volume"], reverse=True)
    else:
        results.sort(key=lambda x: x["change_percent"], reverse=True)
    save_scanner_results(results, "US")
    return results[:limit]


def get_crypto_leaderboard(sort: str = "change_percent", limit: int = 20) -> List[Dict[str, Any]]:
    tickers = get_spot_tickers_with_fallback()
    results = []

    for item in tickers:
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        last_price = safe_float(item.get("lastPrice"))
        prev_24h = safe_float(item.get("prevPrice24h"))
        change = last_price - prev_24h
        change_percent = safe_float(item.get("price24hPcnt")) * 100
        turnover = safe_float(item.get("turnover24h"))

        results.append({
            "symbol": symbol,
            "name": symbol,
            "exchange": "BYBIT",
            "market": "crypto",
            "price": round(last_price, 4),
            "change": round(change, 4),
            "change_percent": round(change_percent, 2),
            "volume": turnover,
            "score": 0,
            "signals": [],
            "summary": "",
        })

    if sort == "volume":
        results.sort(key=lambda x: x["volume"], reverse=True)
    else:
        results.sort(key=lambda x: x["change_percent"], reverse=True)

    return results[:limit]


def is_tw_market_hours() -> bool:
    """台股交易時段 9:00-13:30（台灣時間 UTC+8）"""
    try:
        tw_now = datetime.utcnow() + timedelta(hours=8)
        now = tw_now.time()
        return time(9, 0) <= now <= time(13, 30)
    except Exception:
        return False


def refresh_tw_cache():
    """預留：應由外部排程 / worker 呼叫，勿在 API 請求鏈內全市場掃描。"""
    print("🟡 refresh_tw_cache skipped (use scheduled job / admin endpoint)")


def get_leaderboard(
    market: str = "US",
    pool: str = "TOP100",
    sort_by: str = "change_percent",
    limit: int = 20,
    sort_direction: str = "gainers",  # "gainers" | "losers"
):
    """排行榜：僅讀 DB 快取；不在 HTTP 請求內做全市場掃描。"""
    market = market.upper()
    reverse = sort_direction != "losers"

    def _from_cache():
        cache_limit = 5000
        if market == "CRYPTO":
            return get_cached_results("CRYPTO", cache_limit)
        elif market == "TW":
            return get_cached_results("TW", cache_limit)
        return get_cached_results("US", cache_limit)

    items = _from_cache()
    sorted_items = sorted(items, key=lambda x: x.get(sort_by) or 0, reverse=reverse)
    return enrich_tw_names(sorted_items[:limit], market)


def get_opportunities(market: str = "US", pool: str = "TOP100", limit: int = 20):
    market = market.upper()

    if market == "CRYPTO":
        return get_crypto_opportunities(limit)
    elif market == "TW":
        return get_tw_opportunities(limit)
    else:
        return get_us_opportunities(limit)


def get_watchlist_opportunities(
    watchlist_items: List[Dict[str, Any]],
    market: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """針對自選股清單逐一取得技術分析結果"""
    results = []
    market = market.upper()

    for item in watchlist_items:
        symbol = str(item.get("symbol", "")).strip().upper()
        item_market = str(item.get("market", market)).upper()
        if item_market != market:
            continue
        if not symbol:
            continue

        try:
            if item_market == "TW":
                yf_symbol = symbol if symbol.endswith(".TW") else f"{symbol}.TW"
                res = process_tw_symbol(yf_symbol)
            elif item_market == "US":
                res = process_us_symbol(symbol)
            elif item_market == "CRYPTO":
                sym = symbol.replace("-", "").replace(" ", "")
                if not sym.endswith("USDT"):
                    sym = f"{sym}USDT"
                res = process_crypto_symbol(sym)
            else:
                continue
            if res:
                results.append(res)
        except Exception:
            pass

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return enrich_tw_names(results[:limit], market)


def passes_filters(item: Dict[str, Any], req) -> bool:
    if req.min_price is not None and item.get("price") is not None and item["price"] < req.min_price:
        return False
    if req.max_price is not None and item.get("price") is not None and item["price"] > req.max_price:
        return False
    if req.min_volume is not None and item.get("volume", 0) < req.min_volume:
        return False
    if req.min_change_percent is not None and item.get("change_percent") is not None and item["change_percent"] < req.min_change_percent:
        return False
    if req.max_change_percent is not None and item.get("change_percent") is not None and item["change_percent"] > req.max_change_percent:
        return False

    rsi_val = item.get("rsi")
    if getattr(req, "rsi_min", None) is not None and rsi_val is not None:
        try:
            if float(rsi_val) < float(req.rsi_min):
                return False
        except (TypeError, ValueError):
            pass
    if getattr(req, "rsi_max", None) is not None and rsi_val is not None:
        try:
            if float(rsi_val) > float(req.rsi_max):
                return False
        except (TypeError, ValueError):
            pass

    signals = item.get("signals", [])

    if req.above_ma20 and "站上 MA20" not in signals:
        return False
    if req.above_ma60 and "均線多頭排列" not in signals:
        return False
    if req.macd_bullish and "MACD 柱體翻正" not in signals:
        return False
    if getattr(req, "only_breakout", False):
        if "突破近 20 日高點" not in signals and "接近突破" not in signals:
            return False
    if getattr(req, "only_bull", False):
        if "均線多頭排列" not in signals:
            return False

    # v14 選股器條件
    if getattr(req, "volume_ratio_30d_min", None) is not None:
        vr = item.get("volume_ratio_30d")
        if vr is None or float(vr) < req.volume_ratio_30d_min:
            return False
    if getattr(req, "breakout_30d", False) and not item.get("breakout_30d", False):
        return False
    if getattr(req, "macd_golden", False) and not item.get("macd_golden", False):
        return False
    if getattr(req, "macd_death", False) and not item.get("macd_death", False):
        return False

    return True


def filter_symbols(req) -> Dict[str, Any]:
    """選股器：僅篩選 DB 快取，不在請求內掃描交易所 / Yahoo。"""
    market = str(req.market).upper()
    source = "cache"

    def _from_cache():
        return get_cached_results(market, 5000)

    base = _from_cache()
    filtered = [x for x in base if passes_filters(x, req)]

    # 以訊號分數由高到低排序
    filtered.sort(key=lambda x: x.get("score") or 0, reverse=True)

    # 確保符合 OpportunityItem schema：name, change, signals, summary 不可為 None；台股補中文名
    filtered = enrich_tw_names(filtered, market)
    for x in filtered:
        x.setdefault("name", x.get("symbol", ""))
        x.setdefault("change", 0)
        x.setdefault("signals", [])
        x.setdefault("summary", "")
        if x.get("price") is None:
            x["price"] = 0
        if x.get("volume") is None:
            x["volume"] = 0

    return {"items": filtered[: req.limit], "source": source}