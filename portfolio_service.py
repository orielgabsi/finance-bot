import connect_firebase
import price_service


def compute_portfolio_value(portfolio: dict, prices: dict) -> dict:
    """`prices` maps ticker -> {"price": float, "day_change_pct": float|None} | None,
    i.e. the shape returned by price_service.get_current_prices_full."""
    holdings = {}
    total_cost = 0.0
    total_value = 0.0
    total_day_change_value = 0.0
    for ticker, details in portfolio.items():
        qty = details.get("quantity", 0)
        buy_price = details.get("buy_price", 0)
        price_info = prices.get(ticker) or {}
        price = price_info.get("price")
        day_change_pct = price_info.get("day_change_pct")
        period_change_pct = price_info.get("period_change_pct")
        period_label = price_info.get("period_label")
        cost = qty * buy_price
        value = qty * price if price is not None else None
        # Approximate today's dollar move (value * pct/100) — a display
        # convenience, not exact accounting, so no need for the slightly
        # more precise value*(pct/100)/(1+pct/100) derivation from previousClose.
        day_change_value = value * day_change_pct / 100 if (value is not None and day_change_pct is not None) else None
        holdings[ticker] = {
            "quantity": qty,
            "buy_price": buy_price,
            "name": details.get("name"),
            "cost_basis": cost,
            "current_price": price,
            "market_value": value,
            "gain_loss": (value - cost) if value is not None else None,
            "day_change_pct": day_change_pct,
            "day_change_value": day_change_value,
            "period_change_pct": period_change_pct,
            "period_label": period_label,
        }
        total_cost += cost
        if value is not None:
            total_value += value
        if day_change_value is not None:
            total_day_change_value += day_change_value

    return {
        "holdings": holdings,
        "total_cost": total_cost,
        "total_value": total_value,
        "total_gain_loss": total_value - total_cost,
        "total_gain_loss_pct": ((total_value - total_cost) / total_cost * 100) if total_cost else 0.0,
        "total_day_change_value": total_day_change_value,
    }


def get_portfolio_valuation(user_id: str) -> dict:
    portfolio = connect_firebase.get_portfolio(user_id)
    prices = price_service.get_current_prices_full(list(portfolio.keys()))
    return compute_portfolio_value(portfolio, prices)
