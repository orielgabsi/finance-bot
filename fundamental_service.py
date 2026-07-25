"""Deterministic stock/fund research built from public market data.

The score is an educational screening aid, not a trade instruction. AI text is
generated separately from this structured result so the model cannot invent
the underlying ratios or silently change the numeric score.
"""

from __future__ import annotations

import math
import time
from typing import Any

import yfinance as yf

import finance_engine
import price_service


_CACHE_TTL_SECONDS = 900
_analysis_cache: dict[str, tuple[dict, float]] = {}


def _number(value) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _percent(value) -> float | None:
    value = _number(value)
    if value is None:
        return None
    return value * 100 if abs(value) <= 2 else value


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, float(value)))


def _linear_score(value, bad, good, higher_is_better=True):
    value = _number(value)
    if value is None or good == bad:
        return None
    raw = (value - bad) / (good - bad) * 100
    if not higher_is_better:
        raw = 100 - raw
    return _clamp(raw)


def _average_available(values, default=50.0):
    usable = [float(v) for v in values if v is not None]
    return sum(usable) / len(usable) if usable else default


def _safe_info(ticker):
    try:
        return ticker.get_info() or {}
    except Exception:
        # .info is an alias for the same network call; retrying it here turns
        # one 30-second provider outage into a 60-second wait with no benefit.
        return {}


def resolve_symbol(query: str) -> tuple[str, Any, dict]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("נא להזין טיקר או שם של מניה/קרן.")

    normalized = query.upper()
    broker_alias = price_service.resolve_broker_symbol(normalized)
    candidates = [broker_alias] if broker_alias else [normalized]
    # Numeric Israeli security numbers commonly need Yahoo's .TA suffix.
    # Do not blindly try AAPL.TA after an AAPL outage: that doubles latency and
    # almost never represents the user's intent.
    if not broker_alias and "." not in normalized and normalized.isdigit():
        candidates.append(f"{normalized}.TA")

    for symbol in candidates:
        ticker = yf.Ticker(symbol)
        info = _safe_info(ticker)
        quote_type = str(info.get("quoteType") or "").upper()
        if info.get("symbol") or info.get("longName") or quote_type in {"EQUITY", "ETF", "MUTUALFUND"}:
            return str(info.get("symbol") or symbol).upper(), ticker, info

    # A company/fund name is also accepted. Search is deliberately a fallback
    # so an exact ticker never gets silently replaced by a fuzzy match.
    try:
        search = yf.Search(query, max_results=6, timeout=8, raise_errors=False)
        for item in search.quotes or []:
            quote_type = str(item.get("quoteType") or "").upper()
            if quote_type not in {"EQUITY", "ETF", "MUTUALFUND"}:
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol:
                ticker = yf.Ticker(symbol)
                return symbol, ticker, _safe_info(ticker)
    except Exception:
        pass

    raise ValueError(f"לא הצלחתי לזהות מניה או קרן עבור '{query}'.")


