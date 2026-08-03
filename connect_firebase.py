import json
import math
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
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

# Web AI jobs can be expensive (live prices, browser fallback, news and two AI
# passes). A bounded worker prevents repeated clicks or listener catch-up after
# a restart from launching many Playwright/Chromium process trees at once.
_ai_request_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-ai")
_portfolio_request_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-portfolio")
_web_signup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-signup")

# Prefix marks a user record as website-only (no linked Telegram chat), so it
# can never collide with a real numeric Telegram user id.
WEB_ONLY_ID_PREFIX = "web_"

DEFAULT_PROFILE = {
    "display_name": "",
    "risk_profile": "balanced",
    "investment_horizon": "medium",
    "investment_goal": "long_term_growth",
    "base_currency": "ILS",
    "default_broker": "",
}


def _positive_number(value, field_name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} חייב להיות מספר.")
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} חייב להיות גדול מאפס.")
    return number


def _portfolio_total_cost(portfolio):
    return sum(
        float(item.get("reported_total_cost") or (
            float(item.get("quantity", 0)) * float(item.get("buy_price", 0))
        ))
        for item in portfolio.values()
        if isinstance(item, dict)
    )

def check_user_exists(user_id):
    print(f"Checking if user {user_id} exists in Firestore...")
    user_ref = db.collection("users").document(user_id)
    return user_ref.get().exists

