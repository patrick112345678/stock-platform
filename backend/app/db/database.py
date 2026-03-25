import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

_raw = os.getenv("DATABASE_URL")
# Render 內建 PostgreSQL 常給 postgres://，SQLAlchemy 2 需 postgresql://
if _raw and _raw.startswith("postgres://"):
    _raw = _raw.replace("postgres://", "postgresql://", 1)

if not _raw:
    if os.getenv("RENDER"):
        raise RuntimeError(
            "未設定 DATABASE_URL。請在 Render 後端服務「連結 PostgreSQL」或手動設定 DATABASE_URL。"
        )
    _raw = "sqlite:///./stock_platform.db"

DATABASE_URL = _raw

# Render 上 PostgreSQL 多數需要 SSL；未加時登入查庫容易 500（畫面只顯示 Internal Server Error）
_engine_kw: dict = {"pool_pre_ping": True}
if DATABASE_URL.startswith("postgresql") and os.getenv("RENDER"):
    if "sslmode=" not in DATABASE_URL and "ssl=" not in DATABASE_URL.lower():
        _engine_kw["connect_args"] = {"sslmode": "require"}

# 正式環境多使用者時適度放大連線池，避免請求排隊等連線（非「語法」問題，是併發資源）
if DATABASE_URL.startswith("postgresql"):
    try:
        _engine_kw["pool_size"] = max(2, int(os.getenv("DB_POOL_SIZE", "8")))
    except ValueError:
        _engine_kw["pool_size"] = 8
    try:
        _engine_kw["max_overflow"] = max(0, int(os.getenv("DB_MAX_OVERFLOW", "16")))
    except ValueError:
        _engine_kw["max_overflow"] = 16
    try:
        _engine_kw["pool_recycle"] = max(300, int(os.getenv("DB_POOL_RECYCLE", "1800")))
    except ValueError:
        _engine_kw["pool_recycle"] = 1800

engine = create_engine(DATABASE_URL, **_engine_kw)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()