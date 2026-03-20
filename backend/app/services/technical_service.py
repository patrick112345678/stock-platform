"""
技術指標摘要與訊號表（對齊 Streamlit ai_members_crypto_v14_mobile_desktop.py 的輸出字串規則）

目前前端 dashboard 需要的欄位包含：
- 多空強度%
- 支撐/壓力
- 技術訊號總表
- 估值/風險高低
"""

import json
import math
from typing import Any, Dict, List, Optional, Tuple


def _is_nan(v: Any) -> bool:
    try:
        if v is None:
            return True
        # 包含 numpy 的 NaN：先轉 float 再檢查
        return math.isnan(float(v))
    except Exception:
        return v is None


def _safe_float(v: Any, digits: int = 4) -> Optional[float]:
    """轉成 float（含 NaN/None 會回傳 None）。"""
    try:
        if _is_nan(v):
            return None
        return round(float(v), digits)
    except Exception:
        return None


def _fmt_value(v: Any, digits: int = 2, default: str = "N/A") -> str:
    try:
        fv = _safe_float(v, digits=digits)
        if fv is None:
            return default
        return f"{fv:,.{digits}f}"
    except Exception:
        return default


def _fmt_large_num(v: Any) -> str:
    """格式化大數（近似 Streamlit 的 fmt_large_num）。"""
    try:
        fv = _safe_float(v, digits=2)
        if fv is None:
            return "N/A"
        if abs(fv) >= 1_000_000_000:
            return f"{fv / 1_000_000_000:.2f}B"
        if abs(fv) >= 1_000_000:
            return f"{fv / 1_000_000:.2f}M"
        return f"{fv:,.0f}"
    except Exception:
        return "N/A"


def trend_label(score: int, lang: str = "zh") -> str:
    if lang == "en":
        if score >= 4:
            return "Bullish"
        if score == 3:
            return "Slightly Bullish"
        if score == 2:
            return "Neutral"
        if score == 1:
            return "Slightly Bearish"
        return "Bearish"

    if score >= 4:
        return "偏多"
    if score == 3:
        return "中性偏多"
    if score == 2:
        return "中性"
    if score == 1:
        return "中性偏空"
    return "偏空"


def valuation_label(pe: Any, pb: Any, lang: str = "zh") -> str:
    pe_f = _safe_float(pe, digits=6)
    pb_f = _safe_float(pb, digits=6)

    if pe_f is None and pb_f is None:
        return "資料不足" if lang != "en" else "Insufficient Data"

    if pe_f is not None:
        if pe_f < 15:
            return "偏低" if lang != "en" else "Undervalued"
        if pe_f <= 25:
            return "合理" if lang != "en" else "Fair"
        return "偏高" if lang != "en" else "Overvalued"

    if pb_f is not None:
        if pb_f < 1.5:
            return "偏低" if lang != "en" else "Undervalued"
        if pb_f <= 3:
            return "合理" if lang != "en" else "Fair"
        return "偏高" if lang != "en" else "Overvalued"

    return "資料不足" if lang != "en" else "Insufficient Data"


def risk_label(latest: Any, support: Any, resistance: Any, lang: str = "zh") -> str:
    if latest is None:
        return "未知" if lang != "en" else "Unknown"

    price = _safe_float(latest.get("Close") if hasattr(latest, "get") else None, digits=8)
    rsi = _safe_float(latest.get("RSI") if hasattr(latest, "get") else None, digits=6)
    support_f = _safe_float(support, digits=8)
    resistance_f = _safe_float(resistance, digits=8)

    flags = 0
    if support_f is not None and price is not None and price <= support_f * 1.02:
        flags += 1
    if resistance_f is not None and price is not None and price >= resistance_f * 0.98:
        flags += 1
    if rsi is not None and (rsi >= 70 or rsi <= 30):
        flags += 1

    if flags >= 2:
        return "高" if lang != "en" else "High"
    if flags == 1:
        return "中" if lang != "en" else "Medium"
    return "低" if lang != "en" else "Low"


