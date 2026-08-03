"""Run weekly (via .github/workflows/weekly_recommendations.yml) to email every
user with a saved address a plain-language portfolio update plus an AI-written,
news-informed note. Not run by the Telegram bot process itself."""

import html

import connect_firebase
import portfolio_service
import ai_recommendation
import email_service

DISCLAIMER = (
    "<p style='color:#888; font-size:12px'>הודעה זו נוצרה אוטומטית על ידי בינה "
    "מלאכותית למטרות מידע בלבד, ואינה מהווה ייעוץ השקעות. קבל החלטות השקעה "
    "בהתייעצות עם איש מקצוע מוסמך.</p>"
)

# Same picture set on the Telegram bot itself, hosted from the public
# dashboard site so email clients (which won't render inline/base64 images
# reliably) can load it.
BOT_AVATAR_URL = "https://finance-bot-ori19.vercel.app/bot-avatar.jpg"


def build_email_html(valuation: dict, recommendation_text: str) -> str:
    # Names/tickers can originate from an imported spreadsheet, and the
    # recommendation text from an LLM — escape all of it before interpolating
    # into HTML, same as web/dashboard.js does for the same untrusted-content
    # reason.
    row_parts = []
    for ticker, h in valuation["holdings"].items():
        name = h.get("name")
        ticker_html = f"<bdi>{html.escape(ticker)}</bdi>"
        label = f"<bdi>{html.escape(name)}</bdi> ({ticker_html})" if name else ticker_html
        value_str = f"{h['market_value']:.2f}" if h["market_value"] is not None else "N/A"
        broker_str = html.escape(h.get("broker") or "—")
        row_parts.append(f"<tr><td>{label}</td><td>{h['quantity']}</td><td>{value_str}</td><td>{broker_str}</td></tr>")
    holding_rows = "".join(row_parts)
    recommendation_text = html.escape(recommendation_text)
    return f"""
    <div style="font-family: Arial, sans-serif; direction: rtl; text-align: right;">
      <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
        <img src="{BOT_AVATAR_URL}" width="48" height="48" alt="FinPilot"
             style="width:48px; height:48px; border-radius:50%; display:block; flex-shrink:0;">
        <h2 style="margin:0;">ההמלצה השבועית שלך 📈</h2>
      </div>
      <p><b>שווי כולל:</b> {valuation['total_value']:.2f}</p>
      <p><b>מזומן פנוי:</b> {valuation.get('cash_balance', 0):.2f}</p>
      <p><b>שווי חשבון כולל:</b> {valuation.get('account_total_value', valuation['total_value']):.2f}</p>
      <p><b>עלות כוללת:</b> {valuation['total_cost']:.2f}</p>
      <p><b>רווח/הפסד:</b> {valuation['total_gain_loss']:.2f} ({valuation['total_gain_loss_pct']:.1f}%)</p>
      <table border="1" cellpadding="6" style="border-collapse: collapse;">
        <tr><th>טיקר</th><th>כמות</th><th>שווי נוכחי</th><th>ברוקר</th></tr>
        {holding_rows}
      </table>
      <h3>סקירה שבועית</h3>
      <p style="white-space: pre-wrap;">{recommendation_text}</p>
      {DISCLAIMER}
    </div>
    """


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

            html = build_email_html(valuation, recommendation)
            status = email_service.send_email(email, "ההמלצה השבועית שלך 📈", html)
            print(f"Sent to {email} (status {status})")
        except Exception as e:
            # One user's bad data/API hiccup shouldn't stop everyone else's email.
            print(f"FAILED for {uid} ({email}): {e}")


if __name__ == "__main__":
    main()
