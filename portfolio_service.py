import connect_firebase
import price_service
import savings_service


# Israeli brokerage exports sometimes contain only a local security number.
# These names are stable identifiers for securities already encountered in
# this portfolio; keeping the mapping here also lets pricing resolve US funds
# by name when Yahoo does not understand the local brokerage number.
KNOWN_SECURITY_NAMES = {
    "410393": "SPDR Gold MiniShares Trust",
    "411462": "iShares Bitcoin Trust",
    "1215771": "אי.בי.אי. סל ת״א-ביטוח",
    "1144401": "תכלית סל NASDAQ 100",
    "5112628": "אי.בי.אי. מחקה ת״א 125",
    "5141189": "אי.בי.אי. מניות תעשיות ביטחוניות ישראל",
}


def get_holding_name(ticker: str, details: dict | None = None) -> str | None:
    saved = str((details or {}).get("name") or "").strip()
    return saved or KNOWN_SECURITY_NAMES.get(str(ticker).strip().upper())


def compute_portfolio_value(portfolio: dict, prices: dict, cash_balance: float = 0.0) -> dict:
    """`prices` maps ticker -> {"price": float, "day_change_pct": float|None} | None,
    i.e. the shape returned by price_service.get_current_prices_full."""
    holdings = {}
    total_cost = 0.0
    priced_cost = 0.0
    unpriced_cost = 0.0
    total_value = 0.0
    total_day_change_value = 0.0
    unpriced_tickers = []
    price_timestamps = []
    for ticker, details in portfolio.items():
        qty = details.get("quantity", 0)
        buy_price = details.get("buy_price", 0)
        price_info = prices.get(ticker) or {}
        price = price_info.get("price")
        day_change_pct = price_info.get("day_change_pct")
        week_change_pct = price_info.get("week_change_pct")
        month_change_pct = price_info.get("month_change_pct")
        year_change_pct = price_info.get("year_change_pct")
        period_change_pct = price_info.get("period_change_pct")
        period_label = price_info.get("period_label")
        price_unit = price_info.get("price_unit")
        price_fetched_at = price_info.get("fetched_at")
        if price_fetched_at:
            price_timestamps.append(str(price_fetched_at))
        # TASE funds on Globes are quoted in agorot. The brokerage cost price
        # uses the same convention, while portfolio totals are monetary ILS.
        # Without this scale both value and P/L are inflated exactly 100x.
        unit_scale = price_info.get("account_unit_scale")
        if unit_scale is None:
            unit_scale = 0.01 if price_unit == "agorot" else 1.0
        current_fx_rate = float(price_info.get("fx_rate_to_account") or 1.0)
        quote_unit_scale = 0.01 if price_unit == "agorot" else 1.0
        entry_fx_rate = details.get("buy_fx_rate")
        cost_unit_scale = quote_unit_scale * float(entry_fx_rate) if entry_fx_rate else unit_scale
        reported_total_cost = details.get("reported_total_cost")
        try:
            reported_total_cost = float(reported_total_cost) if reported_total_cost is not None else None
        except (TypeError, ValueError):
            reported_total_cost = None
        if reported_total_cost is not None and reported_total_cost > 0:
            reported_cost_scale = cost_unit_scale / quote_unit_scale if quote_unit_scale else 1.0
            cost = reported_total_cost * reported_cost_scale
        else:
            cost = qty * buy_price * cost_unit_scale
        value = qty * price * unit_scale if price is not None else None
        gain_loss = (value - cost) if value is not None else None
        asset_gain_at_entry_fx = (
            qty * (price - buy_price) * cost_unit_scale
            if price is not None and entry_fx_rate else None
        )
        fx_gain_loss = (
            gain_loss - asset_gain_at_entry_fx
            if gain_loss is not None and asset_gain_at_entry_fx is not None else None
        )
        # Approximate today's dollar move (value * pct/100) — a display
        # convenience, not exact accounting, so no need for the slightly
        # more precise value*(pct/100)/(1+pct/100) derivation from previousClose.
        day_change_value = value * day_change_pct / 100 if (value is not None and day_change_pct is not None) else None
        holdings[ticker] = {
            "quantity": qty,
            "buy_price": buy_price,
            "buy_price_account_currency": buy_price * cost_unit_scale,
            "reported_total_cost": reported_total_cost,
            "exact_unit_cost_account_currency": cost / qty if qty else None,
            "buy_fx_rate": float(entry_fx_rate) if entry_fx_rate else None,
            "current_fx_rate": current_fx_rate,
            "buy_date": details.get("buy_date"),
            "name": get_holding_name(ticker, details),
            "cost_basis": cost,
            "current_price": price,
            "current_price_account_currency": price * unit_scale if price is not None else None,
            "market_value": value,
            "gain_loss": gain_loss,
            "gain_loss_pct": (gain_loss / cost * 100) if gain_loss is not None and cost else None,
            "asset_gain_loss_at_entry_fx": asset_gain_at_entry_fx,
            "fx_gain_loss": fx_gain_loss,
            "fx_return_pct": ((current_fx_rate / float(entry_fx_rate) - 1) * 100) if entry_fx_rate else None,
            "day_change_pct": day_change_pct,
            "day_change_value": day_change_value,
            "week_change_pct": week_change_pct,
            "month_change_pct": month_change_pct,
            "year_change_pct": year_change_pct,
            "period_change_pct": period_change_pct,
            "period_label": period_label,
            "price_source": price_info.get("source"),
            "price_source_url": price_info.get("source_url"),
            "price_fetched_at": price_fetched_at,
            "price_unit": price_unit,
            "unit_scale": unit_scale,
            "quote_currency": price_info.get("currency"),
            "account_currency": price_info.get("account_currency"),
            "fx_rate_to_account": price_info.get("fx_rate_to_account"),
            "sector": price_info.get("sector"),
            "category": price_info.get("category"),
            "country": price_info.get("country"),
            "market": price_info.get("market"),
            "exchange": price_info.get("exchange"),
        }
        total_cost += cost
        if value is not None:
            total_value += value
            priced_cost += cost
        else:
            unpriced_cost += cost
            unpriced_tickers.append(ticker)
        if day_change_value is not None:
            total_day_change_value += day_change_value

    # Never count an unpriced asset as if it were worthless. Gain/loss is
    # calculated only over the subset for which a live value is available;
    # the response separately exposes missing-price cost/tickers so every UI
    # can be explicit about incomplete coverage.
    gain_loss = total_value - priced_cost
    cash_balance = max(float(cash_balance or 0), 0.0)

    return {
        "holdings": holdings,
        "total_cost": total_cost,
        "priced_cost": priced_cost,
        "unpriced_cost": unpriced_cost,
        "total_value": total_value,
        "cash_balance": cash_balance,
        "account_total_value": total_value + cash_balance,
        "total_gain_loss": gain_loss,
        "total_gain_loss_pct": (gain_loss / priced_cost * 100) if priced_cost else 0.0,
        "total_day_change_value": total_day_change_value,
        "pricing_complete": not unpriced_tickers,
        "unpriced_tickers": unpriced_tickers,
        "prices_fetched_at": max(price_timestamps, default=None),
    }


