import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

load_dotenv()

# 1. Firebase Connection Setup
# Locally: a service-account JSON file on disk (path from FIREBASE_SERVICE_ACCOUNT_PATH).
# In CI (GitHub Actions): the whole JSON as one secret env var, so no key file has
# to touch the runner's disk or the repo.
if not firebase_admin._apps:
    raw_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        cred = credentials.Certificate(json.loads(raw_json))
    else:
        cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "serviceAccountKey.json")
        cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)

db = firestore.client()

def check_user_exists(user_id):
    print(f"Checking if user {user_id} exists in Firestore...")
    user_ref = db.collection("users").document(user_id)
    return user_ref.get().exists

# Your Document ID
def create_user_document(user_id):
    if check_user_exists(user_id):
        print(f"User {user_id} already exists. Skipping creation.")
        user_ref = db.collection("users").document(user_id)
        user_ref.set({
            "last_login": firestore.SERVER_TIMESTAMP
        }, merge=True)  # עדכון שדה last_login בלבד

        return  # Indicates that the user already exists
    # 1. יצירת המסמך הראשי של המשתמש (בתוך אוסף users)
    user_ref = db.collection("users").document(user_id)
    print(f"Creating user: {user_id}")
    
    user_ref.set({
        "user_id": user_id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "total_invested": 0,  # סכום התחלתי שהושקע
        "status": "active"
    }, merge=True)

    # 2. יצירת מסמך ראשון בתוך תת-אוסף (Sub-collection) שנקרא transactions
    # זה יוצר את ה-Collection באופן אוטומטי!
    user_ref.collection("transactions").document("initial_setup").set({
        "type": "system",
        "message": "Welcome to your finance bot!",
        "timestamp": firestore.SERVER_TIMESTAMP
    })
    
    return True

def get_user_data(user_id):
    print(f"Fetching user finance data from Firestore for user: {user_id}")
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    #class function to print map and loss from finance update
    return doc.to_dict() if doc.exists else None


def get_portfolio(user_id):
    data = get_user_data(user_id)
    return data.get("portfolio", {}) if data else {}


def add_holding(user_id, ticker, quantity, buy_price, name=None):
    """`name` is an optional human-readable security name (e.g. from an
    Israeli brokerage export's "שם נייר" column, since the ticker there is
    often just an opaque numeric security number). Kept alongside the raw
    ticker so displays can show it; a later buy of the same ticker without a
    name preserves whatever name is already on file rather than clearing it."""
    ticker = ticker.strip().upper()
    quantity = float(quantity)
    buy_price = float(buy_price)

    user_ref = db.collection("users").document(user_id)
    portfolio = get_portfolio(user_id)
    existing = portfolio.get(ticker)

    if existing:
        old_qty = existing.get("quantity", 0)
        old_price = existing.get("buy_price", 0)
        new_qty = old_qty + quantity
        new_avg_price = (old_qty * old_price + quantity * buy_price) / new_qty
        holding = {"quantity": new_qty, "buy_price": new_avg_price}
    else:
        holding = {"quantity": quantity, "buy_price": buy_price}

    resolved_name = name or (existing.get("name") if existing else None)
    if resolved_name:
        holding["name"] = resolved_name

    user_ref.update({
        f"portfolio.{ticker}": holding,
        "total_invested": firestore.Increment(quantity * buy_price),
    })
    return holding


