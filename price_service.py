import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
_CACHE_TTL_SECONDS = 300
# Unsupported numeric TASE identifiers are expensive: Yahoo fails twice and
# Globes may need a browser. Retrying that sequence every 30 seconds made the
# bot feel frozen. Keep failures for 15 minutes; successful values remain much
# fresher (5 minutes) and every account snapshot is refreshed in the background.
_CACHE_FAILURE_TTL_SECONDS = 900
_price_cache: dict[str, tuple[dict | None, float]] = {}  # ticker+name -> (result, fetched_at)
_cache_lock = threading.Lock()
_fx_cache: dict[tuple[str, str], tuple[float, float]] = {}

# Israeli brokerage exports use local security numbers even for US-listed
# ETFs. Resolve the two known holdings directly instead of first making
# several doomed Yahoo requests for the numeric code and its `.TA` variant.
_BROKER_SECURITY_ALIASES = {
    "410393": "GLDM",  # SPDR Gold MiniShares Trust
    "411462": "IBIT",  # iShares Bitcoin Trust ETF
}


def resolve_broker_symbol(value: str) -> str | None:
    """Translate a broker-local security number to its exchange symbol."""
    return _BROKER_SECURITY_ALIASES.get(str(value or "").strip().upper())

_ISRAELI_ALLOCATION_METADATA = {
    "1215771": {"sector": "ביטוח", "country": "ישראל", "market": "בורסת תל אביב"},
    "1144401": {"sector": "מדד NASDAQ 100", "country": "ארה״ב", "market": "NASDAQ"},
    "5112628": {"sector": "שוק מניות רחב", "country": "ישראל", "market": "ת״א 125"},
    "5141189": {"sector": "תעשיות ביטחוניות", "country": "ישראל", "market": "בורסת תל אביב"},
}


def _day_change_pct(last: float, prev_close: float | None) -> float | None:
    if not prev_close:
        return None
    return (last - prev_close) / prev_close * 100


def _historical_changes(ticker: str) -> dict:
    """Week, month and year changes from one compact history request."""
    try:
        closes = yf.Ticker(ticker).history(period="1y")["Close"].dropna()
        if len(closes) < 2:
            return {}
        last = float(closes.iloc[-1])

        def change_from(trading_days):
            first = float(closes.iloc[max(0, len(closes) - trading_days - 1)])
            return (last - first) / first * 100 if first else None

        first_year = float(closes.iloc[0])
        return {
            "week_change_pct": change_from(5),
            "month_change_pct": change_from(21),
            "year_change_pct": (last - first_year) / first_year * 100 if first_year else None,
        }
    except Exception:
        return {}


