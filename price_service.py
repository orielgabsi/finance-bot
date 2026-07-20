import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

import finance_engine

# yfinance's fast_info exposes the current price as "lastPrice" (camelCase) as
# of yfinance>=1.x — the older 0.2.x line used "last_price" and silently
# returned None under 1.x, which looked like "ticker not found" but was
# actually just a renamed field. Check both to be safe across versions.
_PRICE_KEYS = ("lastPrice", "last_price")
_PREV_CLOSE_KEYS = ("previousClose", "previous_close")

# Short-lived cache so repeated /cake presses (and different users holding the
# same ticker) don't re-trigger a network round-trip — or worse, a full
# headless-browser Globes lookup — every time. 120s is short enough that
# prices don't go stale during a session, long enough to absorb bursts.
_CACHE_TTL_SECONDS = 120
_price_cache: dict[str, tuple[dict, float]] = {}  # ticker -> (result, fetched_at)


def _day_change_pct(last: float, prev_close: float | None) -> float | None:
    if not prev_close:
        return None
    return (last - prev_close) / prev_close * 100


def _week_change_pct(ticker: str) -> float | None:
    """Change from ~6 trading days ago to the most recent close — an extra
    network round-trip on top of fast_info, but only paid once per TTL-cache
    window per ticker, same as everything else in this module."""
    try:
        closes = yf.Ticker(ticker).history(period="6d")["Close"].dropna()
        if len(closes) < 2:
            return None
        first, last = float(closes.iloc[0]), float(closes.iloc[-1])
        return (last - first) / first * 100 if first else None
    except Exception:
        return None


def _yfinance_price(ticker: str) -> dict | None:
    try:
        info = yf.Ticker(ticker).fast_info
        price = None
        for key in _PRICE_KEYS:
            val = info.get(key)
            if val:
                price = float(val)
                break
        if price is None:
            return None
        prev_close = None
        for key in _PREV_CLOSE_KEYS:
            val = info.get(key)
            if val:
                prev_close = float(val)
                break
        return {
            "price": price,
            "day_change_pct": _day_change_pct(price, prev_close),
            "period_change_pct": _week_change_pct(ticker),
            "period_label": "השבוע",
        }
    except Exception:
        return None


def _parse_globes_price(raw) -> float | None:
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def _parse_globes_change(raw) -> float | None:
    if not raw:
        return None
    try:
        return float(str(raw).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


def _globes_price(ticker: str) -> dict | None:
    """Fallback for Israeli-only instruments (TASE ETNs/structured products
    identified by a numeric security number, e.g. IBI's index trackers) that
    Yahoo Finance simply doesn't carry at all, at any ticker format. Slow —
    launches a real headless browser per call — so it's only reached once
    yfinance has already come up empty for both the plain ticker and the
    `.TA`-suffixed retry."""
    try:
        price_str, daily_change, monthly_change, _name = finance_engine.finance_engine_globes(ticker)
        price = _parse_globes_price(price_str)
        if price is None:
            return None
        # Globes only exposes daily and month-to-date change, not a weekly
        # figure — surfaced honestly as "since start of month" rather than
        # mislabeled as weekly, unlike the yfinance path above which has a
        # real week-over-week number.
        return {
            "price": price,
            "day_change_pct": _parse_globes_change(daily_change),
            "period_change_pct": _parse_globes_change(monthly_change),
            "period_label": "מתחילת החודש",
        }
    except Exception:
        return None


def _fetch_uncached(ticker: str) -> dict | None:
    result = _yfinance_price(ticker)
    if result is not None:
        return result
    # Most TASE-listed Israeli tickers resolve on Yahoo Finance with a `.TA`
    # suffix — try that before falling back to the much slower Playwright/
    # Globes scrape below (a full headless browser launch + a hardcoded 3s
    # sleep, per ticker, per call).
    if not ticker.upper().endswith(".TA"):
        result = _yfinance_price(f"{ticker}.TA")
        if result is not None:
            return result
    return _globes_price(ticker)


def get_current_price_full(ticker: str) -> dict | None:
    """Returns {"price", "day_change_pct", "period_change_pct", "period_label"}
    (all but "price" may be None) or None if the ticker couldn't be resolved
    anywhere. "period_label" says what "period_change_pct" actually measures
    ("השבוע" via yfinance, "מתחילת החודש" via the Globes fallback) since the
    two sources don't expose the same timeframe."""
    ticker = ticker.strip()
    cached = _price_cache.get(ticker)
    if cached is not None and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]
    result = _fetch_uncached(ticker)
    if result is not None:
        _price_cache[ticker] = (result, time.time())
    return result


def get_current_price(ticker: str) -> float | None:
    result = get_current_price_full(ticker)
    return result["price"] if result else None


def get_current_prices_full(tickers: list[str]) -> dict[str, dict | None]:
    """Each not-yet-cached lookup is a separate network round-trip (and
    occasionally a full headless-browser launch for the Globes fallback), so
    fetching them sequentially made /cake take 40-60s for a 6-holding
    portfolio in testing. Fetching in parallel cuts that to roughly the
    slowest single lookup, and the cache above cuts repeat calls further."""
    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(tickers), 5)) as pool:
        results = list(pool.map(get_current_price_full, tickers))
    return dict(zip(tickers, results))


def get_current_prices(tickers: list[str]) -> dict[str, float | None]:
    full = get_current_prices_full(tickers)
    return {ticker: (v["price"] if v else None) for ticker, v in full.items()}
