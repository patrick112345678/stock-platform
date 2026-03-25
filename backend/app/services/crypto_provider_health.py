"""
加密所 API 健康狀態：程序內 circuit breaker + 錯誤日誌節流。
Render 等環境若 Binance 451 / Bybit 403，避免每支 symbol 重複打爆外部 API 與日誌。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Literal

ExchangeName = Literal["binance", "bybit"]

_lock = threading.Lock()
# exchange -> monotonic() until which we treat as unavailable
_outage_until: dict[str, float] = {}
# throttle key -> last monotonic() log time
_last_log: dict[str, float] = {}


def _outage_seconds() -> float:
    try:
        return float(os.getenv("CRYPTO_PROVIDER_OUTAGE_SECONDS", "1200"))
    except ValueError:
        return 1200.0


def _log_interval_seconds() -> float:
    try:
        return float(os.getenv("CRYPTO_ERROR_LOG_INTERVAL_SECONDS", "300"))
    except ValueError:
        return 300.0


def is_exchange_down(name: ExchangeName) -> bool:
    with _lock:
        until = _outage_until.get(name)
        return until is not None and time.monotonic() < until


def both_exchanges_down() -> bool:
    return is_exchange_down("binance") and is_exchange_down("bybit")


def mark_exchange_down(name: ExchangeName, reason: str = "") -> None:
    with _lock:
        _outage_until[name] = time.monotonic() + _outage_seconds()


def note_exchange_success(name: ExchangeName) -> None:
    """單次成功即解除該所 blackout，加快恢復。"""
    with _lock:
        _outage_until.pop(name, None)


def log_crypto_throttled(key: str, message: str) -> None:
    """相同 key 在間隔內只印一次。"""
    interval = _log_interval_seconds()
    now = time.monotonic()
    with _lock:
        last = _last_log.get(key, 0.0)
        if now - last < interval:
            return
        _last_log[key] = now
    print(message)


def should_attempt_crypto_background_scan() -> bool:
    """預設關閉：Render 等環境常遇 Binance 451 / Bybit 403，避免每 10 分鐘轟炸。需時設 ENABLE_CRYPTO_BACKGROUND_SCAN=true。"""
    return os.getenv("ENABLE_CRYPTO_BACKGROUND_SCAN", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
