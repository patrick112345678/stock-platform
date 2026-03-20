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

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()