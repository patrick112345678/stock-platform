# 後端健康檢查腳本
# 用法：在 stock_v1 目錄下執行 .\stock-platform\backend\check_backend.ps1

Write-Host "=== 後端健康檢查 ===" -ForegroundColor Cyan
Write-Host ""

$base = "http://127.0.0.1:8000"

try {
    $r = Invoke-WebRequest -Uri "$base/" -UseBasicParsing -TimeoutSec 3
    Write-Host "[OK] 後端根路徑可連線" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] 無法連線後端 ($base)" -ForegroundColor Red
    Write-Host "請執行：cd stock-platform\backend" -ForegroundColor Yellow
    Write-Host "       uvicorn app.main:app --reload --host 0.0.0.0 --port 8000" -ForegroundColor Yellow
    exit 1
}

$endpoints = @(
    "/market/search",
    "/market/multi-timeframe",
    "/market/signal-table",
    "/scanner/watchlist",
    "/scanner/leaderboard",
    "/scanner/opportunities"
)

foreach ($ep in $endpoints) {
    try {
        $r = Invoke-WebRequest -Uri "$base$ep" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        Write-Host "[OK] $ep" -ForegroundColor Green
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -eq 404) {
            Write-Host "[404] $ep - 路由未載入，請重啟後端" -ForegroundColor Red
        } elseif ($code -eq 401) {
            Write-Host "[401] $ep - 需登入（正常）" -ForegroundColor Yellow
        } else {
            Write-Host "[FAIL] $ep - $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "檢查完成。若出現 404，請從 stock-platform\backend 目錄重啟 uvicorn。" -ForegroundColor Cyan