def trend_score(df: Any) -> int:
    """
    0~5 分（對齊 Streamlit trend_score）
    - Close > MA20
    - MA20 > MA60
    - RSI > 55
    - MACD > MACD_SIGNAL
    - Volume > Volume_MA20
    """
    if df is None or df.empty:
        return 0

    latest = df.iloc[-1]

    score = 0
    close = _safe_float(latest.get("Close") if hasattr(latest, "get") else None, digits=8)
    ma20 = _safe_float(latest.get("MA20") if hasattr(latest, "get") else None, digits=8)
    ma60 = _safe_float(latest.get("MA60") if hasattr(latest, "get") else None, digits=8)
    rsi = _safe_float(latest.get("RSI") if hasattr(latest, "get") else None, digits=6)
    macd = _safe_float(latest.get("MACD") if hasattr(latest, "get") else None, digits=8)
    macd_signal = _safe_float(
        latest.get("MACD_SIGNAL") if hasattr(latest, "get") else None, digits=8
    )
    volume = _safe_float(latest.get("Volume") if hasattr(latest, "get") else None, digits=8)

    if close is not None and ma20 is not None and close > ma20:
        score += 1

    if ma20 is not None and ma60 is not None and ma20 > ma60:
        score += 1

    if rsi is not None and rsi > 55:
        score += 1

    if macd is not None and macd_signal is not None and macd > macd_signal:
        score += 1

    vol_ma20 = None
    try:
        vol_ma20 = df["Volume"].rolling(20).mean().iloc[-1]
    except Exception:
        vol_ma20 = None
    vol_ma20_f = _safe_float(vol_ma20, digits=6)
    if vol_ma20_f is not None and volume is not None and volume > vol_ma20_f:
        score += 1

    return score


def calculate_target(price: Any, resistance: Any, support: Any) -> Tuple[Optional[float], Optional[float]]:
    price_f = _safe_float(price, digits=8)
    resistance_f = _safe_float(resistance, digits=8)
    support_f = _safe_float(support, digits=8)
    if resistance_f is None or support_f is None or price_f is None:
        return None, None
    up = resistance_f * 1.05
    down = support_f * 0.95
    return _safe_float(up, digits=6), _safe_float(down, digits=6)


def detect_patterns(df: Any) -> List[str]:
    if df is None or df.empty:
        return ["暫無明確型態"]

    latest = df.iloc[-1]
    close = _safe_float(latest.get("Close") if hasattr(latest, "get") else None, digits=8)
    ma20 = _safe_float(latest.get("MA20") if hasattr(latest, "get") else None, digits=8)
    ma60 = _safe_float(latest.get("MA60") if hasattr(latest, "get") else None, digits=8)
    rsi = _safe_float(latest.get("RSI") if hasattr(latest, "get") else None, digits=6)

    patterns: List[str] = []

    if ma20 is not None and ma60 is not None and close is not None:
        if close > ma20 and ma20 > ma60:
            patterns.append("多頭排列")

    resistance = None
    support = None
    vol_ma20 = None
    try:
        resistance = _safe_float(df["High"].rolling(20).max().iloc[-1], digits=8)
        support = _safe_float(df["Low"].rolling(20).min().iloc[-1], digits=8)
        vol_ma20 = _safe_float(df["Volume"].rolling(20).mean().iloc[-1], digits=8)
    except Exception:
        resistance = None
        support = None
        vol_ma20 = None

    volume = _safe_float(latest.get("Volume") if hasattr(latest, "get") else None, digits=8)

    if resistance is not None and close is not None:
        if close >= resistance * 0.98:
            if vol_ma20 is not None and volume is not None and volume >= vol_ma20:
                patterns.append("接近突破")
            else:
                patterns.append("接近壓力")

    if support is not None and close is not None:
        if close <= support * 1.02:
            patterns.append("接近支撐")

    if rsi is not None and ma20 is not None and close is not None:
        if 50 <= rsi <= 65 and close > ma20:
            patterns.append("強勢整理")

    if not patterns:
        patterns.append("暫無明確型態")

    return patterns


def pattern_text(key: str, lang: str = "zh") -> str:
    if lang == "en":
        mapping = {
            "多頭排列": "Bullish Alignment",
            "接近突破": "Near Breakout",
            "接近壓力": "Near Resistance",
            "接近支撐": "Near Support",
            "強勢整理": "Strong Consolidation",
            "暫無明確型態": "No Clear Pattern",
        }
        return mapping.get(key, key)
    return key


