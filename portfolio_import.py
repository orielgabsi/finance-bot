import base64
import io
import json
import os
import re

import pandas as pd
from groq import Groq

# qwen/qwen3.6-27b is currently the only vision-capable model on Groq
# (meta-llama/llama-4-scout-17b-16e-instruct, used previously, was retired
# 2026-07-17). Check console.groq.com/docs/vision if this ever stops working.
VISION_MODEL = "qwen/qwen3.6-27b"
EXCEL_TEXT_MODEL = os.environ.get("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")

# Recognized column headers, Hebrew and English. Excel parsing only proceeds
# if a header row matching these is found — we never guess column order
# positionally, since a wrong guess would silently import wrong numbers into
# someone's real portfolio.
TICKER_HEADERS = ("מספר נייר", "טיקר", "סימבול", "ticker", "symbol", "נייר")
QUANTITY_HEADERS = ("כמות", "יחידות", "פוזיציה", "quantity", "qty", "units", "position")
# Prefer an explicit *per-unit average purchase price*. A plain "עלות" column
# in Israeli brokerage exports is often the total position cost and must not
# be used as the unit price, or the calculated holding becomes vastly inflated.
PRICE_HEADERS = (
    "מחיר קנייה ממוצע", "מחיר קניה ממוצע", "שער עלות", "מחיר עלות",
    "מחיר קנייה", "מחיר קניה", "avg cost", "average cost", "buy price", "price", "מחיר",
)
TOTAL_COST_HEADERS = ("עלות כוללת", "סך עלות", "שווי עלות", "בסיס עלות", "total cost", "cost basis", "עלות", "cost")
# Optional — many Israeli brokerage exports use a numeric "מספר נייר" (security
# number) as the ticker, which is meaningless to a human, alongside a separate
# readable "שם נייר" (security name) column. Captured when present so displays
# can show the name instead of the raw number; not required for import to work.
NAME_HEADERS = ("שם נייר", "תיאור", "תאור", "name", "שם")
AGOROT_SECURITY_CODES = {"1215771", "1144401", "5112628", "5141189"}


def _normalized_header(value) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"[\s_.\-/]+", " ", str(value).strip().casefold())


def _header_column(headers, aliases):
    normalized_aliases = {_normalized_header(alias) for alias in aliases}
    return next((index for index, header in enumerate(headers) if header in normalized_aliases), None)


def _detect_header_mapping(df: pd.DataFrame) -> dict | None:
    """Find the semantic header row even when account metadata appears above it."""
    for row_index in range(min(len(df), 80)):
        headers = [_normalized_header(value) for value in df.iloc[row_index].tolist()]
        ticker_col = _header_column(headers, TICKER_HEADERS)
        quantity_col = _header_column(headers, QUANTITY_HEADERS)
        price_col = _header_column(headers, PRICE_HEADERS)
        total_cost_col = _header_column(headers, TOTAL_COST_HEADERS)
        if ticker_col is None or quantity_col is None or (price_col is None and total_cost_col is None):
            continue
        return {
            "header_row": row_index,
            "ticker_col": ticker_col,
            "quantity_col": quantity_col,
            "price_col": price_col,
            "total_cost_col": total_cost_col,
            "name_col": _header_column(headers, NAME_HEADERS),
        }
    return None


def _holdings_from_indices(df: pd.DataFrame, mapping: dict) -> list[dict]:
    holdings = []
    start = int(mapping["header_row"]) + 1
    for row_index in range(start, len(df)):
        row = df.iloc[row_index]
        ticker = _clean_ticker(row.iloc[int(mapping["ticker_col"])])
        quantity = _parse_number(row.iloc[int(mapping["quantity_col"])])
        price_col = mapping.get("price_col")
        total_cost_col = mapping.get("total_cost_col")
        price = _parse_number(row.iloc[int(price_col)]) if price_col is not None else None
        reported_total_cost = _parse_number(row.iloc[int(total_cost_col)]) if total_cost_col is not None else None
        derived_from_total_cost = False
        if price is None and total_cost_col is not None and quantity and quantity > 0:
            price = reported_total_cost / quantity if reported_total_cost is not None else None
            derived_from_total_cost = price is not None
        if derived_from_total_cost and ticker in AGOROT_SECURITY_CODES:
            price *= 100
        if not ticker or ticker == "NAN" or quantity is None or price is None or quantity <= 0 or price <= 0:
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": price}
        if reported_total_cost is not None and reported_total_cost > 0:
            holding["reported_total_cost"] = reported_total_cost
        name_col = mapping.get("name_col")
        if name_col is not None:
            name = str(row.iloc[int(name_col)]).strip()
            if name and name.casefold() != "nan":
                holding["name"] = name
        holdings.append(holding)
    return holdings