def _attach_financial_assets(user_id: str, valuation: dict) -> dict:
    """Keep brokerage value separate while also exposing the full financial view."""
    assets = connect_firebase.list_financial_assets(user_id)
    valued_assets = {}
    for asset_id, asset in assets.items():
        valued = savings_service.value_financial_asset(asset)
        # A persisted estimate is newer/more useful than a no-network rebuild.
        if asset.get("estimated_balance") is not None:
            valued["estimated_balance"] = float(asset.get("estimated_balance") or 0)
            valued["estimated_gain_loss"] = asset.get("estimated_gain_loss")
        valued_assets[asset_id] = valued
    savings_total = sum(
        float(item.get("estimated_balance", item.get("reported_balance", 0)) or 0)
        for item in valued_assets.values()
    )
    valuation["financial_assets"] = valued_assets
    valuation["savings_total_value"] = savings_total
    valuation["total_financial_value"] = valuation.get("account_total_value", 0) + savings_total
    goal = connect_firebase.get_financial_goal(user_id)
    target = float(goal.get("target_amount", 0) or 0)
    valuation["financial_goal"] = goal
    valuation["financial_goal_progress_pct"] = (
        min(100.0, valuation["total_financial_value"] / target * 100) if target > 0 else None
    )
    return valuation


_BENCHMARK_PERIODS = ("day_change_pct", "week_change_pct", "month_change_pct", "year_change_pct")


