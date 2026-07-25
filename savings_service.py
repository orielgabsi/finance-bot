"""Public performance data and transparent estimates for long-term savings.

An individual's balance is private and cannot be fetched from a public fund
page.  The user therefore supplies an authoritative balance and date.  We may
then estimate subsequent movement using only *completed monthly* returns that
were published after that date.  The estimate is always kept separate from the
reported balance and carries its reporting period/source.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date

import requests
from bs4 import BeautifulSoup


TRACKS = {
    "7799": {
        "name": "אלטשולר שחם חיסכון פלוס מניות",
        "provider": "אלטשולר שחם",
        "asset_type": "gemel_investment",
        "data_url": "https://moregemelnet.co.il/fund/7799",
        "official_url": "https://www.as-invest.co.il/interstedin/חיסכון-והשקעה/אלטשולר-שחם-חיסכון-פלוס-מניות/",
    },
    "14864": {
        "name": "אלטשולר שחם חיסכון פלוס עוקב מדדי מניות",
        "provider": "אלטשולר שחם",
        "asset_type": "gemel_investment",
        "data_url": "https://moregemelnet.co.il/fund/14864",
        "official_url": "https://www.as-invest.co.il/interstedin/חיסכון-והשקעה/אלטשולר-שחם-חיסכון-פלוס-עוקב-מדדי-מניות/",
    },
}

_HEBREW_MONTHS = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "אפריל": 4,
    "מאי": 5, "יוני": 6, "יולי": 7, "אוגוסט": 8,
    "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}
_CACHE_TTL_SECONDS = 6 * 60 * 60
_cache: dict[str, tuple[dict, float]] = {}
_cache_lock = threading.Lock()


def supported_tracks() -> dict:
    return {key: dict(value) for key, value in TRACKS.items()}


def _percent(value) -> float | None:
    cleaned = str(value or "").replace("\u200e", "").replace("%", "").replace(",", "").strip()
    if not cleaned or cleaned == "—":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _report_period(label: str) -> str | None:
    parts = str(label or "").strip().split()
    if len(parts) != 2 or parts[0] not in _HEBREW_MONTHS:
        return None
    try:
        return f"{int(parts[1]):04d}-{_HEBREW_MONTHS[parts[0]]:02d}"
    except ValueError:
        return None


def _month_key(short_date: str) -> str | None:
    match = re.fullmatch(r"(\d{2})/(\d{2})", str(short_date or "").strip())
    if not match:
        return None
    month, year = match.groups()
    return f"20{year}-{month}"


def _after(strings: list[str], label: str, default=None):
    try:
        return strings[strings.index(label) + 1]
    except (ValueError, IndexError):
        return default


def parse_track_page(html_text: str, track_id: str) -> dict:
    """Parse the server-rendered public page without relying on CSS classes."""
    track_id = str(track_id)
    metadata = TRACKS.get(track_id)
    if not metadata:
        raise ValueError("מסלול החיסכון אינו נתמך.")
    strings = list(BeautifulSoup(html_text, "html.parser").stripped_strings)
    if not strings:
        raise ValueError("עמוד נתוני הקופה ריק.")

    report_label = _after(strings, "תקופת דיווח:")
    report_period = _report_period(report_label)
    return_12m = _percent(_after(strings, "תשואה 12 חודשים"))
    management_fee = _percent(_after(strings, "דמי ניהול ממוצעים"))

    monthly_returns = {}
    monthly_heading = next(
        (index for index, value in enumerate(strings) if value.startswith("תשואה חודשית")),
        None,
    )
    if monthly_heading is not None:
        end = next(
            (index for index in range(monthly_heading + 1, len(strings)) if strings[index] == "פרטי הקרן"),
            len(strings),
        )
        for index in range(monthly_heading + 1, end):
            key = _month_key(strings[index])
            if key and index > monthly_heading:
                value = _percent(strings[index - 1])
                if value is not None:
                    monthly_returns[key] = value

    latest_period = max(monthly_returns, default=report_period)
    monthly_return = monthly_returns.get(latest_period) if latest_period else None
    return {
        "track_id": track_id,
        **metadata,
        "report_period": latest_period or report_period,
        "report_period_label": report_label,
        "monthly_return_pct": monthly_return,
        "return_12m_pct": return_12m,
        "management_fee_pct": management_fee,
        "monthly_returns": monthly_returns,
        "source": "נתוני רשות שוק ההון באמצעות MoreGemelNet",
        "source_url": metadata["data_url"],
        "fetched_at": date.today().isoformat(),
    }


def get_track_data(track_id: str, force: bool = False) -> dict:
    track_id = str(track_id).strip()
    if track_id not in TRACKS:
        raise ValueError("מסלול החיסכון אינו נתמך.")
    now = time.time()
    with _cache_lock:
        cached = _cache.get(track_id)
    if cached and not force and now - cached[1] < _CACHE_TTL_SECONDS:
        return dict(cached[0])
    response = requests.get(
        TRACKS[track_id]["data_url"],
        timeout=25,
        headers={"User-Agent": "FinPilot/1.0 (+personal-finance-dashboard)"},
    )
    response.raise_for_status()
    result = parse_track_page(response.text, track_id)
    with _cache_lock:
        _cache[track_id] = (dict(result), time.time())
    return result


def value_financial_asset(asset: dict, track_data: dict | None = None) -> dict:
    """Return a display-ready asset while preserving reported vs estimated values."""
    track_id = str(asset.get("track_id") or "").strip()
    metadata = TRACKS.get(track_id, {})
    track_data = track_data or {}
    reported_balance = max(float(asset.get("reported_balance", 0) or 0), 0.0)
    monthly_contribution = max(float(asset.get("monthly_contribution", 0) or 0), 0.0)
    balance_as_of = str(asset.get("balance_as_of") or date.today().isoformat())[:10]
    baseline_month = balance_as_of[:7]
    monthly_returns = track_data.get("monthly_returns") or {}

    estimated_balance = reported_balance
    applied_periods = []
    if asset.get("auto_update", True):
        for period in sorted(monthly_returns):
            # The supplied balance is authoritative for its calendar month.
            # Start only with the next full published month to avoid applying
            # a full-month return to a mid-month personal balance.
            if period <= baseline_month:
                continue
            estimated_balance *= 1 + float(monthly_returns[period]) / 100
            estimated_balance += monthly_contribution
            applied_periods.append(period)

    contributed_raw = asset.get("total_contributed")
    total_contributed = float(contributed_raw or 0)
    contributed_to_estimate = total_contributed + monthly_contribution * len(applied_periods)
    estimated_gain_loss = (
        estimated_balance - contributed_to_estimate if total_contributed > 0 else None
    )
    estimated_gain_loss_pct = (
        estimated_gain_loss / contributed_to_estimate * 100
        if estimated_gain_loss is not None and contributed_to_estimate > 0 else None
    )
    earliest = min(monthly_returns, default=None)
    history_complete = not earliest or baseline_month >= earliest
    return {
        **asset,
        "track_id": track_id,
        "name": asset.get("name") or metadata.get("name") or track_id,
        "provider": asset.get("provider") or metadata.get("provider"),
        "asset_type": asset.get("asset_type") or metadata.get("asset_type", "gemel_investment"),
        "reported_balance": reported_balance,
        "balance_as_of": balance_as_of,
        "monthly_contribution": monthly_contribution,
        "total_contributed": total_contributed,
        "estimated_balance": round(estimated_balance, 2),
        "estimated_gain_loss": round(estimated_gain_loss, 2) if estimated_gain_loss is not None else None,
        "estimated_gain_loss_pct": round(estimated_gain_loss_pct, 2) if estimated_gain_loss_pct is not None else None,
        "estimate_period_label": f"מאז היתרה שדווחה ב־{balance_as_of}",
        "applied_report_periods": applied_periods,
        "history_complete": history_complete,
        "latest_report_period": track_data.get("report_period") or asset.get("latest_report_period"),
        "monthly_return_pct": track_data.get("monthly_return_pct", asset.get("monthly_return_pct")),
        "return_12m_pct": track_data.get("return_12m_pct", asset.get("return_12m_pct")),
        "management_fee_pct": track_data.get("management_fee_pct", asset.get("management_fee_pct")),
        "data_source": track_data.get("source") or asset.get("data_source"),
        "source_url": track_data.get("source_url") or asset.get("source_url") or metadata.get("data_url"),
        "official_url": metadata.get("official_url"),
    }
