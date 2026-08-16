"""Deterministic technical-analysis indicators for a single ticker.

Mirrors the error-handling and no-invented-numbers conventions used by
fundamental_service.py / price_service.py: any missing/insufficient history
degrades to None fields plus a data_status flag, never a guessed number.
This module returns structured data only — no free-text reasoning, so an AI
recommendation layer consuming it must derive any narrative itself from
these numbers.
"""

import math

import pandas as pd
import yfinance as yf

_RSI_PERIOD = 14
_ATR_PERIOD = 14
_SMA_SHORT = 50
_SMA_LONG = 200


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _round(value, digits=2):
    return round(value, digits) if value is not None else None


def _rsi(closes: pd.Series, period: int = _RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean().iloc[-1]
    avg_loss = loss.rolling(window=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(history: pd.DataFrame, period: int = _ATR_PERIOD):
    if len(history) < period + 1:
        return None
    high, low, close = history["High"], history["Low"], history["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    value = true_range.rolling(window=period).mean().iloc[-1]
    return value


def _linear_score(value, low, high):
    """Maps value linearly onto 0-100 (low->0, high->100), clipped. Works
    for an inverted range (low > high) too."""
    if value is None:
        return None
    if low == high:
        return 50.0
    pct = (value - low) / (high - low) * 100
    return max(0.0, min(100.0, pct))


def _average_available(*values):
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 1) if present else None


def _compute_technical_score(rsi, price_vs_sma_50_pct, price_vs_sma_200_pct, high_52w, low_52w, last_price):
    momentum_score = _linear_score(rsi, 30, 70)
    trend_score = _average_available(
        _linear_score(price_vs_sma_50_pct, -10, 10),
        _linear_score(price_vs_sma_200_pct, -15, 15),
    )
    range_position_score = None
    if high_52w is not None and low_52w is not None and last_price is not None and high_52w != low_52w:
        range_position_score = max(0.0, min(100.0, (last_price - low_52w) / (high_52w - low_52w) * 100))
    score = _average_available(momentum_score, trend_score, range_position_score)
    return round(score, 1) if score is not None else None


def _empty_result(data_status="insufficient_data"):
    return {
        "rsi": None,
        "sma_50": None,
        "sma_200": None,
        "price_vs_sma_50_pct": None,
        "price_vs_sma_200_pct": None,
        "high_52w": None,
        "low_52w": None,
        "distance_from_high_pct": None,
        "distance_from_low_pct": None,
        "volatility": None,
        "atr": None,
        "technical_score": None,
        "data_status": data_status,
    }


def get_technical_analysis(ticker: str) -> dict:
    """Structured technical indicators for `ticker`. `data_status` is
    "ok", "partial" (some indicators lacked enough history) or
    "insufficient_data"/"data_unavailable" (no usable price history at all)."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return _empty_result("insufficient_data")
    try:
        history = yf.Ticker(ticker).history(period="2y", auto_adjust=True, timeout=15)
    except Exception:
        return _empty_result("data_unavailable")
    if history is None or history.empty:
        return _empty_result("data_unavailable")

    closes = history["Close"].dropna()
    if len(closes) < 5:
        return _empty_result("insufficient_data")

    last_price = _number(closes.iloc[-1])
    sma_50 = _number(closes.tail(_SMA_SHORT).mean()) if len(closes) >= _SMA_SHORT else None
    sma_200 = _number(closes.tail(_SMA_LONG).mean()) if len(closes) >= _SMA_LONG else None
    price_vs_sma_50_pct = ((last_price - sma_50) / sma_50 * 100) if (last_price is not None and sma_50) else None
    price_vs_sma_200_pct = ((last_price - sma_200) / sma_200 * 100) if (last_price is not None and sma_200) else None

    year_window = closes.tail(min(len(closes), 252))
    high_52w = _number(year_window.max())
    low_52w = _number(year_window.min())
    distance_from_high_pct = (
        (last_price - high_52w) / high_52w * 100 if (last_price is not None and high_52w) else None
    )
    distance_from_low_pct = (
        (last_price - low_52w) / low_52w * 100 if (last_price is not None and low_52w) else None
    )

    rsi = _number(_rsi(closes))

    returns = closes.pct_change().dropna().tail(252)
    volatility = _number(returns.std() * math.sqrt(252) * 100) if len(returns) > 10 else None

    atr = _number(_atr(history))

    technical_score = _compute_technical_score(
        rsi, price_vs_sma_50_pct, price_vs_sma_200_pct, high_52w, low_52w, last_price
    )

    missing_core_indicator = sma_50 is None or sma_200 is None or rsi is None
    data_status = "partial" if missing_core_indicator else "ok"

    return {
        "rsi": _round(rsi),
        "sma_50": _round(sma_50),
        "sma_200": _round(sma_200),
        "price_vs_sma_50_pct": _round(price_vs_sma_50_pct),
        "price_vs_sma_200_pct": _round(price_vs_sma_200_pct),
        "high_52w": _round(high_52w),
        "low_52w": _round(low_52w),
        "distance_from_high_pct": _round(distance_from_high_pct),
        "distance_from_low_pct": _round(distance_from_low_pct),
        "volatility": _round(volatility),
        "atr": _round(atr),
        "technical_score": technical_score,
        "data_status": data_status,
    }