def generate_signal_table(df: Any, support: Any, resistance: Any, lang: str = "zh") -> List[Dict[str, str]]:
    latest = df.iloc[-1]
    rows: List[Dict[str, str]] = []

    def add_signal(signal: str, status: str, description: str) -> None:
        rows.append({"signal": signal, "status": status, "description": description})

    # 均線
    ma20 = _safe_float(latest.get("MA20") if hasattr(latest, "get") else None, digits=8)
    ma60 = _safe_float(latest.get("MA60") if hasattr(latest, "get") else None, digits=8)
    close = _safe_float(latest.get("Close") if hasattr(latest, "get") else None, digits=8)
    if ma20 is not None and ma60 is not None:
        status = "多頭" if ma20 > ma60 else "空頭"
        add_signal(
            "均線排列" if lang != "en" else "Moving Average Structure",
            status,
            f"MA20={_fmt_value(ma20)} / MA60={_fmt_value(ma60)}",
        )

    # RSI
    rsi = _safe_float(latest.get("RSI") if hasattr(latest, "get") else None, digits=6)
    if rsi is not None:
        if rsi >= 70:
            status = "過熱"
        elif rsi <= 30:
            status = "超賣"
        elif rsi >= 55:
            status = "偏強"
        elif rsi <= 45:
            status = "偏弱"
        else:
            status = "中性"
        add_signal(
            "RSI" if lang != "en" else "RSI",
            status,
            f"RSI={_fmt_value(rsi)}",
        )

    # MACD
    macd = _safe_float(latest.get("MACD") if hasattr(latest, "get") else None, digits=8)
    macd_signal = _safe_float(
        latest.get("MACD_SIGNAL") if hasattr(latest, "get") else None, digits=8
    )
    if macd is not None and macd_signal is not None:
        status = "黃金交叉上方" if macd > macd_signal else "死亡交叉下方"
        add_signal(
            "MACD" if lang != "en" else "MACD",
            status,
            f"MACD={_fmt_value(macd)} / Signal={_fmt_value(macd_signal)}",
        )

    # 關鍵價位（支撐/壓力）
    support_f = _safe_float(support, digits=8)
    resistance_f = _safe_float(resistance, digits=8)
    if support_f is not None and resistance_f is not None and close is not None:
        if close >= resistance_f * 0.98:
            status = "接近壓力/突破"
        elif close <= support_f * 1.02:
            status = "接近支撐"
        else:
            status = "區間中段"
        add_signal(
            "關鍵價位" if lang != "en" else "Key Levels",
            status,
            f"支撐={_fmt_value(support_f)} / 壓力={_fmt_value(resistance_f)}",
        )

    # 量能
    volume = _safe_float(latest.get("Volume") if hasattr(latest, "get") else None, digits=8)
    vol_ma20_f = None
    try:
        vol_ma20_f = _safe_float(df["Volume"].rolling(20).mean().iloc[-1], digits=8)
    except Exception:
        vol_ma20_f = None

    if volume is not None and vol_ma20_f is not None:
        status = "放量" if volume > vol_ma20_f else "量縮"
        add_signal(
            "成交量" if lang != "en" else "成交量",
            status,
            f"成交量={_fmt_large_num(volume)} / MA20={_fmt_large_num(vol_ma20_f)}",
        )

    # 型態
    patterns = detect_patterns(df)
    pattern_status = " / ".join([pattern_text(p, lang) for p in patterns])
    add_signal(
        "型態" if lang != "en" else "Pattern",
        pattern_status,
        "系統根據均線、區間高低與量能做簡易偵測" if lang != "en" else "Detected from moving averages, range levels, and volume.",
    )

    return rows


