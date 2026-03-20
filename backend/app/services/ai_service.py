import json
import os
import traceback
from datetime import datetime, timedelta

from google import genai
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.watchlist import Watchlist
from app.services.market_service import get_market_data
from app.services.scanner_service import get_cached_results
from app.services.technical_service import build_ai_payload, build_quick_summary

AI_OPPORTUNITY_CACHE_TTL_MINUTES = 1440  # 1 天（24 * 60）


def _to_str(val) -> str | None:
    """將值轉為字串，若為 dict 則格式化成可讀文字"""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        parts = [f"{k}: {v}" for k, v in val.items() if v]
        return "\n".join(parts) if parts else None
    return str(val)


def _format_zh_action_block(action: dict) -> str | None:
    """將 Gemini 回傳的 action 物件轉成券商式中文區塊，避免畫面出現 suggestion: 等英文鍵。"""
    labels = (
        ("suggestion", "操作建議"),
        ("watch_points", "觀察重點"),
        ("entry_conditions", "進場條件"),
        ("risk_reminder", "風險提示"),
    )
    blocks: list[str] = []
    for key, title in labels:
        v = action.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, str):
            text = v.strip()
        else:
            text = (_to_str(v) or "").strip()
        if text:
            blocks.append(f"【{title}】{text}")
    return "\n\n".join(blocks) if blocks else None


def _normalize_ai_report(raw: dict) -> dict:
    """正規化 AI 回傳：確保 trend/valuation/risk/summary/action 為字串；保留 confidence 物件"""
    conf = raw.get("confidence")
    if isinstance(conf, dict):
        confidence_obj = {
            "overall": conf.get("overall") or "medium",
            "fundamental": conf.get("fundamental") or "low",
            "technical": conf.get("technical") or "medium",
            "industry": conf.get("industry") or "medium",
        }
    else:
        confidence_obj = None

    # 技術面物件：優先 technical_detail，其次 technical（若為 dict）
    tech_obj = None
    if isinstance(raw.get("technical_detail"), dict):
        tech_obj = raw.get("technical_detail")
    elif isinstance(raw.get("technical"), dict):
        tech_obj = raw.get("technical")
    elif isinstance(raw.get("technical"), str):
        tech_obj = {"trend": raw.get("trend"), "rsi_macd_volume": raw.get("technical")}

    # 基本面物件
    fund_obj = None
    if isinstance(raw.get("fundamental_detail"), dict):
        fund_obj = raw.get("fundamental_detail")
    elif isinstance(raw.get("fundamental"), dict):
        fund_obj = raw.get("fundamental")

    action_raw = raw.get("action")
    if isinstance(action_raw, dict):
        action_str = _format_zh_action_block(action_raw) or _to_str(action_raw)
    else:
        action_str = _to_str(action_raw)

    out = {
        "trend": _to_str(raw.get("trend")),
        "valuation": _to_str(raw.get("valuation")),
        "risk": _to_str(raw.get("risk")),
        "summary": _to_str(raw.get("summary")),
        "action": action_str,
        "action_short": _to_str(raw.get("action_short")),
        "fundamental": _to_str(raw.get("fundamental")) if isinstance(raw.get("fundamental"), str) else None,
        "technical": tech_obj,
        "industry": _to_str(raw.get("industry")),
        "risk_opportunity": _to_str(raw.get("risk_opportunity")),
        "strategy": _to_str(raw.get("strategy")),
        "confidence": raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None,
        "rating": raw.get("rating") if isinstance(raw.get("rating"), dict) else None,
        "action_detail": action_raw if isinstance(action_raw, dict) else None,
    }
    if fund_obj:
        out["fundamental_detail"] = fund_obj
    if confidence_obj:
        out["confidence_detail"] = confidence_obj
    return out


