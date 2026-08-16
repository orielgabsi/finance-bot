"""Single source of truth for every AI-facing view of a user's portfolio.

Any prompt that needs to know about the user (profile/risk), their holdings,
cash, exposure, watchlist, open theses, journal history or recent
recommendations should call build_context() instead of independently
querying portfolio_service/connect_firebase for a slice of that state — this
keeps every AI surface (recommendations, chat, monitoring) looking at the
same facts.
"""

import connect_firebase
import portfolio_service


def _build_allocation(holdings: dict, portfolio_value: float) -> dict:
    """Per-holding share of the priced securities value, as a percentage."""
    if not portfolio_value:
        return {}
    return {
        ticker: round((details.get("market_value") or 0) / portfolio_value * 100, 2)
        for ticker, details in holdings.items()
    }


def _build_sector_exposure(holdings: dict, portfolio_value: float) -> dict:
    if not portfolio_value:
        return {}
    exposure = {}
    for details in holdings.values():
        sector = details.get("sector") or "Unknown"
        exposure[sector] = exposure.get(sector, 0.0) + (details.get("market_value") or 0)
    return {sector: round(value / portfolio_value * 100, 2) for sector, value in exposure.items()}


def _build_asset_class_exposure(valuation: dict) -> dict:
    """Equities vs cash vs savings/pension, as a share of total financial value.

    Uses total_financial_value (securities + cash + savings) as the
    denominator so it reflects the user's whole financial picture, not just
    the brokerage account.
    """
    total = valuation.get("total_financial_value")
    if not total:
        return {}
    equities = valuation.get("total_value", 0.0)
    cash = valuation.get("cash_balance", 0.0)
    savings = valuation.get("savings_total_value", 0.0)
    return {
        "equities": round(equities / total * 100, 2),
        "cash": round(cash / total * 100, 2),
        "savings_pension": round(savings / total * 100, 2),
    }


# Deterministic concentration limits per risk profile and position type —
# expressed as a max share of account_total_value a single ticker may reach.
# The AI recommendation engine may only suggest a size inside the resulting
# range; it must never invent position_size_ils itself.
_CONCENTRATION_LIMITS = {
    "conservative": {"Core": 0.05, "Satellite": 0.02},
    "balanced": {"Core": 0.08, "Satellite": 0.03},
    "aggressive": {"Core": 0.12, "Satellite": 0.05},
}
_DEFAULT_CONCENTRATION_LIMITS = {"Core": 0.08, "Satellite": 0.03}


def compute_position_size_range(context: dict, ticker: str, position_type: str = "Core") -> dict:
    """Deterministic min/max/suggested position size in the account's base
    currency, computed from portfolio value, available cash, risk profile,
    existing exposure to this ticker and a concentration cap for the
    position type. Never returns a size when the inputs required to size
    safely aren't available — callers must not substitute a guessed number
    for position_size_status != "OK"."""
    portfolio_value = float(context.get("account_total_value") or 0.0)
    cash = float(context.get("cash") or 0.0)
    risk_profile = str(context.get("risk_profile") or "balanced")
    position_type = position_type if position_type in ("Core", "Satellite") else "Core"
    ticker = str(ticker or "").strip().upper()

    if portfolio_value <= 0:
        return {"position_size_status": "INSUFFICIENT_DATA", "min_ils": None, "max_ils": None, "suggested_ils": None}

    limits = _CONCENTRATION_LIMITS.get(risk_profile, _DEFAULT_CONCENTRATION_LIMITS)
    concentration_cap_pct = limits[position_type]
    existing_allocation_pct = (context.get("allocation") or {}).get(ticker, 0.0) / 100
    remaining_room_pct = max(0.0, concentration_cap_pct - existing_allocation_pct)

    base = {
        "concentration_cap_pct": round(concentration_cap_pct * 100, 2),
        "existing_allocation_pct": round(existing_allocation_pct * 100, 2),
    }

    if remaining_room_pct <= 0:
        return {**base, "position_size_status": "AT_CONCENTRATION_LIMIT", "min_ils": 0.0, "max_ils": 0.0, "suggested_ils": 0.0}

    max_ils = min(portfolio_value * remaining_room_pct, cash)
    if max_ils <= 0:
        return {**base, "position_size_status": "INSUFFICIENT_CASH", "min_ils": 0.0, "max_ils": 0.0, "suggested_ils": 0.0}

    return {
        **base,
        "position_size_status": "OK",
        "min_ils": round(max_ils * 0.4, 2),
        "max_ils": round(max_ils, 2),
        "suggested_ils": round(max_ils * 0.7, 2),
    }


def build_context(user_id: str) -> dict:
    """Returns a unified snapshot of everything an AI prompt needs to know
    about this user. Falls back to a live valuation if no cached one exists
    yet (e.g. brand-new user)."""
    user_id = str(user_id)
    user_data = connect_firebase.get_user_data(user_id) or {}
    profile = connect_firebase.get_user_profile(user_id)

    valuation = portfolio_service.get_cached_portfolio_valuation(user_id)
    if valuation is None:
        valuation = portfolio_service.get_portfolio_valuation(user_id)

    holdings = valuation.get("holdings") or {}
    cash = valuation.get("cash_balance", 0.0)
    portfolio_value = valuation.get("total_value", 0.0)

    return {
        "user": {
            "user_id": user_id,
            "email": user_data.get("email"),
            "status": user_data.get("status"),
        },
        "profile": profile,
        "risk_profile": profile.get("risk_profile", "balanced"),
        "holdings": holdings,
        "cash": cash,
        "portfolio_value": portfolio_value,
        "account_total_value": valuation.get("account_total_value", 0.0),
        "allocation": _build_allocation(holdings, portfolio_value),
        "sector_exposure": _build_sector_exposure(holdings, portfolio_value),
        "asset_class_exposure": _build_asset_class_exposure(valuation),
        "watchlist": list(profile.get("watchlist") or []),
        "open_theses": connect_firebase.get_open_theses(user_id),
        "recent_journal": connect_firebase.get_recent_journal_entries(user_id),
        "recent_recommendations": connect_firebase.get_recent_analyses(user_id),
        "valuation": valuation,
    }
