"""
Yahoo Finance（yfinance）共用 Session。

雲端 / 資料中心 IP 若無合理 User-Agent，常出現 401 Invalid Crumb 或
「User is unable to access this feature」。所有 yf.Ticker / yf.download 應共用此 session。
"""

from __future__ import annotations

import requests

_session: requests.Session | None = None


def get_yfinance_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
            }
        )
        _session = s
    return _session
