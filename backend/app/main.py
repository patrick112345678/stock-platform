import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

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

# 👇 背景掃描
import asyncio
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
from app.services.stock_fundamental_service import run_tw_fundamentals_daily_sync


def _init_db_sync() -> None:
    """於 lifespan 內以 thread 執行，避免阻塞 ASGI；勿在模組 import 時連線建表（Render 易 port scan timeout）。"""
    Base.metadata.create_all(bind=engine)
    ensure_users_plan_expires_column()


async def _init_db_async() -> None:
    """背景執行建表／遷移，避免阻塞 lifespan → 讓埠先綁定（Render port scan）。"""
    try:
        await asyncio.to_thread(_init_db_sync)
        print("🟢 DB init (create_all / migrations) complete")
    except Exception as e:
        print("🔴 DB init failed:", e)


async def _delayed_background_jobs() -> None:
    """
    延遲啟動背景掃描，讓 Uvicorn 先完成綁定與 Render 健康檢查。
    若仍 OOM，可設 ENABLE_BACKGROUND_SCANNER=false
    """
    await asyncio.sleep(20)
    if os.getenv("ENABLE_BACKGROUND_SCANNER", "true").lower() not in ("1", "true", "yes", "on"):
        print("🟡 ENABLE_BACKGROUND_SCANNER 已關閉，跳過背景掃描任務")
        return
    asyncio.create_task(scanner_background_job())
    asyncio.create_task(scanner_cache_10min_job())
    asyncio.create_task(tw_fundamentals_background_job())
    print("🟢 background scanner tasks scheduled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 勿在 yield 前 await 長時間 DB 作業，否則 Render 在「尚未 listen」時會 port scan timeout
    asyncio.create_task(_init_db_async())
    asyncio.create_task(_delayed_background_jobs())
    yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    """登入/註冊查庫失敗時回 JSON，避免只剩 500 Internal Server Error 無法排查"""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "資料庫錯誤，請檢查 DATABASE_URL 與 PostgreSQL 是否已連結到本服務。",
            "error_type": type(exc).__name__,
            "error": str(exc)[:800],
        },
    )


# CORS：不可同時 allow_origins=["*"] 與 allow_credentials=True（瀏覽器會擋跨網域 fetch →「Failed to fetch」）
# 本專案用 Bearer token，不靠 cookie，故 credentials=False 即可。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth_router)
app.include_router(market_router)
app.include_router(watchlist_router)
app.include_router(ai_router)
app.include_router(scanner_router)
app.include_router(payment_router)


# =========================
# 📊 台股基本面（DB 日同步）
# =========================
async def tw_fundamentals_background_job():
    """每 24h 同步一次 stock_fundamentals；啟動後先延遲再跑，避免與 DB init 撞車。"""
    await asyncio.sleep(45)
    while True:
        if os.getenv("ENABLE_FUNDAMENTAL_DAILY_SYNC", "true").lower() not in ("1", "true", "yes", "on"):
            await asyncio.sleep(3600)
            continue
        try:
            await asyncio.to_thread(run_tw_fundamentals_daily_sync)
        except Exception as e:
            print("🔴 tw_fundamentals_daily_sync:", repr(e))
        await asyncio.sleep(86400)


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


@app.get("/health")
def health_live():
    """Render / 負載平衡探活：不連資料庫、不做外部請求，盡快回 200。"""
    return {"status": "ok", "service": "stock-platform-api"}


@app.get("/")
def root():
    return {"message": "Stock Platform API is running"}


@app.head("/")
def root_head():
    """Render 等平台可能對 / 發 HEAD 探活；僅 GET 會回 405。"""
    return Response(status_code=200)


@app.get("/health/db")
def health_db():
    """在瀏覽器開此網址可確認 Render 是否真的連上 PostgreSQL（與 / 不同，/ 不會碰資料庫）"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "ok": True,
            "dialect": engine.dialect.name,
            "message": "資料庫連線正常",
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_type": type(e).__name__,
                "message": str(e)[:800],
                "hint": "在 Render：PostgreSQL 實例 → Connect → 複製「Internal Database URL」到「後端 Web Service」的 Environment 變數 DATABASE_URL，並儲存後重新部署後端。",
            },
        )


@app.get("/health/routes")
def health_routes():
    """列出已註冊的 market 相關路由，用於確認 multi-timeframe / signal-table 是否載入"""
    routes = []
    for r in app.routes:
        if hasattr(r, "path") and "/market/" in str(r.path):
            routes.append(r.path)
    return {"market_routes": sorted(routes)}