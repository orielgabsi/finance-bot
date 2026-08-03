"""Run monthly (via .github/workflows/monthly_recommendations.yml) to email
every user with a saved address a full-month portfolio review: everything
the weekly email covers PLUS long-term savings/funds AND a deep, per-holding
AI review (the same "thinking mode" pipeline the Telegram bot runs on
demand) — fundamental screening of every holding, concrete actions per
holding, allocation priorities, a cash plan, and next steps. Not run by the
Telegram bot process itself."""

from concurrent.futures import ThreadPoolExecutor, as_completed

import connect_firebase
import portfolio_service
import fundamental_service
import ai_recommendation
import email_service
from email_report import MUTED, bullet_list, build_report_email_html, section_title

SUBJECT = "הסיכום החודשי שלך 🗓️"

VERDICT_LABELS = {
    "strong": "🟢 תיק חזק יחסית",
    "healthy_but_watch": "🟡 תיק בריא, עם נקודות למעקב",
    "needs_changes": "🟠 נדרשים שינויים",
    "high_risk": "🔴 רמת סיכון גבוהה",
}

STANCE_LABELS = {
    "maintain": "שמירה/מעקב רגיל",
    "watch": "מעקב הדוק",
    "research": "מחקר נוסף",
    "consider_reduce": "לשקול הפחתת ריכוזיות",
    "insufficient_data": "אין מספיק נתונים",
}


def _run_deep_analysis(uid: str, valuation: dict, profile: dict | None) -> dict:
    """Screens every holding fundamentally, then runs the two-pass AI audit —
    the same pipeline Finance_bot.py's "thinking mode" runs on demand, reused
    here since a monthly cadence is the right frequency for something this
    thorough (each holding needs its own fundamental data pull)."""
    analyses = []
    failed_symbols = []
    symbols = list(valuation["holdings"])
    worker_count = min(4, len(symbols)) or 1
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        pending = {
            pool.submit(
                fundamental_service.analyze_asset,
                valuation["holdings"][symbol].get("name") or symbol,
            ): symbol for symbol in symbols
        }
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                analyses.append(future.result())
            except Exception as exc:
                print(f"Monthly deep-analysis screening failed for {symbol}: {exc}")
                failed_symbols.append(symbol)

    holdings_for_search = [(symbol, h.get("name")) for symbol, h in valuation["holdings"].items()]
    market_context = ai_recommendation.search_market_context(holdings_for_search)
    result = ai_recommendation.generate_deep_portfolio_recommendation(
        valuation, analyses, market_context, profile
    )
    stored_result = {
        **result,
        "analyzed_count": len(analyses),
        "failed_symbols": failed_symbols,
        "portfolio_symbols": symbols,
    }
    connect_firebase.save_deep_portfolio_analysis(uid, stored_result)
    return stored_result


def _build_deep_analysis_html(result: dict) -> str:
    verdict_line = VERDICT_LABELS.get(result.get("overall_verdict"), result.get("overall_verdict") or "")
    sections = [f'<p style="font-size:13px; color:{MUTED}; margin:0 0 16px;">{verdict_line} · ביטחון {result.get("confidence", 0)}%</p>']

    strengths = result.get("portfolio_strengths") or []
    if strengths:
        sections.append(section_title("✅ נקודות חוזקה") + bullet_list(strengths))

    risks = result.get("portfolio_risks") or []
    if risks:
        sections.append(section_title("⚠️ סיכונים מרכזיים") + bullet_list(risks))

    holding_actions = result.get("holding_actions") or []
    if holding_actions:
        lines = [
            f"{item.get('name') or item.get('symbol') or 'נייר'} — "
            f"{STANCE_LABELS.get(item.get('stance'), item.get('stance') or 'מעקב')}: {item.get('reason', '')}"
            for item in holding_actions
        ]
        sections.append(section_title("📌 המלצה לכל החזקה") + bullet_list(lines))

    allocation_actions = result.get("allocation_actions") or []
    if allocation_actions:
        sections.append(section_title("🎯 שינויים לפי סדר עדיפות") + bullet_list(allocation_actions))

    cash_plan = result.get("cash_plan")
    if cash_plan:
        sections.append(section_title("💵 תוכנית מזומן") + bullet_list([cash_plan]))

    next_steps = result.get("next_steps") or []
    if next_steps:
        sections.append(section_title("🧭 צעדים לחודש הקרוב") + bullet_list(next_steps))

    return "".join(sections)


def build_monthly_email_html(valuation: dict, deep_result: dict, profile: dict | None = None) -> str:
    executive_summary = deep_result.get("executive_summary") or "אין סיכום זמין החודש."
    extra_html = _build_deep_analysis_html(deep_result)
    return build_report_email_html(
        valuation,
        executive_summary,
        profile,
        title=SUBJECT,
        subtitle="הסיכום המלא של החודש — כולל קופות וחסכונות וניתוח AI מעמיק",
        insight_title="🧠 תמצית מנהלים",
        include_savings=True,
        extra_html=extra_html,
    )


def main():
    users = connect_firebase.get_all_users_with_email()
    print(f"Found {len(users)} user(s) with an email on file.")

    for user in users:
        uid = user["user_id"]
        email = user.get("email")

        try:
            valuation = portfolio_service.get_portfolio_valuation(uid)
            if not valuation["holdings"] and not valuation.get("financial_assets"):
                print(f"Skipping {uid} — empty portfolio.")
                continue

            connect_firebase.save_valuation_snapshot(uid, valuation)
            profile = connect_firebase.get_user_profile(uid)

            deep_result = _run_deep_analysis(uid, valuation, profile)

            html_body = build_monthly_email_html(valuation, deep_result, profile)
            status = email_service.send_email(email, SUBJECT, html_body)
            print(f"Sent to {email} (status {status})")
        except Exception as e:
            # One user's bad data/API hiccup shouldn't stop everyone else's email.
            print(f"FAILED for {uid} ({email}): {e}")


if __name__ == "__main__":
    main()