def _clean_ticker(value) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, (int, float)) and float(value).is_integer():
        return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text.upper()


def _parse_number(value) -> float | None:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("₪", "").replace("$", "")
    text = text.replace("−", "-").replace("–", "-")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _holdings_from_columns(
    df, ticker_col, quantity_col, price_col=None, name_col=None, total_cost_col=None,
) -> list[dict]:
    holdings = []
    for _, row in df.iterrows():
        ticker = _clean_ticker(row[ticker_col])
        quantity = _parse_number(row[quantity_col])
        price = _parse_number(row[price_col]) if price_col is not None else None
        reported_total_cost = _parse_number(row[total_cost_col]) if total_cost_col is not None else None
        derived_from_total_cost = False
        if price is None and total_cost_col is not None and quantity and quantity > 0:
            price = reported_total_cost / quantity if reported_total_cost is not None else None
            derived_from_total_cost = price is not None
        if derived_from_total_cost and ticker in AGOROT_SECURITY_CODES:
            price *= 100
        if not ticker or ticker == "NAN" or quantity is None or price is None or quantity <= 0 or price <= 0:
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": price}
        if reported_total_cost is not None and reported_total_cost > 0:
            holding["reported_total_cost"] = reported_total_cost
        if name_col is not None:
            name = str(row[name_col]).strip()
            if name and name.lower() != "nan":
                holding["name"] = name
        holdings.append(holding)
    return holdings


def parse_excel_holdings(file_bytes: bytes) -> list[dict]:
    """Reads an .xlsx file and returns [{'ticker', 'quantity', 'buy_price', 'name'?}, ...].
    Tries a recognizable header row first (see *_HEADERS above — never guesses
    column order positionally on this path, since a wrong guess would silently
    import wrong numbers). If no such header row is found, falls back to
    parse_excel_holdings_ai to let the model interpret the sheet — the caller
    must still show the user a preview and get explicit confirmation before
    writing anything, exactly like the AI-vision image path already requires."""
    sheets = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl", header=None, sheet_name=None)
    first_df = None
    for df in sheets.values():
        if first_df is None:
            first_df = df
        mapping = _detect_header_mapping(df)
        if mapping:
            holdings = _holdings_from_indices(df, mapping)
            if holdings:
                return holdings
    return parse_excel_holdings_ai(file_bytes, first_df)


