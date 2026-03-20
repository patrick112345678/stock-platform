# 藍新金流（NewebPay）串接說明

## 流程

1. 使用者登入後，在前端選方案 → `POST /payment/newebpay/checkout`（需 Bearer Token）。
2. 後端建立 `orders` 記錄（`pending`）、組好 `TradeInfo` / `TradeSha`，回傳 `gateway_url` 與表單欄位。
3. 前端以 **POST form** 導向 `https://ccore.newebpay.com/MPG/mpg_gateway`（測試）或正式網域。
4. 使用者於藍新頁面付款。
5. 藍新 **POST** `NotifyURL` → `/payment/newebpay/notify`（背景）：驗證 TradeSha、解密、若成功則更新訂單與會員 `plan_code`、`plan_expires_at`。
6. 瀏覽器 **ReturnURL** → `/payment/newebpay/return` → 302 導回 `PUBLIC_FRONTEND_URL/pricing?payment=return`。

## 環境變數（`.env`）

| 變數 | 說明 |
|------|------|
| `NEWEBPAY_MERCHANT_ID` | 商店代號 |
| `NEWEBPAY_HASH_KEY` | 32 字元 |
| `NEWEBPAY_HASH_IV` | 16 字元 |
| `NEWEBPAY_ENV` | `test` 或 `prod`（正式可用 `production` / `live`） |
| `NEWEBPAY_PAID_PLAN_CODE` | 付款成功後寫入的 `plan_code`（須符合 `AI_PAID_PLAN_CODES`） |
| `NEWEBPAY_AMT_1M` / `6M` / `12M` | 各方案台幣金額（選填） |
| `PUBLIC_API_BASE_URL` | **必須為網際網路可存取網址**，否則 Notify 收不到（本機請用 ngrok 等） |
| `PUBLIC_FRONTEND_URL` | Next 前端網址，供 `ClientBackURL` 與 return 導向 |

## 本機測試 Notify

藍新伺服器需能連到你的 `PUBLIC_API_BASE_URL`。僅有 `localhost` 時請使用：

- [ngrok](https://ngrok.com/) / Cloudflare Tunnel 等，將 `https://xxx.ngrok.io` 設為 `PUBLIC_API_BASE_URL`，並在藍新後台（若有）登記允許的網域。

## 測試卡號（藍新測試環境）

參考藍新文件：例如 `4000-2211-1111-1111`，效期任意未來月年，CVC 任意 3 碼。

## API

- `GET /payment/newebpay/plans` — 公開，回傳是否已設定金流與台幣金額。
- `POST /payment/newebpay/checkout` — 需登入，`{"plan_id":"1m"|"6m"|"12m"}`。
- `POST /payment/newebpay/notify` — 藍新呼叫，勿加認證。
- `GET|POST /payment/newebpay/return` — 瀏覽器導回。

## 注意

- MPG 版本目前使用 **2.3**，加密 **AES-256-CBC**，與官方 PHP 範例一致。
- 若解密失敗，請確認 HashKey 長度 **32**、HashIV **16**（與後台顯示一致，無多餘空白）。
