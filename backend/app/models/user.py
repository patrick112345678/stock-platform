from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from app.db.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, unique=True, nullable=True, index=True)
    password_hash = Column(String, nullable=False)
    plan_code = Column(String, default="free", nullable=False)
    role = Column(String, default="user", nullable=False)
    status = Column(String, default="active", nullable=False)
    # 付費方案到期時間（UTC）；None 表示未設定（免費或後台未寫入）
    plan_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)