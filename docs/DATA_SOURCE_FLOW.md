# 台股 / 美股 資料來源流程說明

## 一、資料來源優先順序

### 台股 (TW)

| 優先 | 來源 | 用途 | API / 說明 |
|------|------|------|------------|
| 主要 | **TWSE** | 上市股票 | `https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL`（當日全量）<br>`https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=YYYYMMDD&stockNo=CODE`（單一歷史）<br>`https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL`（當日全量，JSON 物件格式） |
| 次要 | **TPEx** | 上櫃股票 | 櫃買中心 `www.tpex.org.tw`，需查詢 OpenAPI 或 E-Data Shop |
| 備援 | **Yahoo Finance** | 上述失敗時 | `yfinance` 庫，台股代號格式：上市 `2330.TW`、上櫃 `5283.TWO` |

### 美股 (US)

| 優先 | 來源 | 用途 | API / 說明 |
|------|------|------|------------|
| 主要 | **Finnhub** | 報價、K 線 | `https://finnhub.io/api/v1/stock/candle`（需 API Key）<br>`/quote` 即時報價 |
| 輔助 | **Alpha Vantage** | 部分指標 | 需 API Key，可補部分指標 |
| 備援 | **Yahoo Finance** | 上述失敗時 | `yfinance` 庫 |

---

## 二、現有程式碼流程

### 1. 報價 (Quote)

| 函數 | 檔案 | 用途 | 目前使用 |
|------|------|------|----------|
| `get_quote_data()` | `market_service.py` | 統一取得股票報價 | `yf.Ticker` |
| `_build_quote_data()` | `watchlist.py` | 自選股報價 | `yf.Ticker` |

**流程**：`symbol` → `normalize_stock_symbol()` → `yf.Ticker(symbol)` → `info` / `fast_info` / `history`

### 2. 詳細資料 (Detail)

| 函數 | 檔案 | 用途 | 目前使用 |
|------|------|------|----------|
| `get_detail_data()` | `market_service.py` | 52 週、市值、PE、PB 等 | `yf.Ticker` + `get_quote_data` |

### 3. K 線 (Chart)

| 函數 | 檔案 | 用途 | 目前使用 |
|------|------|------|----------|
| `get_chart_data()` | `market_service.py` | 圖表 K 線 | `yf.Ticker.history()` |
| `get_stock_hist()` | `scanner_service.py` | 掃描用歷史 K 線 | `yf.download()` |

### 4. AI 分析用資料

| 函數 | 檔案 | 用途 | 目前使用 |
|------|------|------|----------|
| `get_market_data()` | `market_service.py` | AI 分析用單一標的 | `yf.Ticker.history()` + 技術指標計算 |

### 5. 台股 / 美股清單

| 函數 | 檔案 | 用途 | 目前使用 |
|------|------|------|----------|
| `get_tw_universe()` | `scanner_service.py` | 台股標的池 | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` |
| `get_us_universe()` | `scanner_service.py` | 美股標的池 | Wikipedia S&P 500 |
| `get_tw_search_items()` | `scanner_service.py` | 台股搜尋 | 同上 TWSE API |
| `get_tw_symbol_to_name()` | `scanner_service.py` | 代號→名稱 | 同上 |

---

## 三、台股代號與市場判斷

### 現有邏輯

- `normalize_stock_symbol(symbol)`：
  - 純數字 → `{symbol}.TW`（僅上市）
  - 已 `.TW` → 保持
  - 其他 → 保持（美股）
- `detect_stock_market(symbol)`：`isdigit()` 或 `.endswith(".TW")` → `"TW"`

### 上市 vs 上櫃

- 上市：TWSE，代號格式 `2330.TW`
- 上櫃：TPEx，代號格式 `5283.TWO`
- 兩者皆為 4 碼數字，需依市場區分：
  - 若 `market == "TW"` 且未指定上市/上櫃：可先查 TWSE，找不到再查 TPEx
  - 或依 TWSE / TPEx 清單判斷代號是否屬於該市場

---

## 四、TWSE API 格式

### STOCK_DAY_ALL（當日全量）

- **openapi.twse.com.tw**：回傳 `[{ "Date", "Code", "Name", "OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice", "Change", "TradeVolume", "Transaction" }, ...]`
- **www.twse.com.tw**：回傳 `{ "stat", "date", "fields", "data" }`，`data` 為二維陣列

### STOCK_DAY（單一股票歷史）

- URL：`https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=YYYYMMDD&stockNo=CODE`
- fields：日期、成交股數、成交金額、開盤價、最高價、最低價、收盤價、漲跌價差、成交筆數、註記

---

## 五、Finnhub API 格式

- **Stock Candles**：`GET /stock/candle?symbol=AAPL&resolution=D&from=...&to=...&token=...`
- **Quote**：`GET /quote?symbol=AAPL&token=...`
- 需環境變數：`FINNHUB_API_KEY`

---

## 六、實作建議順序

1. **台股資料源**
   - 新增 `twse_service.py` 或於 `market_service` 內實作：
     - `get_tw_quote_from_twse(symbol)`：從 STOCK_DAY_ALL 取單一報價
     - `get_tw_hist_from_twse(symbol, period)`：從 STOCK_DAY 取歷史 K 線
   - 上櫃：查 TPEx API 或暫時以 Yahoo 備援

2. **美股資料源**
   - 新增 `finnhub_service.py`：
     - `get_us_quote_from_finnhub(symbol)`
     - `get_us_candles_from_finnhub(symbol, resolution, from_ts, to_ts)`

3. **整合層**
   - `get_quote_data()`：依 market 選擇 TWSE / Finnhub，失敗則 fallback Yahoo
   - `get_chart_data()`：同上
   - `get_market_data()`：同上

4. **環境變數**
   - `FINNHUB_API_KEY`
   - `ALPHA_VANTAGE_API_KEY`（若使用）
