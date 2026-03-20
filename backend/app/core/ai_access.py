"""
AI / Gemini 相關功能權限：
- 付費會員：plan_code 須在 AI_PAID_PLAN_CODES（環境變數，逗號分隔）
- 無限使用：role == admin，或帳號在 AI_UNLIMITED_USERNAMES（逗號分隔，不分大小寫比對 username）
"""

import os

from fastapi import HTTPException, status

from app.models.user import User


def _paid_plan_codes() -> set[str]:
    raw = os.getenv("AI_PAID_PLAN_CODES", "pro,premium,paid,member,vip")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _unlimited_usernames() -> set[str]:
    raw = os.getenv("AI_UNLIMITED_USERNAMES", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def user_has_unlimited_membership(user: User | None) -> bool:
    """管理員或 AI_UNLIMITED_USERNAMES 帳號：會員到期日視為無限制。"""
    if user is None:
        return False
    if (user.role or "").lower() == "admin":
        return True
    un = (user.username or "").strip().lower()
    return bool(un and un in _unlimited_usernames())


def user_can_use_ai_features(user: User | None) -> bool:
    """是否可使用會消耗 AI Token 的功能（付費方案 / 管理員 / 指定總帳號）。"""
    if user is None:
        return False
    if (user.status or "").lower() != "active":
        return False
    if (user.role or "").lower() == "admin":
        return True
    un = (user.username or "").strip().lower()
    if un and un in _unlimited_usernames():
        return True
    plan = (user.plan_code or "free").strip().lower()
    return plan in _paid_plan_codes()


def assert_ai_access(user: User) -> None:
    if not user_can_use_ai_features(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此功能僅限付費會員使用；若已付費請聯絡客服開通方案。管理員與指定帳號不受限。",
        )