def build_quick_summary(data: dict, lang: str = "zh") -> dict:
    """根據技術指標建立快速摘要（含 dashboard 需要的擴充欄位）"""
    hist = data.get("hist")
    if hist is None or getattr(hist, "empty", False):
        return {
            "trend": "無資料",
            "valuation": "無資料",
            "risk": "無資料",
            "patterns": [],
            "bullish": ["資料不足"],
            "bearish": ["資料不足"],
            "one_line": "目前資料不足，無法產生快速摘要。",
            "bull_strength": 0,
            "bear_strength": 0,
            "support": None,
            "resistance": None,
            "up_target": None,
            "down_target": None,
            "signal_table": [],
        }

    latest = hist.iloc[-1]

    name = data.get("name", data.get("raw_symbol", "UNKNOWN"))
    close = _safe_float(latest.get("Close") if hasattr(latest, "get") else None, digits=8)
    ma20 = _safe_float(latest.get("MA20") if hasattr(latest, "get") else None, digits=8)
    ma60 = _safe_float(latest.get("MA60") if hasattr(latest, "get") else None, digits=8)
    rsi = _safe_float(latest.get("RSI") if hasattr(latest, "get") else None, digits=6)
    macd = _safe_float(latest.get("MACD") if hasattr(latest, "get") else None, digits=8)
    macd_signal = _safe_float(
        latest.get("MACD_SIGNAL") if hasattr(latest, "get") else None, digits=8
    )

    support = data.get("support")
    resistance = data.get("resistance")
    support_f = _safe_float(support, digits=8)
    resistance_f = _safe_float(resistance, digits=8)

    pe = data.get("pe")
    pb = data.get("pb")

    score = trend_score(hist)
    bull_strength = round((score / 5) * 100, 1) if score is not None else 0.0
    bear_strength = round(100 - bull_strength, 1)
    trend = trend_label(score, lang=lang)

    valuation = valuation_label(pe=pe, pb=pb, lang=lang)
    risk = risk_label(latest, support_f, resistance_f, lang=lang)

    patterns = detect_patterns(hist)

    bullish: List[str] = []
    bearish: List[str] = []

    # 建立多空理由（對齊 Streamlit build_quick_summary 的規則）
    if close is not None and ma20 is not None:
        if close > ma20:
            bullish.append("股價站上 MA20，短線結構偏強")
        else:
            bearish.append("股價未站穩 MA20，短線動能偏弱")

    if ma20 is not None and ma60 is not None:
        if ma20 > ma60:
            bullish.append("MA20 位於 MA60 之上，均線排列偏多")
        else:
            bearish.append("MA20 尚未有效站上 MA60，趨勢確認不足")

    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            bullish.append("MACD 位於訊號線上方，動能結構較佳")
        else:
            bearish.append("MACD 位於訊號線下方，短期動能保守")

    if rsi is not None:
        if rsi >= 70:
            bearish.append("RSI 偏高，需留意過熱拉回風險")
        elif rsi <= 30:
            bullish.append("RSI 進入低檔區，可能接近技術性反彈區")

    if resistance_f is not None and close is not None:
        if close >= resistance_f * 0.98:
            bearish.append("股價接近壓力區，追價風險提高")

    if support_f is not None and close is not None:
        if close <= support_f * 1.02:
            bullish.append("股價靠近支撐區，可觀察承接力道")

    if not bullish:
        bullish.append("目前偏多訊號有限，需等待更明確確認")
    if not bearish:
        bearish.append("明顯偏空訊號有限，但仍需留意市場波動")

    up_target, down_target = calculate_target(close, resistance_f, support_f)

    signal_table = generate_signal_table(hist, support_f, resistance_f, lang=lang)

    # 一句話摘要（對齊 Streamlit one_line）
    one_line = f"{name} 目前趨勢為{trend}，估值評級為{valuation}，整體風險屬於{risk}。"

    return {
        "trend": trend,
        "valuation": valuation,
        "risk": risk,
        "patterns": patterns,
        "bullish": bullish[:3],
        "bearish": bearish[:3],
        "one_line": one_line,
        "bull_strength": bull_strength,
        "bear_strength": bear_strength,
        "support": support_f,
        "resistance": resistance_f,
        "up_target": up_target,
        "down_target": down_target,
        "score": score,
        "signal_table": signal_table,
    }


