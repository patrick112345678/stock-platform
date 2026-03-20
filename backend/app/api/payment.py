"""
藍新金流 MPG：建立訂單、幕前導轉、Notify 背景通知、Return 導回前端。
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.order import Order
from app.models.user import User
from app.core.security import decode_access_token
from app.services import newebpay_service as np

from fastapi.security import OAuth2PasswordBearer

oauth2_payment = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=True)

router = APIRouter(prefix="/payment", tags=["payment"])

PLAN_DAYS: dict[str, int] = {
    "1m": 31,
    "6m": 186,
    "12m": 372,
}

DEFAULT_AMT_TWD: dict[str, int] = {
    "1m": 99,
    "6m": 475,
    "12m": 833,
}


def _plan_amt_twd(plan_id: str) -> int:
    env_map = {
        "1m": "NEWEBPAY_AMT_1M",
        "6m": "NEWEBPAY_AMT_6M",
        "12m": "NEWEBPAY_AMT_12M",
    }
    key = env_map.get(plan_id)
    if key:
        raw = os.getenv(key)
        if raw and raw.strip().isdigit():
            return int(raw.strip())
    return DEFAULT_AMT_TWD[plan_id]


def _paid_plan_code() -> str:
    return (os.getenv("NEWEBPAY_PAID_PLAN_CODE") or "premium").strip().lower()


def _public_api_base() -> str:
    return (os.getenv("PUBLIC_API_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")


def _public_frontend() -> str:
    return (os.getenv("PUBLIC_FRONTEND_URL") or "http://localhost:3000").rstrip("/")


def _user_id_from_token(token: str = Depends(oauth2_payment)) -> int:
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return int(payload["sub"])


class NewebPayCheckoutBody(BaseModel):
    plan_id: Literal["1m", "6m", "12m"] = Field(..., description="訂閱方案")


class NewebPayCheckoutResponse(BaseModel):
    gateway_url: str
    merchant_id: str
    trade_info: str
    trade_sha: str
    version: str
    merchant_order_no: str
    amt: int


@router.get("/newebpay/plans")
def newebpay_plans_public():
    """前端顯示台幣金額用（不含密鑰）。"""
    return {
        "gateway_ready": np.is_configured(),
        "amounts_twd": {k: _plan_amt_twd(k) for k in ("1m", "6m", "12m")},
        "plan_days": PLAN_DAYS,
    }


@router.post("/newebpay/checkout", response_model=NewebPayCheckoutResponse)
def newebpay_checkout(
    body: NewebPayCheckoutBody,
    user_id: int = Depends(_user_id_from_token),
    db: Session = Depends(get_db),
):
    if not np.is_configured():
        raise HTTPException(
            status_code=503,
            detail="藍新金流尚未設定（NEWEBPAY_MERCHANT_ID / HASH_KEY / HASH_IV）",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    mid, hash_key, hash_iv = np.get_credentials()
    plan_id = body.plan_id
    amt = _plan_amt_twd(plan_id)

    # 訂單編號 ≤30 字元，英數
    suffix = f"{int(time.time())}{secrets.randbelow(9000) + 1000}"
    merchant_order_no = f"SP{user_id}{suffix}"[:30]

    public_api = _public_api_base()
    notify_url = f"{public_api}/payment/newebpay/notify"
    return_url = f"{public_api}/payment/newebpay/return"
    client_back = f"{_public_frontend()}/pricing"

    ts = str(int(time.time()))
    item_desc = {"1m": "Premium 月繳", "6m": "Premium 6 個月", "12m": "Premium 12 個月"}[plan_id]
    if len(item_desc) > 50:
        item_desc = item_desc[:50]

    trade_pairs: list[tuple[str, str | int]] = [
        ("MerchantID", mid),
        ("RespondType", "JSON"),
        ("TimeStamp", ts),
        ("Version", np.MPG_VERSION),
        ("MerchantOrderNo", merchant_order_no),
        ("Amt", amt),
        ("ItemDesc", item_desc),
        ("NotifyURL", notify_url),
        ("ReturnURL", return_url),
        ("ClientBackURL", client_back),
        ("CREDIT", 1),
        ("LoginType", 0),
    ]
    if user.email:
        trade_pairs.append(("Email", user.email))
        trade_pairs.append(("EmailModify", 0))

    trade_info = np.encrypt_trade_info(trade_pairs, hash_key, hash_iv)
    trade_sha = np.trade_sha(trade_info, hash_key, hash_iv)

    order = Order(
        user_id=user.id,
        order_no=merchant_order_no,
        plan_code=plan_id,
        amount=float(amt),
        status="pending",
    )
    db.add(order)
    db.commit()

    return NewebPayCheckoutResponse(
        gateway_url=np.mpg_gateway_base_url(),
        merchant_id=mid,
        trade_info=trade_info,
        trade_sha=trade_sha,
        version=np.MPG_VERSION,
        merchant_order_no=merchant_order_no,
        amt=amt,
    )


def _apply_payment_success(db: Session, merchant_order_no: str) -> None:
    order = db.query(Order).filter(Order.order_no == merchant_order_no).first()
    if not order:
        print("NewebPay notify: order not found", merchant_order_no)
        return

    if order.status == "paid":
        return

    user = db.query(User).filter(User.id == order.user_id).first()
    if not user:
        print("NewebPay notify: user missing", order.user_id)
        return

    plan_id = (order.plan_code or "1m").lower()
    days = PLAN_DAYS.get(plan_id, 31)
    now = datetime.utcnow()
    base = user.plan_expires_at
    if base and base > now:
        new_exp = base + timedelta(days=days)
    else:
        new_exp = now + timedelta(days=days)

    user.plan_code = _paid_plan_code()
    user.plan_expires_at = new_exp
    user.updated_at = now

    order.status = "paid"
    order.paid_at = now

    db.add(order)
    db.add(user)
    db.commit()


@router.post("/newebpay/notify")
async def newebpay_notify(request: Request, db: Session = Depends(get_db)):
    """藍新背景通知（不需登入）。"""
    if not np.is_configured():
        return PlainTextResponse("NEWEBPAY_NOT_CONFIGURED", status_code=503)

    form = await request.form()
    status_outer = (form.get("Status") or "").strip()
    trade_info_hex = (form.get("TradeInfo") or "").strip()
    trade_sha_in = (form.get("TradeSha") or "").strip()

    if not trade_info_hex or not trade_sha_in:
        return PlainTextResponse("MISSING_TRADE", status_code=400)

    _mid, hash_key, hash_iv = np.get_credentials()
    expected_sha = np.trade_sha(trade_info_hex, hash_key, hash_iv)
    if expected_sha != trade_sha_in:
        print("NewebPay notify: TradeSha mismatch")
        return PlainTextResponse("TRADESHA_ERROR", status_code=400)

    try:
        inner = np.decrypt_trade_info(trade_info_hex, hash_key, hash_iv)
    except Exception as e:
        print("NewebPay notify decrypt error:", repr(e))
        return PlainTextResponse("DECRYPT_ERROR", status_code=400)

    if status_outer != "SUCCESS":
        print("NewebPay notify outer status:", status_outer)
        return PlainTextResponse("OK")

    inner_status = (inner.get("Status") or "").upper()
    if inner_status != "SUCCESS":
        print("NewebPay notify inner status:", inner.get("Status"), inner.get("Message"))
        return PlainTextResponse("OK")

    merchant_order_no = (inner.get("MerchantOrderNo") or "").strip()
    if merchant_order_no:
        _apply_payment_success(db, merchant_order_no)

    return PlainTextResponse("OK")


@router.api_route("/newebpay/return", methods=["GET", "POST"])
async def newebpay_return(request: Request):
    """付款完成後瀏覽器導回（可再依藍新 POST 內容擴充驗證）。"""
    fe = _public_frontend()
    return RedirectResponse(url=f"{fe}/pricing?payment=return", status_code=302)
