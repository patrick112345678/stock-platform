from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str
    email: str | None = None
    plan_code: str
    role: str
    status: str
    """是否可使用 AI（Gemini）功能：付費方案 / admin / AI_UNLIMITED_USERNAMES"""
    ai_access: bool = False
    plan_expires_at: datetime | None = None
    """付費到期日（UTC）。無則為 None。"""
    membership_days_remaining: int | None = None
    """距離 plan_expires_at 的剩餘日數；無到期日、或無限會員時為 None。"""
    membership_unlimited: bool = False
    """管理員或總帳號：不顯示到期倒數。"""

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=72)