def build_ai_payload(data: dict, quick_summary: dict, lang: str = "zh") -> str:
    symbol = data.get("raw_symbol") or data.get("symbol") or "UNKNOWN"
    name = data.get("name") or symbol
    price = data.get("price")
    change = data.get("change")
    change_pct = data.get("change_percent")
    support = data.get("support")
    resistance = data.get("resistance")

    if lang == "zh":
        return f"""
你是一位專業券商研究員。

請根據提供的市場資料生成一份結構化投資研究報告。

【重要規則】
1. 不可編造任何數據（營收、EPS、PE、成長率等）
2. 若資料不足，請明確說明「資料不足」，不可自行補齊
3. 使用保守、客觀、條件式語氣
4. 禁止使用「一定」、「必然」、「必漲」、「明顯低估」等絕對語句
5. 分析需基於已提供的價格、技術指標與可用資訊
6. 報告需專業但精簡，不超過 1200 字
7. 所有「字串欄位」（summary、fundamental、technical、industry 等）必須是**通順中文段落**，禁止在段落內出現英文字段名或類似 suggestion:、watch_points:、entry_conditions: 的清單格式
8. technical 與 technical_detail：請做**綜合解讀與情境推演**，不可逐條複製上方 JSON 裡的 bullish/bearish 原句
9. action 物件內四個子欄位各用 1～3 句完整中文，勿只列關鍵字或價位數字

【資料缺失時的標準寫法】
- 基本面不足：目前缺乏完整基本面數據（如EPS/營收），本段分析僅供參考，無法進行完整估值判斷。
- 產業資料不足：目前未取得明確產業資料，無法完整評估其產業競爭力與未來成長性。
- 無法判斷時：必須明確寫「無法判斷」、「資料不足以支持結論」

【語氣要求】使用券商/法人研究報告語氣：客觀、保守、條件式推論、有不確定性。
例如：若後續量能持續放大，股價有機會延續上行趨勢；反之，若跌破關鍵支撐，短期需轉為保守。

分析標的：
- Symbol: {symbol}
- Name: {name}
- Current Price: {price}
- Change: {change}
- Change Percent: {change_pct}
- Support: {support}
- Resistance: {resistance}
- PE: {data.get('pe')}
- PB: {data.get('pb')}
- Market Cap: {data.get('market_cap')}

技術摘要：
{json.dumps(quick_summary, ensure_ascii=False, indent=2)}

請只回傳 JSON，不要加 markdown，不要加 ```json。格式如下：

{{
  "summary": "一句話專業摘要，限 80 字",
  "fundamental": "基本面分析。若無 PE/營收/EPS 等資料，必須寫：目前缺乏完整基本面數據，本段分析僅供參考，無法進行完整估值判斷。",
  "technical": "技術面分析，基於均線、RSI、MACD、支撐壓力等已提供數據",
  "industry": "產業分析。若無產業資料，必須寫：目前未取得明確產業資料，無法完整評估其產業競爭力與未來成長性。",
  "risk_opportunity": "風險與機會，使用條件式語氣",
  "strategy": "操作策略建議，限 100 字，使用「可能」、「若…則」、「需觀察」等保守用語",
  "one_line": "一句話摘要",
  "technical_detail": {{
    "trend": "偏多/中性/偏空",
    "ma_structure": "均線結構描述",
    "rsi_macd_volume": "RSI、MACD、量價關係",
    "support_resistance": "支撐壓力區說明",
    "technical_risk": "技術面風險評估"
  }},
  "fundamental_detail": {{
    "pe_comment": "本益比/估值評估，無資料寫「目前缺乏完整基本面數據」",
    "summary": "基本面摘要，無資料寫「無法進行完整估值判斷」"
  }},
  "rating": {{
    "bias": "偏多/中性/偏空",
    "risk_level": "低/中/高",
    "medium_term_view": "短中期看法"
  }},
  "action": {{
    "suggestion": "操作建議",
    "watch_points": "觀察重點",
    "entry_conditions": "可能進場條件",
    "risk_reminder": "風險提醒"
  }},
  "trend": "偏多/中性/偏空",
  "valuation": "估值評級或「資料不足」",
  "risk": "風險描述",
  "action_short": "具體建議",
  "confidence": {{
    "overall": "high/medium/low",
    "fundamental": "high/medium/low",
    "technical": "high/medium/low",
    "industry": "high/medium/low"
  }}
}}

confidence 各欄位：若該類資料不足則填 "low"，技術指標完整則 "technical" 可填 "high"。
"""
    else:
        return f"""
You are a professional securities research analyst.

Generate a structured investment research report based on the provided market data.

【Rules】
1. Do NOT fabricate any data (revenue, EPS, PE, growth rate, etc.)
2. If data is insufficient, clearly state "Insufficient data" - do not fill in
3. Use conservative, objective, conditional language
4. Avoid absolute statements like "will definitely", "must", "obviously undervalued"
5. Base analysis only on provided price, technical indicators, and available info
6. Report should be professional and concise, under 1200 words

Symbol: {symbol}
Name: {name}
Current Price: {price}
Change: {change}
Change Percent: {change_pct}
Support: {support}
Resistance: {resistance}
PE: {data.get('pe')}
PB: {data.get('pb')}

Technical summary:
{json.dumps(quick_summary, ensure_ascii=False, indent=2)}

Return JSON only. No markdown. No code fences.

Format:
{{
  "summary": "one-line summary",
  "fundamental": "fundamental analysis; if no PE/revenue/EPS data, state: Insufficient fundamental data",
  "technical": "technical analysis",
  "industry": "industry analysis; if no data, state: No industry data available",
  "risk_opportunity": "risks and opportunities",
  "strategy": "strategy suggestion",
  "trend": "bullish/neutral/bearish",
  "valuation": "valuation or insufficient data",
  "risk": "risk description",
  "action_short": "action suggestion",
  "confidence": {{
    "overall": "high/medium/low",
    "fundamental": "high/medium/low",
    "technical": "high/medium/low",
    "industry": "high/medium/low"
  }}
}}
"""