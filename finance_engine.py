"""Fast Globes market-data reader for Israeli securities.

Globes exposes a compact accessible instrument page containing server-rendered
tables. Reading that page directly is substantially faster and more reliable
than driving the portal search UI with a headless browser.
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup


_GLOBES_INSTRUMENT_IDS = {
    "1215771": "545291",  # I.B.I. SAL TA-Insurance
    "1144401": "250159",  # Tachlit SAL NASDAQ 100
    "5112628": "39366",   # I.B.I. MEHAKA TA-125
    "5141189": "596791",  # I.B.I. Defense Industries Israel
}
_GLOBES_SECURITY_NAMES = {
    "1215771": "אי.בי.אי. סל ת״א-ביטוח",
    "1144401": "תכלית סל NASDAQ 100",
    "5112628": "אי.בי.אי. מחקה ת״א 125",
    "5141189": "אי.בי.אי. מניות תעשיות ביטחוניות ישראל",
}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
    )
}


def has_known_instrument(search_term: str) -> bool:
    return str(search_term or "").strip().upper() in _GLOBES_INSTRUMENT_IDS


def resolve_known_code(search_term: str) -> str | None:
    raw = str(search_term or "").strip()
    if raw.upper() in _GLOBES_INSTRUMENT_IDS:
        return raw.upper()
    term = re.sub(r"[\W_]+", "", raw.casefold())
    for code, name in _GLOBES_SECURITY_NAMES.items():
        normalized_name = re.sub(r"[\W_]+", "", name.casefold())
        if term == normalized_name or (len(term) >= 6 and term in normalized_name):
            return code
    return None


def _number(raw) -> float | None:
    text = str(raw or "").replace(",", "").replace("%", "").strip()
    if not text or text in {"---", "--"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _table_values(soup: BeautifulSoup, heading: str) -> dict[str, str]:
    header = next(
        (tag for tag in soup.find_all(["h2", "h3"]) if heading in tag.get_text(" ", strip=True)),
        None,
    )
    table = header.find_next("table") if header else None
    values = {}
    if table is None:
        return values
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            key = cells[0].get_text(" ", strip=True)
            value = cells[1].get_text(" ", strip=True)
            values[key] = value
    return values


def _value_starting_with(values: dict[str, str], label: str) -> str | None:
    """Globes' accessible HTML omits closing td/tr tags.

    BeautifulSoup therefore includes later cells in each key/value text. The
    wanted number is still always the first token of the matching value.
    """
    return next((value for key, value in values.items() if key.startswith(label)), None)


def _resolve_instrument_id(search_term: str) -> str | None:
    term = str(search_term or "").strip().upper()
    known = _GLOBES_INSTRUMENT_IDS.get(term)
    if known:
        return known
    # Generic fallback for future numeric Israeli securities. Search results
    # are server-rendered; select only a row that contains the exact code.
    try:
        response = requests.get(
            "https://www.globes.co.il/finance/shared/searchresults.asp",
            params={
                "Field": 3, "TypeID": 16, "WhatType": 1,
                "strToSearch": term,
            },
            headers=_HEADERS,
            timeout=12,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link.get("href") or ""
            match = re.search(r"instrumentid=(\d+)", href, re.IGNORECASE)
            row = link.find_parent("tr")
            row_text = row.get_text(" ", strip=True) if row else link.get_text(" ", strip=True)
            if match and term in row_text.upper().split():
                return match.group(1)
    except Exception:
        pass
    return None


def finance_engine_globes_data(search_term: str) -> dict | None:
    """Return current price and explicitly labelled period returns."""
    instrument_id = _resolve_instrument_id(search_term)
    if not instrument_id:
        return None
    url = f"https://www.globes.co.il/portal/nagish/instrument.aspx?id=0.{instrument_id}"
    try:
        response = requests.get(url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        trade = _table_values(soup, "נתוני מסחר")
        returns = _table_values(soup, "תשואות")
        page = soup.select_one("#instrument_page")
        title = page.find("h1").get_text(" ", strip=True) if page and page.find("h1") else str(search_term)

        price_raw = _value_starting_with(trade, "שער אחרון")
        daily_raw = _value_starting_with(trade, "שינוי באחוזים")
        price = _number(price_raw)
        if price is None:
            return None
        return {
            "price": price,
            "day_change_pct": _number(daily_raw),
            "week_change_pct": _number(_value_starting_with(returns, "השבוע")),
            "month_change_pct": _number(
                _value_starting_with(returns, "החודש")
                or _value_starting_with(returns, "מתחילת החודש")
            ),
            "year_change_pct": _number(
                _value_starting_with(returns, "השנה")
                or _value_starting_with(returns, "12 חודשים")
            ),
            "name": title,
            "source": "Globes",
            "source_url": url,
            "price_unit": "agorot",
        }
    except Exception:
        return None


def finance_engine_globes(search_term):
    """Backward-compatible four-value wrapper used by older callers."""
    data = finance_engine_globes_data(search_term)
    if not data:
        return None, None, None, None
    return (
        str(data["price"]),
        None if data.get("day_change_pct") is None else f"{data['day_change_pct']}%",
        None if data.get("month_change_pct") is None else f"{data['month_change_pct']}%",
        data.get("name"),
    )


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(finance_engine_globes_data(sys.argv[1] if len(sys.argv) > 1 else "1215771"), ensure_ascii=False, indent=2))