# Your Document ID
def create_user_document(user_id):
    if check_user_exists(user_id):
        print(f"User {user_id} already exists. Skipping creation.")
        user_ref = db.collection("users").document(user_id)
        existing = user_ref.get().to_dict() or {}
        updates = {"last_login": firestore.SERVER_TIMESTAMP}
        if "cash_balance" not in existing:
            updates["cash_balance"] = 0
        if "profile" not in existing:
            updates["profile"] = DEFAULT_PROFILE
        user_ref.set(updates, merge=True)

        return  # Indicates that the user already exists
    # 1. יצירת המסמך הראשי של המשתמש (בתוך אוסף users)
    user_ref = db.collection("users").document(user_id)
    print(f"Creating user: {user_id}")
    
    user_ref.set({
        "user_id": user_id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "total_invested": 0,  # סכום התחלתי שהושקע
        "cash_balance": 0,
        "profile": DEFAULT_PROFILE,
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


def list_financial_assets(user_id):
    """Return savings/pension-style assets stored separately from tradeable holdings."""
    collection = db.collection("users").document(str(user_id)).collection("financial_assets")
    return {snapshot.id: {"id": snapshot.id, **(snapshot.to_dict() or {})} for snapshot in collection.stream()}


def add_financial_asset(
    user_id,
    track_id,
    reported_balance,
    balance_as_of,
    monthly_contribution=0,
    total_contributed=0,
    auto_update=True,
):
    import savings_service

    track_id = str(track_id or "").strip()
    metadata = savings_service.TRACKS.get(track_id)
    if not metadata:
        raise ValueError("מסלול החיסכון אינו נתמך.")
    reported_balance = _positive_number(reported_balance, "צבירה נוכחית")
    monthly_contribution = float(monthly_contribution or 0)
    total_contributed = float(total_contributed or 0)
    if (not math.isfinite(monthly_contribution) or not math.isfinite(total_contributed)
            or monthly_contribution < 0 or total_contributed < 0):
        raise ValueError("הפקדה חודשית וסך הפקדות לא יכולים להיות שליליים.")
    balance_as_of = str(balance_as_of or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", balance_as_of):
        raise ValueError("תאריך הצבירה חייב להיות בפורמט YYYY-MM-DD.")

    user_ref = db.collection("users").document(str(user_id))
    assets_collection = user_ref.collection("financial_assets")
    existing_matches = list(assets_collection.where(filter=FieldFilter("track_id", "==", track_id)).stream())
    asset_ref = existing_matches[0].reference if existing_matches else assets_collection.document()
    asset = {
        "asset_type": metadata["asset_type"],
        "provider": metadata["provider"],
        "track_id": track_id,
        "name": metadata["name"],
        "reported_balance": reported_balance,
        "balance_as_of": balance_as_of,
        "monthly_contribution": monthly_contribution,
        "total_contributed": total_contributed,
        "auto_update": bool(auto_update),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if not existing_matches:
        asset["created_at"] = firestore.SERVER_TIMESTAMP
    batch = db.batch()
    batch.set(asset_ref, asset, merge=True)
    for duplicate in existing_matches[1:]:
        batch.delete(duplicate.reference)
    transaction_ref = user_ref.collection("transactions").document()
    batch.set(transaction_ref, {
        "type": "financial_asset_update" if existing_matches else "financial_asset_add",
        "name": metadata["name"],
        "track_id": track_id,
        "amount": reported_balance,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })
    batch.commit()
    return {"id": asset_ref.id, **asset}


def update_financial_asset(user_id, asset_id, updates):
    allowed = {
        "reported_balance", "balance_as_of", "monthly_contribution",
        "total_contributed", "auto_update", "name",
    }
    clean = {key: value for key, value in dict(updates or {}).items() if key in allowed}
    if "reported_balance" in clean:
        clean["reported_balance"] = _positive_number(clean["reported_balance"], "צבירה נוכחית")
    for field in ("monthly_contribution", "total_contributed"):
        if field in clean:
            clean[field] = float(clean[field] or 0)
            if not math.isfinite(clean[field]) or clean[field] < 0:
                raise ValueError("סכומי הפקדה לא יכולים להיות שליליים.")
    if "balance_as_of" in clean:
        clean["balance_as_of"] = str(clean["balance_as_of"] or "").strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean["balance_as_of"]):
            raise ValueError("תאריך הצבירה חייב להיות בפורמט YYYY-MM-DD.")
    clean["updated_at"] = firestore.SERVER_TIMESTAMP
    db.collection("users").document(str(user_id)).collection("financial_assets").document(str(asset_id)).update(clean)
    return clean


def delete_financial_asset(user_id, asset_id):
    db.collection("users").document(str(user_id)).collection("financial_assets").document(str(asset_id)).delete()


def refresh_user_financial_assets(user_id, force=False):
    """Fetch latest public monthly returns and persist transparent estimates."""
    import savings_service

    assets = list_financial_assets(user_id)
    if not assets:
        return {}
    track_data = {}
    refreshed = {}
    batch = db.batch()
    for asset_id, asset in assets.items():
        track_id = str(asset.get("track_id") or "")
        try:
            if track_id not in track_data:
                track_data[track_id] = savings_service.get_track_data(track_id, force=force)
            valued = savings_service.value_financial_asset(asset, track_data[track_id])
        except Exception:
            # Preserve the last known estimate if the public source is briefly unavailable.
            valued = savings_service.value_financial_asset(asset, None)
        payload = {
            key: valued.get(key) for key in (
                "estimated_balance", "estimated_gain_loss", "estimate_period_label",
                "estimated_gain_loss_pct",
                "applied_report_periods", "history_complete", "latest_report_period",
                "monthly_return_pct", "return_12m_pct", "management_fee_pct",
                "data_source", "source_url", "official_url",
            )
        }
        payload["last_refreshed_at"] = firestore.SERVER_TIMESTAMP
        ref = db.collection("users").document(str(user_id)).collection("financial_assets").document(asset_id)
        batch.update(ref, payload)
        refreshed[asset_id] = {**valued, "id": asset_id}
    batch.commit()
    user_ref = db.collection("users").document(str(user_id))
    user_ref.set({
        "financial_assets_total": sum(float(item.get("estimated_balance", 0) or 0) for item in refreshed.values()),
        "last_savings_refresh_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return refreshed


def get_user_ids_with_financial_assets():
    user_ids = set()
    for snapshot in db.collection_group("financial_assets").stream():
        user_ids.add(snapshot.reference.parent.parent.id)
    return sorted(user_ids)


def _buy_in_transaction(
    transaction, user_ref, ticker, quantity, buy_price, name,
    tx_type=None, buy_fx_rate=None, reported_total_cost=None, broker=None,
):
    snapshot = user_ref.get(transaction=transaction)
    data = snapshot.to_dict() if snapshot.exists else {}
    portfolio = dict(data.get("portfolio") or {})
    existing = dict(portfolio.get(ticker) or {})

    old_qty = float(existing.get("quantity", 0))
    old_price = float(existing.get("buy_price", 0))
    new_qty = old_qty + quantity
    new_avg_price = ((old_qty * old_price) + (quantity * buy_price)) / new_qty
    holding = {"quantity": new_qty, "buy_price": new_avg_price}
    old_reported_total = existing.get("reported_total_cost")
    if old_reported_total is not None:
        old_reported_total = _positive_number(old_reported_total, "בסיס עלות מדווח")
    if reported_total_cost is not None:
        reported_total_cost = _positive_number(reported_total_cost, "בסיס עלות מדווח")
    if old_reported_total is not None or reported_total_cost is not None:
        existing_cost = old_reported_total if old_reported_total is not None else old_qty * old_price
        added_cost = reported_total_cost if reported_total_cost is not None else quantity * buy_price
        holding["reported_total_cost"] = existing_cost + added_cost
    old_fx = existing.get("buy_fx_rate")
    if buy_fx_rate is not None:
        buy_fx_rate = _positive_number(buy_fx_rate, "שער מט״ח בקנייה")
        if old_qty > 0 and old_fx:
            total_account_cost = old_qty * old_price * float(old_fx) + quantity * buy_price * buy_fx_rate
            holding["buy_fx_rate"] = total_account_cost / (new_qty * new_avg_price)
        else:
            holding["buy_fx_rate"] = buy_fx_rate
    elif old_fx:
        holding["buy_fx_rate"] = float(old_fx)
    resolved_name = name or existing.get("name")
    if resolved_name:
        holding["name"] = str(resolved_name).strip()
    resolved_broker = broker or existing.get("broker")
    if resolved_broker:
        holding["broker"] = str(resolved_broker).strip()[:60]
    portfolio[ticker] = holding

    updates = {
        "portfolio": portfolio,
        "total_invested": _portfolio_total_cost(portfolio),
    }
    if snapshot.exists:
        # update() replaces the top-level portfolio map as one value. A
        # merge=True set would recursively merge map keys, so sold/removed
        # tickers would silently survive — especially when portfolio == {}.
        transaction.update(user_ref, updates)
    else:
        transaction.set(user_ref, {"user_id": user_ref.id, **updates})
    if tx_type:
        tx_ref = user_ref.collection("transactions").document()
        transaction.set(tx_ref, {
            "ticker": ticker,
            "quantity": quantity,
            "price": buy_price,
            "buy_fx_rate": buy_fx_rate,
            "reported_total_cost": reported_total_cost,
            "broker": holding.get("broker"),
            "type": tx_type,
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
    return holding


def set_holding_broker(user_id, ticker, broker):
    """Relabels an existing holding's broker without touching quantity, cost
    basis, or writing a transaction record — unlike record_buy, this isn't a
    purchase."""
    ticker = str(ticker).strip().upper()
    broker = str(broker or "").strip()[:60]
    user_ref = db.collection("users").document(user_id)
    user_ref.update({f"portfolio.{ticker}.broker": broker})


def add_holding(user_id, ticker, quantity, buy_price, name=None, broker=None):
    """`name` is an optional human-readable security name (e.g. from an
    Israeli brokerage export's "שם נייר" column, since the ticker there is
    often just an opaque numeric security number). Kept alongside the raw
    ticker so displays can show it; a later buy of the same ticker without a
    name preserves whatever name is already on file rather than clearing it."""
    ticker = ticker.strip().upper()
    quantity = _positive_number(quantity, "כמות")
    buy_price = _positive_number(buy_price, "מחיר")
    if not ticker:
        raise ValueError("טיקר לא יכול להיות ריק.")

    user_ref = db.collection("users").document(user_id)
    transaction = db.transaction()
    return firestore.transactional(_buy_in_transaction)(
        transaction, user_ref, ticker, quantity, buy_price, name, None, None, None, broker
    )


def record_buy(
    user_id, ticker, quantity, buy_price, name=None, tx_type="buy",
    buy_fx_rate=None, reported_total_cost=None, broker=None,
):
    """Atomically updates a holding and writes its transaction record."""
    ticker = str(ticker).strip().upper()
    quantity = _positive_number(quantity, "כמות")
    buy_price = _positive_number(buy_price, "מחיר")
    if not ticker:
        raise ValueError("טיקר לא יכול להיות ריק.")
    user_ref = db.collection("users").document(user_id)
    transaction = db.transaction()
    return firestore.transactional(_buy_in_transaction)(
        transaction, user_ref, ticker, quantity, buy_price, name, tx_type,
        buy_fx_rate, reported_total_cost, broker,
    )


def _normalized_import_portfolio(holdings: list[dict]):
    """Validate and consolidate an imported brokerage snapshot."""
    portfolio = {}
    for item in holdings:
        ticker = str(item.get("ticker") or "").strip().upper()
        quantity = _positive_number(item.get("quantity"), "כמות")
        buy_price = _positive_number(item.get("buy_price"), "מחיר בסיס")
        reported_total_cost = item.get("reported_total_cost")
        if reported_total_cost is not None:
            reported_total_cost = _positive_number(reported_total_cost, "בסיס עלות מדווח")
        buy_fx_rate = item.get("buy_fx_rate")
        if buy_fx_rate is not None:
            buy_fx_rate = _positive_number(buy_fx_rate, "שער מט״ח בקנייה")
        if not ticker:
            raise ValueError("נמצא נייר ללא טיקר או מספר נייר.")
        existing = portfolio.get(ticker)
        if existing:
            old_qty = existing["quantity"]
            old_price = existing["buy_price"]
            old_reported_total = existing.get("reported_total_cost")
            total_qty = old_qty + quantity
            existing["buy_price"] = (
                old_qty * old_price + quantity * buy_price
            ) / total_qty
            existing["quantity"] = total_qty
            if reported_total_cost is not None or old_reported_total is not None:
                existing["reported_total_cost"] = float(old_reported_total or old_qty * old_price) + float(reported_total_cost or quantity * buy_price)
            if item.get("name") and not existing.get("name"):
                existing["name"] = str(item["name"]).strip()
            if buy_fx_rate is not None:
                existing["buy_fx_rate"] = buy_fx_rate
        else:
            portfolio[ticker] = {"quantity": quantity, "buy_price": buy_price}
            if reported_total_cost is not None:
                portfolio[ticker]["reported_total_cost"] = reported_total_cost
            if item.get("name"):
                portfolio[ticker]["name"] = str(item["name"]).strip()
            if buy_fx_rate is not None:
                portfolio[ticker]["buy_fx_rate"] = buy_fx_rate

    if not portfolio:
        raise ValueError("לא נמצאו החזקות תקינות לייבוא.")
    return portfolio


def preview_portfolio_sync(existing_portfolio: dict, holdings: list[dict]):
    """Compare an imported snapshot with the currently stored portfolio.

    Missing securities are reported as removed (normally sold at the broker),
    while repeated uploads of the same file are reported as unchanged.
    """
    new_portfolio = _normalized_import_portfolio(holdings)
    existing_portfolio = {
        str(ticker).strip().upper(): dict(item or {})
        for ticker, item in (existing_portfolio or {}).items()
    }
    added, updated, removed, unchanged = [], [], [], []
    tolerance = 1e-8

    for ticker, current in new_portfolio.items():
        previous = existing_portfolio.get(ticker)
        label = current.get("name") or (previous or {}).get("name") or ticker
        if previous is None:
            added.append({"ticker": ticker, "name": label, **current})
            continue
        old_quantity = float(previous.get("quantity", 0) or 0)
        new_quantity = float(current.get("quantity", 0) or 0)
        old_price = float(previous.get("buy_price", 0) or 0)
        new_price = float(current.get("buy_price", 0) or 0)
        quantity_changed = abs(old_quantity - new_quantity) > tolerance
        price_changed = abs(old_price - new_price) > tolerance
        name_changed = bool(current.get("name")) and current.get("name") != previous.get("name")
        old_reported_cost = previous.get("reported_total_cost")
        new_reported_cost = current.get("reported_total_cost")
        reported_cost_changed = (
            old_reported_cost is None
        ) != (
            new_reported_cost is None
        ) or (
            old_reported_cost is not None and new_reported_cost is not None
            and abs(float(old_reported_cost) - float(new_reported_cost)) > tolerance
        )
        record = {
            "ticker": ticker,
            "name": label,
            "old_quantity": old_quantity,
            "new_quantity": new_quantity,
            "quantity_delta": new_quantity - old_quantity,
            "old_buy_price": old_price,
            "new_buy_price": new_price,
            "old_reported_total_cost": old_reported_cost,
            "new_reported_total_cost": new_reported_cost,
        }
        if quantity_changed or price_changed or name_changed or reported_cost_changed:
            updated.append(record)
        else:
            unchanged.append(record)

    for ticker, previous in existing_portfolio.items():
        if ticker not in new_portfolio:
            removed.append({
                "ticker": ticker,
                "name": previous.get("name") or ticker,
                "old_quantity": float(previous.get("quantity", 0) or 0),
                "old_buy_price": float(previous.get("buy_price", 0) or 0),
            })

    return {
        "portfolio": new_portfolio,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "counts": {
            "added": len(added),
            "updated": len(updated),
            "removed": len(removed),
            "unchanged": len(unchanged),
            "total": len(new_portfolio),
        },
    }


def sync_portfolio_from_import(user_id, holdings: list[dict]):
    """Synchronize a brokerage snapshot and persist an auditable change log."""
    user_ref = db.collection("users").document(str(user_id))
    snapshot = user_ref.get()
    existing = (snapshot.to_dict() or {}).get("portfolio") if snapshot.exists else {}
    plan = preview_portfolio_sync(existing or {}, holdings)
    portfolio = plan["portfolio"]
    batch = db.batch()
    batch.update(user_ref, {
        "portfolio": portfolio,
        "total_invested": _portfolio_total_cost(portfolio),
        "last_portfolio_sync": {
            **plan["counts"],
            "synced_at": firestore.SERVER_TIMESTAMP,
        },
    })

    for item in plan["added"]:
        tx_ref = user_ref.collection("transactions").document()
        batch.set(tx_ref, {
            "ticker": item["ticker"],
            "name": item.get("name"),
            "quantity": item["quantity"],
            "price": item["buy_price"],
            "type": "sync_added",
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
    for item in plan["updated"]:
        tx_ref = user_ref.collection("transactions").document()
        batch.set(tx_ref, {
            **item,
            "quantity": item["new_quantity"],
            "price": item["new_buy_price"],
            "type": "sync_updated",
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
    for item in plan["removed"]:
        tx_ref = user_ref.collection("transactions").document()
        batch.set(tx_ref, {
            "ticker": item["ticker"],
            "name": item.get("name"),
            "quantity": item["old_quantity"],
            "price": item["old_buy_price"],
            "type": "sync_removed",
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
    summary_ref = user_ref.collection("transactions").document()
    batch.set(summary_ref, {
        "type": "portfolio_sync",
        **plan["counts"],
        "timestamp": firestore.SERVER_TIMESTAMP,
    })
    batch.commit()
    return plan


def upsert_portfolio_holdings(user_id, holdings: list[dict]):
    """Replace only the supplied tickers with a broker snapshot.

    Pasted brokerage tables describe the current state of those positions, not
    additional buys. Existing tickers are therefore replaced instead of added,
    while unrelated holdings from other brokers remain untouched.
    """
    incoming = _normalized_import_portfolio(holdings)
    user_ref = db.collection("users").document(str(user_id))
    transaction = db.transaction()

    @firestore.transactional
    def _upsert(transaction):
        snapshot = user_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        portfolio = dict(data.get("portfolio") or {})
        added = updated = unchanged = 0
        tolerance = 1e-8
        for ticker, replacement in incoming.items():
            previous = dict(portfolio.get(ticker) or {})
            if not previous:
                added += 1
            else:
                same_quantity = abs(float(previous.get("quantity", 0) or 0) - replacement["quantity"]) <= tolerance
                same_price = abs(float(previous.get("buy_price", 0) or 0) - replacement["buy_price"]) <= tolerance
                same_cost = (
                    previous.get("reported_total_cost") is None
                ) == (
                    replacement.get("reported_total_cost") is None
                ) and (
                    previous.get("reported_total_cost") is None
                    or abs(float(previous["reported_total_cost"]) - float(replacement["reported_total_cost"])) <= tolerance
                )
                same_name = (replacement.get("name") or previous.get("name")) == previous.get("name")
                if same_quantity and same_price and same_cost and same_name:
                    unchanged += 1
                else:
                    updated += 1
            if previous.get("buy_fx_rate") and not replacement.get("buy_fx_rate"):
                replacement["buy_fx_rate"] = float(previous["buy_fx_rate"])
            portfolio[ticker] = dict(replacement)

        payload = {
            "portfolio": portfolio,
            "total_invested": _portfolio_total_cost(portfolio),
            "last_partial_portfolio_sync": {
                "added": added,
                "updated": updated,
                "unchanged": unchanged,
                "synced_at": firestore.SERVER_TIMESTAMP,
            },
        }
        if snapshot.exists:
            transaction.update(user_ref, payload)
        else:
            transaction.set(user_ref, {"user_id": user_ref.id, **payload})
        tx_ref = user_ref.collection("transactions").document()
        transaction.set(tx_ref, {
            "type": "partial_portfolio_sync",
            "tickers": sorted(incoming),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
        return {"added": added, "updated": updated, "unchanged": unchanged, "total": len(incoming)}

    return _upsert(transaction)


def replace_portfolio_from_import(user_id, holdings: list[dict]):
    """Backward-compatible wrapper returning only the synchronized portfolio."""
    return sync_portfolio_from_import(user_id, holdings)["portfolio"]


def _sell_in_transaction(transaction, user_ref, ticker, quantity, sell_price=None, write_tx=False):
    snapshot = user_ref.get(transaction=transaction)
    data = snapshot.to_dict() if snapshot.exists else {}
    portfolio = dict(data.get("portfolio") or {})
    existing = dict(portfolio.get(ticker) or {})
    held_qty = float(existing.get("quantity", 0))
    if not existing or quantity > held_qty:
        raise ValueError(f"אין מספיק {ticker} בתיק למכירה (יש רק {held_qty}).")

    buy_price = float(existing.get("buy_price", 0))
    new_qty = held_qty - quantity
    if new_qty <= 1e-12:
        portfolio.pop(ticker, None)
    else:
        existing["quantity"] = new_qty
        if existing.get("reported_total_cost") is not None and held_qty > 0:
            existing["reported_total_cost"] = float(existing["reported_total_cost"]) * new_qty / held_qty
        portfolio[ticker] = existing

    transaction.update(user_ref, {
        "portfolio": portfolio,
        "total_invested": _portfolio_total_cost(portfolio),
    })
    if write_tx:
        tx_ref = user_ref.collection("transactions").document()
        transaction.set(tx_ref, {
            "ticker": ticker,
            "quantity": quantity,
            "price": sell_price,
            "type": "sell",
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
    return {"quantity_sold": quantity, "buy_price": buy_price, "realized_cost": quantity * buy_price}


def reduce_holding(user_id, ticker, quantity):
    """Records a sale: subtracts `quantity` from the held amount. Mirrors
    add_holding but the reverse direction. Raises ValueError if the user
    doesn't hold enough to sell (never let a sale go negative). Leaves
    buy_price (average cost basis) untouched — standard weighted-avg-cost
    accounting, only quantity changes on a partial sell. total_invested is
    decremented by the realized *cost* of the sold shares (quantity *
    buy_price), not by sale proceeds, since it tracks money put in, not out."""
    ticker = ticker.strip().upper()
    quantity = _positive_number(quantity, "כמות")

    user_ref = db.collection("users").document(user_id)
    transaction = db.transaction()
    return firestore.transactional(_sell_in_transaction)(
        transaction, user_ref, ticker, quantity, None, False
    )


def record_sell(user_id, ticker, quantity, sell_price):
    """Atomically reduces a holding and writes the sale transaction."""
    ticker = str(ticker).strip().upper()
    quantity = _positive_number(quantity, "כמות")
    sell_price = _positive_number(sell_price, "מחיר")
    user_ref = db.collection("users").document(user_id)
    transaction = db.transaction()
    return firestore.transactional(_sell_in_transaction)(
        transaction, user_ref, ticker, quantity, sell_price, True
    )


@firestore.transactional
def _update_cost_basis_in_transaction(transaction, user_ref, ticker, new_buy_price):
    snapshot = user_ref.get(transaction=transaction)
    data = snapshot.to_dict() if snapshot.exists else {}
    portfolio = dict(data.get("portfolio") or {})
    existing = dict(portfolio.get(ticker) or {})
    if not existing:
        raise ValueError(f"הנייר {ticker} לא נמצא בתיק.")
    old_price = float(existing.get("buy_price", 0) or 0)
    existing["buy_price"] = new_buy_price
    existing.pop("reported_total_cost", None)
    portfolio[ticker] = existing
    transaction.update(user_ref, {
        "portfolio": portfolio,
        "total_invested": _portfolio_total_cost(portfolio),
    })
    tx_ref = user_ref.collection("transactions").document()
    transaction.set(tx_ref, {
        "ticker": ticker,
        "quantity": float(existing.get("quantity", 0) or 0),
        "price": new_buy_price,
        "old_price": old_price,
        "type": "cost_basis_edit",
        "timestamp": firestore.SERVER_TIMESTAMP,
    })
    return {"old_price": old_price, "new_price": new_buy_price, "quantity": float(existing.get("quantity", 0) or 0)}


def update_holding_buy_price(user_id, ticker, new_buy_price):
    """Atomically edits average cost basis without changing held quantity."""
    ticker = str(ticker).strip().upper()
    new_buy_price = _positive_number(new_buy_price, "מחיר בסיס")
    if not ticker:
        raise ValueError("טיקר לא יכול להיות ריק.")
    user_ref = db.collection("users").document(str(user_id))
    return _update_cost_basis_in_transaction(db.transaction(), user_ref, ticker, new_buy_price)


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


def get_cash_balance(user_id):
    data = get_user_data(user_id) or {}
    return float(data.get("cash_balance", 0) or 0)


def set_cash_balance(user_id, amount):
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError("יתרת המזומן חייבת להיות מספר.")
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("יתרת המזומן לא יכולה להיות שלילית.")
    db.collection("users").document(user_id).set({"cash_balance": amount}, merge=True)
    return amount


def adjust_cash_balance(user_id, delta):
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        raise ValueError("סכום המזומן חייב להיות מספר.")
    if not math.isfinite(delta) or delta == 0:
        raise ValueError("הסכום חייב להיות שונה מאפס.")

    user_ref = db.collection("users").document(user_id)
    transaction = db.transaction()

    @firestore.transactional
    def _adjust(transaction):
        snapshot = user_ref.get(transaction=transaction)
        current = float((snapshot.to_dict() or {}).get("cash_balance", 0) or 0)
        new_balance = current + delta
        if new_balance < 0:
            raise ValueError(f"אין מספיק מזומן פנוי (יתרה נוכחית: {current:.2f}).")
        transaction.set(user_ref, {"cash_balance": new_balance}, merge=True)
        cash_ref = user_ref.collection("cash_transactions").document()
        transaction.set(cash_ref, {
            "amount": delta,
            "balance_after": new_balance,
            "type": "deposit" if delta > 0 else "withdrawal",
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
        return new_balance

    return _adjust(transaction)


def get_user_profile(user_id):
    data = get_user_data(user_id) or {}
    return {**DEFAULT_PROFILE, **(data.get("profile") or {})}


def update_user_profile(user_id, profile):
    allowed = set(DEFAULT_PROFILE)
    clean = {key: value for key, value in profile.items() if key in allowed}
    merged = {**get_user_profile(user_id), **clean}
    merged["display_name"] = str(merged.get("display_name", "")).strip()[:80]
    merged["investment_goal"] = str(merged.get("investment_goal", "")).strip()[:200]
    merged["default_broker"] = str(merged.get("default_broker", "")).strip()[:60]
    if merged["risk_profile"] not in {"conservative", "balanced", "aggressive"}:
        raise ValueError("פרופיל הסיכון אינו תקין.")
    if merged["investment_horizon"] not in {"short", "medium", "long"}:
        raise ValueError("טווח ההשקעה אינו תקין.")
    if merged["base_currency"] not in {"ILS", "USD", "EUR"}:
        raise ValueError("מטבע הבסיס אינו נתמך.")
    db.collection("users").document(user_id).set({"profile": merged}, merge=True)
    return merged


def set_financial_goal(user_id, target_amount, name="היעד הפיננסי שלי"):
    target_amount = _positive_number(target_amount, "סכום היעד")
    name = str(name or "היעד הפיננסי שלי").strip()[:80]
    goal = {
        "name": name,
        "target_amount": target_amount,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    db.collection("users").document(str(user_id)).set({"financial_goal": goal}, merge=True)
    return goal


def get_financial_goal(user_id):
    data = get_user_data(user_id) or {}
    return data.get("financial_goal") or {}


def save_valuation_snapshot(user_id, valuation):
    """Caches the last computed portfolio valuation on the user doc, since the
    web dashboard is a static site with no backend of its own — it reads this
    cached snapshot instead of calling yfinance itself. Refreshed whenever the
    Telegram bot computes a valuation (/portfolio, /cake) or the weekly job runs.
    """
    user_ref = db.collection("users").document(user_id)
    # update() replaces the top-level last_valuation map. set(..., merge=True)
    # recursively merged nested holdings, so symbols removed from the real
    # portfolio (GOOGL, AAPL, etc.) survived forever in the website snapshot.
    user_ref.update({
        "last_valuation": valuation,
        "last_valuation_at": firestore.SERVER_TIMESTAMP,
    })


LINK_CODE_TTL_MINUTES = 10


def create_link_code(user_id):
    """Generates a one-time, short-lived code a user enters on the website to
    prove they control this Telegram account, linking their web login to it."""
    code = secrets.token_urlsafe(6)  # ~48 bits of entropy — infeasible to guess
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    db.collection("link_codes").document(code).set({
        "telegram_id": user_id,
        "expires_at": expires_at,
        "used": False,
    })
    return code, LINK_CODE_TTL_MINUTES


def watch_pending_ai_requests(on_request):
    """The website has no backend of its own, so an AI question is relayed
    through Firestore instead of calling Groq/Tavily directly from the
    browser (which would expose the API keys). This sets up a live listener
    across every user's users/{id}/ai_requests subcollection (via a
    collection-group query — Admin SDK, bypasses firestore.rules by design)
    and calls on_request(request_id, telegram_id, request_data, doc_ref) for each
    new pending one. Runs in a background thread managed by the Firestore
    SDK, so this call itself doesn't block."""
    def _claim_and_run(doc_ref):
        transaction = db.transaction()

        @firestore.transactional
        def _claim(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            if data.get("status") != "pending":
                return None
            transaction.update(doc_ref, {
                "status": "processing",
                "processing_started_at": firestore.SERVER_TIMESTAMP,
            })
            return data

        data = _claim(transaction)
        if data is None:
            return
        telegram_id = doc_ref.parent.parent.id
        on_request(doc_ref.id, telegram_id, data, doc_ref)

    def _callback(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != "ADDED":
                continue
            doc = change.document
            data = doc.to_dict()
            if data.get("status") != "pending":
                continue
            _ai_request_executor.submit(_claim_and_run, doc.reference)

    query = db.collection_group("ai_requests").where(filter=FieldFilter("status", "==", "pending"))
    return query.on_snapshot(_callback)


def answer_ai_request(doc_ref, answer_text, analysis=None):
    payload = {
        "status": "answered",
        "answer": answer_text,
        "answered_at": firestore.SERVER_TIMESTAMP,
    }
    if analysis is not None:
        payload["analysis"] = analysis
    doc_ref.update(payload)


def watch_pending_portfolio_requests(on_request):
    """Claim server-validated buy/import jobs created by the linked website."""
    def _claim_and_run(doc_ref):
        transaction = db.transaction()

        @firestore.transactional
        def _claim(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            if data.get("status") != "pending":
                return None
            transaction.update(doc_ref, {
                "status": "processing",
                "processing_started_at": firestore.SERVER_TIMESTAMP,
            })
            return data

        data = _claim(transaction)
        if data is not None:
            on_request(doc_ref.id, doc_ref.parent.parent.id, data, doc_ref)

    def _callback(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != "ADDED":
                continue
            data = change.document.to_dict() or {}
            if data.get("status") == "pending":
                _portfolio_request_executor.submit(_claim_and_run, change.document.reference)

    query = db.collection_group("portfolio_requests").where(filter=FieldFilter("status", "==", "pending"))
    return query.on_snapshot(_callback)


def answer_portfolio_request(doc_ref, status, message, result=None):
    payload = {
        "status": status,
        "message": str(message)[:1000],
        "answered_at": firestore.SERVER_TIMESTAMP,
    }
    if result is not None:
        payload["result"] = result
    doc_ref.update(payload)


def watch_pending_web_signups(on_request):
    """Lets someone use the full website (portfolio, AI, everything) without
    ever opening Telegram. The browser can't create its own users/{id} or
    account_links/{uid} documents directly (firestore.rules blocks that on
    purpose — those paths are otherwise only ever written by a trusted
    linking handshake or the bot itself), so — same relay pattern as AI/buy
    requests — it drops a request here and this admin-privileged process
    provisions a synthetic "web_<uid>" user record and links it."""
    def _claim_and_run(doc_ref):
        transaction = db.transaction()

        @firestore.transactional
        def _claim(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            if data.get("status") != "pending":
                return None
            transaction.update(doc_ref, {
                "status": "processing",
                "processing_started_at": firestore.SERVER_TIMESTAMP,
            })
            return data

        data = _claim(transaction)
        if data is not None:
            on_request(doc_ref.id, doc_ref.id, data, doc_ref)

    def _callback(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != "ADDED":
                continue
            data = change.document.to_dict() or {}
            if data.get("status") == "pending":
                _web_signup_executor.submit(_claim_and_run, change.document.reference)

    query = db.collection("web_signups").where(filter=FieldFilter("status", "==", "pending"))
    return query.on_snapshot(_callback)


def mark_web_signup_done(doc_ref, telegram_id):
    doc_ref.update({"status": "done", "telegram_id": telegram_id, "completed_at": firestore.SERVER_TIMESTAMP})


def mark_web_signup_failed(doc_ref, error_message):
    doc_ref.update({"status": "failed", "error": str(error_message)[:500]})


def complete_web_signup(uid):
    """Provisions a website-only user and links it to the given Firebase
    uid, returning the new synthetic telegram_id."""
    telegram_id = f"{WEB_ONLY_ID_PREFIX}{uid}"
    create_user_document(telegram_id)
    db.collection("account_links").document(uid).set({
        "telegram_id": telegram_id,
        "used_code": None,
        "linked_at": firestore.SERVER_TIMESTAMP,
    })
    return telegram_id


def save_fundamental_analysis(user_id, analysis):
    user_ref = db.collection("users").document(user_id)
    record = {**analysis, "created_at": firestore.SERVER_TIMESTAMP}
    user_ref.collection("analyses").add(record)
    user_ref.set({
        "last_analysis": analysis,
        "last_analysis_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)


def save_deep_portfolio_analysis(user_id, analysis):
    """Stores one shared thinking-mode result for both Telegram and the web UI."""
    user_ref = db.collection("users").document(user_id)
    record = {
        "type": "deep_portfolio",
        "result": analysis,
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    user_ref.collection("analyses").add(record)
    user_ref.set({
        "last_deep_analysis": analysis,
        "last_deep_analysis_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)


def get_all_users_with_email():
    """Used by the weekly recommendation job (runs with the Admin SDK, so this
    bypasses Firestore security rules by design — only trusted server code calls it)."""
    users = []
    for doc in db.collection("users").where(filter=FieldFilter("email", "!=", "")).stream():
        data = doc.to_dict()
        data["user_id"] = doc.id
        users.append(data)
    return users