def _yfinance_price(ticker: str) -> dict | None:
    try:
        instrument = yf.Ticker(ticker)
        info = instrument.fast_info
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
        changes = _historical_changes(ticker)
        try:
            metadata = instrument.get_info() or {}
        except Exception:
            metadata = {}
        return {
            "price": price,
            "day_change_pct": _day_change_pct(price, prev_close),
            "period_change_pct": changes.get("month_change_pct"),
            "period_label": "בחודש האחרון",
            **changes,
            "resolved_symbol": ticker,
            "source": "Yahoo Finance",
            "currency": str(info.get("currency") or "").upper() or None,
            "sector": metadata.get("sector") or metadata.get("category"),
            "category": metadata.get("category"),
            "country": metadata.get("country") or metadata.get("region"),
            "market": metadata.get("market") or metadata.get("fullExchangeName"),
            "exchange": metadata.get("fullExchangeName") or metadata.get("exchange"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
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
    """Fast Globes accessible-page fallback for Israeli security numbers."""
    try:
        data = finance_engine.finance_engine_globes_data(ticker)
        if not data:
            return None
        return {
            "price": data["price"],
            "day_change_pct": data.get("day_change_pct"),
            "week_change_pct": data.get("week_change_pct"),
            "month_change_pct": data.get("month_change_pct"),
            "year_change_pct": data.get("year_change_pct"),
            "period_change_pct": data.get("month_change_pct"),
            "period_label": "החודש",
            "resolved_symbol": ticker,
            "resolved_name": data.get("name"),
            "source": data.get("source"),
            "source_url": data.get("source_url"),
            "price_unit": data.get("price_unit"),
            "currency": "ILS",
            **_ISRAELI_ALLOCATION_METADATA.get(str(ticker), {}),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return None


def _yfinance_name_price(name: str) -> dict | None:
    """Resolve imported security numbers through their human-readable name."""
    if not str(name or "").strip():
        return None
    try:
        search = yf.Search(str(name).strip(), max_results=6, timeout=8, raise_errors=False)
        for item in search.quotes or []:
            if str(item.get("quoteType") or "").upper() not in {"EQUITY", "ETF", "MUTUALFUND"}:
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            result = _yfinance_price(symbol)
            if result is not None:
                result["resolved_from_name"] = str(name).strip()
                return result
    except Exception:
        return None
    return None


def _fetch_uncached(ticker: str, name: str | None = None) -> dict | None:
    # Never fuzzy-match a known Israeli security name on Yahoo: it can return
    # an unrelated US fund with a similar English name and a plausible-looking
    # but completely wrong price. These codes have exact Globes instrument IDs.
    if finance_engine.has_known_instrument(ticker):
        return _globes_price(ticker)
    yahoo_alias = _BROKER_SECURITY_ALIASES.get(ticker.upper())
    if yahoo_alias:
        result = _yfinance_price(yahoo_alias)
        if result is not None:
            result["resolved_from_broker_number"] = ticker
            return result
    result = _yfinance_price(ticker)
    if result is not None:
        return result
    # Most TASE-listed Israeli tickers resolve on Yahoo Finance with a `.TA`
    # suffix — try that before falling back to the much slower Playwright/
    # Globes accessible-page reader below.
    if not ticker.upper().endswith(".TA"):
        result = _yfinance_price(f"{ticker}.TA")
        if result is not None:
            return result
    # Portfolio imports often preserve a local numeric security id but also
    # include the exact US fund name (for example 410393 + SPDR Gold
    # MiniShares Trust). Resolve the name before trying the browser scraper.
    result = _yfinance_name_price(name)
    if result is not None:
        return result
    return _globes_price(ticker)


def get_current_price_full(ticker: str, name: str | None = None) -> dict | None:
    """Returns {"price", "day_change_pct", "period_change_pct", "period_label"}
    (all but "price" may be None) or None if the ticker couldn't be resolved
    anywhere. "period_label" says what "period_change_pct" actually measures
    ("בחודש האחרון" via yfinance, "מתחילת החודש" via the Globes fallback) since the
    two sources don't expose the same timeframe."""
    ticker = ticker.strip().upper()
    cache_key = f"{ticker}\0{str(name or '').strip().casefold()}"
    now = time.time()
    with _cache_lock:
        cached = _price_cache.get(cache_key)
    if cached is not None:
        ttl = _CACHE_TTL_SECONDS if cached[0] is not None else _CACHE_FAILURE_TTL_SECONDS
        if (now - cached[1]) < ttl:
            return cached[0]
    result = _fetch_uncached(ticker, name)
    with _cache_lock:
        _price_cache[cache_key] = (result, time.time())
    return result


def get_current_price(ticker: str, name: str | None = None) -> float | None:
    result = get_current_price_full(ticker, name)
    return result["price"] if result else None


def get_current_prices_full(tickers: list[str], names: dict[str, str] | None = None) -> dict[str, dict | None]:
    """Each not-yet-cached lookup is a separate network round-trip (and
    occasionally a full headless-browser launch for the Globes fallback), so
    fetching them sequentially made /cake take 40-60s for a 6-holding
    portfolio in testing. Fetching in parallel cuts that to roughly the
    slowest single lookup, and the cache above cuts repeat calls further."""
    if not tickers:
        return {}
    names = names or {}
    with ThreadPoolExecutor(max_workers=min(len(tickers), 5)) as pool:
        futures = [pool.submit(get_current_price_full, ticker, names.get(ticker)) for ticker in tickers]
        results = [future.result() for future in futures]
    return dict(zip(tickers, results))


def get_current_prices(tickers: list[str], names: dict[str, str] | None = None) -> dict[str, float | None]:
    full = get_current_prices_full(tickers, names)
    return {ticker: (v["price"] if v else None) for ticker, v in full.items()}


# The web dashboard's portfolio-vs-index comparison. Nasdaq and TA-125 use
# the same known TASE tracker-fund security numbers already resolved via the
# Globes fallback elsewhere in this file (see finance_engine._GLOBES_INSTRUMENT_IDS) —
# reusing them means these two benchmarks share the exact price pipeline
# already proven for real user portfolios, instead of depending on a
# separate, untested Yahoo symbol for the Tel Aviv index.
_BENCHMARK_TICKERS = {
    "sp500": ("^GSPC", "S&P 500"),
    "nasdaq100": ("1144401", "נאסד״ק 100"),
    "ta125": ("5112628", "ת״א 125"),
}


def get_benchmark_performance() -> dict:
    """Day/week/month/year change for each of the three benchmarks shown in
    the web dashboard. Reuses the same cache as any other ticker lookup, so
    this doesn't add a network round-trip per refresh beyond the first."""
    result = {}
    for key, (ticker, label) in _BENCHMARK_TICKERS.items():
        info = get_current_price_full(ticker)
        result[key] = {**info, "label": label} if info else None
    return result


def get_fx_rate(source_currency: str, target_currency: str) -> float | None:
    source = str(source_currency or "").upper()
    target = str(target_currency or "").upper()
    if not source or not target or source == target:
        return 1.0
    key = (source, target)
    now = time.time()
    with _cache_lock:
        cached = _fx_cache.get(key)
    if cached and now - cached[1] < 1800:
        return cached[0]
    symbols = {
        ("USD", "ILS"): "ILS=X",
        ("EUR", "ILS"): "EURILS=X",
        ("ILS", "USD"): "ILS=X",
        ("ILS", "EUR"): "EURILS=X",
    }
    symbol = symbols.get(key)
    if not symbol:
        return None
    try:
        info = yf.Ticker(symbol).fast_info
        raw = next((float(info.get(k)) for k in _PRICE_KEYS if info.get(k)), None)
        rate = (1 / raw) if source == "ILS" else raw
    except Exception:
        return None
    with _cache_lock:
        _fx_cache[key] = (rate, time.time())
    return rate


def add_account_currency_scales(prices: dict, account_currency: str = "ILS") -> dict:
    """Attach quote-to-account conversion without changing displayed quotes."""
    account_currency = str(account_currency or "ILS").upper()
    currencies = {
        str(info.get("currency") or account_currency).upper()
        for info in prices.values() if info
    }
    rates = {currency: get_fx_rate(currency, account_currency) for currency in currencies}
    for info in prices.values():
        if not info:
            continue
        quote_currency = str(info.get("currency") or account_currency).upper()
        fx_rate = rates.get(quote_currency) or 1.0
        quote_unit_scale = 0.01 if info.get("price_unit") == "agorot" else 1.0
        info["fx_rate_to_account"] = fx_rate
        info["account_currency"] = account_currency
        info["account_unit_scale"] = quote_unit_scale * fx_rate
    return prices