def parse_excel_holdings_ai(file_bytes: bytes, df: pd.DataFrame | None = None) -> list[dict]:
    """Fallback for spreadsheets whose columns don't match TICKER_HEADERS/
    QUANTITY_HEADERS/PRICE_HEADERS (extra columns, differently-named columns,
    a different language, etc.) — hands the raw sheet to a Groq text model and
    asks it to figure out which columns are which. Less reliable than the
    exact-header-match path since the model can misjudge a column's meaning
    (e.g. "current price" vs "buy price") — the caller must show a preview and
    get explicit confirmation before writing anything, same as the image path."""
    if df is None:
        df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl", header=None)

    if len(df.columns) > 80:
        raise ValueError("הקובץ מכיל יותר מדי עמודות. השאר רק את עמודות התיק ונסה שוב.")

    # Only ask the model to map the header row and column positions. Parsing all
    # holdings remains local, so extra columns and large files do not inflate
    # the prompt or allow the model to invent portfolio values.
    row_profile = []
    for row_index in range(min(len(df), 80)):
        values = []
        for col_index, value in enumerate(df.iloc[row_index].tolist()[:40]):
            if pd.isna(value) or str(value).strip() == "":
                continue
            values.append({"col": col_index, "value": str(value)[:80]})
        if values:
            row_profile.append({"row": row_index, "cells": values})
        # Keep the complete request comfortably below the provider's token and
        # request-size limits even when the sheet contains many noisy columns.
        if len(json.dumps(row_profile, ensure_ascii=False)) > 12000:
            row_profile.pop()
            break
    profile_json = json.dumps(row_profile, ensure_ascii=False)
    prompt = f"""Identify the header row and columns in an investment-portfolio spreadsheet.
Return JSON only:
{{"header_row":0,"ticker_col":1,"quantity_col":3,"price_col":6,"total_cost_col":null,"name_col":0}}
All positions are zero-based integers. price_col must be the average purchase/cost price PER UNIT, never current price or total value. Map total_cost_col to the historical TOTAL COST BASIS when it exists, even when price_col also exists; this preserves the broker's exact reported position cost. If only total cost exists, set price_col to null. Ignore unrelated recommendation, rating, current-value, return and note columns. Do not extract holdings; only map positions.
INDEXED NON-EMPTY CELLS:
{profile_json}"""

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    try:
        response = client.chat.completions.create(
            model=EXCEL_TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_completion_tokens=900,
            reasoning_effort="low",
            response_format={"type": "json_object"},
        )
    except Exception as e:
        if "413" in str(e) or "Request too large" in str(e):
            raise ValueError("הקובץ רחב מדי לניתוח. השאר רק עמודות נייר, שם, כמות ומחיר קנייה.")
        raise

    text = (response.choices[0].message.content or "").strip()
    if "<think>" in text:
        think_end = text.find("</think>")
        text = text[think_end + len("</think>"):].strip() if think_end != -1 else ""
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"לא הצלחתי לזהות את עמודות הקובץ: {e}")

    header_row = mapping.get("header_row", 0)
    header_values = [_normalized_header(value) for value in df.iloc[int(header_row)].tolist()]

    def resolve_position(new_key, old_key, required=True):
        proposed = mapping.get(new_key, mapping.get(old_key))
        if proposed is None and not required:
            return None
        if isinstance(proposed, int) or str(proposed).isdigit():
            position = int(proposed)
        else:
            normalized = _normalized_header(proposed)
            position = next((index for index, value in enumerate(header_values) if value == normalized), -1)
        if position < 0 or position >= len(df.columns):
            if required:
                raise ValueError(f"ה-AI לא זיהה עמודה תקינה עבור {new_key}.")
            return None
        return position

    resolved = {
        "header_row": int(header_row),
        "ticker_col": resolve_position("ticker_col", "ticker_column"),
        "quantity_col": resolve_position("quantity_col", "quantity_column"),
        "price_col": resolve_position("price_col", "price_column", required=False),
        "total_cost_col": resolve_position("total_cost_col", "total_cost_column", required=False),
        "name_col": resolve_position("name_col", "name_column", required=False),
    }
    if resolved["price_col"] is None and resolved["total_cost_col"] is None:
        raise ValueError("ה-AI לא זיהה מחיר בסיס ליחידה או עלות כוללת.")
    holdings = _holdings_from_indices(df, resolved)

    if not holdings:
        raise ValueError("לא זיהיתי עמודות מתאימות בקובץ, גם בעזרת AI. ודא שיש עמודות טיקר/כמות/מחיר.")
    return holdings


