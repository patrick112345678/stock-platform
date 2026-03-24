# 台股基本面 API（前端對照）

## 統一結構 `fundamental`

`GET` 詳情（`get_detail_data`）回傳中，台股會多一層 **`fundamental`**（與頂層 `pe` / `pb` / `roe` 等數值一致）：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `pe` | float \| null | 本益比 |
| `pb` | float \| null | 股淨比 |
| `eps` | float \| null | 每股盈餘（元） |
| `roe` | float \| null | ROE（%，近四季稅後淨利／最新權益） |
| `gross_margin` | float \| null | 毛利率（%，單季） |
| `revenue_growth_yoy` | float \| null | 月營收年增率（%） |
| `debt_ratio` | float \| null | 負債比（%，負債／資產） |
| `valuation` | string \| null | 估值評級（偏低估／合理／偏高估／資料不足） |
| `industry` | string \| null | 產業別 |
| `stock_name_zh` | string \| null | 中文簡稱 |
| `display_name` | string | 頁面標題：`台積電（2330）` |

`GET` 行情組裝（`get_market_data`）台股亦含 **`fundamental`** 與頂層 **`roe` / `gross_margin` / `revenue_growth_yoy` / `debt_ratio` / `industry`**。

## 無資料顯示

後端以 **`null`** 表示缺值；前端請將 **`null`** 顯示為 **`"-"`**。

## 自選股 `name`

自選股 API 的 **`name`** 已為 **`台積電（2330）`** 單一字串，**請勿**再與 `symbol` 串接，否則會出現重複代號。

## 主標題

詳情／報價的 **`name`** 與 **`fundamental.display_name`** 皆為 **`台積電（2330）`**，請勿再顯示 `2330.TW` 作為主標題。