def _attach_benchmark_comparison(valuation: dict) -> dict:
    """Compares the priced securities' value-weighted return to the S&P 500,
    Nasdaq 100 and TA-125 over the same day/week/month/year windows. Reuses
    the per-holding change percentages already computed for each holding's
    own price lookup, so no extra per-holding network calls are needed —
    only one extra lookup per index."""
    holdings = valuation.get("holdings") or {}
    portfolio_returns = {}
    for period in _BENCHMARK_PERIODS:
        weighted_sum = 0.0
        weight_total = 0.0
        for holding in holdings.values():
            value = holding.get("market_value")
            change = holding.get(period)
            if value and value > 0 and change is not None:
                weighted_sum += value * change
                weight_total += value
        portfolio_returns[period] = (weighted_sum / weight_total) if weight_total else None
    indices = price_service.get_benchmark_performance()
    valuation["benchmark"] = {
        "portfolio": portfolio_returns,
        "indices": {
            key: {
                "label": info.get("label") if info else None,
                **{period: (info.get(period) if info else None) for period in _BENCHMARK_PERIODS},
            }
            for key, info in indices.items()
        },
        "coverage_value": sum(
            holding.get("market_value") or 0 for holding in holdings.values() if holding.get("market_value")
        ),
    }
    return valuation


def get_portfolio_valuation(user_id: str) -> dict:
    data = connect_firebase.get_user_data(user_id) or {}
    portfolio = data.get("portfolio") or {}
    names = {ticker: get_holding_name(ticker, details) for ticker, details in portfolio.items()}
    prices = price_service.get_current_prices_full(list(portfolio.keys()), names)
    account_currency = (data.get("profile") or {}).get("base_currency") or "ILS"
    price_service.add_account_currency_scales(prices, account_currency)
    cash_balance = data.get("cash_balance", 0)
    valuation = compute_portfolio_value(portfolio, prices, cash_balance)
    _attach_benchmark_comparison(valuation)
    return _attach_financial_assets(user_id, valuation)


def get_cached_portfolio_valuation(user_id: str) -> dict | None:
    """Returns a fast snapshot reconciled against the authoritative portfolio.

    A valuation may finish after a sell/import and therefore contain holdings
    that no longer exist. The website reads this same snapshot, so always
    rebuild membership, quantities and cost basis from the current portfolio
    while reusing only cached market prices/changes.
    """
    data = connect_firebase.get_user_data(user_id) or {}
    valuation = data.get("last_valuation")
    if not isinstance(valuation, dict):
        return None

    portfolio = data.get("portfolio") or {}
    cached_holdings = valuation.get("holdings") or {}
    cached_prices = {}
    for ticker in portfolio:
        cached = cached_holdings.get(ticker) or {}
        cached_prices[ticker] = {
            "price": cached.get("current_price"),
            "day_change_pct": cached.get("day_change_pct"),
            "week_change_pct": cached.get("week_change_pct"),
            "month_change_pct": cached.get("month_change_pct"),
            "year_change_pct": cached.get("year_change_pct"),
            "period_change_pct": cached.get("period_change_pct"),
            "period_label": cached.get("period_label"),
            "source": cached.get("price_source"),
            "source_url": cached.get("price_source_url"),
            "price_unit": cached.get("price_unit"),
            "currency": cached.get("quote_currency"),
            "account_currency": cached.get("account_currency"),
            "fx_rate_to_account": cached.get("fx_rate_to_account"),
            "account_unit_scale": cached.get("unit_scale"),
            "fetched_at": cached.get("price_fetched_at"),
            "sector": cached.get("sector"),
            "category": cached.get("category"),
            "country": cached.get("country"),
            "market": cached.get("market"),
            "exchange": cached.get("exchange"),
        }

    reconciled = compute_portfolio_value(
        portfolio,
        cached_prices,
        data.get("cash_balance", 0),
    )
    _attach_benchmark_comparison(reconciled)
    _attach_financial_assets(user_id, reconciled)
    old_symbols = set(cached_holdings)
    new_symbols = set(portfolio)
    holdings_changed = old_symbols != new_symbols or any(
        float((cached_holdings.get(ticker) or {}).get("quantity", 0) or 0)
        != float(details.get("quantity", 0) or 0)
        or float((cached_holdings.get(ticker) or {}).get("buy_price", 0) or 0)
        != float(details.get("buy_price", 0) or 0)
        for ticker, details in portfolio.items()
    )
    if holdings_changed or float(valuation.get("cash_balance", 0) or 0) != float(data.get("cash_balance", 0) or 0):
        connect_firebase.save_valuation_snapshot(user_id, reconciled)
    return reconciled
