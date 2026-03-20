"""
藍新金流 NewebPay MPG 加解密（AES-256-CBC + SHA256 TradeSha）。
規格參考官方 MPG 2.3 文件。
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

MPG_VERSION = "2.3"


def _require_key_iv(hash_key: str, hash_iv: str) -> tuple[bytes, bytes]:
    key = hash_key.encode("utf-8")
    iv = hash_iv.encode("utf-8")
    if len(key) != 32:
        raise ValueError("NEWEBPAY_HASH_KEY 須為 32 字元（AES-256）")
    if len(iv) != 16:
        raise ValueError("NEWEBPAY_HASH_IV 須為 16 字元")
    return key, iv


def encrypt_trade_info(params: list[tuple[str, Any]], hash_key: str, hash_iv: str) -> str:
    """params: (key, value) 列表；value 會轉成字串。"""
    key, iv = _require_key_iv(hash_key, hash_iv)
    pairs = [(k, str(v)) for k, v in params]
    query_string = urllib.parse.urlencode(pairs)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(query_string.encode("utf-8"), AES.block_size))
    return ct.hex()


def trade_sha(trade_info_hex: str, hash_key: str, hash_iv: str) -> str:
    raw = f"HashKey={hash_key}&{trade_info_hex}&HashIV={hash_iv}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()


def decrypt_trade_info(trade_info_hex: str, hash_key: str, hash_iv: str) -> dict[str, str]:
    key, iv = _require_key_iv(hash_key, hash_iv)
    clean = trade_info_hex.replace(" ", "").strip()
    data = bytes.fromhex(clean)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = unpad(cipher.decrypt(data), AES.block_size).decode("utf-8")
    parsed = urllib.parse.parse_qs(decrypted, keep_blank_values=True)
    out: dict[str, str] = {}
    for k, v in parsed.items():
        out[k] = v[0] if v else ""
    return out


def mpg_gateway_base_url() -> str:
    env = (os.getenv("NEWEBPAY_ENV") or "test").lower().strip()
    if env in ("prod", "production", "live"):
        return "https://core.newebpay.com/MPG/mpg_gateway"
    return "https://ccore.newebpay.com/MPG/mpg_gateway"


def get_credentials() -> tuple[str, str, str]:
    mid = (os.getenv("NEWEBPAY_MERCHANT_ID") or "").strip()
    key = (os.getenv("NEWEBPAY_HASH_KEY") or "").strip()
    iv = (os.getenv("NEWEBPAY_HASH_IV") or "").strip()
    return mid, key, iv


def is_configured() -> bool:
    mid, key, iv = get_credentials()
    return bool(mid and key and iv)