def _history_metrics(ticker) -> dict:
    try:
        history = ticker.history(period="5y", auto_adjust=True, timeout=15)
        closes = history["Close"].dropna()
    except Exception:
        closes = []
    if len(closes) < 2:
        return {
            "latest_close": None,
            "average_200d": None,
            "high_52w": None,
            "low_52w": None,
            "return_1y_pct": None,
            "return_3y_annualized_pct": None,
            "return_5y_annualized_pct": None,
            "volatility_1y_pct": None,
            "max_drawdown_5y_pct": None,
        }

    def trailing_return(days):
        if len(closes) < 2:
            return None
        start = closes.iloc[max(0, len(closes) - days - 1)]
        end = closes.iloc[-1]
        return ((end / start) - 1) * 100 if start else None

    def annualized(days):
        if len(closes) < min(days // 2, 252):
            return None
        start = closes.iloc[max(0, len(closes) - days - 1)]
        end = closes.iloc[-1]
        years = min(days, len(closes) - 1) / 252
        return ((end / start) ** (1 / years) - 1) * 100 if start and years > 0 else None

    recent = closes.tail(253)
    returns = recent.pct_change().dropna()
    volatility = float(returns.std() * math.sqrt(252) * 100) if len(returns) > 10 else None
    rolling_peak = closes.cummax()
    drawdown = (closes / rolling_peak - 1) * 100
    return {
        "latest_close": _number(closes.iloc[-1]),
        "average_200d": _number(closes.tail(200).mean()) if len(closes) >= 40 else None,
        "high_52w": _number(recent.max()),
        "low_52w": _number(recent.min()),
        "return_1y_pct": _number(trailing_return(252)),
        "return_3y_annualized_pct": _number(annualized(756)),
        "return_5y_annualized_pct": _number(annualized(1260)),
        "volatility_1y_pct": _number(volatility),
        "max_drawdown_5y_pct": _number(drawdown.min()),
    }


def _stock_metrics(info: dict, history: dict) -> dict:
    return {
        "market_cap": _number(info.get("marketCap")),
        "current_price": _number(info.get("currentPrice") or info.get("regularMarketPrice")),
        "trailing_pe": _number(info.get("trailingPE")),
        "forward_pe": _number(info.get("forwardPE")),
        "price_to_book": _number(info.get("priceToBook")),
        "enterprise_to_ebitda": _number(info.get("enterpriseToEbitda")),
        "profit_margin_pct": _percent(info.get("profitMargins")),
        "operating_margin_pct": _percent(info.get("operatingMargins")),
        "return_on_equity_pct": _percent(info.get("returnOnEquity")),
        "revenue_growth_pct": _percent(info.get("revenueGrowth")),
        "earnings_growth_pct": _percent(info.get("earningsGrowth")),
        "debt_to_equity": _number(info.get("debtToEquity")),
        "current_ratio": _number(info.get("currentRatio")),
        "free_cash_flow": _number(info.get("freeCashflow")),
        "operating_cash_flow": _number(info.get("operatingCashflow")),
        "dividend_yield_pct": _percent(info.get("dividendYield")),
        "beta": _number(info.get("beta")),
        "analyst_target_mean": _number(info.get("targetMeanPrice")),
        "forward_eps": _number(info.get("forwardEps")),
        "trailing_eps": _number(info.get("trailingEps")),
        "analyst_recommendation": info.get("recommendationKey"),
        **history,
    }


def _score_stock(metrics: dict) -> tuple[float, dict]:
    pe = metrics.get("forward_pe") or metrics.get("trailing_pe")
    valuation = _average_available([
        _linear_score(pe, 45, 12, True),
        _linear_score(metrics.get("price_to_book"), 10, 2, True),
        _linear_score(metrics.get("enterprise_to_ebitda"), 25, 8, True),
    ])
    quality = _average_available([
        _linear_score(metrics.get("return_on_equity_pct"), 0, 25, True),
        _linear_score(metrics.get("profit_margin_pct"), 0, 25, True),
        _linear_score(metrics.get("operating_margin_pct"), 0, 25, True),
        80 if (metrics.get("free_cash_flow") or 0) > 0 else 25,
    ])
    growth = _average_available([
        _linear_score(metrics.get("revenue_growth_pct"), -5, 20, True),
        _linear_score(metrics.get("earnings_growth_pct"), -10, 25, True),
        _linear_score(metrics.get("return_3y_annualized_pct"), -5, 18, True),
    ])
    health = _average_available([
        _linear_score(metrics.get("debt_to_equity"), 250, 30, True),
        _linear_score(metrics.get("current_ratio"), 0.7, 2.0, True),
        _linear_score(metrics.get("volatility_1y_pct"), 55, 18, True),
        _linear_score(metrics.get("max_drawdown_5y_pct"), -65, -20, True),
    ])
    breakdown = {
        "valuation": round(valuation, 1),
        "quality": round(quality, 1),
        "growth": round(growth, 1),
        "financial_health_and_risk": round(health, 1),
    }
    total = valuation * 0.25 + quality * 0.30 + growth * 0.25 + health * 0.20
    return round(total, 1), breakdown


def _stock_metric_scores(metrics: dict) -> dict:
    pe = metrics.get("forward_pe") or metrics.get("trailing_pe")
    return {
        "pe_ratio": _linear_score(pe, 45, 12, True),
        "price_to_book": _linear_score(metrics.get("price_to_book"), 10, 2, True),
        "enterprise_to_ebitda": _linear_score(metrics.get("enterprise_to_ebitda"), 25, 8, True),
        "return_on_equity": _linear_score(metrics.get("return_on_equity_pct"), 0, 25, True),
        "profit_margin": _linear_score(metrics.get("profit_margin_pct"), 0, 25, True),
        "operating_margin": _linear_score(metrics.get("operating_margin_pct"), 0, 25, True),
        "free_cash_flow": 80 if (metrics.get("free_cash_flow") or 0) > 0 else (25 if metrics.get("free_cash_flow") is not None else None),
        "revenue_growth": _linear_score(metrics.get("revenue_growth_pct"), -5, 20, True),
        "earnings_growth": _linear_score(metrics.get("earnings_growth_pct"), -10, 25, True),
        "return_1y": _linear_score(metrics.get("return_1y_pct"), -15, 25, True),
        "debt_to_equity": _linear_score(metrics.get("debt_to_equity"), 250, 30, True),
        "current_ratio": _linear_score(metrics.get("current_ratio"), 0.7, 2.0, True),
        "volatility": _linear_score(metrics.get("volatility_1y_pct"), 55, 18, True),
        "max_drawdown": _linear_score(metrics.get("max_drawdown_5y_pct"), -65, -20, True),
    }


def _fund_data(ticker) -> dict:
    result = {"top_holdings": [], "sector_weightings": {}, "fund_overview": {}}
    try:
        data = ticker.get_funds_data()
        if data is None:
            return result
        overview = data.fund_overview or {}
        result["fund_overview"] = {
            str(k): v for k, v in dict(overview).items()
            if isinstance(v, (str, int, float, bool)) or v is None
        }
        sectors = data.sector_weightings or {}
        result["sector_weightings"] = {
            str(k): _percent(v) for k, v in dict(sectors).items()
            if _number(v) is not None
        }
        top = data.top_holdings
        if top is not None and not top.empty:
            for symbol, row in top.head(10).iterrows():
                row_dict = row.to_dict()
                result["top_holdings"].append({
                    "symbol": str(symbol),
                    "name": str(row_dict.get("Name") or row_dict.get("name") or symbol),
                    "weight_pct": _percent(row_dict.get("Holding Percent") or row_dict.get("holdingPercent")),
                })
    except Exception:
        pass
    return result


def _fund_metrics(info: dict, history: dict, fund_data: dict) -> dict:
    top_weight = sum(item.get("weight_pct") or 0 for item in fund_data.get("top_holdings", []))
    sector_max = max(fund_data.get("sector_weightings", {}).values(), default=None)
    return {
        "current_price": _number(info.get("regularMarketPrice") or info.get("navPrice")),
        "nav_price": _number(info.get("navPrice")),
        "total_assets": _number(info.get("totalAssets")),
        "expense_ratio_pct": _percent(
            info.get("annualReportExpenseRatio")
            or fund_data.get("fund_overview", {}).get("expenseRatio")
        ),
        "dividend_yield_pct": _percent(info.get("yield") or info.get("dividendYield")),
        "beta_3y": _number(info.get("beta3Year") or info.get("beta")),
        "category": info.get("category"),
        "top_10_weight_pct": round(top_weight, 2) if top_weight else None,
        "largest_sector_weight_pct": _number(sector_max),
        **history,
    }


def _score_fund(metrics: dict) -> tuple[float, dict]:
    cost = _average_available([
        _linear_score(metrics.get("expense_ratio_pct"), 1.5, 0.08, True),
    ])
    performance = _average_available([
        _linear_score(metrics.get("return_1y_pct"), -10, 18, True),
        _linear_score(metrics.get("return_3y_annualized_pct"), -3, 14, True),
        _linear_score(metrics.get("return_5y_annualized_pct"), 0, 12, True),
    ])
    diversification = _average_available([
        _linear_score(metrics.get("top_10_weight_pct"), 70, 25, True),
        _linear_score(metrics.get("largest_sector_weight_pct"), 65, 25, True),
    ])
    risk = _average_available([
        _linear_score(metrics.get("volatility_1y_pct"), 45, 12, True),
        _linear_score(metrics.get("max_drawdown_5y_pct"), -60, -15, True),
    ])
    breakdown = {
        "cost": round(cost, 1),
        "performance": round(performance, 1),
        "diversification": round(diversification, 1),
        "risk": round(risk, 1),
    }
    total = cost * 0.25 + performance * 0.35 + diversification * 0.20 + risk * 0.20
    return round(total, 1), breakdown


def _fund_metric_scores(metrics: dict) -> dict:
    return {
        "expense_ratio": _linear_score(metrics.get("expense_ratio_pct"), 1.5, 0.08, True),
        "return_1y": _linear_score(metrics.get("return_1y_pct"), -10, 18, True),
        "return_3y": _linear_score(metrics.get("return_3y_annualized_pct"), -3, 14, True),
        "return_5y": _linear_score(metrics.get("return_5y_annualized_pct"), 0, 12, True),
        "top_10_weight": _linear_score(metrics.get("top_10_weight_pct"), 70, 25, True),
        "largest_sector_weight": _linear_score(metrics.get("largest_sector_weight_pct"), 65, 25, True),
        "volatility": _linear_score(metrics.get("volatility_1y_pct"), 45, 12, True),
        "max_drawdown": _linear_score(metrics.get("max_drawdown_5y_pct"), -60, -15, True),
    }


def _verdict(score: float) -> str:
    if score >= 75:
        return "attractive"
    if score >= 60:
        return "watch"
    if score >= 45:
        return "cautious"
    return "avoid_for_now"


def _analyze_known_israeli_fund(code: str) -> dict:
    data = finance_engine.finance_engine_globes_data(code)
    if not data:
        raise ValueError(f"גלובס לא החזיר כרגע נתונים עבור הקרן {code}.")
    quoted_price = _number(data.get("price"))
    price_ils = quoted_price / 100 if quoted_price is not None else None
    metrics = {
        "current_price": price_ils,
        "quoted_price_agorot": quoted_price,
        "nav_price": None,
        "total_assets": None,
        "expense_ratio_pct": None,
        "dividend_yield_pct": None,
        "beta_3y": None,
        "category": "קרן ישראלית",
        "top_10_weight_pct": None,
        "largest_sector_weight_pct": None,
        "latest_close": price_ils,
        "average_200d": None,
        "high_52w": None,
        "low_52w": None,
        "return_1w_pct": _number(data.get("week_change_pct")),
        "return_1m_pct": _number(data.get("month_change_pct")),
        "return_1y_pct": _number(data.get("year_change_pct")),
        "return_3y_annualized_pct": None,
        "return_5y_annualized_pct": None,
        "volatility_1y_pct": None,
        "max_drawdown_5y_pct": None,
        "day_change_pct": _number(data.get("day_change_pct")),
    }
    score, breakdown = _score_fund(metrics)
    metric_scores = _fund_metric_scores(metrics)
    return {
        "symbol": code,
        "name": data.get("name") or code,
        "asset_type": "fund",
        "quote_type": "ISRAELI_FUND",
        "currency": "ILS",
        "exchange": "TLV",
        "sector": None,
        "industry": None,
        "summary": "נתוני מחיר ותשואה פומביים מגלובס. נתונים פונדמנטליים שאינם זמינים מסומנים כחסרים.",
        "metrics": metrics,
        "score": score,
        "score_breakdown": breakdown,
        "metric_scores": {
            key: (round(value, 1) if value is not None else None)
            for key, value in metric_scores.items()
        },
        "screening_verdict": _verdict(score),
        "data_quality": "limited",
        "top_holdings": [],
        "sector_weightings": {},
        "data_source": "Globes",
        "source_url": data.get("source_url"),
    }


def build_entry_guidance(analysis: dict, profile: dict | None = None) -> dict:
    """Build a transparent educational entry zone from trusted market fields.

    Analyst consensus is preferred for stocks and NAV for funds. A 200-day
    average is only a trend reference fallback and is explicitly labelled as
    such; it must never be presented as intrinsic value.
    """
    profile = profile or {}
    metrics = analysis.get("metrics") or {}
    asset_type = analysis.get("asset_type") or "stock"
    risk_profile = str(profile.get("risk_profile") or "balanced").lower()
    current = _number(metrics.get("current_price") or metrics.get("latest_close"))
    score = _number(analysis.get("score"))

    reference = None
    reference_kind = "unavailable"
    reference_label_he = "אין מקור ייחוס אמין זמין"
    if asset_type == "fund" and _number(metrics.get("nav_price")):
        reference = _number(metrics.get("nav_price"))
        reference_kind = "nav"
        reference_label_he = "שווי נכסי נקי (NAV) שדווח לקרן"
        margin = {"conservative": 0.03, "balanced": 0.02, "aggressive": 0.01}.get(risk_profile, 0.02)
        zone_width = 0.03
    elif asset_type == "stock" and _number(metrics.get("analyst_target_mean")):
        reference = _number(metrics.get("analyst_target_mean"))
        reference_kind = "analyst_consensus"
        reference_label_he = "יעד מחיר ממוצע של אנליסטים"
        margin = {"conservative": 0.25, "balanced": 0.20, "aggressive": 0.15}.get(risk_profile, 0.20)
        zone_width = 0.08
    elif _number(metrics.get("average_200d")):
        reference = _number(metrics.get("average_200d"))
        reference_kind = "historical_200d_average"
        reference_label_he = "ממוצע 200 יום — ייחוס מגמה בלבד, לא שווי הוגן"
        margin = ({"conservative": 0.10, "balanced": 0.07, "aggressive": 0.04}.get(risk_profile, 0.07)
                  if asset_type == "fund" else
                  {"conservative": 0.15, "balanced": 0.10, "aggressive": 0.05}.get(risk_profile, 0.10))
        zone_width = 0.05
    else:
        margin = None
        zone_width = None

    result = {
        "status": "insufficient_data",
        "status_label_he": "אין מספיק נתונים לקביעת טווח כניסה",
        "current_price": round(current, 4) if current is not None else None,
        "reference_price": round(reference, 4) if reference is not None else None,
        "reference_kind": reference_kind,
        "reference_label_he": reference_label_he,
        "currency": analysis.get("currency"),
        "entry_zone_low": None,
        "entry_zone_high": None,
        "current_vs_reference_pct": None,
        "margin_of_safety_pct": round(margin * 100, 1) if margin is not None else None,
        "conditions_he": [
            "לוודא שהדוחות או נתוני הקרן לא הורעו מאז העדכון האחרון",
            "להתאים את גודל הפוזיציה לסיכון ולפיזור בתיק",
        ],
        "methodology_he": "טווח לימודי המבוסס על נתוני שוק זמינים; אינו הבטחת תשואה או הוראת קנייה.",
    }
    if current is None or reference is None or reference <= 0:
        return result

    entry_high = reference * (1 - margin)
    entry_low = reference * (1 - margin - zone_width)
    comparison = (current / reference - 1) * 100
    result.update({
        "entry_zone_low": round(entry_low, 4),
        "entry_zone_high": round(entry_high, 4),
        "current_vs_reference_pct": round(comparison, 2),
    })

    if score is not None and score < 50:
        result["status"] = "weak_fundamentals"
        result["status_label_he"] = "להמתין — הציון הפונדמנטלי חלש גם אם המחיר נמוך"
        result["conditions_he"].insert(0, "להמתין לשיפור בציון הפונדמנטלי מעל 50/100")
    elif current < entry_low:
        result["status"] = "below_zone_verify"
        result["status_label_he"] = "מתחת לטווח — לבדוק אם הירידה נובעת מהרעה מהותית לפני פעולה"
        result["conditions_he"].insert(0, "לבדוק שאין אירוע שלילי חדש שמסביר את המחיר הנמוך")
    elif current <= entry_high:
        result["status"] = "in_entry_zone"
        result["status_label_he"] = "בתוך טווח הכניסה הלימודי — אפשר לשקול קנייה הדרגתית"
        result["conditions_he"].insert(0, "לשקול כניסה בשלבים ולא בפעולה אחת")
    elif current <= reference:
        result["status"] = "near_reference_wait"
        result["status_label_he"] = "קרוב למחיר הייחוס אך מעל טווח הכניסה — להמתין או לעקוב"
        result["conditions_he"].insert(0, f"להמתין למחיר של {entry_high:,.2f} או פחות")
    else:
        result["status"] = "above_reference"
        result["status_label_he"] = "מעל מחיר הייחוס — לא לרדוף אחרי המחיר"
        result["conditions_he"].insert(0, f"להמתין למחיר של {entry_high:,.2f} או פחות")
    return result


def analyze_asset(query: str, force_refresh: bool = False) -> dict:
    cache_key = str(query or "").strip().upper()
    cached = _analysis_cache.get(cache_key)
    if not force_refresh and cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    israeli_code = finance_engine.resolve_known_code(query)
    if israeli_code:
        result = _analyze_known_israeli_fund(israeli_code)
        _analysis_cache[cache_key] = (result, time.time())
        _analysis_cache[israeli_code] = (result, time.time())
        return result

    symbol, ticker, info = resolve_symbol(query)
    quote_type = str(info.get("quoteType") or "EQUITY").upper()
    asset_type = "fund" if quote_type in {"ETF", "MUTUALFUND"} else "stock"
    history = _history_metrics(ticker)
    fund_data = _fund_data(ticker) if asset_type == "fund" else {}
    if asset_type == "fund":
        metrics = _fund_metrics(info, history, fund_data)
        score, breakdown = _score_fund(metrics)
        metric_scores = _fund_metric_scores(metrics)
    else:
        metrics = _stock_metrics(info, history)
        score, breakdown = _score_stock(metrics)
        metric_scores = _stock_metric_scores(metrics)

    available = sum(value is not None for value in metrics.values())
    result = {
        "symbol": symbol,
        "name": info.get("longName") or info.get("shortName") or symbol,
        "asset_type": asset_type,
        "quote_type": quote_type,
        "currency": info.get("currency"),
        "exchange": info.get("exchange"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "summary": (info.get("longBusinessSummary") or fund_data.get("fund_overview", {}).get("description") or "")[:1200],
        "metrics": metrics,
        "score": score,
        "score_breakdown": breakdown,
        "metric_scores": {
            key: (round(value, 1) if value is not None else None)
            for key, value in metric_scores.items()
        },
        "screening_verdict": _verdict(score),
        "data_quality": "high" if available >= 12 else "medium" if available >= 7 else "limited",
        "top_holdings": fund_data.get("top_holdings", []),
        "sector_weightings": fund_data.get("sector_weightings", {}),
    }
    _analysis_cache[cache_key] = (result, time.time())
    _analysis_cache[symbol] = (result, time.time())
    return result
