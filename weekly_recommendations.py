"""Run weekly (via .github/workflows/weekly_recommendations.yml) to email every
user with a saved address a plain-language portfolio update plus an AI-written,
news-informed note. Not run by the Telegram bot process itself."""

import html

import connect_firebase
import portfolio_service
import ai_recommendation
import email_service

DISCLAIMER = (
    "הודעה זו נוצרה אוטומטית על ידי בינה מלאכותית למטרות מידע בלבד, ואינה מהווה "
    "ייעוץ השקעות. קבל החלטות השקעה בהתייעצות עם איש מקצוע מוסמך."
)

# Same picture set on the Telegram bot itself, hosted from the public
# dashboard site so email clients (which won't render inline/base64 images
# reliably) can load it.
BOT_AVATAR_URL = "https://finance-bot-ori19.vercel.app/bot-avatar.jpg"
DASHBOARD_URL = "https://finance-bot-ori19.vercel.app/dashboard.html"

BG = "#06101c"
CARD_BG = "#0e1c2e"
BORDER = "#1e3348"
TEXT = "#edf5fb"
MUTED = "#91a6ba"
ACCENT = "#46d7a7"
DANGER = "#ff7d90"


def _money(value) -> str:
    try:
        return f"{float(value):,.2f} ₪"
    except (TypeError, ValueError):
        return "—"


def _trend_color(value) -> str:
    if value is None:
        return TEXT
    return ACCENT if float(value) >= 0 else DANGER


def _stat_cell(label: str, value: str, color: str = TEXT) -> str:
    return f"""
    <td style="padding:14px 10px; background:{CARD_BG}; border:1px solid {BORDER}; border-radius:12px;">
      <div style="color:{MUTED}; font-size:11px; margin-bottom:6px;">{html.escape(label)}</div>
      <div style="color:{color}; font-size:16px; font-weight:700;">{value}</div>
    </td>"""


def _section_title(text: str) -> str:
    return f'<h3 style="margin:28px 0 12px; color:{TEXT}; font-size:15px; border-right:3px solid {ACCENT}; padding-right:10px;">{html.escape(text)}</h3>'