def parse_rows_holdings_ai(rows) -> list[dict]:
    """Map unfamiliar browser-extracted Excel columns with AI, then parse locally."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 250:
        raise ValueError("מספר השורות שנשלח לזיהוי אינו תקין.")
    clean_rows = []
    for row in rows:
        if not isinstance(row, list) or len(row) > 60:
            raise ValueError("מבנה הגיליון רחב מדי לזיהוי.")
        clean_rows.append([
            str(value)[:200] if value is not None else ""
            for value in row
        ])
    return parse_excel_holdings_ai(b"", pd.DataFrame(clean_rows))


def parse_pasted_holdings_local(text: str) -> list[dict]:
    """Parse brokerage table text copied from Telegram/HTML without AI.

    Altshuler's copied table repeats the currency on separate lines and puts
    average purchase price after position, last price, daily change, total
    cost and market value. Validate both quantity*last~=market value and
    quantity*average~=cost basis so a daily P&L number cannot be mistaken for
    the purchase price.
    """
    cleaned = str(text or "")
    cleaned = cleaned.replace("\u200e", "").replace("\u200f", "").replace("\u2066", "").replace("\u2067", "").replace("\u2069", "")
    cleaned = cleaned.replace("\xa0", " ")
    ticker_pattern = re.compile(
        r"(?mi)^\s*(?:\*\*)?([A-Z][A-Z0-9.^=_-]{0,14})(?:\*\*)?\s*$"
    )
    matches = [
        match for match in ticker_pattern.finditer(cleaned)
        if match.group(1).upper() not in {"USD", "ILS", "EUR", "P&L", "AI"}
    ]
    holdings = []
    number_pattern = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?%?")
    for index, match in enumerate(matches):
        ticker = match.group(1).upper()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        block = cleaned[match.end():block_end]
        tokens = number_pattern.findall(block)
        percent_index = next((i for i, token in enumerate(tokens) if token.endswith("%")), None)
        if percent_index is None or percent_index < 2 or len(tokens) <= percent_index + 3:
            continue

        def value(token):
            return float(token.replace(",", "").replace("%", ""))

        try:
            quantity = value(tokens[percent_index - 2])
            last_price = value(tokens[percent_index - 1])
            total_cost = value(tokens[percent_index + 1])
            market_value = value(tokens[percent_index + 2])
            average_price = value(tokens[percent_index + 3])
        except (ValueError, IndexError):
            continue
        if quantity <= 0 or average_price <= 0:
            continue
        cost_tolerance = max(0.08, abs(total_cost) * 0.035)
        value_tolerance = max(0.08, abs(market_value) * 0.035)
        if abs(quantity * average_price - total_cost) > cost_tolerance:
            continue
        if abs(quantity * last_price - market_value) > value_tolerance:
            continue
        name_lines = []
        for raw_line in block.splitlines():
            line = raw_line.replace("**", "").strip()
            if "%" in line:
                break
            if not line or line.upper() in {"USD", "ILS", "EUR"}:
                continue
            name_lines.append(line)
        currency_match = re.search(r"(?mi)^\s*(USD|ILS|EUR)\s*$", block)
        holding = {
            "ticker": ticker,
            "quantity": quantity,
            "buy_price": average_price,
            "reported_total_cost": total_cost,
            "currency": currency_match.group(1).upper() if currency_match else "",
        }
        if name_lines:
            holding["name"] = name_lines[0][:120]
        holdings.append(holding)
    return holdings


def parse_pasted_holdings_ai(text: str) -> list[dict]:
    """Use AI only when deterministic parsing cannot understand pasted text."""
    source = str(text or "").strip()
    if not source or len(source) > 20000:
        raise ValueError("הטקסט ריק או ארוך מדי לזיהוי.")
    prompt = f"""Extract purchases/positions from brokerage text. Return JSON only as:
{{"holdings":[{{"ticker":"AAPL","name":"Apple Inc","quantity":0.1,"buy_price":234.87,"reported_total_cost":23.49,"currency":"USD"}}]}}
Use the position/quantity and the AVERAGE PURCHASE PRICE per unit. Never use last/current price, total cost basis, market value, daily P&L or unrealized P&L as buy_price. When a historical total cost basis is shown, copy it separately to reported_total_cost so rounding in quantity × average price does not change the exact reported cost. Do not invent missing values. If multiple securities appear, return all of them. If this is a natural sentence such as 'I bought 2 AAPL at 150', extract it too.
TEXT:
{source[:16000]}"""
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    response = client.chat.completions.create(
        model=EXCEL_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_completion_tokens=1200,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )
    text_result = (response.choices[0].message.content or "").strip()
    if "<think>" in text_result:
        end = text_result.find("</think>")
        text_result = text_result[end + len("</think>"):].strip() if end != -1 else ""
    if text_result.startswith("```"):
        text_result = text_result.strip("`")
        if text_result.startswith("json"):
            text_result = text_result[4:]
    parsed = json.loads(text_result.strip())
    rows = parsed.get("holdings") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError("ה-AI לא החזיר רשימת החזקות תקינה.")
    holdings = []
    for item in rows[:50]:
        try:
            ticker = str(item.get("ticker") or "").strip().upper()
            quantity = float(item.get("quantity"))
            buy_price = float(item.get("buy_price"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not re.fullmatch(r"[A-Z0-9.=_^-]{1,30}", ticker) or quantity <= 0 or buy_price <= 0:
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": buy_price}
        try:
            reported_total_cost = float(item.get("reported_total_cost"))
        except (TypeError, ValueError):
            reported_total_cost = None
        if reported_total_cost is not None and reported_total_cost > 0:
            holding["reported_total_cost"] = reported_total_cost
        name = str(item.get("name") or "").strip()
        currency = str(item.get("currency") or "").strip().upper()
        if name:
            holding["name"] = name[:120]
        if currency in {"ILS", "USD", "EUR"}:
            holding["currency"] = currency
        holdings.append(holding)
    if not holdings:
        raise ValueError("לא זוהו בטקסט טיקר, כמות ומחיר קנייה ממוצע.")
    return holdings


def parse_pasted_holdings(text: str, use_ai: bool = True) -> list[dict]:
    holdings = parse_pasted_holdings_local(text)
    if holdings or not use_ai:
        return holdings
    return parse_pasted_holdings_ai(text)


def parse_image_holdings(image_bytes: bytes) -> list[dict]:
    """Sends a screenshot to a Groq vision model and asks it to extract
    portfolio holdings as structured JSON. Less reliable than the Excel path
    (OCR/vision can misread numbers) — the caller must show the user a preview
    and get explicit confirmation before writing anything to Firestore."""
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """This image shows a stock/investment portfolio (possibly in Hebrew,
possibly from an Israeli brokerage). Extract every holding you can clearly read.

