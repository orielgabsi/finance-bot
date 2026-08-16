"""Investment thesis lifecycle: create an immutable entry snapshot when the
user approves a BUY, then re-check it over time against fresh data.

A thesis's reasoning/snapshots are frozen at approval time and never
rewritten — check_thesis() always compares fresh facts against that frozen
snapshot, it never edits it. Signal classification is deterministic (based
on the same fundamental/technical scores technical_service/fundamental_service
already compute), not AI-generated, so a thesis re-check never depends on an
LLM call succeeding.
"""

from firebase_admin import firestore

import connect_firebase
import fundamental_service
import technical_service

db = connect_firebase.db

_ALLOWED_POSITION_TYPES = {"Core", "Satellite"}

# Deterministic thresholds for classifying how a thesis has moved since
# entry. Applied to whichever of the fundamental score delta / technical
# score delta / price change is most negative (worst case) or most positive
# (best case) — a single badly-moving input is enough to flag a change, a
# single improving input is enough to flag strength.
_BROKEN_SCORE_DELTA = -15
_BROKEN_PRICE_PCT = -20
_WEAKENED_SCORE_DELTA = -7
_WEAKENED_PRICE_PCT = -12
_STRENGTHENED_SCORE_DELTA = 10


def save_pending_recommendation(user_id: str, recommendation: dict) -> str:
    """Stores a structured BUY recommendation while it waits for the user to
    tap Approve/Reject in Telegram (callback_data can't carry the full JSON,
    only a short id)."""
    doc_ref = db.collection("pending_recommendations").document()
    doc_ref.set({
        "user_id": str(user_id),
        "recommendation": recommendation,
        "status": "pending",
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    return doc_ref.id


def get_pending_recommendation(pending_id: str) -> dict | None:
    doc = db.collection("pending_recommendations").document(pending_id).get()
    return doc.to_dict() if doc.exists else None


def resolve_pending_recommendation(pending_id: str, status: str) -> None:
    db.collection("pending_recommendations").document(pending_id).update({
        "status": status,
        "resolved_at": firestore.SERVER_TIMESTAMP,
    })


def create_thesis(user_id: str, recommendation: dict, portfolio_snapshot: dict | None = None) -> dict:
    """Creates the frozen entry snapshot for an approved BUY. Never call this
    for anything other than an explicit, user-approved BUY — approval alone
    documents intent, it never triggers a real trade."""
    user_id = str(user_id)
    ticker = str(recommendation.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("Cannot create a thesis without a ticker.")
    position_type = recommendation.get("position_type")
    if position_type not in _ALLOWED_POSITION_TYPES:
        position_type = "Core"

    thesis = {
        "user_id": user_id,
        "ticker": ticker,
        "entry_price": recommendation.get("current_price"),
        "entry_date": firestore.SERVER_TIMESTAMP,
        "position_type": position_type,
        "horizon": recommendation.get("horizon"),
        "reasoning": recommendation.get("reasoning"),
        "fundamental_snapshot": recommendation.get("fundamental_analysis"),
        "technical_snapshot": recommendation.get("technical_analysis"),
        "portfolio_snapshot": portfolio_snapshot,
        "risk_profile_snapshot": (portfolio_snapshot or {}).get("risk_profile"),
        "exit_condition": recommendation.get("exit_condition"),
        "initial_score": recommendation.get("score"),
        "initial_confidence": recommendation.get("confidence"),
        "position_size_ils": recommendation.get("position_size_ils"),
        "status": "open",
        "reconciliation_status": "ok",
        "last_signal": None,
        "last_recommendation": None,
        "last_notified_at": None,
        "last_checked_at": None,
    }
    doc_ref = db.collection("theses").document()
    doc_ref.set(thesis)
    return {"id": doc_ref.id, **thesis}


def _classify_signal(score_delta, technical_score_delta, price_change_pct):
    deltas = [d for d in (score_delta, technical_score_delta) if d is not None]
    worst = min(deltas) if deltas else None
    best = max(deltas) if deltas else None

    broken = (worst is not None and worst <= _BROKEN_SCORE_DELTA) or (
        price_change_pct is not None and price_change_pct <= _BROKEN_PRICE_PCT
    )
    if broken:
        return "broken", "EXIT"

    weakened = (worst is not None and worst <= _WEAKENED_SCORE_DELTA) or (
        price_change_pct is not None and price_change_pct <= _WEAKENED_PRICE_PCT
    )
    if weakened:
        return "weakened", "REDUCE"

    strengthened = best is not None and best >= _STRENGTHENED_SCORE_DELTA
    if strengthened:
        return "strengthened", "ADD"

    return "unchanged", "HOLD"


def _build_diff_text(thesis, fresh_score, fresh_technical_score, fresh_price, price_change_pct) -> str:
    """Plain-language explanation of what actually moved, built only from
    real numbers — never a free-text AI summary — so it can never claim a
    change that isn't in the data."""
    lines = []
    entry_price = thesis.get("entry_price")
    if entry_price is not None and fresh_price is not None:
        pct = f"{price_change_pct:+.1f}%" if price_change_pct is not None else "אין נתון"
        lines.append(f"מחיר: {entry_price:.2f} → {fresh_price:.2f} ({pct})")
    initial_score = thesis.get("initial_score")
    if initial_score is not None and fresh_score is not None:
        lines.append(f"ציון פונדמנטלי: {initial_score} → {fresh_score}")
    entry_tech_score = (thesis.get("technical_snapshot") or {}).get("technical_score")
    if entry_tech_score is not None and fresh_technical_score is not None:
        lines.append(f"ציון טכני: {entry_tech_score} → {fresh_technical_score}")
    return "\n".join(lines) if lines else "אין מספיק נתונים חדשים להשוואה."


def check_thesis(thesis_id: str) -> dict:
    """Re-fetches fundamental/technical data for the thesis's ticker and
    compares it against the frozen entry snapshot. Returns the signal, the
    recommended action, and a factual diff — never edits the thesis's
    original reasoning/snapshots."""
    doc_ref = db.collection("theses").document(thesis_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise ValueError(f"Thesis {thesis_id} not found.")
    thesis = doc.to_dict()
    ticker = thesis.get("ticker")

    fresh_fundamental = fundamental_service.analyze_asset(ticker)
    fresh_technical = technical_service.get_technical_analysis(ticker)

    fresh_metrics = fresh_fundamental.get("metrics") or {}
    fresh_price = fresh_metrics.get("current_price") or fresh_metrics.get("latest_close")
    entry_price = thesis.get("entry_price")
    price_change_pct = (
        (fresh_price - entry_price) / entry_price * 100
        if entry_price and fresh_price else None
    )

    fresh_score = fresh_fundamental.get("score")
    initial_score = thesis.get("initial_score")
    score_delta = (fresh_score - initial_score) if (initial_score is not None and fresh_score is not None) else None

    fresh_technical_score = fresh_technical.get("technical_score")
    entry_technical_score = (thesis.get("technical_snapshot") or {}).get("technical_score")
    technical_score_delta = (
        (fresh_technical_score - entry_technical_score)
        if (entry_technical_score is not None and fresh_technical_score is not None) else None
    )

    signal, recommendation = _classify_signal(score_delta, technical_score_delta, price_change_pct)
    what_changed = _build_diff_text(thesis, fresh_score, fresh_technical_score, fresh_price, price_change_pct)

    doc_ref.update({"last_checked_at": firestore.SERVER_TIMESTAMP})

    return {
        "thesis_id": thesis_id,
        "ticker": ticker,
        "signal": signal,
        "recommendation": recommendation,
        "original_thesis": thesis.get("reasoning"),
        "exit_condition": thesis.get("exit_condition"),
        "what_changed": what_changed,
        "current_price": fresh_price,
        "entry_price": entry_price,
        "price_change_pct": price_change_pct,
        "score_delta": score_delta,
        "technical_score_delta": technical_score_delta,
        "fresh_fundamental": fresh_fundamental,
        "fresh_technical": fresh_technical,
    }


def check_all_open_theses(user_id: str) -> list[dict]:
    """Checks every open thesis for a user. A single ticker's data failure
    (e.g. delisted/unavailable) is reported per-thesis, not raised — one bad
    ticker must never abort checking the rest of the user's theses."""
    results = []
    for thesis in connect_firebase.get_open_theses(user_id):
        thesis_id = thesis.get("id")
        try:
            results.append(check_thesis(thesis_id))
        except Exception as exc:
            results.append({
                "thesis_id": thesis_id,
                "ticker": thesis.get("ticker"),
                "error": str(exc),
                "signal": "data_unavailable",
                "recommendation": None,
            })
    return results


def reconcile_theses_with_holdings(user_id: str) -> list[str]:
    """Flags open theses whose ticker no longer appears in the user's actual
    holdings as NEEDS_REVIEW. A missing holding could mean it was sold, or
    just a naming/import mismatch — never auto-close a thesis on this signal
    alone, only flag it for the user to confirm."""
    user_id = str(user_id)
    held_tickers = set(connect_firebase.get_portfolio(user_id).keys())
    flagged = []
    for thesis in connect_firebase.get_open_theses(user_id):
        ticker = str(thesis.get("ticker") or "").upper()
        if ticker and ticker not in held_tickers:
            db.collection("theses").document(thesis["id"]).update({
                "reconciliation_status": "NEEDS_REVIEW",
                "reconciliation_checked_at": firestore.SERVER_TIMESTAMP,
            })
            flagged.append(thesis["id"])
    return flagged


def should_notify(thesis: dict, signal: str) -> bool:
    """Dedup rule: never notify on 'unchanged', and never repeat a
    notification for the same signal the user was already told about."""
    if signal == "unchanged":
        return False
    return thesis.get("last_signal") != signal


def record_notification(thesis_id: str, signal: str, recommendation: str) -> None:
    db.collection("theses").document(thesis_id).update({
        "last_signal": signal,
        "last_recommendation": recommendation,
        "last_notified_at": firestore.SERVER_TIMESTAMP,
    })


def close_thesis(thesis_id: str, reason: str = "") -> None:
    db.collection("theses").document(thesis_id).update({
        "status": "closed",
        "closed_reason": str(reason)[:300],
        "closed_at": firestore.SERVER_TIMESTAMP,
    })


def format_thesis_diff_card(check_result: dict) -> str:
    """Telegram-ready 'what changed' card for a weakened/broken thesis."""
    signal_labels = {
        "unchanged": "ללא שינוי",
        "weakened": "🟠 נחלשה",
        "strengthened": "🟢 התחזקה",
        "broken": "🔴 נשברה",
        "data_unavailable": "⚠️ אין נתונים",
    }
    return "\n".join(part for part in [
        f"📉 {check_result.get('ticker', '')} — עדכון תזה",
        "",
        "התזה המקורית:",
        str(check_result.get("original_thesis") or ""),
        "",
        "מה השתנה:",
        str(check_result.get("what_changed") or ""),
        "",
        f"סיגנל: {signal_labels.get(check_result.get('signal'), check_result.get('signal', ''))}",
        f"המלצה: {check_result.get('recommendation') or ''}",
        "",
        f"תנאי יציאה מקורי: {check_result.get('exit_condition') or ''}",
        "",
        "⚠️ לא בוצעה כל פעולה אוטומטית. כל פעולה בפועל היא ידנית בלבד.",
    ] if part)