def build_email_html(valuation: dict, recommendation_text: str, profile: dict | None = None) -> str:
    # Names/tickers can originate from an imported spreadsheet, and the
    # recommendation text from an LLM — escape all of it before interpolating
    # into HTML, same as web/dashboard.js does for the same untrusted-content
    # reason.
    profile = profile or {}

    holding_rows = []
    for ticker, h in sorted(
        valuation["holdings"].items(), key=lambda item: item[1].get("market_value") or 0, reverse=True
    ):
        name = h.get("name")
        ticker_html = f"<bdi>{html.escape(ticker)}</bdi>"
        label = f"<bdi>{html.escape(name)}</bdi><br><span style='color:{MUTED}; font-size:11px;'>{ticker_html}</span>" if name else ticker_html
        value_str = _money(h["market_value"]) if h["market_value"] is not None else "לא זמין"
        gain = h.get("gain_loss")
        gain_str = f"{'+' if gain >= 0 else ''}{_money(gain)}" if gain is not None else "—"
        broker_str = html.escape(h.get("broker") or "—")
        cell_style = f"padding:10px 8px; border-bottom:1px solid {BORDER}; font-size:13px;"
        holding_rows.append(
            f"<tr>"
            f"<td style='{cell_style}'>{label}</td>"
            f"<td style='{cell_style}'>{h['quantity']}</td>"
            f"<td style='{cell_style}'>{value_str}</td>"
            f"<td style='{cell_style} color:{_trend_color(gain)};'>{gain_str}</td>"
            f"<td style='{cell_style} color:{MUTED};'>{broker_str}</td>"
            f"</tr>"
        )
    holdings_table = f"""
    <table width="100%" style="border-collapse:collapse; margin-top:8px;">
      <tr style="color:{MUTED}; font-size:11px; text-transform:uppercase;">
        <td style="padding:6px 8px;">נייר</td><td style="padding:6px 8px;">כמות</td>
        <td style="padding:6px 8px;">שווי</td><td style="padding:6px 8px;">רווח/הפסד</td>
        <td style="padding:6px 8px;">ברוקר</td>
      </tr>
      {"".join(holding_rows)}
    </table>"""

    gain = valuation.get("total_gain_loss")
    gain_pct = valuation.get("total_gain_loss_pct")
    gain_str = f"{'+' if gain is not None and gain >= 0 else ''}{_money(gain)} ({gain_pct:+.1f}%)" if gain is not None else "—"

    stats_row = f"""
    <table width="100%" style="border-collapse:separate; border-spacing:8px 0;">
      <tr>
        {_stat_cell("שווי חשבון כולל", _money(valuation.get("account_total_value", valuation.get("total_value"))))}
        {_stat_cell("רווח/הפסד בתיק", gain_str, _trend_color(gain))}
        {_stat_cell("מזומן פנוי", _money(valuation.get("cash_balance", 0)))}
      </tr>
    </table>"""

    savings_section = ""
    financial_assets = valuation.get("financial_assets") or {}
    if financial_assets:
        savings_rows = []
        for asset in financial_assets.values():
            name = html.escape(str(asset.get("name") or "מכשיר פיננסי"))
            balance = _money(asset.get("estimated_balance", asset.get("reported_balance", 0)))
            asset_gain = asset.get("estimated_gain_loss")
            asset_gain_str = f"{'+' if asset_gain is not None and asset_gain >= 0 else ''}{_money(asset_gain)}" if asset_gain is not None else "—"
            savings_rows.append(
                f"<tr><td style='padding:8px; border-bottom:1px solid {BORDER}; font-size:13px;'>{name}</td>"
                f"<td style='padding:8px; border-bottom:1px solid {BORDER}; font-size:13px;'>{balance}</td>"
                f"<td style='padding:8px; border-bottom:1px solid {BORDER}; font-size:13px; color:{_trend_color(asset_gain)};'>{asset_gain_str}</td></tr>"
            )
        savings_section = (
            _section_title(f"קופות וחסכונות · {_money(valuation.get('savings_total_value', 0))}")
            + f'<table width="100%" style="border-collapse:collapse;">{"".join(savings_rows)}</table>'
        )

    goal_section = ""
    goal = valuation.get("financial_goal") or {}
    goal_pct = valuation.get("financial_goal_progress_pct")
    if goal.get("target_amount") and goal_pct is not None:
        goal_name = html.escape(str(goal.get("name") or "היעד הפיננסי שלי"))
        bar_pct = max(0, min(100, goal_pct))
        goal_section = (
            _section_title(f"התקדמות ליעד: {goal_name}")
            + f"""
            <div style="background:{CARD_BG}; border:1px solid {BORDER}; border-radius:12px; padding:14px 16px;">
              <table width="100%" style="border-collapse:collapse; margin-bottom:8px;"><tr>
                <td style="font-size:13px; color:{MUTED}; padding:0;">{_money(valuation.get('total_financial_value', 0))} מתוך {_money(goal.get('target_amount'))}</td>
                <td style="font-size:13px; color:{ACCENT}; font-weight:700; padding:0; text-align:left;">{goal_pct:.0f}%</td>
              </tr></table>
              <div style="height:10px; background:#132538; border-radius:999px; overflow:hidden;">
                <div style="height:100%; width:{bar_pct:.0f}%; background:{ACCENT}; border-radius:999px;"></div>
              </div>
            </div>"""
        )

    broker_label = html.escape(str(profile.get("default_broker") or "")).strip()
    broker_line = f" · ברוקר ברירת מחדל: {broker_label}" if broker_label else ""

    recommendation_html = html.escape(recommendation_text).replace("\n", "<br>")

    return f"""
    <div style="font-family:'Segoe UI', Arial, Helvetica, sans-serif; direction:rtl; text-align:right; background:{BG}; color:{TEXT}; padding:24px; max-width:640px; margin:0 auto;">
      <table width="100%" style="border-collapse:collapse; margin-bottom:20px;"><tr>
        <td width="48" style="padding:0;">
          <img src="{BOT_AVATAR_URL}" width="48" height="48" alt="FinPilot" style="display:block; border-radius:50%;">
        </td>
        <td style="padding:0 12px 0 0; vertical-align:middle;">
          <h2 style="margin:0; font-size:19px;">ההמלצה השבועית שלך 📈</h2>
          <div style="color:{MUTED}; font-size:12px; margin-top:2px;">סיכום התיק והתובנות של השבוע{broker_line}</div>
        </td>
      </tr></table>

      {stats_row}

      {_section_title("החזקות")}
      {holdings_table}

      {savings_section}
      {goal_section}

      {_section_title("💡 תובנת השבוע")}
      <div style="background:{CARD_BG}; border:1px solid {BORDER}; border-radius:12px; padding:16px; font-size:14px; line-height:1.6;">
        {recommendation_html}
      </div>

      <div style="text-align:center; margin:28px 0 8px;">
        <a href="{DASHBOARD_URL}" style="display:inline-block; background:{ACCENT}; color:#03120d; font-weight:700; text-decoration:none; padding:12px 28px; border-radius:10px; font-size:14px;">
          צפה בדשבורד המלא
        </a>
      </div>

      <p style="color:{MUTED}; font-size:11px; line-height:1.5; margin-top:24px; border-top:1px solid {BORDER}; padding-top:14px;">
        {DISCLAIMER}
      </p>
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

            html_body = build_email_html(valuation, recommendation, profile)
            status = email_service.send_email(email, "ההמלצה השבועית שלך 📈", html_body)
            print(f"Sent to {email} (status {status})")
        except Exception as e:
            # One user's bad data/API hiccup shouldn't stop everyone else's email.
            print(f"FAILED for {uid} ({email}): {e}")


if __name__ == "__main__":
    main()
