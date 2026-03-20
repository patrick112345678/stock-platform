from fastapi import FastAPI
from app.db.database import engine, Base
from app.db.migrations_runtime import ensure_users_plan_expires_column
import app.db.base
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.market import router as market_router
from app.api.watchlist import router as watchlist_router
from app.api.ai import router as ai_router
from app.api.scanner import router as scanner_router
from app.api.payment import router as payment_router

# 👇 新增
import asyncio
from app.services.ai_service import AIService
from app.services.scanner_service import (
    get_us_universe,
    get_tw_universe,
    get_crypto_universe,
    process_us_symbol,
    process_tw_symbol,
    process_crypto_symbol,
    run_parallel,
    save_scanner_results,
    is_scanner_cache_recent,
)

app = FastAPI()

# CORS（開發時放寬，避免連線問題）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DB
Base.metadata.create_all(bind=engine)
ensure_users_plan_expires_column()

# Routers
app.include_router(auth_router)
app.include_router(market_router)
app.include_router(watchlist_router)
app.include_router(ai_router)
app.include_router(scanner_router)
app.include_router(payment_router)


# =========================
# 🔥 Background Scanner
# =========================
async def scanner_background_job():
    await asyncio.sleep(5)

    while True:
        # 若 6 小時內有掃過，跳過本次（避免重啟時重跑耗 token）
        if is_scanner_cache_recent(hours=6):
            print("🟡 scanner cache 尚新，跳過本次掃描（6h 內有更新）")
            await asyncio.sleep(3600)  # 1 小時後再檢查
            continue

        print("🟡 background scanner running...")

        try:
            us_symbols = get_us_universe("ALL")
            us_results = await asyncio.to_thread(run_parallel, us_symbols, process_us_symbol, 5)
            us_results = sorted(us_results, key=lambda x: x["score"], reverse=True)
            await asyncio.to_thread(save_scanner_results, us_results, "US")

            tw_symbols = get_tw_universe("ALL")
            tw_results = await asyncio.to_thread(run_parallel, tw_symbols, process_tw_symbol)
            tw_results = sorted(tw_results, key=lambda x: x["score"], reverse=True)
            await asyncio.to_thread(save_scanner_results, tw_results, "TW")

            crypto_symbols = get_crypto_universe("ALL")
            crypto_results = await asyncio.to_thread(run_parallel, crypto_symbols, process_crypto_symbol)
            crypto_results = sorted(crypto_results, key=lambda x: x["score"], reverse=True)
            await asyncio.to_thread(save_scanner_results, crypto_results, "CRYPTO")

            # 註解：後台重啟時不自動跑 AI 今日機會（省 token）
            # for market in ("US", "TW", "CRYPTO"):
            #     try:
            #         await asyncio.to_thread(
            #             AIService.refresh_ai_opportunities,
            #             market,
            #             "zh",
            #             8,
            #         )
            #         print(f"🟣 AI opportunity cache updated: {market}")
            #     except Exception as ai_err:
            #         print(f"🟠 AI opportunity cache update failed ({market}):", ai_err)

            print("🟢 scanner update done")

        except Exception as e:
            print("🔴 scanner background error:", e)

        await asyncio.sleep(86400)  # 1 天更新一次（原 30 分鐘）


async def scanner_cache_10min_job():
    """每 10 分鐘更新快取，供排行榜/選股器即時抓取失敗時 fallback"""
    await asyncio.sleep(60)  # 啟動後 1 分鐘開始
    while True:
        try:
            for market, pool in [("US", "ALL"), ("TW", "ALL"), ("CRYPTO", "ALL")]:
                try:
                    if market == "US":
                        syms = get_us_universe(pool)
                        res = await asyncio.to_thread(run_parallel, syms, process_us_symbol, 5)
                    elif market == "TW":
                        syms = get_tw_universe(pool)
                        res = await asyncio.to_thread(run_parallel, syms, process_tw_symbol)
                    else:
                        syms = get_crypto_universe(pool)
                        res = await asyncio.to_thread(run_parallel, syms, process_crypto_symbol)
                    res = sorted(res, key=lambda x: x.get("score", 0), reverse=True)
                    await asyncio.to_thread(save_scanner_results, res, market)
                except Exception as e:
                    print(f"🟠 10min cache ({market}) failed:", repr(e))
        except Exception as e:
            print("🟠 10min cache job error:", repr(e))
        await asyncio.sleep(600)  # 每 10 分鐘更新一次


# 👇 啟動時自動跑
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scanner_background_job())
    asyncio.create_task(scanner_cache_10min_job())


@app.get("/")
def root():
    return {"message": "Stock Platform API is running"}


@app.get("/health/routes")
def health_routes():
    """列出已註冊的 market 相關路由，用於確認 multi-timeframe / signal-table 是否載入"""
    routes = []
    for r in app.routes:
        if hasattr(r, "path") and "/market/" in str(r.path):
            routes.append(r.path)
    return {"market_routes": sorted(routes)}