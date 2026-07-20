import base64
import io
import json
import os

import pandas as pd
from groq import Groq

# qwen/qwen3.6-27b is currently the only vision-capable model on Groq
# (meta-llama/llama-4-scout-17b-16e-instruct, used previously, was retired
# 2026-07-17). Check console.groq.com/docs/vision if this ever stops working.
VISION_MODEL = "qwen/qwen3.6-27b"

# Recognized column headers, Hebrew and English. Excel parsing only proceeds
# if a header row matching these is found — we never guess column order
# positionally, since a wrong guess would silently import wrong numbers into
# someone's real portfolio.
TICKER_HEADERS = {"ticker", "symbol", "טיקר", "סימבול", "מספר נייר", "נייר"}
QUANTITY_HEADERS = {"quantity", "qty", "units", "כמות", "יחידות"}
PRICE_HEADERS = {"price", "buy price", "cost", "avg cost", "מחיר", "מחיר קנייה", "עלות", "שער עלות", "מחיר עלות"}
# Optional — many Israeli brokerage exports use a numeric "מספר נייר" (security
# number) as the ticker, which is meaningless to a human, alongside a separate
# readable "שם נייר" (security name) column. Captured when present so displays
# can show the name instead of the raw number; not required for import to work.
NAME_HEADERS = {"name", "שם נייר", "שם", "תיאור", "תאור"}


def parse_excel_holdings(file_bytes: bytes) -> list[dict]:
    """Reads an .xlsx file and returns [{'ticker', 'quantity', 'buy_price', 'name'?}, ...].
    Tries a recognizable header row first (see *_HEADERS above — never guesses
    column order positionally on this path, since a wrong guess would silently
    import wrong numbers). If no such header row is found, falls back to
    parse_excel_holdings_ai to let the model interpret the sheet — the caller
    must still show the user a preview and get explicit confirmation before
    writing anything, exactly like the AI-vision image path already requires."""
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    columns_lower = {str(c).strip().lower(): c for c in df.columns}

    ticker_col = next((columns_lower[h] for h in TICKER_HEADERS if h in columns_lower), None)
    quantity_col = next((columns_lower[h] for h in QUANTITY_HEADERS if h in columns_lower), None)
    price_col = next((columns_lower[h] for h in PRICE_HEADERS if h in columns_lower), None)
    name_col = next((columns_lower[h] for h in NAME_HEADERS if h in columns_lower), None)

    if not (ticker_col and quantity_col and price_col):
        return parse_excel_holdings_ai(file_bytes, df)

    holdings = []
    for _, row in df.iterrows():
        ticker = str(row[ticker_col]).strip().upper()
        try:
            quantity = float(row[quantity_col])
            price = float(row[price_col])
        except (ValueError, TypeError):
            continue  # skip rows that don't have real numbers (blank/summary rows)
        if not ticker or ticker == "NAN":
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": price}
        if name_col is not None:
            name = str(row[name_col]).strip()
            if name and name.lower() != "nan":
                holding["name"] = name
        holdings.append(holding)

    if not holdings:
        raise ValueError("לא נמצאו שורות תקינות לייבוא בקובץ.")
    return holdings


def parse_excel_holdings_ai(file_bytes: bytes, df: pd.DataFrame | None = None) -> list[dict]:
    """Fallback for spreadsheets whose columns don't match TICKER_HEADERS/
    QUANTITY_HEADERS/PRICE_HEADERS (extra columns, differently-named columns,
    a different language, etc.) — hands the raw sheet to a Groq text model and
    asks it to figure out which columns are which. Less reliable than the
    exact-header-match path since the model can misjudge a column's meaning
    (e.g. "current price" vs "buy price") — the caller must show a preview and
    get explicit confirmation before writing anything, same as the image path."""
    if df is None:
        df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

    # Cap rows sent to the model — a huge sheet would blow the context budget
    # and isn't a realistic personal-portfolio size anyway.
    sheet_csv = df.head(200).to_csv(index=False)

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    prompt = f"""This is a spreadsheet (as CSV) of a stock/investment portfolio,
possibly in Hebrew, possibly from an Israeli brokerage. The column names may not
match any standard format — use your judgment to figure out which column is the
ticker/symbol, which is the quantity/units held, and which is the buy/cost price
per unit (not current market price, not total value).

CSV:
{sheet_csv}

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"ticker": "AAPL", "quantity": 10, "buy_price": 150.5, "name": "Apple Inc."}}, ...]

- "name" is optional: include it only if there's a separate readable
  name/description column distinct from the ticker/security-number column
  (e.g. an Israeli brokerage sheet with both "מספר נייר" and "שם נייר").
  Omit it entirely if there's no such column.
- Skip header/summary/total rows and any row you can't confidently interpret.
- If you find nothing readable, respond with []."""

    # qwen3.6-27b writes its visible reasoning as <think>...</think> straight
    # into the content field, drawn from the same max_completion_tokens budget
    # as the JSON answer (see parse_image_holdings above). A wide sheet like a
    # real Israeli brokerage export (15 columns, ambiguous Hebrew headers)
    # needs more reasoning than a screenshot does, and 3000 tokens observed in
    # practice can be exhausted entirely on thinking, leaving an empty answer.
    # Retry once with a bigger budget rather than giving up on the first empty
    # response, mirroring ai_recommendation.generate_recommendation's retry.
    text = ""
    for max_tokens in (3000, 7000):
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_completion_tokens=max_tokens,
        )
        text = (response.choices[0].message.content or "").strip()
        if "<think>" in text:
            think_end = text.find("</think>")
            text = text[think_end + len("</think>"):].strip() if think_end != -1 else ""
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        if text:
            break

    if not text:
        raise ValueError("לא הצלחתי לזהות עמודות בקובץ, גם בעזרת AI. נסה קובץ עם כותרות ברורות יותר.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"לא הצלחתי לפענח את הנתונים מהקובץ: {e}")

    holdings = []
    for item in parsed:
        try:
            ticker = str(item["ticker"]).strip().upper()
            quantity = float(item["quantity"])
            price = float(item["buy_price"])
        except (KeyError, ValueError, TypeError):
            continue
        if not ticker:
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": price}
        name = str(item.get("name") or "").strip()
        if name:
            holding["name"] = name
        holdings.append(holding)

    if not holdings:
        raise ValueError("לא זיהיתי עמודות מתאימות בקובץ, גם בעזרת AI. ודא שיש עמודות טיקר/כמות/מחיר.")
    return holdings


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
[{"ticker": "AAPL", "quantity": 10, "buy_price": 150.5, "name": "Apple Inc."}, ...]

- "ticker" should be the stock symbol or security number exactly as shown.
- "quantity" is the number of units/shares held.
- "buy_price" is the average cost/purchase price per unit (not the current market price,
  and not the total value) — use your best judgment based on the column labels in the image.
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
        if not ticker:
            continue
        holding = {"ticker": ticker, "quantity": quantity, "buy_price": price}
        name = str(item.get("name") or "").strip()
        if name:
            holding["name"] = name
        holdings.append(holding)
    return holdings