Respond with ONLY a JSON array, no other text, in this exact shape:
[{"ticker": "AAPL", "quantity": 10, "buy_price": 150.5, "reported_total_cost": 1505.0, "name": "Apple Inc."}, ...]

- "ticker" should be the stock symbol or security number exactly as shown.
- "quantity" is the number of units/shares held.
- "buy_price" is the average cost/purchase price per unit (not the current market price,
  and not the total value) — use your best judgment based on the column labels in the image.
- "reported_total_cost" is optional: copy the historical total cost basis exactly when it
  is clearly shown, so rounded quantity and average-price fields do not alter the cost basis.
- "name" is optional: include it only if a separate readable security name is shown
  alongside a ticker/security-number (common on Israeli brokerage screens). Omit if none.
- If you cannot confidently read a row, skip it rather than guessing.
- If you find nothing readable, respond with []."""

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
            ],
        }],
        temperature=0.1,
        # qwen3.6-27b writes its reasoning as visible <think>...</think> text
        # directly in the content field (unlike gpt-oss, which buckets it
        # separately) — the budget below has to cover the reasoning AND the
        # JSON both, not just the JSON.
        max_completion_tokens=3000,
    )

    text = (response.choices[0].message.content or "").strip()

    # Strip the model's visible chain-of-thought before parsing.
    if "<think>" in text:
        think_end = text.find("</think>")
        text = text[think_end + len("</think>"):].strip() if think_end != -1 else ""

    # Models sometimes wrap JSON in ```json fences despite instructions not to.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    if not text:
        raise ValueError("המודל לא החזיר תשובה — נסה שוב או שלח תמונה ברורה יותר.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"לא הצלחתי לפענח את הנתונים מהתמונה: {e}")

    holdings = []
    for item in parsed:
        try:
            ticker = str(item["ticker"]).strip().upper()
            quantity = float(item["quantity"])
            price = float(item["buy_price"])
        except (KeyError, ValueError, TypeError):
            continue
        if not ticker or quantity <= 0 or price <= 0:
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": price}
        try:
            reported_total_cost = float(item.get("reported_total_cost"))
        except (TypeError, ValueError):
            reported_total_cost = None
        if reported_total_cost is not None and reported_total_cost > 0:
            holding["reported_total_cost"] = reported_total_cost
        name = str(item.get("name") or "").strip()
        if name:
            holding["name"] = name
        holdings.append(holding)
    return holdings
