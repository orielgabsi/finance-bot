"""Run weekly (via .github/workflows/weekly_recommendations.yml) to email every
user with a saved address a plain-language portfolio update plus an AI-written,
news-informed note. Not run by the Telegram bot process itself.

The weekly email is intentionally light — holdings, broker breakdown (with
free cash folded in), and a short AI note. Long-term savings/funds are left
out here on purpose (they don't move week to week) and instead covered in
the monthly email (monthly_recommendations.py), which also includes a
deeper, full-month AI review."""

import connect_firebase
import portfolio_service
import ai_recommendation
import email_service
from email_report import build_report_email_html

SUBJECT = "ההמלצה השבועית שלך 📈"


def build_email_html(valuation: dict, recommendation_text: str, profile: dict | None = None) -> str:
    return build_report_email_html(
        valuation,
        recommendation_text,
        profile,
        title=SUBJECT,
        subtitle="סיכום התיק והתובנות של השבוע",
        insight_title="💡 תובנת השבוע",
        include_savings=False,
    )


def main():
    users = connect_firebase.get_all_users_with_email()
    print(f"Found {len(users)} user(s) with an email on file.")

    for user in users:
        uid = user["user_id"]
        email = user.get("email")

        try:
            valuation = portfolio_service.get_portfolio_valuation(uid)
            if not valuation["holdings"]:
                print(f"Skipping {uid} — empty portfolio.")
                continue

            connect_firebase.save_valuation_snapshot(uid, valuation)

            holdings_for_search = [(t, h.get("name")) for t, h in valuation["holdings"].items()]
            market_context = ai_recommendation.search_market_context(holdings_for_search)
            profile = connect_firebase.get_user_profile(uid)
            recommendation = ai_recommendation.generate_recommendation(valuation, market_context, profile)

            html_body = build_email_html(valuation, recommendation, profile)
            status = email_service.send_email(email, SUBJECT, html_body)
            print(f"Sent to {email} (status {status})")
        except Exception as e:
            # One user's bad data/API hiccup shouldn't stop everyone else's email.
            print(f"FAILED for {uid} ({email}): {e}")


if __name__ == "__main__":
    main()
