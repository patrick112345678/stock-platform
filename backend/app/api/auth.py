from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
)
from app.core.ai_access import user_can_use_ai_features, user_has_unlimited_membership


def _membership_fields(user: User) -> tuple[datetime | None, int | None, bool]:
    """回傳 (plan_expires_at, days_remaining, unlimited)"""
    unlimited = user_has_unlimited_membership(user)
    exp = user.plan_expires_at
    if unlimited:
        return exp, None, True
    if exp is None:
        return None, None, False
    exp_d = exp.date() if isinstance(exp, datetime) else exp
    today = datetime.utcnow().date()
    days = (exp_d - today).days
    if days < 0:
        days = 0
    return exp, days, False


def _user_response(user: User) -> UserResponse:
    exp, days_rem, unlimited = _membership_fields(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        plan_code=user.plan_code or "free",
        role=user.role or "user",
        status=user.status or "active",
        ai_access=user_can_use_ai_features(user),
        plan_expires_at=exp,
        membership_days_remaining=days_rem,
        membership_unlimited=unlimited,
    )

router = APIRouter(prefix="/auth", tags=["auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@router.post("/register", response_model=UserResponse)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    try:
        existing_user = db.query(User).filter(User.username == data.username).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists")

        existing_email = db.query(User).filter(User.email == data.email).first()
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already exists")

        user = User(
            username=data.username,
            email=data.email,
            password_hash=hash_password(data.password),
            plan_code="free",
            role="user",
            status="active",
        )

        db.add(user)
        db.commit()
        db.refresh(user)
        return _user_response(user)
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Username or email already exists",
        ) from None
    except SQLAlchemyError:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"註冊失敗：{type(e).__name__}: {str(e)[:500]}",
        ) from e


@router.post("/login", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    try:
        user = db.query(User).filter(User.username == form_data.username).first()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid username or password")

        if not verify_password(form_data.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username}
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
        }
    except HTTPException:
        raise
    except SQLAlchemyError:
        # 交給 main.py 的 SQLAlchemyError handler（JSON 503）
        raise
    except Exception as e:
        # bcrypt/passlib 等若噴錯，Swagger 只會看到 500；改回可讀 JSON
        raise HTTPException(
            status_code=500,
            detail=f"登入處理失敗：{type(e).__name__}: {str(e)[:500]}",
        ) from e


@router.get("/me", response_model=UserResponse)
def get_me(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
):
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing user id")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _user_response(user)


@router.post("/change-password")
def change_password(
    data: ChangePasswordRequest,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing user id")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="目前密碼不正確")

    user.password_hash = hash_password(data.new_password)
    user.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "message": "密碼已更新"}