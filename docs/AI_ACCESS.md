# AI 功能權限說明

## 後端上鎖範圍（會呼叫或依賴 Gemini / AI 快取）

| 路由 | 條件 |
|------|------|
| `POST /ai/analyze` | `quick_only=false` 時需付費／admin／總帳號 |
| `POST /ai/analyze` | `quick_only=true` 僅技術規則摘要，**不需付費** |
| `POST /ai/opportunities` | **一律需付費**／admin／總帳號 |
| `POST /ai/watchlist-daily` | `quick_only=false` 時需付費；`true` 僅規則摘要 |

## 如何開通使用者

1. **付費會員**：在資料庫將 `users.plan_code` 設為 `pro`、`premium`、`paid`、`member`、`vip` 等（與 `AI_PAID_PLAN_CODES` 一致）。
2. **管理員**：`users.role = admin` → 無限使用。
3. **總帳號**：在 `.env` 設定 `AI_UNLIMITED_USERNAMES=帳號1,帳號2`（對應 `username`）。

## 前端

登入後會呼叫 `GET /auth/me`，回傳 `ai_access: true/false`，用於隱藏／停用 AI 按鈕與說明文案。

## 環境變數

見專案 `backend/.env.example`。