def reduce_holding(user_id, ticker, quantity):
    """Records a sale: subtracts `quantity` from the held amount. Mirrors
    add_holding but the reverse direction. Raises ValueError if the user
    doesn't hold enough to sell (never let a sale go negative). Leaves
    buy_price (average cost basis) untouched — standard weighted-avg-cost
    accounting, only quantity changes on a partial sell. total_invested is
    decremented by the realized *cost* of the sold shares (quantity *
    buy_price), not by sale proceeds, since it tracks money put in, not out."""
    ticker = ticker.strip().upper()
    quantity = float(quantity)

    user_ref = db.collection("users").document(user_id)
    portfolio = get_portfolio(user_id)
    existing = portfolio.get(ticker)

    held_qty = existing.get("quantity", 0) if existing else 0
    if not existing or quantity > held_qty:
        raise ValueError(f"אין מספיק {ticker} בתיק למכירה (יש רק {held_qty}).")

    buy_price = existing.get("buy_price", 0)
    realized_cost = quantity * buy_price
    new_qty = held_qty - quantity

    if new_qty <= 0:
        user_ref.update({
            f"portfolio.{ticker}": firestore.DELETE_FIELD,
            "total_invested": firestore.Increment(-realized_cost),
        })
    else:
        user_ref.update({
            f"portfolio.{ticker}.quantity": new_qty,
            "total_invested": firestore.Increment(-realized_cost),
        })

    return {"quantity_sold": quantity, "buy_price": buy_price, "realized_cost": realized_cost}


def add_transaction(user_id, ticker, quantity, price, tx_type="buy"):
    user_ref = db.collection("users").document(user_id)
    user_ref.collection("transactions").add({
        "ticker": ticker.strip().upper(),
        "quantity": float(quantity),
        "price": float(price),
        "type": tx_type,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })


def set_user_email(user_id, email):
    user_ref = db.collection("users").document(user_id)
    user_ref.set({"email": email.strip()}, merge=True)


def save_valuation_snapshot(user_id, valuation):
    """Caches the last computed portfolio valuation on the user doc, since the
    web dashboard is a static site with no backend of its own — it reads this
    cached snapshot instead of calling yfinance itself. Refreshed whenever the
    Telegram bot computes a valuation (/portfolio, /cake) or the weekly job runs.
    """
    user_ref = db.collection("users").document(user_id)
    user_ref.set({
        "last_valuation": valuation,
        "last_valuation_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)


LINK_CODE_TTL_MINUTES = 10


def create_link_code(user_id):
    """Generates a one-time, short-lived code a user enters on the website to
    prove they control this Telegram account, linking their web login to it."""
    code = secrets.token_urlsafe(6)  # ~48 bits of entropy — infeasible to guess
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    db.collection("link_codes").document(code).set({
        "telegram_id": user_id,
        "expires_at": expires_at,
    })
    return code, LINK_CODE_TTL_MINUTES


def watch_pending_ai_requests(on_request):
    """The website has no backend of its own, so an AI question is relayed
    through Firestore instead of calling Groq/Tavily directly from the
    browser (which would expose the API keys). This sets up a live listener
    across every user's users/{id}/ai_requests subcollection (via a
    collection-group query — Admin SDK, bypasses firestore.rules by design)
    and calls on_request(request_id, telegram_id, question, doc_ref) for each
    new pending one. Runs in a background thread managed by the Firestore
    SDK, so this call itself doesn't block."""
    def _callback(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != "ADDED":
                continue
            doc = change.document
            data = doc.to_dict()
            if data.get("status") != "pending":
                continue
            telegram_id = doc.reference.parent.parent.id
            on_request(doc.id, telegram_id, data.get("question", ""), doc.reference)

    query = db.collection_group("ai_requests").where(filter=FieldFilter("status", "==", "pending"))
    return query.on_snapshot(_callback)


def answer_ai_request(doc_ref, answer_text):
    doc_ref.update({
        "status": "answered",
        "answer": answer_text,
        "answered_at": firestore.SERVER_TIMESTAMP,
    })


def get_all_users_with_email():
    """Used by the weekly recommendation job (runs with the Admin SDK, so this
    bypasses Firestore security rules by design — only trusted server code calls it)."""
    users = []
    for doc in db.collection("users").where(filter=FieldFilter("email", "!=", "")).stream():
        data = doc.to_dict()
        data["user_id"] = doc.id
        users.append(data)
    return users