def _ensure_ai_cache_table() -> None:
    db = SessionLocal()
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_opportunity_cache (
                market TEXT NOT NULL,
                lang TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                score INTEGER,
                price REAL,
                change_pct REAL,
                reason TEXT,
                risk TEXT,
                action TEXT,
                setup_type TEXT,
                confidence INTEGER,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (market, lang, symbol)
            )
        """))
        db.commit()
    finally:
        db.close()


def _normalize_ai_items(items: list[dict], fallback_by_symbol: dict[str, dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in items or []:
        symbol = str(item.get("symbol", "")).upper().strip()
        if not symbol:
            continue

        fallback = fallback_by_symbol.get(symbol, {})
        normalized.append(
            {
                "symbol": symbol,
                "name": fallback.get("name") or fallback.get("symbol") or symbol,
                "score": item.get("score"),
                "price": fallback.get("price"),
                "change_pct": fallback.get("change_pct")
                if fallback.get("change_pct") is not None
                else fallback.get("change_percent"),
                "reason": item.get("reason"),
                "risk": item.get("risk"),
                "action": item.get("action"),
                "setup_type": item.get("setup_type"),
                "confidence": item.get("confidence"),
            }
        )
    return normalized


def _save_ai_opportunity_cache(market: str, lang: str, items: list[dict]) -> str:
    _ensure_ai_cache_table()
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        db.execute(
            text("DELETE FROM ai_opportunity_cache WHERE market = :market AND lang = :lang"),
            {"market": market, "lang": lang},
        )

        for item in items:
            db.execute(
                text("""
                    INSERT INTO ai_opportunity_cache
                    (market, lang, symbol, name, score, price, change_pct, reason, risk, action, setup_type, confidence, updated_at)
                    VALUES
                    (:market, :lang, :symbol, :name, :score, :price, :change_pct, :reason, :risk, :action, :setup_type, :confidence, :updated_at)
                """),
                {
                    "market": market,
                    "lang": lang,
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "score": item.get("score"),
                    "price": item.get("price"),
                    "change_pct": item.get("change_pct"),
                    "reason": item.get("reason"),
                    "risk": item.get("risk"),
                    "action": item.get("action"),
                    "setup_type": item.get("setup_type"),
                    "confidence": item.get("confidence"),
                    "updated_at": now,
                },
            )

        db.commit()
        return now.isoformat()
    finally:
        db.close()


def _load_ai_opportunity_cache(market: str, lang: str, limit: int = 20) -> tuple[list[dict], str | None]:
    _ensure_ai_cache_table()
    db = SessionLocal()
    try:
        rows = db.execute(
            text("""
                SELECT market, lang, symbol, name, score, price, change_pct, reason, risk, action, setup_type, confidence, updated_at
                FROM ai_opportunity_cache
                WHERE market = :market AND lang = :lang
                ORDER BY COALESCE(score, 0) DESC, COALESCE(confidence, 0) DESC, symbol ASC
                LIMIT :limit
            """),
            {"market": market, "lang": lang, "limit": limit},
        ).fetchall()

        items = [dict(row._mapping) for row in rows]
        updated_at = None
        if items:
            last = items[0].get("updated_at")
            if isinstance(last, datetime):
                updated_at = last.isoformat()
        return items, updated_at
    finally:
        db.close()


def _is_ai_cache_fresh(
    market: str,
    lang: str,
    ttl_minutes: int = AI_OPPORTUNITY_CACHE_TTL_MINUTES,
) -> bool:
    _ensure_ai_cache_table()
    db = SessionLocal()
    try:
        row = db.execute(
            text("""
                SELECT MAX(updated_at) AS last_update
                FROM ai_opportunity_cache
                WHERE market = :market AND lang = :lang
            """),
            {"market": market, "lang": lang},
        ).fetchone()

        if not row or not row[0]:
            return False

        return datetime.utcnow() - row[0] < timedelta(minutes=ttl_minutes)
    finally:
        db.close()


class AIService:
    @staticmethod
    def analyze_symbol(
        symbol: str,
        market: str = "US",
        interval: str = "1d",
        lang: str = "zh",
        quick_only: bool = True,
    ):
        try:
            data = get_market_data(symbol=symbol, market=market, interval=interval)

            if not data or data.get("hist") is None:
                raise ValueError("查無可分析資料")

            quick_summary = build_quick_summary(data, lang=lang)
            if quick_summary is None:
                raise ValueError("quick_summary 產生失敗")

            result = {
                "symbol": data.get("raw_symbol") or symbol.upper(),
                "name": data.get("name"),
                "market": market,
                "interval": interval,
                "quick_summary": quick_summary,
                "ai_report": None,
            }

            if not quick_only:
                api_key = os.getenv("GEMINI_API_KEY", "")
                if not api_key:
                    raise ValueError("缺少 GEMINI_API_KEY")

                prompt = build_ai_payload(data=data, quick_summary=quick_summary, lang=lang)
                if not prompt:
                    raise ValueError("build_ai_payload 回傳空值")

                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                )

                text_resp = (getattr(resp, "text", "") or "").strip()
                if not text_resp:
                    result["ai_report"] = {
                        "trend": "無法判斷",
                        "valuation": "資料不足",
                        "risk": "資料不足",
                        "summary": "AI 未回傳內容",
                        "action": "建議稍後重試",
                        "confidence_detail": {
                            "overall": "low",
                            "fundamental": "low",
                            "technical": "low",
                            "industry": "low",
                        },
                    }
                else:
                    try:
                        raw = json.loads(text_resp)
                    except Exception:
                        start = text_resp.find("{")
                        end = text_resp.rfind("}")
                        if start >= 0 and end > start:
                            raw = json.loads(text_resp[start : end + 1])
                        else:
                            raw = {"summary": text_resp[:300], "action": "請檢查 AI 回傳格式"}

                    # 正規化 ai_report：確保 trend/valuation/risk/summary/action 為字串（Gemini 可能回傳巢狀物件）
                    result["ai_report"] = _normalize_ai_report(raw)

            return result

        except Exception as e:
            print("AIService.analyze_symbol ERROR:", repr(e))
            print(traceback.format_exc())
            raise

    @staticmethod
    def analyze_opportunities(candidates: list[dict], market: str = "TW", lang: str = "zh"):
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("缺少 GEMINI_API_KEY")

        if not candidates:
            return []

        fallback_by_symbol: dict[str, dict] = {}
        lines: list[str] = []
        market_label = "台股" if market == "TW" and lang == "zh" else ("美股" if market == "US" and lang == "zh" else ("Taiwan stocks" if market == "TW" else "US stocks"))
        for idx, item in enumerate(candidates, 1):
            symbol = str(item.get("symbol", "")).upper().strip()
            if not symbol:
                continue
            fallback_by_symbol[symbol] = item
            reason = item.get("reason")
            if isinstance(reason, list):
                reason = "｜".join(str(x) for x in reason) if reason else "-"
            else:
                reason = str(reason) if reason else "-"
            price = item.get("price") if item.get("price") is not None else "-"
            chg = item.get("change_pct", item.get("change_percent"))
            chg = chg if chg is not None else "-"
            score = item.get("score") if item.get("score") is not None else "-"
            rsi = item.get("rsi") if item.get("rsi") is not None else "-"
            vol30 = item.get("volume_ratio_30d") if item.get("volume_ratio_30d") is not None else "-"
            sup = item.get("support") if item.get("support") is not None else "-"
            res = item.get("resistance") if item.get("resistance") is not None else "-"
            if lang == "zh":
                lines.append(
                    f"{idx}. {item.get('name') or symbol} ({symbol})｜價格={price}｜漲跌幅={chg}%｜技術分數={score}｜RSI={rsi}｜30天量比={vol30}｜支撐={sup}｜壓力={res}｜規則理由={reason}"
                )
            else:
                lines.append(
                    f"{idx}. {item.get('name') or symbol} ({symbol}) | price={price} | chg={chg}% | score={score} | RSI={rsi} | vol30={vol30} | support={sup} | resistance={res} | rule_reason={reason}"
                )

        if lang == "zh":
            prompt = (
                f"你是保守型股票分析研究員。以下是已經先用規則預篩過的{market_label}候選股票。\n"
                "請你依照動能、突破品質、量能放大、風險報酬，給每檔 0~100 的綜合評分。\n"
                "只回傳嚴格 JSON，格式如下：\n"
                '{"results":[{"symbol":"2330","score":82,"reason":"...","risk":"..."}]}\n'
                "最多只保留 5 檔，依分數高到低排序。\n"
                "候選清單：\n"
                + "\n".join(lines)
            )
        else:
            prompt = (
                f"You are a cautious market analyst. Evaluate the following {market_label} stocks that were prefiltered by technical rules.\n"
                "Give each symbol a composite opportunity score from 0 to 100 based on momentum, breakout quality, volume expansion, and risk/reward.\n"
                "Return STRICT JSON only in this format:\n"
                '{"results":[{"symbol":"AAPL","score":82,"reason":"...","risk":"..."}]}\n'
                "Keep at most 5 results, sorted by score descending.\n"
                "Candidates:\n"
                + "\n".join(lines)
            )

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        text_resp = getattr(resp, "text", "").strip()
        try:
            payload = json.loads(text_resp)
        except Exception:
            start = text_resp.find("{")
            end = text_resp.rfind("}")
            if start >= 0 and end > start:
                payload = json.loads(text_resp[start : end + 1])
            else:
                payload = {"results": []}

        return _normalize_ai_items(payload.get("results", []), fallback_by_symbol)

    @staticmethod
    def get_ai_opportunities(
        market: str = "TW",
        lang: str = "zh",
        limit: int = 8,
        force_refresh: bool = False,
    ):
        market = market.upper()

        if force_refresh or not _is_ai_cache_fresh(market, lang):
            AIService.refresh_ai_opportunities(market=market, lang=lang, limit=limit)

        items, updated_at = _load_ai_opportunity_cache(market=market, lang=lang, limit=limit)
        return {
            "market": market,
            "updated_at": updated_at,
            "source": "cache",
            "items": items,
        }

    @staticmethod
    def refresh_ai_opportunities(
        market: str = "TW",
        lang: str = "zh",
        limit: int = 8,
    ):
        market = market.upper()
        cached_candidates = get_cached_results(market, max(limit * 4, 24))
        # v14 規則：volume_ratio_30d >= 1.5 OR breakout_30d OR macd_golden；若無符合者則取技術分數前段
        vol_ratio = lambda x: (x or 0) >= 1.5 if isinstance(x, (int, float)) else False
        rule_based: list[dict] = []
        for item in cached_candidates:
            vr = item.get("volume_ratio_30d")
            br = item.get("breakout_30d")
            mg = item.get("macd_golden")
            if not (vol_ratio(vr) or br or mg):
                continue
            rule_based.append(item)
        candidates = rule_based if rule_based else cached_candidates[: max(limit * 2, 12)]
        candidates_out: list[dict] = []
        for item in candidates:
            vr = item.get("volume_ratio_30d")
            candidates_out.append(
                {
                    "symbol": item.get("symbol"),
                    "name": item.get("name") or item.get("symbol"),
                    "price": item.get("price"),
                    "change_pct": item.get("change_percent"),
                    "score": item.get("score"),
                    "rsi": item.get("rsi"),
                    "volume_ratio_30d": vr,
                    "support": item.get("support"),
                    "resistance": item.get("resistance"),
                    "reason": item.get("summary") or item.get("signals", []),
                }
            )

        candidates = candidates_out
        if not candidates:
            return {"market": market, "updated_at": None, "source": "cache", "items": []}

        ai_items = AIService.analyze_opportunities(
            candidates=candidates[: max(limit * 2, 12)],
            market=market,
            lang=lang,
        )
        ai_items = sorted(
            ai_items,
            key=lambda x: ((x.get("score") or 0), (x.get("confidence") or 0)),
            reverse=True,
        )[:limit]
        updated_at = _save_ai_opportunity_cache(market=market, lang=lang, items=ai_items)
        return {
            "market": market,
            "updated_at": updated_at,
            "source": "fresh",
            "items": ai_items,
        }

    @staticmethod
    def analyze_watchlist_daily(
        db: Session,
        user_id: int,
        market: str | None = None,
        interval: str = "1d",
        lang: str = "zh",
        quick_only: bool = False,
        limit: int = 20,
    ):
        query = db.query(Watchlist).filter(Watchlist.user_id == user_id)

        if market:
            query = query.filter(Watchlist.market == market)

        items = query.order_by(Watchlist.created_at.desc()).limit(limit).all()

        results = []
        for item in items:
            try:
                if item.market == "CRYPTO":
                    analyze_market = "CRYPTO"
                elif item.market == "TW":
                    analyze_market = "TW"
                else:
                    analyze_market = "US"

                result = AIService.analyze_symbol(
                    symbol=item.symbol,
                    market=analyze_market,
                    interval=interval,
                    lang=lang,
                    quick_only=quick_only,
                )

                results.append(
                    {
                        "watchlist_id": item.id,
                        "symbol": item.symbol,
                        "market": item.market,
                        "name": result.get("name"),
                        "interval": interval,
                        "quick_summary": result.get("quick_summary", {}),
                        "ai_report": result.get("ai_report"),
                        "error": None,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "watchlist_id": item.id,
                        "symbol": item.symbol,
                        "market": item.market,
                        "name": None,
                        "interval": interval,
                        "quick_summary": {},
                        "ai_report": None,
                        "error": str(e),
                    }
                )

        return results
