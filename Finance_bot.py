import json
import base64
import html
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

import telebot
from dotenv import load_dotenv
from groq import Groq
from telebot import types

import ai_recommendation
import chart_service
import connect_firebase
import fundamental_service
import finance_engine
import portfolio_context
import portfolio_import
import portfolio_service
import price_service
import savings_service
import tax_service
import technical_service
import thesis_service

# Reuse the same model ai_recommendation.py already uses for the weekly email
# — one model to track for deprecations instead of two.
FALLBACK_MODEL = "openai/gpt-oss-120b"
CONFIRM_WORDS = ("כן", "yes", "Yes", "אישור")

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TOKEN or ":" not in TOKEN:
    sys.exit(
        "TELEGRAM_BOT_TOKEN is missing or not a real token — edit .env and set it "
        "to a token from @BotFather (copy .env.example to .env first if you haven't)."
    )

Password = os.environ.get("BOT_ACCESS_PASSWORD", "")
if not Password:
    sys.exit("BOT_ACCESS_PASSWORD is missing — set a strong password in .env.")


def _safe_error(exception) -> str:
    """Redacts credentials that libraries sometimes embed in request URLs."""
    text = str(exception)
    text = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|authorization)=?[^\s&]+", r"\1=[REDACTED]", text)
    return text[:500]


class LogAndContinueExceptionHandler(telebot.ExceptionHandler):
    """Without this, any unhandled exception in a message handler — including
    a plain transient network blip on a bot.send_message() call — bubbles up
    through telebot's worker pool and kills the entire polling loop, taking
    the whole bot offline until someone manually restarts it (observed for
    real: a ConnectionResetError during the exit-button handler did exactly
    this). Returning True here marks the exception as handled, so telebot
    logs it and keeps polling instead of dying."""

    def handle(self, exception):
        print(f"Handler error (bot stays alive): {_safe_error(exception)}")
        return True


def _fixed_notify_next_handlers(self, new_messages):
    """Patches a real bug in pyTelegramBotAPI's TeleBot._notify_next_handlers
    (telebot/__init__.py): the original does
        for i, message in enumerate(new_messages):
            ...
            if need_pop:
                new_messages.pop(i)
    which mutates the list while iterating it via enumerate — every pop shifts
    later items down one slot without the iterator knowing, so the message
    immediately after any popped one is silently skipped from ALL further
    processing (never checked against a pending register_next_step_handler,
    and since it's rarely a real /command either, never picked up by any
    other handler). Observed live: send two replies in quick succession while
    e.g. /buy's "which ticker?" prompt is pending (easily done by a fast
    typist, or a double-tap) and the second one vanishes with no reply and no
    error. Rewritten below to only ever pop after the loop, so nothing gets
    skipped. Patched here instead of editing the vendored library file
    directly, since that copy lives in .venv and would be overwritten by any
    reinstall."""
    remaining = []
    for message in new_messages:
        handlers = self.next_step_backend.get_handlers(message.chat.id)
        if handlers:
            for handler in handlers:
                self._exec_task(handler["callback"], message, *handler["args"], **handler["kwargs"])
        else:
            remaining.append(message)
    new_messages[:] = remaining


telebot.TeleBot._notify_next_handlers = _fixed_notify_next_handlers

bot = telebot.TeleBot(
    TOKEN,
    parse_mode="HTML",
    num_threads=8,
    exception_handler=LogAndContinueExceptionHandler(),
)


# --- עזרי תצוגה ---
def _gain_arrow(amount) -> str:
    if amount is None:
        return "⚪"
    if amount > 0:
        return "🟢▲"
    if amount < 0:
        return "🔴▼"
    return "⚪"


def _price_scale_to_account_currency(ticker) -> float:
    """Israeli fund quotes are stored internally in agorot, but every user-facing
    input and display is in shekels. Foreign securities keep their quote unit."""
    return 0.01 if finance_engine.has_known_instrument(str(ticker)) else 1.0


def _display_unit_price(ticker, raw_price) -> float:
    return float(raw_price) * _price_scale_to_account_currency(ticker)


def _stored_unit_price(ticker, displayed_price) -> float:
    return float(displayed_price) / _price_scale_to_account_currency(ticker)


def _day_change_str(day_change_pct) -> str:
    if day_change_pct is None:
        return ""
    arrow = "📈" if day_change_pct > 0 else ("📉" if day_change_pct < 0 else "➖")
    return f" {arrow}{day_change_pct:+.1f}% היום"


def _period_change_str(period_change_pct, period_label) -> str:
    """Show the period supplied by the market-data source honestly."""
    if period_change_pct is None:
        return ""
    arrow = "📈" if period_change_pct > 0 else ("📉" if period_change_pct < 0 else "➖")
    label = period_label or "בתקופה"
    return f" · {arrow}{period_change_pct:+.1f}% {label}"


def _timeframe_changes_str(holding: dict) -> str:
    """Show every available return with an unambiguous timeframe label."""
    definitions = [
        ("יום", holding.get("day_change_pct")),
        ("שבוע", holding.get("week_change_pct")),
        ("חודש", holding.get("month_change_pct")),
        ("שנה", holding.get("year_change_pct")),
    ]
    parts = []
    for label, value in definitions:
        if value is None:
            continue
        arrow = "📈" if value > 0 else ("📉" if value < 0 else "➖")
        parts.append(f"{label}: {arrow}{value:+.1f}%")
    if not parts and holding.get("period_change_pct") is not None:
        label = holding.get("period_label") or "תקופה"
        value = holding["period_change_pct"]
        arrow = "📈" if value > 0 else ("📉" if value < 0 else "➖")
        parts.append(f"{label}: {arrow}{value:+.1f}%")
    return " | ".join(parts)


def _format_market_time(value) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(ZoneInfo("Asia/Jerusalem"))
        return parsed.strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return str(value)


# A Hebrew sentence with an embedded English security name or numeric ticker
# inside parentheses ("SPDR Gold (410393): קנייה") is exactly the case the
# Unicode bidi algorithm handles badly — punctuation and parentheses can
# visually flip order depending on which script started the line. Wrapping
# each embedded run in a bidi isolate (U+2066 LRI ... U+2069 PDI) makes it
# render as a self-contained left-to-right unit without disturbing the
# surrounding Hebrew sentence's own reading order — the same purpose HTML's
# <bdi> tag serves (used equivalently in web/dashboard.js).
_LRI, _PDI = "⁦", "⁩"


def _bidi_isolate(text) -> str:
    return f"{_LRI}{text}{_PDI}"


def _display_label(ticker, details) -> str:
    """Many Israeli brokerage imports use an opaque numeric security number as
    the ticker; when a readable name was captured (portfolio_import.py's
    NAME_HEADERS), show it alongside the number instead of just the number."""
    name = html.escape(str(portfolio_service.get_holding_name(ticker, details) or ""))
    ticker_part = _bidi_isolate(html.escape(str(ticker)))
    return _bidi_isolate(name) if name else ticker_part


def _resolve_ticker_matches(portfolio: dict, query: str) -> list[str]:
    """Looks up holdings by raw ticker/security-number OR by saved display
    name (case-insensitive substring match), so a holding imported under a
    meaningless numeric code can still be referred to by name. An exact
    ticker match always wins outright (returns just that one); a name
    substring can match more than one holding (e.g. several funds from the
    same provider sharing a name prefix) — callers must handle that
    ambiguity explicitly rather than silently guessing which one was meant."""
    query = query.strip()
    if query.upper() in portfolio:
        return [query.upper()]
    query_lower = query.lower()
    return [
        ticker for ticker, details in portfolio.items()
        if query_lower and query_lower in (portfolio_service.get_holding_name(ticker, details) or "").lower()
    ]


def _resolve_ticker_or_reply(message, portfolio: dict, query: str) -> str | None:
    """Convenience wrapper: resolves query to exactly one ticker, or replies
    with a clear error (no match / ambiguous match) and returns None."""
    matches = _resolve_ticker_matches(portfolio, query)
    if not matches:
        return None
    if len(matches) > 1:
        options = ", ".join(_display_label(t, portfolio[t]) for t in matches)
        bot.reply_to(
            message,
            f"❌ '{query}' תואם כמה ניירות: {options}. ציין את מספר הנייר המדויק.",
            reply_markup=main_menu(),
        )
        return "__AMBIGUOUS__"
    return matches[0]


def format_portfolio_message(valuation: dict) -> str:
    """`valuation` is the dict from portfolio_service.get_portfolio_valuation —
    already priced, so holdings carry gain_loss/day_change_pct/period_change_pct
    for the arrows. Deliberately leaves out quantity/buy-price — this is meant
    to answer "how's each asset doing", not restate the raw holding record
    (which /sell and /tax still show, since they need the exact numbers)."""
    holdings = valuation.get("holdings", {})
    financial_assets = valuation.get("financial_assets") or {}
    if not holdings and not financial_assets:
        cash = valuation.get("cash_balance", 0)
        return f"התיק ללא ניירות כרגע. 💵 מזומן פנוי: {cash:.2f}"
    status_msg = "📊 <b>פירוט התיק שלך:</b>\n"
    if not holdings:
        status_msg += "אין ניירות סחירים כרגע.\n"
    for ticker, h in holdings.items():
        label = _display_label(ticker, h)
        gain = h.get("gain_loss")
        current_price = h.get("current_price")
        status_msg += f"🔹 <b>{label}</b>\n"
        if current_price is not None:
            if h.get("price_unit") == "agorot":
                status_msg += f"   💰 מחיר נוכחי: {h.get('current_price_account_currency', current_price / 100):,.2f} ₪"
            elif h.get("quote_currency") and h.get("account_currency") and h.get("quote_currency") != h.get("account_currency"):
                status_msg += (
                    f"   💰 מחיר נוכחי: {current_price:,.2f} {h['quote_currency']} "
                    f"({h.get('current_price_account_currency', current_price):,.2f} {h['account_currency']})"
                )
            else:
                status_msg += f"   💰 מחיר נוכחי: {current_price:,.2f} {h.get('quote_currency') or ''}".rstrip()
        if gain is not None:
            gain_period = f"מאז {h['buy_date']}" if h.get("buy_date") else "מאז מחיר הבסיס שהוזן"
            gain_pct = h.get("gain_loss_pct")
            pct_text = f" ({gain_pct:+.2f}%)" if gain_pct is not None else ""
            status_msg += f" | {_gain_arrow(gain)} רווח/הפסד אישי {gain_period}: {gain:+,.2f} ₪{pct_text}"
            if h.get("fx_gain_loss") is not None:
                status_msg += (
                    f"\n   💱 השפעת מט״ח על הרווח: {h['fx_gain_loss']:+,.2f} ₪ "
                    f"(שער קנייה {h['buy_fx_rate']:.4f}, נוכחי {h['current_fx_rate']:.4f})"
                )
        if current_price is not None or gain is not None:
            status_msg += "\n"
        changes = _timeframe_changes_str(h)
        if changes:
            status_msg += f"   📊 {changes}\n"
        if h.get("price_source"):
            fetched = _format_market_time(h.get("price_fetched_at"))
            time_note = f" · נשלף {fetched}" if fetched else ""
            status_msg += f"   מקור מחיר: {html.escape(str(h['price_source']))}{time_note}\n"
    status_msg += (
        f"\n💵 מזומן פנוי: {valuation.get('cash_balance', 0):.2f}\n"
        f"💼 שווי חשבון מסחר משוער: {valuation.get('account_total_value', valuation.get('total_value', 0)):.2f}\n"
        f"📈 רווח/הפסד מאז מחירי הבסיס שהוזנו: {valuation.get('total_gain_loss', 0):+.2f} "
        f"({valuation.get('total_gain_loss_pct', 0):.1f}%)"
    )
    if financial_assets:
        status_msg += "\n\n🏦 <b>קופות וחסכונות:</b>"
        for asset in financial_assets.values():
            balance = float(asset.get("estimated_balance", asset.get("reported_balance", 0)) or 0)
            status_msg += f"\n🔸 {html.escape(str(asset.get('name') or 'מכשיר פיננסי'))}: {balance:,.2f} ₪"
            if asset.get("estimated_gain_loss") is not None:
                status_msg += (
                    f" · {_gain_arrow(asset['estimated_gain_loss'])} רווח/הפסד משוער "
                    f"{html.escape(str(asset.get('estimate_period_label') or ''))}: {asset['estimated_gain_loss']:+,.2f} ₪ "
                    f"({float(asset.get('estimated_gain_loss_pct') or 0):+.2f}%)"
                )
            if asset.get("latest_report_period"):
                status_msg += f"\n   דיווח ציבורי אחרון: {asset['latest_report_period']}"
        status_msg += (
            f"\n🏦 סך קופות וחסכונות: {valuation.get('savings_total_value', 0):,.2f} ₪"
            f"\n🧾 סך כל הנכסים הפיננסיים: {valuation.get('total_financial_value', 0):,.2f} ₪"
            "\nℹ️ יתרת הקופה היא אומדן בין עדכונים חודשיים; האזור האישי של הגוף המנהל הוא המקור הקובע."
        )
    if valuation.get("financial_goal"):
        status_msg += "\n\n" + _goal_message(valuation)
    status_msg += (
        "\nℹ️ שווי חשבון המסחר הוא אומדן לפי המחירים ושערי המטבע האחרונים. "
        "פער קטן מול הברוקר יכול לנבוע מעיכוב מחיר, שעת מט״ח, עיגול ועמלות שאינן כלולות."
    )
    if not valuation.get("pricing_complete", True):
        missing = ", ".join(
            _display_label(ticker, holdings.get(ticker) or {})
            for ticker in valuation.get("unpriced_tickers", [])
        )
        status_msg += f"\n⚠️ אין מחיר עדכני עבור: {missing}. הם לא נחשבו כרווח/הפסד."
    return status_msg


def get_user_id(message):
    print(f"Received message from user ID: {message.from_user.id}")
    return str(message.from_user.id)


# Telegram user IDs who've entered the correct password this run. In-memory
# only (not Firestore) — on purpose: a bot restart re-locks everyone, which
# is the safer default for a single-shared-password gate. Every handler that
# reads or changes portfolio data must check this first; previously only the
# /start welcome message checked the password at all, so anyone who simply
# typed a menu button's text (e.g. "📊 התיק שלי") got straight to the data
# with no password ever asked.
authenticated_users = set()


def _require_auth(message) -> bool:
    if get_user_id(message) in authenticated_users:
        return True
    bot.reply_to(message, "🔒 יש להתחבר קודם. שלח /start והזן את הסיסמה.")
    return False


def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 התיק שלי", "🥧 עוגת ההשקעות")
    markup.row("➕ קניה חדשה", "➕ מכשיר פיננסי")
    markup.row("➖ מכירה", "🧮 סימולטור מס")
    markup.row("💵 מזומן פנוי", "🎯 יעד פיננסי")
    markup.row("📋 חסכונות וקופות", "✏️ עריכת מחיר בסיס")
    markup.row("📥 ייבוא תיק", "🔎 ניתוח מניה/קרן")
    markup.row("🤖 המלצת AI", "🧠 מצב חשיבה")
    markup.row("⚙️ התאמה אישית", "🌐 חיבור לאתר")
    markup.row("❓ עזרה", "🚪 יציאה")
    return markup


# pyTelegramBotAPI routes the message right after a register_next_step_handler
# call straight to that callback, bypassing every normal @bot.message_handler
# (including menu buttons and the AI catch-all) — so pressing a different menu
# button mid-flow used to get swallowed as if it were an answer to whatever
# question the flow was asking. Every step-callback below calls this first;
# if the incoming message is actually a menu button or a command, it cancels
# the pending flow and dispatches to the real handler instead. Referenced by
# name (not directly) since these dicts are defined before those handlers
# exist yet — resolved lazily via globals() at call time.
_MENU_BUTTON_HANDLER_NAMES = {
    "📊 התיק שלי": "portfolio_command",
    "🥧 עוגת ההשקעות": "cake_command",
    "➕ קניה חדשה": "buy_command",
    "➕ מכשיר פיננסי": "financial_asset_command",
    "📋 חסכונות וקופות": "savings_command",
    "🎯 יעד פיננסי": "goal_command",
    "➖ מכירה": "sell_command",
    "📥 ייבוא תיק": "import_command",
    "🧮 סימולטור מס": "tax_command",
    "💵 מזומן פנוי": "cash_command",
    "✏️ עריכת מחיר בסיס": "edit_price_command",
    "🔎 ניתוח מניה/קרן": "analyze_command",
    "🤖 המלצת AI": "menu_ai_recommendation",
    "🧠 מצב חשיבה": "thinking_command",
    "⚙️ התאמה אישית": "profile_command",
    "🌐 חיבור לאתר": "link_command",
    "🚪 יציאה": "menu_exit",
    "❓ עזרה": "menu_help",
}
_COMMAND_HANDLER_NAMES = {
    "start": "send_welcome",
    "buy": "buy_command",
    "sell": "sell_command",
    "tax": "tax_command",
    "import": "import_command",
    "portfolio": "portfolio_command",
    "cake": "cake_command",
    "email": "email_command",
    "link": "link_command",
    "cash": "cash_command",
    "deposit": "deposit_command",
    "withdraw": "withdraw_command",
    "analyze": "analyze_command",
    "think": "thinking_command",
    "profile": "profile_command",
    "editprice": "edit_price_command",
    "asset": "financial_asset_command",
    "savings": "savings_command",
    "goal": "goal_command",
    "recommend": "recommend_command",
}


def _redirect_if_menu_action(message) -> bool:
    """Call at the top of every next-step-handler callback. Returns True (and
    the caller must return immediately) if the message was actually a menu
    button/command the user switched to mid-flow, in which case the pending
    flow is cancelled and the real action already ran."""
    text = (message.text or "").strip()
    handler_name = _MENU_BUTTON_HANDLER_NAMES.get(text)
    if handler_name is None and text.startswith("/"):
        handler_name = _COMMAND_HANDLER_NAMES.get(text.split()[0][1:].lower())
    if handler_name is None:
        return False
    bot.clear_step_handler(message)
    globals()[handler_name](message)
    return True


# --- שלב 2: בדיקת הסיסמה ---
def process_password_step(message):
    password_guess = message.text

    if password_guess == Password:
        bot.reply_to(message, "✅ סיסמה נכונה!")

        uid = get_user_id(message)
        authenticated_users.add(uid)
        connect_firebase.create_user_document(uid)

        portfolio = connect_firebase.get_portfolio(uid)
        if not portfolio:
            bot.send_message(
                message.chat.id,
                "<b>ברוך הבא!</b>\nהתיק שלך ריק כרגע — נסה ➕ קניה חדשה.",
                reply_markup=main_menu(),
                parse_mode="HTML",
            )
            return

        # Live pricing (needed for gain/loss arrows + daily %) can be genuinely
        # slow — some holdings (e.g. Israeli security numbers yfinance can't
        # resolve) fall back to a much slower scrape. Reply with the raw
        # holdings immediately so login is never blocked on that, then send
        # the priced view as a follow-up once it's ready.
        quick_lines = "\n".join(f"🔹 {_display_label(ticker, d)}" for ticker, d in portfolio.items())
        bot.send_message(
            message.chat.id,
            f"<b>ברוך הבא!</b>\n📊 <b>התיק שלך:</b>\n{quick_lines}\n\n⏳ מעדכן מחירים חיים...",
            reply_markup=main_menu(),
            parse_mode="HTML",
        )
        threading.Thread(target=_send_priced_login_followup, args=(uid, message.chat.id), daemon=True).start()
    else:
        bot.reply_to(message, "❌ סיסמה שגויה. הקש /start כדי לנסות שוב.")


def _send_priced_login_followup(uid, chat_id):
    """Runs in the background after login: prices the portfolio (also warms
    price_service's cache, so /cake right after login hits a warm cache
    instead of a cold one, and refreshes the snapshot the website reads)."""
    try:
        valuation, _ = _get_fast_valuation(uid)
        if valuation["holdings"]:
            connect_firebase.save_valuation_snapshot(uid, valuation)
        bot.send_message(chat_id, format_portfolio_message(valuation), parse_mode="HTML")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ לא הצלחתי לעדכן מחירים חיים כרגע ({html.escape(_safe_error(e))}).")


# --- שלב 1: פקודת הסטארט ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
    msg = bot.send_message(message.chat.id, "הקש סיסמה כדי להתחבר...")
    bot.register_next_step_handler(msg, process_password_step)


# --- קניה חדשה ---
@bot.message_handler(commands=["buy"])
def buy_command(message):
    if not _require_auth(message):
        return
    args = message.text.split()[1:]
    if len(args) == 3:
        _finish_buy(message, args[0], args[1], args[2])
    else:
        msg = bot.reply_to(message, "איזה טיקר קנית? (למשל AAPL)")
        bot.register_next_step_handler(msg, buy_step_ticker)


def _looks_like_pasted_positions(text: str) -> bool:
    value = str(text or "")
    lowered = value.casefold()
    signals = (
        "מחיר ממוצע", "בסיס עלות", "שווי שוק", "פוזיציה",
        "average price", "cost basis", "market value", "position",
    )
    return value.count("\n") >= 3 and sum(signal in lowered for signal in signals) >= 2


def _prepare_bulk_buy_confirm(message, holdings, replace_existing=False):
    clean = []
    for item in holdings[:50]:
        try:
            ticker = str(item.get("ticker") or "").strip().upper()
            quantity = float(item.get("quantity"))
            buy_price = float(item.get("buy_price"))
            reported_total_cost = float(item["reported_total_cost"]) if item.get("reported_total_cost") is not None else None
        except (AttributeError, TypeError, ValueError):
            continue
        if not re.fullmatch(r"[A-Z0-9.=_^-]{1,30}", ticker) or quantity <= 0 or buy_price <= 0:
            continue
        clean.append({
            "ticker": ticker,
            "quantity": quantity,
            "buy_price": buy_price,
            "reported_total_cost": reported_total_cost if reported_total_cost and reported_total_cost > 0 else None,
            "name": str(item.get("name") or "").strip()[:120],
            "currency": str(item.get("currency") or "").strip().upper(),
        })
    if not clean:
        bot.reply_to(message, "לא הצלחתי לזהות בטקסט טיקר, כמות ומחיר קנייה ממוצע.", reply_markup=main_menu())
        return
    lines = [f"🧠 <b>זיהיתי {len(clean)} רכישות/החזקות:</b>"]
    for item in clean:
        label = html.escape(item["name"] or item["ticker"])
        currency = item["currency"] or ("ILS" if finance_engine.has_known_instrument(item["ticker"]) else "מטבע המסחר")
        lines.append(
            f"• {label} ({_bidi_isolate(html.escape(item['ticker']))}): "
            f"{item['quantity']:g} יח׳ · מחיר ממוצע {item['buy_price']:,.4f} {html.escape(currency)}"
            + (
                f" · בסיס עלות מדויק {item['reported_total_cost']:,.2f} {html.escape(currency)}"
                if item.get("reported_total_cost") is not None else ""
            )
        )
    lines += [
        "",
        "המחיר שנבחר הוא <b>מחיר ממוצע</b>. כשמופיע בסיס עלות, הוא נשמר בנפרד ומשמש לחישוב המדויק במקום כמות × מחיר מעוגל.",
        (
            "זו טבלת מצב מהברוקר: הכמות והעלות של הניירות האלה <b>יוחלפו</b> בנתונים שמוצגים, "
            "ולא יתווספו שוב. החזקות אחרות לא יימחקו."
            if replace_existing else
            "הנתונים יתווספו כרכישות חדשות לכמויות הקיימות."
        ),
        "להמשיך? השב <b>כן</b> לאישור; כל תשובה אחרת תבטל.",
    ]
    msg = bot.reply_to(message, "\n".join(lines))
    bot.register_next_step_handler(msg, _confirm_bulk_buy, clean, replace_existing)


def _confirm_bulk_buy(message, holdings, replace_existing=False):
    if _redirect_if_menu_action(message):
        return
    if (message.text or "").strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "ההוספה בוטלה ולא נשמר דבר.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    if replace_existing:
        replacements = []
        for item in holdings:
            ticker = item["ticker"]
            currency = item.get("currency")
            buy_fx_rate = price_service.get_fx_rate(currency, "ILS") if currency in {"USD", "EUR"} else None
            replacements.append({
                **item,
                "buy_price": _stored_unit_price(ticker, item["buy_price"]),
                **({"buy_fx_rate": buy_fx_rate} if buy_fx_rate else {}),
            })
        try:
            result = connect_firebase.upsert_portfolio_holdings(uid, replacements)
        except ValueError as exc:
            bot.reply_to(message, f"לא ניתן היה לסנכרן את הטבלה: {html.escape(str(exc))}", reply_markup=main_menu())
            return
        _refresh_valuation_snapshot_async(uid)
        bot.reply_to(
            message,
            f"✅ תמונת הברוקר סונכרנה בלי להכפיל כמויות: {result['updated']} עודכנו, "
            f"{result['added']} נוספו ו־{result['unchanged']} לא השתנו. האתר יתעדכן אוטומטית.",
            reply_markup=main_menu(),
        )
        return
    saved = []
    for item in holdings:
        ticker = item["ticker"]
        stored_price = _stored_unit_price(ticker, item["buy_price"])
        currency = item.get("currency")
        buy_fx_rate = None
        if currency in {"USD", "EUR"}:
            buy_fx_rate = price_service.get_fx_rate(currency, "ILS")
        try:
            connect_firebase.record_buy(
                uid, ticker, item["quantity"], stored_price,
                item.get("name") or None, buy_fx_rate=buy_fx_rate,
                reported_total_cost=item.get("reported_total_cost"),
            )
            saved.append(ticker)
        except ValueError:
            continue
    if not saved:
        bot.reply_to(message, "לא נשמרו החזקות; הנתונים שזוהו לא היו תקינים.", reply_markup=main_menu())
        return
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(
        message,
        f"✅ נוספו לתיק {len(saved)} ניירות: {', '.join(saved)}. האתר יסונכרן אוטומטית.",
        reply_markup=main_menu(),
    )


def _run_pasted_buy_ai(message, text, progress=None, replace_existing=False):
    try:
        holdings = portfolio_import.parse_pasted_holdings_ai(text)
        if progress:
            try:
                bot.delete_message(message.chat.id, progress.message_id)
            except Exception:
                pass
        _prepare_bulk_buy_confirm(message, holdings, replace_existing)
    except Exception as exc:
        if progress:
            try:
                bot.delete_message(message.chat.id, progress.message_id)
            except Exception:
                pass
        bot.reply_to(
            message,
            f"לא הצלחתי לזהות את הרכישה מהטקסט ({html.escape(_safe_error(exc))}). "
            "נסה להדביק שוב את הטבלה המלאה או לכתוב: קניתי 2 AAPL במחיר 150.",
            reply_markup=main_menu(),
        )


def _handle_pasted_buy_text(message, text):
    replace_existing = _looks_like_pasted_positions(text)
    holdings = portfolio_import.parse_pasted_holdings_local(text)
    if holdings:
        _prepare_bulk_buy_confirm(message, holdings, replace_existing)
        return
    progress = bot.reply_to(message, "🧠 המבנה לא היה מוכר, ה־AI מזהה טיקר, כמות ומחיר ממוצע…")
    threading.Thread(
        target=_run_pasted_buy_ai,
        args=(message, text, progress, replace_existing),
        daemon=True,
    ).start()


def buy_step_ticker(message):
    if _redirect_if_menu_action(message):
        return
    raw_text = (message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.=_^-]{1,30}", raw_text):
        _handle_pasted_buy_text(message, raw_text)
        return
    ticker = raw_text.upper()
    msg = bot.reply_to(message, f"כמה יחידות של {ticker} קנית?")
    bot.register_next_step_handler(msg, buy_step_quantity, ticker)


def buy_step_quantity(message, ticker):
    if _redirect_if_menu_action(message):
        return
    try:
        qty = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "כמות לא תקינה, נסה /buy שוב.", reply_markup=main_menu())
        return
    unit_hint = "בשקלים (לא באגורות)" if finance_engine.has_known_instrument(ticker) else "במטבע שבו הנייר נסחר"
    msg = bot.reply_to(message, f"באיזה מחיר קנית ליחידה {unit_hint}?")
    bot.register_next_step_handler(msg, buy_step_price, ticker, qty)


def buy_step_price(message, ticker, qty):
    if _redirect_if_menu_action(message):
        return
    try:
        price = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "מחיר לא תקין, נסה /buy שוב.", reply_markup=main_menu())
        return
    _finish_buy(message, ticker, qty, price)


_valuation_refresh_lock = threading.Lock()
_valuation_refresh_state = {}


def _refresh_valuation_snapshot_async(uid):
    """The website reads users/{id}.last_valuation — a cached snapshot only
    refreshed when the bot computes a fresh valuation (/portfolio, /cake,
    login, the weekly job). Without this, the site shows stale holdings right
    after a /buy, /sell, or import until the user happens to view /portfolio
    or /cake again. Runs in the background (pricing can be slow — some
    holdings only resolve via the much slower Globes scrape) so it never
    delays the action's own confirmation reply."""
    with _valuation_refresh_lock:
        if uid in _valuation_refresh_state:
            _valuation_refresh_state[uid] = True
            return
        _valuation_refresh_state[uid] = False

    def _job():
        while True:
            try:
                valuation = portfolio_service.get_portfolio_valuation(uid)
                connect_firebase.save_valuation_snapshot(uid, valuation)
            except Exception as e:
                print(f"Background valuation snapshot refresh failed for {uid}: {_safe_error(e)}")
            with _valuation_refresh_lock:
                if _valuation_refresh_state.get(uid):
                    _valuation_refresh_state[uid] = False
                    continue
                _valuation_refresh_state.pop(uid, None)
                return
    threading.Thread(target=_job, daemon=True).start()


def _get_fast_valuation(uid):
    """Use the synchronized snapshot immediately and refresh it out of band."""
    cached = portfolio_service.get_cached_portfolio_valuation(uid)
    if cached is not None:
        _refresh_valuation_snapshot_async(uid)
        return cached, True
    return portfolio_service.get_portfolio_valuation(uid), False


def _finish_buy(message, ticker, qty, price):
    try:
        ticker, qty, price = str(ticker).upper(), float(qty), float(price)
    except ValueError:
        bot.reply_to(message, "כמות/מחיר לא תקינים, נסה /buy שוב.")
        return
    if not ticker.strip() or qty <= 0 or price <= 0:
        bot.reply_to(message, "טיקר, כמות ומחיר חייבים להיות תקינים וגדולים מאפס.", reply_markup=main_menu())
        return
    displayed_price = price
    price = _stored_unit_price(ticker, displayed_price)
    uid = get_user_id(message)
    try:
        connect_firebase.record_buy(uid, ticker, qty, price)
    except ValueError as e:
        bot.reply_to(message, f"❌ {html.escape(str(e))}", reply_markup=main_menu())
        return
    try:
        matching_theses = [
            thesis for thesis in connect_firebase.get_open_theses(uid)
            if str(thesis.get("ticker", "")).upper() == ticker
        ]
        if matching_theses:
            connect_firebase.add_journal_entry(
                uid, "ADD", ticker=ticker, quantity=qty, price=price, thesis_id=matching_theses[0].get("id"),
            )
    except Exception as exc:
        print(f"journal ADD failed for {uid}/{ticker}: {_safe_error(exc)}")
    _refresh_valuation_snapshot_async(uid)
    currency = "₪" if finance_engine.has_known_instrument(ticker) else "במטבע המסחר"
    bot.reply_to(message, f"✅ נרשם: {qty} {ticker} במחיר {displayed_price:,.4f} {currency}", reply_markup=main_menu())


def _finish_buy_confirm(message, ticker, qty, price):
    """Same as /buy's direct 3-arg form, but always shows a preview and
    requires confirmation first — used for AI-fallback-initiated buys, which
    must never write to Firestore without the user explicitly confirming."""
    msg = bot.reply_to(
        message,
        f"🤖 הבנתי: קנייה של {qty} {ticker} במחיר {price} ליחידה.\n"
        f"לאשר ולשמור? השב <b>כן</b> לאישור, או כל דבר אחר לביטול.",
    )
    bot.register_next_step_handler(msg, _confirm_ai_buy, ticker, qty, price)


def _confirm_ai_buy(message, ticker, qty, price):
    if _redirect_if_menu_action(message):
        return
    if message.text.strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "הפעולה בוטלה.", reply_markup=main_menu())
        return
    _finish_buy(message, ticker, qty, price)


def _is_sell_all_phrase(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in ("all", "sell all", "everything"):
        return True
    return "כל" in text  # "הכל" / "כל התיק" / "את כל הנכסים" — no real ticker contains this word


# --- מכירה ---
@bot.message_handler(commands=["sell"])
def sell_command(message):
    if not _require_auth(message):
        return
    args = message.text.split()[1:]
    if len(args) == 1 and _is_sell_all_phrase(args[0]):
        _prepare_sell_all_confirm(message)
    elif len(args) == 3:
        _prepare_sell_confirm(message, args[0], args[1], args[2])
    else:
        uid = get_user_id(message)
        portfolio = connect_firebase.get_portfolio(uid)
        holdings_list = "\n".join(f"• {_display_label(t, d)}" for t, d in portfolio.items())
        prompt = "איזה טיקר/נייר תרצה למכור? (טיקר או שם, או כתוב 'הכל' כדי למכור את כל התיק)"
        if holdings_list:
            prompt += f"\n\nההחזקות שלך:\n{holdings_list}"
        msg = bot.reply_to(message, prompt)
        bot.register_next_step_handler(msg, sell_step_ticker)


def sell_step_ticker(message):
    if _redirect_if_menu_action(message):
        return
    text = message.text.strip()
    if _is_sell_all_phrase(text):
        _prepare_sell_all_confirm(message)
        return

    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    ticker = _resolve_ticker_or_reply(message, portfolio, text)
    if ticker == "__AMBIGUOUS__":
        return
    if ticker is None:
        bot.reply_to(
            message,
            f"❌ לא מצאתי '{text}' בתיק. נסה שוב עם טיקר/שם, או כתוב 'הכל' כדי למכור את כל התיק.",
            reply_markup=main_menu(),
        )
        return
    label = _display_label(ticker, portfolio[ticker])
    msg = bot.reply_to(message, f"כמה יחידות של {label} תרצה למכור?")
    bot.register_next_step_handler(msg, sell_step_quantity, ticker)


def sell_step_quantity(message, ticker):
    if _redirect_if_menu_action(message):
        return
    try:
        qty = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "כמות לא תקינה, נסה /sell שוב.", reply_markup=main_menu())
        return
    unit_hint = "בשקלים (לא באגורות)" if finance_engine.has_known_instrument(ticker) else "במטבע שבו הנייר נסחר"
    msg = bot.reply_to(message, f"באיזה מחיר אתה מוכר ליחידה {unit_hint}?")
    bot.register_next_step_handler(msg, sell_step_price, ticker, qty)


def sell_step_price(message, ticker, qty):
    if _redirect_if_menu_action(message):
        return
    try:
        price = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "מחיר לא תקין, נסה /sell שוב.", reply_markup=main_menu())
        return
    _prepare_sell_confirm(message, ticker, qty, price)


def _sale_estimate(uid, ticker, existing, quantity, stored_sell_price):
    cached = portfolio_service.get_cached_portfolio_valuation(uid) or {}
    priced = (cached.get("holdings") or {}).get(ticker) or {}
    quote_currency = str(priced.get("quote_currency") or "").upper()
    is_foreign = quote_currency in {"USD", "EUR"} or bool(existing.get("buy_fx_rate"))
    is_tase = not is_foreign
    quote_scale = 0.01 if finance_engine.has_known_instrument(ticker) or priced.get("price_unit") == "agorot" else 1.0
    if is_tase:
        current_fx = 1.0
        cost_fx = 1.0
        market = "tase"
    else:
        quote_currency = quote_currency or "USD"
        current_fx = float(priced.get("current_fx_rate") or priced.get("fx_rate_to_account") or 0)
        if not current_fx:
            current_fx = float(price_service.get_fx_rate(quote_currency, "ILS") or existing.get("buy_fx_rate") or 1.0)
        cost_fx = float(existing.get("buy_fx_rate") or current_fx)
        market = "foreign"
    proceeds = float(quantity) * float(stored_sell_price) * quote_scale * current_fx
    reported_total_cost = existing.get("reported_total_cost")
    held_quantity = float(existing.get("quantity", 0) or 0)
    if reported_total_cost is not None and held_quantity > 0:
        cost = float(quantity) / held_quantity * float(reported_total_cost) * cost_fx
    else:
        cost = float(quantity) * float(existing.get("buy_price", 0) or 0) * quote_scale * cost_fx
    name = str(existing.get("name") or portfolio_service.get_holding_name(ticker, existing) or "").casefold()
    if "מחקה" in name:
        instrument_type = "tracking_fund"
    elif "סל" in name:
        instrument_type = "etf"
    elif any(term in name for term in ("קרן", "נאמנות")):
        instrument_type = "other_mutual_fund"
    else:
        instrument_type = "security"
    commission = tax_service.estimate_trade_commission(
        quantity,
        proceeds,
        market=market,
        instrument_type=instrument_type,
        fx_rate_to_ils=current_fx,
    )
    estimate = tax_service.estimate_sale_from_amounts(proceeds, cost, commission["amount_ils"])
    estimate.update({
        "commission_label": commission["label"],
        "commission_quote": commission["amount_quote"],
        "commission_currency": commission["quote_currency"],
        "current_fx_rate": current_fx,
        "cost_fx_rate": cost_fx,
        "market": market,
    })
    return estimate


def _prepare_sell_confirm(message, ticker, qty, price):
    try:
        ticker, qty, price = str(ticker).upper(), float(qty), float(price)
    except ValueError:
        bot.reply_to(message, "כמות/מחיר לא תקינים, נסה /sell שוב.", reply_markup=main_menu())
        return
    if qty <= 0 or price <= 0:
        bot.reply_to(message, "כמות ומחיר חייבים להיות גדולים מאפס.", reply_markup=main_menu())
        return

    displayed_price = price
    price = _stored_unit_price(ticker, displayed_price)
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    existing = portfolio.get(ticker)
    held_qty = existing.get("quantity", 0) if existing else 0
    if not existing or qty > held_qty:
        bot.reply_to(message, f"❌ אין מספיק {ticker} בתיק למכירה (יש {held_qty}).", reply_markup=main_menu())
        return

    tax = _sale_estimate(uid, ticker, existing, qty, price)
    lines = [
        f"📤 <b>הדמיית מכירה</b>: {qty} {_display_label(ticker, existing)} במחיר {displayed_price:,.4f} ליחידה",
        f"💰 תמורה ברוטו: {tax['proceeds']:.2f} ₪",
        f"🏦 עמלת מסחר צפויה: {tax['sale_commission']:.2f} ₪",
        f"   {html.escape(tax['commission_label'])}",
        f"📊 רווח/הפסד לפני עמלה: {tax['gain']:+.2f} ₪",
        f"📉 רווח חייב משוער לאחר עמלת המכירה: {tax['taxable_gain']:+.2f} ₪",
        f"💸 מס משוער (הערכה בלבד — 25% שטוח, לא ייעוץ מס): {tax['estimated_tax']:.2f} ₪",
        f"✅ נטו משוער אחרי עמלה ומס: {tax['net_after_tax_and_fees']:.2f} ₪",
        "ℹ️ זו הדמיה; האישור הבא מעדכן את התיק ב-FinPilot ואינו שולח הוראה לברוקר.",
        "\nלאשר ולעדכן את המכירה בתיק? השב <b>כן</b> לאישור, או כל דבר אחר לביטול.",
    ]
    msg = bot.reply_to(message, "\n".join(lines))
    bot.register_next_step_handler(msg, _finish_sell, ticker, qty, price)


def _journal_sell(uid, ticker, qty, price, existing_before, gain=None):
    """Logs a completed sale. If the ticker has an open thesis, logs
    REDUCE/EXIT instead of a plain SELL and closes the thesis on a full
    exit — never on a partial sell, since the position still exists."""
    ticker = str(ticker).strip().upper()
    remaining_qty = float((existing_before or {}).get("quantity", 0) or 0) - float(qty)
    matching_theses = [
        thesis for thesis in connect_firebase.get_open_theses(uid)
        if str(thesis.get("ticker", "")).upper() == ticker
    ]
    if matching_theses:
        thesis = matching_theses[0]
        if remaining_qty <= 1e-9:
            thesis_service.close_thesis(thesis["id"], reason="Sold full position")
            action = "EXIT"
        else:
            action = "REDUCE"
        connect_firebase.add_journal_entry(
            uid, action, ticker=ticker, quantity=qty, price=price, gain=gain, thesis_id=thesis.get("id"),
        )
    else:
        connect_firebase.add_journal_entry(uid, "SELL", ticker=ticker, quantity=qty, price=price, gain=gain)


def _finish_sell(message, ticker, qty, price):
    if _redirect_if_menu_action(message):
        return
    if message.text.strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "המכירה בוטלה.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    existing = connect_firebase.get_portfolio(uid).get(ticker) or {}
    result = _sale_estimate(uid, ticker, existing, qty, price)
    invested_cost = result["cost"]
    profit_pct = (result["gain"] / invested_cost * 100) if invested_cost else 0.0
    try:
        connect_firebase.record_sell(uid, ticker, qty, price)
    except ValueError as e:
        bot.reply_to(message, f"❌ {html.escape(_safe_error(e))}", reply_markup=main_menu())
        return
    try:
        _journal_sell(uid, ticker, qty, price, existing, gain=result["gain"])
    except Exception as exc:
        print(f"journal_sell failed for {uid}/{ticker}: {_safe_error(exc)}")
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(
        message,
        "\n".join([
            f"✅ <b>המכירה נרשמה ב-FinPilot — {_display_label(ticker, existing)}</b>",
            f"כמות: {qty} · מחיר מכירה: {_display_unit_price(ticker, price):,.4f} ₪",
            f"📥 עלות החלק שנמכר: {invested_cost:.2f} ₪",
            f"💰 תמורה ברוטו: {result['proceeds']:.2f} ₪",
            f"🏦 עמלת מסחר צפויה: {result['sale_commission']:.2f} ₪",
            f"{_gain_arrow(result['gain'])} <b>רווח ממומש: {result['gain']:+.2f} ₪ ({profit_pct:+.1f}%)</b>",
            f"💸 מס משוער: {result['estimated_tax']:.2f} ₪",
            f"✅ רווח נטו משוער אחרי עמלה ומס: {result['net_gain_after_tax_and_fees']:+.2f} ₪",
            "ℹ️ הרישום אינו שולח הוראת מסחר לאלטשולר שחם.",
        ]),
        reply_markup=main_menu(),
    )


def _prepare_sell_all_confirm(message):
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    if not portfolio:
        bot.reply_to(message, "התיק כבר ריק — אין מה למכור.", reply_markup=main_menu())
        return

    cached_valuation = portfolio_service.get_cached_portfolio_valuation(uid) or {}
    cached_holdings = cached_valuation.get("holdings") or {}
    prices = {
        ticker: (cached_holdings.get(ticker) or {}).get("current_price")
        for ticker in portfolio
    }
    missing = [ticker for ticker, price in prices.items() if price is None]
    if not missing:
        _show_sell_all_confirmation(message, portfolio, prices)
        _refresh_valuation_snapshot_async(uid)
        return

    progress = bot.reply_to(
        message,
        f"⏳ מזהה מחיר עבור {len(missing)} ניירות לפי השם שלהם. בסיום אציג אישור אחד לכל התיק...",
    )
    threading.Thread(
        target=_resolve_sell_all_prices,
        args=(message, portfolio, prices, missing, progress),
        daemon=True,
    ).start()


def _resolve_sell_all_prices(message, portfolio, prices, missing, progress):
    names = {ticker: portfolio[ticker].get("name") for ticker in missing}
    try:
        resolved = price_service.get_current_prices_full(missing, names)
        for ticker in missing:
            price = (resolved.get(ticker) or {}).get("price")
            if price is not None:
                prices[ticker] = price
    except Exception as e:
        print(f"Sell-all name price resolution failed: {_safe_error(e)}")
    try:
        bot.delete_message(message.chat.id, progress.message_id)
    except Exception:
        pass

    still_missing = [ticker for ticker in missing if prices.get(ticker) is None]
    if still_missing:
        _ask_manual_sell_all_price(message, portfolio, prices, still_missing, 0)
        return
    _show_sell_all_confirmation(message, portfolio, prices)


def _ask_manual_sell_all_price(message, portfolio, prices, missing, index):
    ticker = missing[index]
    label = _display_label(ticker, portfolio[ticker])
    msg = bot.reply_to(
        message,
        f"לא הצלחתי לקבל מחיר עבור {label}.\n"
        "כתוב את מחיר המכירה ליחידה בשקלים (לא באגורות) כדי לכלול גם אותו במכירת הכול, או כתוב <b>ביטול</b>.",
    )
    bot.register_next_step_handler(
        msg, _manual_sell_all_price_step, portfolio, prices, missing, index
    )


def _manual_sell_all_price_step(message, portfolio, prices, missing, index):
    if _redirect_if_menu_action(message):
        return
    text = (message.text or "").strip()
    if text in {"ביטול", "cancel", "Cancel"}:
        bot.reply_to(message, "מכירת הכול בוטלה.", reply_markup=main_menu())
        return
    try:
        price = float(text.replace(",", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        msg = bot.reply_to(message, "מחיר לא תקין. כתוב מספר גדול מאפס, או <b>ביטול</b>.")
        bot.register_next_step_handler(
            msg, _manual_sell_all_price_step, portfolio, prices, missing, index
        )
        return
    prices[missing[index]] = _stored_unit_price(missing[index], price)
    if index + 1 < len(missing):
        _ask_manual_sell_all_price(message, portfolio, prices, missing, index + 1)
        return
    _show_sell_all_confirmation(message, portfolio, prices)


def _show_sell_all_confirmation(message, portfolio, prices):
    uid = get_user_id(message)
    lines = [f"📤 <b>מכירת כל התיק — {len(portfolio)} החזקות:</b>"]
    sells = []
    total_proceeds = total_gain = total_tax = total_commission = 0.0
    for ticker, details in portfolio.items():
        qty = details.get("quantity", 0)
        buy_price = details.get("buy_price", 0)
        price = prices.get(ticker)
        label = _display_label(ticker, details)
        if price is None:
            bot.reply_to(message, f"❌ חסר מחיר עבור {label}; המכירה בוטלה כדי לא לדלג על נייר.", reply_markup=main_menu())
            return
        tax = _sale_estimate(uid, ticker, details, qty, price)
        sells.append((ticker, qty, price))
        total_proceeds += tax["proceeds"]
        total_gain += tax["gain"]
        total_tax += tax["estimated_tax"]
        total_commission += tax["sale_commission"]
        lines.append(
            f"🔹 {label}: {qty} יח' × {_display_unit_price(ticker, price):,.4f} = "
            f"{_gain_arrow(tax['gain'])} {tax['gain']:+.2f} ₪ · עמלה {tax['sale_commission']:.2f} ₪"
        )

    lines += [
        "",
        f"💰 סה\"כ תמורה: {total_proceeds:.2f} ₪",
        f"📊 סה\"כ רווח/הפסד: {total_gain:+.2f} ₪",
        f"🏦 סה\"כ עמלות מסחר צפויות: {total_commission:.2f} ₪",
        f"💸 מס משוער כולל (הערכה בלבד — 25% שטוח, לא ייעוץ מס): {total_tax:.2f} ₪",
        f"✅ נטו משוער אחרי עמלות ומס: {total_proceeds - total_commission - total_tax:.2f} ₪",
        "ℹ️ כל ההחזקות נכללות. המחירים נעולים לפעולה שמופיעה באישור זה.",
        "ℹ️ האישור מעדכן את FinPilot בלבד ואינו שולח הוראות לברוקר.",
        "\nלאשר ולעדכן את מכירת כל התיק? השב <b>כן</b> לאישור, או כל דבר אחר לביטול.",
    ]
    msg = bot.reply_to(message, "\n".join(lines))
    bot.register_next_step_handler(msg, _finish_sell_all, sells)


def _finish_sell_all(message, sells):
    if _redirect_if_menu_action(message):
        return
    if message.text.strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "המכירה בוטלה.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    sold = []
    portfolio = connect_firebase.get_portfolio(uid)
    total_cost = total_proceeds = total_gain = total_tax = total_commission = 0.0
    for ticker, qty, price in sells:
        existing = portfolio.get(ticker) or {}
        result = _sale_estimate(uid, ticker, existing, qty, price)
        try:
            connect_firebase.record_sell(uid, ticker, qty, price)
        except ValueError:
            continue  # holding changed since the quote — skip it, don't fail the whole batch
        try:
            _journal_sell(uid, ticker, qty, price, existing, gain=result["gain"])
        except Exception as exc:
            print(f"journal_sell failed for {uid}/{ticker}: {_safe_error(exc)}")
        sold.append(ticker)
        total_cost += result["cost"]
        total_proceeds += result["proceeds"]
        total_gain += result["gain"]
        total_tax += result["estimated_tax"]
        total_commission += result["sale_commission"]
    if sold:
        _refresh_valuation_snapshot_async(uid)
        profit_pct = (total_gain / total_cost * 100) if total_cost else 0.0
        bot.reply_to(
            message,
            "\n".join([
                f"✅ <b>נמכר כל התיק — {len(sold)} החזקות</b>",
                f"ניירות: {', '.join(sold)}",
                f"📥 עלות כוללת שנמכרה: {total_cost:.2f} ₪",
                f"💰 תמורה כוללת: {total_proceeds:.2f} ₪",
                f"{_gain_arrow(total_gain)} <b>רווח ממומש כולל: {total_gain:+.2f} ₪ ({profit_pct:+.1f}%)</b>",
                f"🏦 עמלות מסחר צפויות: {total_commission:.2f} ₪",
                f"💸 מס משוער כולל: {total_tax:.2f} ₪",
                f"✅ רווח נטו משוער אחרי עמלות ומס: {total_gain - total_commission - total_tax:+.2f} ₪",
                "ℹ️ הרישום אינו שולח הוראות מסחר לברוקר.",
            ]),
            reply_markup=main_menu(),
        )
    else:
        bot.reply_to(message, "❌ לא נמכר דבר — ההחזקות השתנו בינתיים. נסה /sell הכל שוב.", reply_markup=main_menu())


# --- סימולטור מס (בדיקת "מה אם" בלי למכור בפועל) ---
@bot.message_handler(commands=["tax"])
def tax_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    if not portfolio:
        bot.reply_to(message, "התיק ריק — אין מה לסמלץ. נסה ➕ קניה חדשה קודם.", reply_markup=main_menu())
        return
    holdings_list = "\n".join(f"• {_display_label(t, d)}" for t, d in portfolio.items())
    msg = bot.reply_to(message, f"על איזה טיקר/נייר תרצה לסמלץ מכירה?\n\nההחזקות שלך:\n{holdings_list}")
    bot.register_next_step_handler(msg, tax_sim_step_ticker)


def tax_sim_step_ticker(message):
    if _redirect_if_menu_action(message):
        return
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    ticker = _resolve_ticker_or_reply(message, portfolio, message.text.strip())
    if ticker == "__AMBIGUOUS__":
        return
    if ticker is None:
        bot.reply_to(message, "לא מצאתי את זה בתיק. נסה /tax שוב עם טיקר או שם.", reply_markup=main_menu())
        return
    existing = portfolio[ticker]
    held_qty = existing.get("quantity", 0)
    label = _display_label(ticker, existing)
    msg = bot.reply_to(message, f"כמה יחידות מתוך {held_qty} של {label} תרצה לסמלץ למכירה? (מספר, או 'הכל')")
    bot.register_next_step_handler(msg, tax_sim_step_quantity, ticker, existing)


def tax_sim_step_quantity(message, ticker, existing):
    if _redirect_if_menu_action(message):
        return
    held_qty = existing.get("quantity", 0)
    text = message.text.strip()
    try:
        qty = held_qty if text in ("הכל", "all", "כל") else float(text)
    except ValueError:
        bot.reply_to(message, "כמות לא תקינה, נסה /tax שוב.", reply_markup=main_menu())
        return
    if qty <= 0 or qty > held_qty:
        bot.reply_to(message, f"❌ יש לך רק {held_qty} {_display_label(ticker, existing)}.", reply_markup=main_menu())
        return
    current_price = price_service.get_current_price(ticker)
    price_scale = 0.01 if finance_engine.has_known_instrument(ticker) else 1.0
    displayed_current = current_price * price_scale if current_price is not None else None
    cached = portfolio_service.get_cached_portfolio_valuation(get_user_id(message)) or {}
    priced = (cached.get("holdings") or {}).get(ticker) or {}
    currency = "₪" if finance_engine.has_known_instrument(ticker) else (priced.get("quote_currency") or "מטבע המסחר")
    price_hint = f" (מחיר נוכחי: {displayed_current:,.4f} {currency})" if displayed_current else ""
    msg = bot.reply_to(message, f"באיזה מחיר תרצה לסמלץ מכירה ליחידה, ב{currency}?{price_hint} (מספר, או 'נוכחי')")
    bot.register_next_step_handler(msg, tax_sim_step_price, ticker, existing, qty, current_price, price_scale)


def tax_sim_step_price(message, ticker, existing, qty, current_price, price_scale=1.0):
    if _redirect_if_menu_action(message):
        return
    text = message.text.strip()
    try:
        if text in ("נוכחי", "current") and current_price:
            sell_price = current_price
        else:
            # The user always enters shekels; convert back to the source quote
            # only for the internal tax calculation of agorot-priced funds.
            sell_price = float(text) / price_scale
    except ValueError:
        bot.reply_to(message, "מחיר לא תקין, נסה /tax שוב.", reply_markup=main_menu())
        return
    if sell_price is None:
        bot.reply_to(message, "לא הצלחתי לקבל מחיר נוכחי — הזן מחיר ידנית עם /tax.", reply_markup=main_menu())
        return

    tax = _sale_estimate(get_user_id(message), ticker, existing, qty, sell_price)
    lines = [
        f"🧮 <b>הדמיית מכירה, עמלה ומס — {_display_label(ticker, existing)}</b>",
        f"{qty} יח' × {sell_price * price_scale:,.4f} = תמורה ברוטו {tax['proceeds']:.2f} ₪",
        f"עלות מקורית: {tax['cost']:.2f} ₪",
        f"🏦 עמלת מסחר צפויה: {tax['sale_commission']:.2f} ₪",
        f"   {html.escape(tax['commission_label'])}",
        f"{_gain_arrow(tax['gain'])} רווח/הפסד לפני עמלה: {tax['gain']:+.2f} ₪",
        f"📉 רווח חייב משוער לאחר עמלת המכירה: {tax['taxable_gain']:+.2f} ₪",
        f"💸 מס משוער (25% שטוח, הערכה בלבד — לא ייעוץ מס): {tax['estimated_tax']:.2f} ₪",
        f"✅ נטו משוער אחרי עמלה ומס: {tax['net_after_tax_and_fees']:.2f} ₪",
        "\nזו רק סימולציה — שום דבר לא נמכר בפועל. למכירה אמיתית: /sell",
    ]
    bot.reply_to(message, "\n".join(lines), reply_markup=main_menu())


# --- מזומן פנוי בחשבון ---
def _cash_help_text(balance):
    return (
        f"💵 <b>יתרת המזומן הפנוי:</b> {balance:.2f}\n\n"
        "אפשרויות:\n"
        "<code>/deposit 1000</code> — הפקדת מזומן\n"
        "<code>/withdraw 250</code> — משיכת מזומן\n"
        "<code>/cash set 5000</code> — קביעת יתרה מדויקת"
    )


def _apply_cash_text(message, text):
    uid = get_user_id(message)
    parts = str(text or "").strip().split()
    if not parts:
        bot.reply_to(message, _cash_help_text(connect_firebase.get_cash_balance(uid)), reply_markup=main_menu())
        return
    try:
        if parts[0].lower() == "set" and len(parts) == 2:
            balance = connect_firebase.set_cash_balance(uid, parts[1])
        elif parts[0].lower() in {"deposit", "+", "הפקדה"} and len(parts) == 2:
            balance = connect_firebase.adjust_cash_balance(uid, abs(float(parts[1])))
        elif parts[0].lower() in {"withdraw", "-", "משיכה"} and len(parts) == 2:
            balance = connect_firebase.adjust_cash_balance(uid, -abs(float(parts[1])))
        elif len(parts) == 1 and parts[0].startswith(("+", "-")):
            balance = connect_firebase.adjust_cash_balance(uid, float(parts[0]))
        else:
            raise ValueError("פורמט לא תקין.")
    except (ValueError, TypeError) as e:
        bot.reply_to(message, f"❌ {html.escape(str(e))}\n\n{_cash_help_text(connect_firebase.get_cash_balance(uid))}", reply_markup=main_menu())
        return
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(message, f"✅ יתרת המזומן עודכנה ל־{balance:.2f}", reply_markup=main_menu())


@bot.message_handler(commands=["cash"])
def cash_command(message):
    if not _require_auth(message):
        return
    args = message.text.split(maxsplit=1) if (message.text or "").startswith("/") else []
    if len(args) == 2:
        _apply_cash_text(message, args[1])
        return
    uid = get_user_id(message)
    msg = bot.reply_to(
        message,
        _cash_help_text(connect_firebase.get_cash_balance(uid))
        + "\n\nאפשר גם לכתוב עכשיו למשל <code>+ 1000</code>, <code>- 250</code> או <code>set 5000</code>.",
    )
    bot.register_next_step_handler(msg, cash_step)


def cash_step(message):
    if _redirect_if_menu_action(message):
        return
    _apply_cash_text(message, message.text)


@bot.message_handler(commands=["deposit"])
def deposit_command(message):
    if not _require_auth(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        bot.reply_to(message, "שימוש: <code>/deposit 1000</code>", reply_markup=main_menu())
        return
    _apply_cash_text(message, f"deposit {args[1]}")


@bot.message_handler(commands=["withdraw"])
def withdraw_command(message):
    if not _require_auth(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        bot.reply_to(message, "שימוש: <code>/withdraw 250</code>", reply_markup=main_menu())
        return
    _apply_cash_text(message, f"withdraw {args[1]}")


# --- קופות גמל ומכשירים פיננסיים נוספים ---
_SAVINGS_TRACK_BUTTONS = {
    "אלטשולר שחם חיסכון פלוס מניות (7799)": "7799",
    "אלטשולר שחם עוקב מדדי מניות (14864)": "14864",
}


def _format_savings_message(assets: dict) -> str:
    if not assets:
        return "🏦 עדיין לא הוספת קופת גמל או חיסכון. לחץ ➕ מכשיר פיננסי כדי להתחיל."
    lines = ["🏦 <b>קופות וחסכונות</b>"]
    total = 0.0
    for asset in assets.values():
        balance = float(asset.get("estimated_balance", asset.get("reported_balance", 0)) or 0)
        total += balance
        lines.append(f"\n🔸 <b>{html.escape(str(asset.get('name') or 'מכשיר פיננסי'))}</b>")
        lines.append(f"צבירה שהוזנה: {float(asset.get('reported_balance', 0) or 0):,.2f} ₪ נכון ל־{asset.get('balance_as_of', 'לא ידוע')}")
        lines.append(f"צבירה משוערת: {balance:,.2f} ₪")
        if asset.get("estimated_gain_loss") is not None:
            lines.append(
                f"{_gain_arrow(asset['estimated_gain_loss'])} רווח/הפסד משוער {asset.get('estimate_period_label', '')}: "
                f"{asset['estimated_gain_loss']:+,.2f} ₪"
            )
        if asset.get("monthly_return_pct") is not None:
            lines.append(
                f"תשואת חודש הדיווח {asset.get('latest_report_period', '')}: "
                f"{float(asset['monthly_return_pct']):+.2f}%"
            )
        if asset.get("return_12m_pct") is not None:
            lines.append(f"תשואה ב־12 חודשי הדיווח האחרונים: {float(asset['return_12m_pct']):+.2f}%")
        lines.append("מקור: נתונים ציבוריים חודשיים של רשות שוק ההון; היתרה האישית באזור אלטשולר היא הקובעת.")
    lines.append(f"\n💼 סך קופות וחסכונות משוער: {total:,.2f} ₪")
    return "\n".join(lines)


@bot.message_handler(commands=["asset"])
def financial_asset_command(message):
    if not _require_auth(message):
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for label in _SAVINGS_TRACK_BUTTONS:
        markup.row(label)
    markup.row("ביטול")
    msg = bot.reply_to(
        message,
        "➕ <b>הוספת מכשיר פיננסי</b>\nבחר את מסלול קופת הגמל להשקעה:",
        reply_markup=markup,
    )
    bot.register_next_step_handler(msg, _financial_asset_track_step)


def _financial_asset_track_step(message):
    if _redirect_if_menu_action(message):
        return
    text = (message.text or "").strip()
    if text == "ביטול":
        bot.reply_to(message, "ההוספה בוטלה.", reply_markup=main_menu())
        return
    track_id = _SAVINGS_TRACK_BUTTONS.get(text)
    if not track_id and text in savings_service.TRACKS:
        track_id = text
    if not track_id:
        bot.reply_to(message, "לא זיהיתי את המסלול. נסה שוב דרך ➕ מכשיר פיננסי.", reply_markup=main_menu())
        return
    msg = bot.reply_to(
        message,
        "מה הצבירה שמופיעה כרגע באזור האישי של אלטשולר שחם?\nשלח סכום בשקלים, למשל <code>25000</code>.",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, _financial_asset_balance_step, track_id)


def _financial_asset_balance_step(message, track_id):
    if _redirect_if_menu_action(message):
        return
    try:
        balance = float(str(message.text or "").replace(",", ""))
        if balance <= 0:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "הצבירה חייבת להיות מספר גדול מאפס. התחל שוב דרך ➕ מכשיר פיננסי.", reply_markup=main_menu())
        return
    msg = bot.reply_to(
        message,
        "כמה הפקדת לקופה בסך הכול? הנתון מאפשר לחשב רווח/הפסד אישי.\nשלח סכום, או כתוב <code>לא יודע</code>.",
    )
    bot.register_next_step_handler(msg, _financial_asset_contributed_step, track_id, balance)


def _financial_asset_contributed_step(message, track_id, balance):
    if _redirect_if_menu_action(message):
        return
    text = (message.text or "").strip().replace(",", "")
    if text in {"לא יודע", "לא ידוע", "skip", "דלג"}:
        contributed = 0.0
    else:
        try:
            contributed = float(text)
            if contributed < 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "סכום ההפקדות אינו תקין. התחל שוב דרך ➕ מכשיר פיננסי.", reply_markup=main_menu())
            return
    msg = bot.reply_to(
        message,
        "מה ההפקדה החודשית הקבועה? שלח סכום, או <code>0</code> אם אין/לא ידוע.",
    )
    bot.register_next_step_handler(msg, _financial_asset_monthly_step, track_id, balance, contributed)


def _financial_asset_monthly_step(message, track_id, balance, contributed):
    if _redirect_if_menu_action(message):
        return
    try:
        monthly = float(str(message.text or "").replace(",", ""))
        if monthly < 0:
            raise ValueError
        uid = get_user_id(message)
        connect_firebase.add_financial_asset(
            uid, track_id, balance, date.today().isoformat(), monthly, contributed, True
        )
        assets = connect_firebase.refresh_user_financial_assets(uid, force=True)
        _refresh_valuation_snapshot_async(uid)
    except Exception as exc:
        bot.reply_to(message, f"❌ לא הצלחתי לשמור את המכשיר: {html.escape(_safe_error(exc))}", reply_markup=main_menu())
        return
    bot.reply_to(message, "✅ המכשיר הפיננסי נוסף ויסתנכרן לפי דיווחים חודשיים.\n\n" + _format_savings_message(assets), reply_markup=main_menu())


@bot.message_handler(commands=["savings"])
def savings_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    try:
        assets = connect_firebase.refresh_user_financial_assets(uid)
    except Exception:
        assets = connect_firebase.list_financial_assets(uid)
    bot.reply_to(message, _format_savings_message(assets), reply_markup=main_menu())


def _goal_message(valuation: dict) -> str:
    goal = valuation.get("financial_goal") or {}
    target = float(goal.get("target_amount", 0) or 0)
    current = float(valuation.get("total_financial_value", valuation.get("account_total_value", 0)) or 0)
    if target <= 0:
        return f"🎯 עדיין לא הוגדר יעד. השווי הפיננסי הנוכחי הוא {current:,.2f} ₪."
    percent = min(100.0, current / target * 100)
    filled = min(20, round(percent / 5))
    bar = "█" * filled + "░" * (20 - filled)
    remaining = max(target - current, 0)
    return (
        f"🎯 <b>{html.escape(str(goal.get('name') or 'היעד הפיננסי שלי'))}</b>\n"
        f"{bar} {percent:.1f}%\n"
        f"נוכחי: {current:,.2f} ₪ מתוך {target:,.2f} ₪\n"
        f"נותרו ליעד: {remaining:,.2f} ₪"
    )


@bot.message_handler(commands=["goal"])
def goal_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    parts = (message.text or "").split(maxsplit=1) if (message.text or "").startswith("/") else []
    if len(parts) == 2:
        try:
            connect_firebase.set_financial_goal(uid, str(parts[1]).replace(",", ""))
        except ValueError as exc:
            bot.reply_to(message, f"❌ {html.escape(str(exc))}", reply_markup=main_menu())
            return
        valuation = _get_fast_valuation(uid)[0]
        bot.reply_to(message, "✅ היעד נשמר.\n\n" + _goal_message(valuation), reply_markup=main_menu())
        return
    valuation = _get_fast_valuation(uid)[0]
    msg = bot.reply_to(
        message,
        _goal_message(valuation) + "\n\nלאיזה סכום תרצה להגיע? שלח מספר בשקלים, למשל <code>30000</code>.",
    )
    bot.register_next_step_handler(msg, _goal_amount_step)


def _goal_amount_step(message):
    if _redirect_if_menu_action(message):
        return
    uid = get_user_id(message)
    try:
        connect_firebase.set_financial_goal(uid, str(message.text or "").replace(",", ""))
        valuation = _get_fast_valuation(uid)[0]
    except ValueError as exc:
        bot.reply_to(message, f"❌ {html.escape(str(exc))}", reply_markup=main_menu())
        return
    bot.reply_to(message, "✅ היעד נשמר.\n\n" + _goal_message(valuation), reply_markup=main_menu())


# --- התאמה אישית ---
_PROFILE_LABELS = {
    "conservative": "שמרני",
    "balanced": "מאוזן",
    "aggressive": "אגרסיבי",
    "short": "קצר",
    "medium": "בינוני",
    "long": "ארוך",
}


@bot.message_handler(commands=["profile"])
def profile_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    args = message.text.split(maxsplit=2) if (message.text or "").startswith("/") else []
    if len(args) >= 3:
        field_aliases = {
            "name": "display_name", "risk": "risk_profile", "horizon": "investment_horizon",
            "goal": "investment_goal", "currency": "base_currency",
        }
        field = field_aliases.get(args[1].lower())
        if not field:
            bot.reply_to(message, "שדה לא מוכר. השתמש ב־name/risk/horizon/goal/currency.", reply_markup=main_menu())
            return
        try:
            connect_firebase.update_user_profile(uid, {field: args[2]})
        except ValueError as e:
            bot.reply_to(message, f"❌ {html.escape(str(e))}", reply_markup=main_menu())
            return

    profile = connect_firebase.get_user_profile(uid)
    bot.reply_to(
        message,
        "⚙️ <b>הפרופיל האישי שלך</b>\n"
        f"שם: {html.escape(profile.get('display_name') or 'לא הוגדר')}\n"
        f"סיכון: {_PROFILE_LABELS.get(profile.get('risk_profile'), profile.get('risk_profile'))}\n"
        f"טווח: {_PROFILE_LABELS.get(profile.get('investment_horizon'), profile.get('investment_horizon'))}\n"
        f"מטרה: {html.escape(profile.get('investment_goal') or '')}\n"
        f"מטבע בסיס: {profile.get('base_currency', 'ILS')}\n\n"
        "עדכון לדוגמה:\n"
        "<code>/profile name Oriel</code>\n"
        "<code>/profile risk balanced</code>\n"
        "<code>/profile horizon long</code>\n"
        "<code>/profile goal growth and income</code>",
        reply_markup=main_menu(),
    )


# --- ניתוח פונדמנטלי של מניה או קרן ---
def _build_fundamental_analysis(uid, query):
    analysis = fundamental_service.analyze_asset(query)
    profile = connect_firebase.get_user_profile(uid)
    analysis["entry_guidance"] = fundamental_service.build_entry_guidance(analysis, profile)
    market_context = ai_recommendation.search_market_context([
        (analysis["symbol"], analysis.get("name"))
    ])
    analysis["ai"] = ai_recommendation.generate_fundamental_recommendation(
        analysis, market_context, profile
    )
    connect_firebase.save_fundamental_analysis(uid, analysis)
    return analysis


def _send_long_text(chat_id, text, reply_markup=None):
    text = str(text)
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]
    for index, chunk in enumerate(chunks):
        bot.send_message(
            chat_id,
            html.escape(chunk),
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


def _run_analysis_for_message(message, query):
    uid = get_user_id(message)
    thinking = bot.reply_to(message, "🔎 אוסף נתונים פונדמנטליים, ביצועים וחדשות — ואז מבצע בדיקת AI שנייה...")
    try:
        analysis = _build_fundamental_analysis(uid, query)
        report = ai_recommendation.format_fundamental_report(analysis)
    except Exception as e:
        report = f"❌ לא הצלחתי לנתח את הנכס כרגע: {_safe_error(e)}"
    try:
        bot.delete_message(message.chat.id, thinking.message_id)
    except Exception:
        pass
    _send_long_text(message.chat.id, report, reply_markup=main_menu())


@bot.message_handler(commands=["analyze"])
def analyze_command(message):
    if not _require_auth(message):
        return
    args = message.text.split(maxsplit=1) if (message.text or "").startswith("/") else []
    if len(args) == 2 and args[1].strip():
        threading.Thread(
            target=_run_analysis_for_message,
            args=(message, args[1].strip()),
            daemon=True,
        ).start()
        return
    msg = bot.reply_to(message, "איזו מניה או קרן לנתח? אפשר לכתוב טיקר או שם, למשל AAPL או SPY.")
    bot.register_next_step_handler(msg, analyze_step)


def analyze_step(message):
    if _redirect_if_menu_action(message):
        return
    threading.Thread(
        target=_run_analysis_for_message,
        args=(message, message.text.strip()),
        daemon=True,
    ).start()


def _run_structured_recommendation_for_message(message, ticker):
    """Builds a structured BUY/WAIT/PASS card (portfolio_context +
    fundamental_service + technical_service, two-pass verified) and, only
    for a BUY, attaches Approve/Reject inline buttons. Approve never buys
    anything — it only records a thesis for manual, ongoing tracking."""
    uid = get_user_id(message)
    ticker = ticker.strip().upper()
    thinking = bot.reply_to(
        message,
        f"🧮 בונה המלצה מובנית עבור {ticker}: נתונים פונדמנטליים, טכניים, חדשות וגודל פוזיציה מותאם לתיק שלך...",
    )
    markup = None
    try:
        context = portfolio_context.build_context(uid)
        technical = technical_service.get_technical_analysis(ticker)
        fundamental = fundamental_service.analyze_asset(ticker)
        market_context = ai_recommendation.search_market_context([(ticker, fundamental.get("name"))])
        rec = ai_recommendation.generate_structured_recommendation(
            context, ticker, fundamental, technical, market_context,
        )
        connect_firebase.save_fundamental_analysis(uid, {**rec, "type": "structured_recommendation"})
        try:
            connect_firebase.add_journal_entry(
                uid, "RECOMMENDATION", ticker=ticker, recommended_action=rec.get("action"), recommendation_snapshot=rec,
            )
        except Exception as exc:
            print(f"journal RECOMMENDATION failed for {uid}/{ticker}: {_safe_error(exc)}")
        card = ai_recommendation.format_structured_recommendation_card(rec)
        if rec.get("action") == "BUY":
            pending_id = thesis_service.save_pending_recommendation(uid, rec)
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Approve BUY", callback_data=f"rec_approve:{pending_id}"),
                types.InlineKeyboardButton("❌ Reject", callback_data=f"rec_reject:{pending_id}"),
            )
    except Exception as e:
        card = f"❌ לא הצלחתי להכין המלצה מובנית: {_safe_error(e)}"
    try:
        bot.delete_message(message.chat.id, thinking.message_id)
    except Exception:
        pass
    bot.send_message(message.chat.id, card, reply_markup=markup if markup else main_menu())


@bot.message_handler(commands=["recommend"])
def recommend_command(message):
    if not _require_auth(message):
        return
    args = message.text.split(maxsplit=1) if (message.text or "").startswith("/") else []
    if len(args) == 2 and args[1].strip():
        threading.Thread(
            target=_run_structured_recommendation_for_message,
            args=(message, args[1].strip()),
            daemon=True,
        ).start()
        return
    msg = bot.reply_to(message, "עבור איזה טיקר להכין המלצה מובנית (BUY/WAIT/PASS)? למשל AAPL.")
    bot.register_next_step_handler(msg, recommend_step)


def recommend_step(message):
    if _redirect_if_menu_action(message):
        return
    threading.Thread(
        target=_run_structured_recommendation_for_message,
        args=(message, message.text.strip()),
        daemon=True,
    ).start()


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("rec_approve:") or call.data.startswith("rec_reject:")
)
def handle_structured_recommendation_callback(call):
    """Approve only records a thesis for tracking — it never places a trade.
    Reject just logs the decision. Both remove the inline buttons afterward
    so the same recommendation can't be actioned twice."""
    uid = str(call.from_user.id)
    action, _, pending_id = call.data.partition(":")
    pending = thesis_service.get_pending_recommendation(pending_id)
    if not pending or pending.get("user_id") != uid or pending.get("status") != "pending":
        bot.answer_callback_query(call.id, "ההמלצה כבר טופלה או שפג תוקפה.")
        return
    recommendation = pending.get("recommendation") or {}
    try:
        if action == "rec_approve":
            context = portfolio_context.build_context(uid)
            thesis = thesis_service.create_thesis(uid, recommendation, context)
            connect_firebase.add_journal_entry(
                uid, "BUY_APPROVED",
                ticker=recommendation.get("ticker"),
                thesis_id=thesis.get("id"),
                price=recommendation.get("current_price"),
                position_size_ils=recommendation.get("position_size_ils"),
                recommendation_snapshot=recommendation,
                portfolio_snapshot=context,
                risk_profile_snapshot=context.get("risk_profile"),
            )
            thesis_service.resolve_pending_recommendation(pending_id, "approved")
            bot.answer_callback_query(call.id, "אושר ✅")
            confirmation = (
                f"✅ אושר — נוצר תיעוד (thesis) עבור {recommendation.get('ticker')} למעקב.\n"
                "⚠️ לא בוצעה כל קנייה בפועל. יש לבצע את הקנייה ידנית אצל הברוקר, "
                "ואז לתעד אותה בבוט (למשל דרך ➕ קניה חדשה)."
            )
        else:
            connect_firebase.add_journal_entry(
                uid, "BUY_REJECTED",
                ticker=recommendation.get("ticker"),
                recommendation_snapshot=recommendation,
            )
            thesis_service.resolve_pending_recommendation(pending_id, "rejected")
            bot.answer_callback_query(call.id, "נדחה ❌")
            confirmation = f"❌ ההמלצה עבור {recommendation.get('ticker')} נדחתה."
    except Exception as e:
        bot.answer_callback_query(call.id, "שגיאה בעיבוד הבקשה.")
        confirmation = f"⚠️ אירעה שגיאה: {_safe_error(e)}"
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, confirmation)


def _build_deep_portfolio_analysis(uid):
    """Values the account and screens every holding before the two-pass AI audit."""
    valuation = portfolio_service.get_portfolio_valuation(uid)
    if not valuation["holdings"] and not valuation.get("financial_assets"):
        raise ValueError("התיק ריק כרגע. הוסף החזקה או מכשיר פיננסי לפני הפעלת מצב חשיבה.")

    analyses = []
    failed_symbols = []
    symbols = list(valuation["holdings"])
    worker_count = min(4, len(symbols))
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
                print(f"Deep-analysis screening failed for {symbol}: {_safe_error(exc)}")
                failed_symbols.append(symbol)

    holdings_for_search = [
        (symbol, holding.get("name"))
        for symbol, holding in valuation["holdings"].items()
    ]
    market_context = ai_recommendation.search_market_context(holdings_for_search)
    profile = connect_firebase.get_user_profile(uid)
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
    connect_firebase.save_valuation_snapshot(uid, valuation)
    return stored_result


def _run_thinking_for_message(message):
    uid = get_user_id(message)
    progress = bot.reply_to(
        message,
        "🧠 מצב חשיבה התחיל: אני מתמחר את התיק, מנתח כל החזקה, בודק ריכוזיות, "
        "מזומן, התאמה לפרופיל וחדשות — ואז מעביר הכול לבדיקת AI שנייה. זה עשוי לקחת דקה.",
    )
    try:
        result = _build_deep_portfolio_analysis(uid)
        report = ai_recommendation.format_deep_portfolio_report(
            result,
            result.get("analyzed_count", 0),
            result.get("failed_symbols", []),
        )
    except Exception as e:
        print(f"Deep portfolio analysis failed for {uid}: {_safe_error(e)}")
        report = f"❌ מצב החשיבה לא הושלם: {_safe_error(e)}"
    try:
        bot.delete_message(message.chat.id, progress.message_id)
    except Exception:
        pass
    _send_long_text(message.chat.id, report, reply_markup=main_menu())


@bot.message_handler(commands=["think"])
def thinking_command(message):
    if not _require_auth(message):
        return
    threading.Thread(target=_run_thinking_for_message, args=(message,), daemon=True).start()


@bot.message_handler(func=lambda m: m.text == "🧠 מצב חשיבה")
def menu_thinking(message):
    thinking_command(message)


# --- ייבוא תיק (אקסל / תמונה) ---
@bot.message_handler(commands=["import"])
def import_command(message):
    if not _require_auth(message):
        return
    bot.reply_to(
        message,
        "ניתן לייבא תיק שלם בשתי דרכים:\n"
        "📊 שלח קובץ אקסל (.xlsx) עם עמודות: Ticker/טיקר, Quantity/כמות, Price/מחיר\n"
        "📷 שלח צילום מסך של התיק שלך (מברוקר או כל מקום אחר)\n\n"
        "אחרי הניתוח תוצג לך רשימת ההחזקות לאישור לפני שמירה.",
        reply_markup=main_menu(),
    )


@bot.message_handler(content_types=["document"])
def handle_document_import(message):
    if not _require_auth(message):
        return
    filename = message.document.file_name or ""
    if not filename.lower().endswith(".xlsx"):
        bot.reply_to(message, "אני תומך רק בקבצי Excel (.xlsx). לעזרה שלח /import", reply_markup=main_menu())
        return
    if getattr(message.document, "file_size", 0) > 10 * 1024 * 1024:
        bot.reply_to(message, "הקובץ גדול מדי. הגודל המרבי הוא 10MB.", reply_markup=main_menu())
        return

    bot.reply_to(message, "📊 קורא את הקובץ...")
    try:
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        holdings = portfolio_import.parse_excel_holdings(file_bytes)
    except Exception as e:
        print(f"Excel import failed for {get_user_id(message)}: {_safe_error(e)}")
        user_error = str(e) if isinstance(e, ValueError) else (
            "שירות ניתוח הקובץ אינו זמין כרגע. נסה שוב בעוד דקה, "
            "או ודא שיש בקובץ עמודות נייר, כמות ומחיר קנייה."
        )
        bot.reply_to(message, f"❌ שגיאה בייבוא הקובץ: {html.escape(user_error)}", reply_markup=main_menu())
        return

    _show_import_preview(message, holdings)


@bot.message_handler(content_types=["photo"])
def handle_photo_import(message):
    if not _require_auth(message):
        return
    bot.reply_to(message, "📷 מנתח את התמונה...")
    try:
        largest_photo = message.photo[-1]
        file_info = bot.get_file(largest_photo.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        holdings = portfolio_import.parse_image_holdings(file_bytes)
    except Exception as e:
        bot.reply_to(message, f"❌ שגיאה בזיהוי התמונה: {html.escape(_safe_error(e))}", reply_markup=main_menu())
        return

    _show_import_preview(message, holdings)


def _show_import_preview(message, holdings):
    if not holdings:
        bot.reply_to(message, "לא זוהו החזקות תקינות. נסה קובץ/תמונה ברורים יותר.", reply_markup=main_menu())
        return

    uid = get_user_id(message)
    sync_plan = connect_firebase.preview_portfolio_sync(
        connect_firebase.get_portfolio(uid), holdings
    )
    counts = sync_plan["counts"]
    lines = [
        "📋 <b>תצוגה מקדימה של סנכרון התיק:</b>",
        f"➕ חדשות: {counts['added']} · 🔄 השתנו: {counts['updated']} · "
        f"➖ לא מופיעות יותר: {counts['removed']} · ✅ ללא שינוי: {counts['unchanged']}",
    ]
    total_cost = 0.0
    for h in holdings:
        label = _display_label(h["ticker"], h)
        is_agorot = finance_engine.has_known_instrument(h["ticker"])
        unit_scale = 0.01 if is_agorot else 1.0
        position_cost = float(h.get("reported_total_cost") or (
            float(h["quantity"]) * float(h["buy_price"]) * unit_scale
        ))
        total_cost += position_cost
        base_price = f"{float(h['buy_price']) * unit_scale:,.2f} ₪" if is_agorot else f"{h['buy_price']:,.2f}"
        lines.append(
            f"🔹 {label}: {h['quantity']:,.4g} יחידות | מחיר בסיס ליחידה: "
            f"{base_price} | עלות כספית: {position_cost:,.2f}"
        )
    if sync_plan["updated"]:
        lines.append("\n🔄 <b>שינויים שזוהו:</b>")
        for item in sync_plan["updated"][:8]:
            direction = "נוספו יחידות" if item["quantity_delta"] > 0 else "נגרעו יחידות"
            lines.append(
                f"• {html.escape(str(item['name']))}: {item['old_quantity']:,.4g} ← "
                f"{item['new_quantity']:,.4g} ({direction})"
            )
    if sync_plan["removed"]:
        lines.append("\n➖ <b>לא מופיעים בקובץ החדש — יסומנו כנמכרו/הוסרו:</b>")
        for item in sync_plan["removed"][:8]:
            lines.append(f"• {html.escape(str(item['name']))} ({item['old_quantity']:,.4g} יחידות)")
    lines.append(f"\nעלות בסיס כוללת בקובץ: {total_cost:,.2f} ₪")
    lines.append(
        "⚠️ הקובץ הוא תמונת המצב החדשה: נייר קיים יעודכן, נייר חדש יתווסף, "
        "ונייר שאינו מופיע יותר יוסר. קופות וחסכונות לא יושפעו.\n"
        "לאשר ולייבא? השב <b>כן</b> לאישור, או כל דבר אחר לביטול."
    )

    msg = bot.reply_to(message, "\n".join(lines))
    bot.register_next_step_handler(msg, confirm_import_step, holdings)


def confirm_import_step(message, holdings):
    if _redirect_if_menu_action(message):
        return
    if message.text.strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "הייבוא בוטל. אפשר לנסות שוב עם /import.", reply_markup=main_menu())
        return

    uid = get_user_id(message)
    result = connect_firebase.sync_portfolio_from_import(uid, holdings)
    _refresh_valuation_snapshot_async(uid)
    counts = result["counts"]
    removed_names = ", ".join(str(item["name"]) for item in result["removed"][:8])
    removed_note = f"\n➖ הוסרו מהתיק: {html.escape(removed_names)}" if removed_names else ""

    bot.reply_to(
        message,
        "✅ <b>סנכרון התיק הושלם</b>\n"
        f"➕ נוספו: {counts['added']} · 🔄 עודכנו: {counts['updated']} · "
        f"➖ נמכרו/הוסרו: {counts['removed']} · ✅ ללא שינוי: {counts['unchanged']}"
        f"{removed_note}\nשלח /portfolio לצפייה בתמונה המעודכנת.",
        reply_markup=main_menu(),
    )


# --- עריכת מחיר בסיס / מחיר קנייה ממוצע ---
@bot.message_handler(commands=["editprice"])
def edit_price_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    if not portfolio:
        bot.reply_to(message, "התיק ריק — אין מחיר בסיס לערוך.", reply_markup=main_menu())
        return
    args = (message.text or "").split()[1:]
    if len(args) == 2:
        resolved = _resolve_ticker_or_reply(message, portfolio, args[0])
        if resolved and resolved != "__AMBIGUOUS__":
            _prepare_edit_price_confirm(message, resolved, args[1], portfolio)
        elif resolved is None:
            bot.reply_to(message, "לא מצאתי את הנייר בתיק.", reply_markup=main_menu())
        return
    lines = "\n".join(
        f"• {_display_label(ticker, details)} — מחיר בסיס נוכחי: {_display_unit_price(ticker, details.get('buy_price', 0) or 0):,.4f} ₪"
        for ticker, details in portfolio.items()
    )
    msg = bot.reply_to(
        message,
        "✏️ <b>איזה נייר תרצה לערוך?</b>\n"
        "אפשר לכתוב את שם הנייר או את מספרו.\n\n" + lines,
    )
    bot.register_next_step_handler(msg, _edit_price_ticker_step)


def _edit_price_ticker_step(message):
    if _redirect_if_menu_action(message):
        return
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    resolved = _resolve_ticker_or_reply(message, portfolio, (message.text or "").strip())
    if resolved == "__AMBIGUOUS__":
        return
    if resolved is None:
        bot.reply_to(message, "לא מצאתי את הנייר בתיק. נסה שוב דרך ✏️ עריכת מחיר בסיס.", reply_markup=main_menu())
        return
    current = _display_unit_price(resolved, portfolio[resolved].get("buy_price", 0) or 0)
    msg = bot.reply_to(
        message,
        f"מחיר הבסיס הנוכחי של {_display_label(resolved, portfolio[resolved])} הוא {current:,.2f}.\n"
        "מה מחיר הבסיס החדש ליחידה בשקלים (לא באגורות)?",
    )
    bot.register_next_step_handler(msg, _edit_price_value_step, resolved)


def _edit_price_value_step(message, ticker):
    if _redirect_if_menu_action(message):
        return
    portfolio = connect_firebase.get_portfolio(get_user_id(message))
    _prepare_edit_price_confirm(message, ticker, (message.text or "").strip(), portfolio)


def _prepare_edit_price_confirm(message, ticker, raw_price, portfolio=None):
    try:
        displayed_new_price = float(str(raw_price).replace(",", ""))
        if displayed_new_price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        bot.reply_to(message, "מחיר הבסיס חייב להיות מספר גדול מאפס.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    portfolio = portfolio or connect_firebase.get_portfolio(uid)
    details = portfolio.get(ticker)
    if not details:
        bot.reply_to(message, "הנייר כבר לא נמצא בתיק.", reply_markup=main_menu())
        return
    old_price = float(details.get("buy_price", 0) or 0)
    new_price = _stored_unit_price(ticker, displayed_new_price)
    quantity = float(details.get("quantity", 0) or 0)
    unit_scale = 0.01 if finance_engine.has_known_instrument(ticker) else 1.0
    msg = bot.reply_to(
        message,
        f"✏️ לעדכן את מחיר הבסיס של {_display_label(ticker, details)}?\n"
        f"מחיר ישן: {old_price * unit_scale:,.4f} ₪\n"
        f"מחיר חדש: {displayed_new_price:,.4f} ₪\n"
        f"עלות כספית חדשה: {quantity * new_price * unit_scale:,.2f} ₪\n\n"
        "כתוב <b>כן</b> לאישור. כל תשובה אחרת תבטל.",
    )
    bot.register_next_step_handler(msg, _confirm_edit_price, ticker, new_price)


def _confirm_edit_price(message, ticker, new_price):
    if _redirect_if_menu_action(message):
        return
    if (message.text or "").strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "העדכון בוטל ולא בוצע שינוי.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    try:
        result = connect_firebase.update_holding_buy_price(uid, ticker, new_price)
    except ValueError as exc:
        bot.reply_to(message, f"❌ {html.escape(str(exc))}", reply_markup=main_menu())
        return
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(
        message,
        f"✅ מחיר הבסיס עודכן מ־{_display_unit_price(ticker, result['old_price']):,.4f} ₪ "
        f"ל־{_display_unit_price(ticker, result['new_price']):,.4f} ₪. "
        "הכמות לא השתנתה והרווח/הפסד יחושב מחדש.",
        reply_markup=main_menu(),
    )


# --- הצגת תיק ---
@bot.message_handler(commands=["portfolio"])
def portfolio_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    valuation, from_cache = _get_fast_valuation(uid)
    if valuation["holdings"] and not from_cache:
        connect_firebase.save_valuation_snapshot(uid, valuation)
    cache_note = "\n\n⚡ הוצג מיד מהסנכרון האחרון; המחירים מתרעננים ברקע." if from_cache else ""
    bot.reply_to(message, format_portfolio_message(valuation) + cache_note, reply_markup=main_menu())


# --- עוגת ההשקעות ---
@bot.message_handler(commands=["cake"])
def cake_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    valuation, from_cache = _get_fast_valuation(uid)
    if not valuation["holdings"]:
        bot.reply_to(message, "התיק ריק — אין מה לצייר עדיין. נסה /buy קודם.", reply_markup=main_menu())
        return

    if not from_cache:
        connect_firebase.save_valuation_snapshot(uid, valuation)

    priced_holdings = {
        ticker: holding for ticker, holding in valuation["holdings"].items()
        if (holding.get("market_value") or 0) > 0
    }
    if not priced_holdings:
        bot.reply_to(
            message,
            "לא הצלחתי לקבל מחיר לאף החזקה, לכן לא ניתן לצייר תרשים אמין כרגע.\n"
            + format_portfolio_message(valuation),
            reply_markup=main_menu(),
        )
        return

    chart_buf = chart_service.generate_portfolio_pie_chart(priced_holdings)
    total_gain = valuation["total_gain_loss"]
    day_change = valuation.get("total_day_change_value", 0.0)
    day_arrow = "📈" if day_change > 0 else ("📉" if day_change < 0 else "➖")
    caption = (
        f"📈 שווי ניירות: {valuation['total_value']:.2f}\n"
        f"💵 מזומן פנוי: {valuation.get('cash_balance', 0):.2f}\n"
        f"💼 שווי חשבון כולל: {valuation.get('account_total_value', valuation['total_value']):.2f}\n"
        f"📥 עלות כוללת: {valuation['total_cost']:.2f}\n"
        f"{_gain_arrow(total_gain)} רווח/הפסד: {total_gain:+.2f} "
        f"({valuation['total_gain_loss_pct']:.1f}%)\n"
        f"{day_arrow} שינוי היום: {day_change:+.2f}"
    )
    if from_cache:
        caption += "\n⚡ מהסנכרון האחרון; רענון מחירים מתבצע ברקע."
    bot.send_photo(message.chat.id, photo=chart_buf, caption=caption, reply_markup=main_menu())


# --- אימייל לצורך התראות שבועיות ---
@bot.message_handler(commands=["email"])
def email_command(message):
    if not _require_auth(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) == 2:
        _finish_email(message, args[1])
    else:
        msg = bot.reply_to(message, "מה כתובת האימייל שלך? (לשליחת המלצות שבועיות)")
        bot.register_next_step_handler(msg, email_step)


def email_step(message):
    if _redirect_if_menu_action(message):
        return
    _finish_email(message, message.text)


def _finish_email(message, email):
    email = email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        bot.reply_to(message, "זו לא נראית כתובת אימייל תקינה, נסה /email שוב.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    connect_firebase.set_user_email(uid, email)
    bot.reply_to(message, f"✅ נשמר: {email} — משם תגיע ההמלצה השבועית.", reply_markup=main_menu())


# --- קישור לחשבון האתר ---
@bot.message_handler(commands=["link"])
def link_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    code, ttl_minutes = connect_firebase.create_link_code(uid)
    bot.reply_to(
        message,
        f"🔗 קוד החיבור שלך לאתר: <code>{code}</code>\n"
        f"הזן אותו במסך ההתחברות באתר תוך {ttl_minutes} דקות.",
        reply_markup=main_menu(),
    )


# --- תפריט ---
@bot.message_handler(func=lambda m: m.text == "📊 התיק שלי")
def menu_portfolio(message):
    portfolio_command(message)


@bot.message_handler(func=lambda m: m.text == "🥧 עוגת ההשקעות")
def menu_cake(message):
    cake_command(message)


@bot.message_handler(func=lambda m: m.text == "➕ קניה חדשה")
def menu_buy(message):
    buy_command(message)


@bot.message_handler(func=lambda m: m.text == "➕ מכשיר פיננסי")
def menu_financial_asset(message):
    financial_asset_command(message)


@bot.message_handler(func=lambda m: m.text == "📋 חסכונות וקופות")
def menu_savings(message):
    savings_command(message)


@bot.message_handler(func=lambda m: m.text == "🎯 יעד פיננסי")
def menu_goal(message):
    goal_command(message)


@bot.message_handler(func=lambda m: m.text == "➖ מכירה")
def menu_sell(message):
    sell_command(message)


@bot.message_handler(func=lambda m: m.text == "📥 ייבוא תיק")
def menu_import(message):
    import_command(message)


@bot.message_handler(func=lambda m: m.text == "🧮 סימולטור מס")
def menu_tax_sim(message):
    tax_command(message)


@bot.message_handler(func=lambda m: m.text == "💵 מזומן פנוי")
def menu_cash(message):
    cash_command(message)


@bot.message_handler(func=lambda m: m.text == "✏️ עריכת מחיר בסיס")
def menu_edit_price(message):
    edit_price_command(message)


@bot.message_handler(func=lambda m: m.text == "🔎 ניתוח מניה/קרן")
def menu_analyze(message):
    analyze_command(message)


@bot.message_handler(func=lambda m: m.text == "⚙️ התאמה אישית")
def menu_profile(message):
    profile_command(message)


@bot.message_handler(func=lambda m: m.text == "🤖 המלצת AI")
def menu_ai_recommendation(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    thinking = bot.reply_to(message, "🤖 מחפש חדשות עדכניות ברשת וחושב... (כמה שניות)")
    threading.Thread(
        target=_run_ai_recommendation_for_message,
        args=(message, uid, thinking),
        daemon=True,
    ).start()


def _run_ai_recommendation_for_message(message, uid, thinking):
    try:
        valuation, _ = _get_fast_valuation(uid)
        if not valuation["holdings"] and not valuation.get("financial_assets"):
            reply_text = "התיק ריק — אין על מה להמליץ עדיין. הוסף קנייה או מכשיר פיננסי קודם."
        else:
            holdings_for_search = [(t, h.get("name")) for t, h in valuation["holdings"].items()]
            market_context = ai_recommendation.search_market_context(holdings_for_search)
            profile = connect_firebase.get_user_profile(uid)
            recommendation = ai_recommendation.generate_recommendation(valuation, market_context, profile)
            reply_text = (
                "🤖 <b>המלצת AI מאומתת</b> <i>(חדשות עדכניות + בדיקת AI שנייה)</i>\n\n"
                f"{html.escape(recommendation)}\n\n"
                "💬 אפשר גם לשאול אותי כל שאלה על התיק שלך בהודעה חופשית."
            )
    except Exception as e:
        reply_text = f"❌ לא הצלחתי להפיק המלצה כרגע: {html.escape(_safe_error(e))}"

    try:
        bot.delete_message(message.chat.id, thinking.message_id)
    except Exception:
        pass
    bot.send_message(message.chat.id, reply_text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "🌐 חיבור לאתר")
def menu_link(message):
    link_command(message)


@bot.message_handler(func=lambda m: m.text == "❓ עזרה")
def menu_help(message):
    bot.reply_to(
        message,
        "🚀 <b>Finance Bot</b> — עוזר השקעות אישי, פרטי, ישר בטלגרם.\n\n"
        "פקודות זמינות:\n"
        "/buy - הוספת קנייה\n"
        "/sell - מכירת החזקה (כולל הערכת מס)\n"
        "/tax - סימולציית מס בלי למכור בפועל\n"
        "/cash - ניהול יתרת מזומן פנוי\n"
        "/asset - הוספת קופת גמל או מכשיר פיננסי\n"
        "/savings - הצגת קופות וחסכונות\n"
        "/goal 30000 - הגדרת יעד פיננסי ומד התקדמות\n"
        "/editprice - עריכת מחיר הבסיס בלי לשנות כמות\n"
        "/import - ייבוא תיק שלם מאקסל או תמונה\n"
        "/analyze AAPL - ניתוח פונדמנטלי של מניה או קרן\n"
        "/think - מצב חשיבה עמוקה על כל התיק\n"
        "/profile - התאמת רמת סיכון, טווח ומטרה\n"
        "/portfolio - הצגת התיק\n"
        "/cake - תרשים התיק\n"
        "/email - הגדרת אימייל להמלצות שבועיות\n"
        "/link - קבלת קוד חיבור לאתר\n\n"
        "💬 אפשר גם פשוט לכתוב לי בשפה חופשית — למשל \"קניתי 5 מניות של AAPL ב-150\" "
        "או \"כמה שווה התיק שלי\" — ואני אבין מה לעשות.",
        reply_markup=main_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "🚪 יציאה")
def menu_exit(message):
    authenticated_users.discard(get_user_id(message))
    bot.send_message(
        message.chat.id,
        "להתראות! שלח /start כדי לחזור.",
        reply_markup=types.ReplyKeyboardRemove()
    )


# --- נפילה חופשית ל-AI: כל הודעה שלא תאמה אף פקודה/כפתור למעלה ---
# telebot מנתב להנדלר הראשון שמתאים, לפי סדר הרישום — לכן זה חייב
# להיות ההנדלר האחרון שנרשם בקובץ.
def _ai_fallback_prompt(uid: str, user_text: str) -> str:
    portfolio = connect_firebase.get_portfolio(uid)
    holdings_summary = ", ".join(
        f"{_display_label(ticker, d)}: {d.get('quantity')} יח' (מחיר קנייה ממוצע {_display_unit_price(ticker, d.get('buy_price', 0) or 0):,.4f})"
        for ticker, d in portfolio.items()
    ) or "התיק ריק"

    return f"""אתה מנוע הכוונות המאובטח של Finance Bot, בוט טלגרם לניהול תיק השקעות אישי בעברית.
המשתמש שלח הודעה שלא תואמת אף פקודה מוכרת בבוט (לא /buy, /sell, /portfolio וכו').
אתה לא מבצע שום פעולה בעצמך — רק מבין את הכוונה ומחזיר JSON; הבוט עצמו יבצע את הפעולה,
ויציג למשתמש אישור לפני כל שינוי בכסף, בפרופיל, באימייל או בהחזקות. אל תמציא ערכים חסרים.

התיק הנוכחי של המשתמש (חלק מהניירות מזוהים במספר נייר בלבד, יחד עם השם שלהם בסוגריים): {holdings_summary}

הודעת המשתמש: "{user_text}"

החזר אך ורק JSON תקין (בלי טקסט נוסף, בלי הסברים), באחת מהצורות הבאות בלבד:
- קנייה: {{"action": "buy", "ticker": "...", "quantity": <מספר או null>, "price": <מספר או null>}}
- מכירה של החזקה ספציפית (ticker יכול להיות מספר הנייר או השם שלו כפי שמופיע למעלה): {{"action": "sell", "ticker": "...", "quantity": <מספר או null>, "price": <מספר או null>}}
- מכירה של כל התיק (כל ההחזקות): {{"action": "sell_all"}}
- הצגת התיק: {{"action": "portfolio"}}
- גרף/עוגת התיק: {{"action": "cake"}}
- שאלה מסוג "כמה מס אשלם אם אמכור" / בקשה לסימולציית מס (בלי למכור בפועל): {{"action": "tax"}}
- בקשה לנתח מניה או קרן מסוימת: {{"action": "analyze", "ticker": "..."}}
- מצב חשיבה/ניתוח מלא של כל התיק: {{"action": "deep_analysis"}}
- בקשה לסנכרן/לייבא מחדש את תיק המסחר מקובץ: {{"action": "import"}}
- מזומן (operation הוא show/set/deposit/withdraw): {{"action": "cash", "operation": "show", "amount": <מספר או null>}}
- הצגת קופות וחסכונות: {{"action": "savings"}}
- הוספת קופת גמל/מכשיר פיננסי: {{"action": "financial_asset"}}
- הגדרת יעד כספי: {{"action": "goal", "amount": <מספר או null>}}
- עריכת מחיר קנייה/מחיר בסיס של החזקה קיימת: {{"action": "edit_cost_basis", "ticker": "...", "price": <מספר או null>}}
- עדכון התאמה אישית. field חייב להיות display_name/risk_profile/investment_horizon/investment_goal/base_currency.
  risk_profile חייב להיות conservative/balanced/aggressive; investment_horizon חייב להיות short/medium/long;
  base_currency חייב להיות ILS/USD/EUR: {{"action": "profile", "field": "...", "value": "..."}}
- הגדרת אימייל: {{"action": "email", "email": "..."}}
- בקשת עזרה או רשימת יכולות: {{"action": "help"}}
- כל דבר אחר (שאלה על התיק, שאלה כללית, "מי אתה", בקשה לא ברורה): {{"action": "reply"}}"""


def _extract_json_text(raw: str) -> str:
    text = (raw or "").strip()
    if "<think>" in text:
        think_end = text.find("</think>")
        text = text[think_end + len("</think>"):].strip() if think_end != -1 else ""
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _ai_confirm_trade(message, action, parsed):
    ticker_query = parsed.get("ticker")
    if not ticker_query:
        bot.reply_to(message, "לא הבנתי איזה טיקר. נסה /buy או /sell.", reply_markup=main_menu())
        return

    if action == "sell":
        uid = get_user_id(message)
        portfolio = connect_firebase.get_portfolio(uid)
        resolved = _resolve_ticker_or_reply(message, portfolio, str(ticker_query))
        if resolved == "__AMBIGUOUS__":
            return
        if resolved is None:
            bot.reply_to(message, f"לא מצאתי '{ticker_query}' בתיק. נסה /sell שוב.", reply_markup=main_menu())
            return
        ticker = resolved
    else:
        ticker = str(ticker_query).strip().upper()

    quantity, price = parsed.get("quantity"), parsed.get("price")
    if quantity is not None and price is not None:
        try:
            quantity, price = float(quantity), float(price)
        except (TypeError, ValueError):
            quantity = price = None

    if action == "buy":
        if quantity is not None and price is not None:
            _finish_buy_confirm(message, ticker, quantity, price)
        else:
            msg = bot.reply_to(message, f"🤖 הבנתי שאתה רוצה לקנות {ticker}. כמה יחידות?")
            bot.register_next_step_handler(msg, buy_step_quantity, ticker)
    else:
        if quantity is not None and price is not None:
            _prepare_sell_confirm(message, ticker, quantity, price)
        else:
            msg = bot.reply_to(message, f"🤖 הבנתי שאתה רוצה למכור {ticker}. כמה יחידות?")
            bot.register_next_step_handler(msg, sell_step_quantity, ticker)


def _prepare_agent_cash_confirmation(message, operation, amount):
    operation = str(operation or "show").lower()
    if operation == "show":
        uid = get_user_id(message)
        bot.reply_to(message, _cash_help_text(connect_firebase.get_cash_balance(uid)), reply_markup=main_menu())
        return
    if operation not in {"set", "deposit", "withdraw"}:
        bot.reply_to(message, "לא הבנתי את פעולת המזומן. אפשר לבקש הפקדה, משיכה, קביעת יתרה או הצגת יתרה.", reply_markup=main_menu())
        return
    try:
        amount = float(amount)
        if amount < 0 or (operation != "set" and amount == 0):
            raise ValueError
    except (TypeError, ValueError):
        bot.reply_to(message, "חסר סכום מזומן תקין. לדוגמה: ״הפקד 500״.", reply_markup=main_menu())
        return
    labels = {"set": "לקבוע את יתרת המזומן", "deposit": "להפקיד", "withdraw": "למשוך"}
    msg = bot.reply_to(
        message,
        f"🤖 הבנתי שביקשת {labels[operation]} בסך {amount:.2f}.\n"
        "כדי לבצע כתוב <b>כן</b>. כל תשובה אחרת תבטל.",
    )
    bot.register_next_step_handler(msg, _confirm_agent_cash, operation, amount)


def _confirm_agent_cash(message, operation, amount):
    if _redirect_if_menu_action(message):
        return
    if (message.text or "").strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "הפעולה בוטלה ולא בוצע שינוי במזומן.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    try:
        if operation == "set":
            balance = connect_firebase.set_cash_balance(uid, amount)
        elif operation == "deposit":
            balance = connect_firebase.adjust_cash_balance(uid, amount)
        else:
            balance = connect_firebase.adjust_cash_balance(uid, -amount)
    except (ValueError, TypeError) as e:
        bot.reply_to(message, f"❌ {html.escape(str(e))}", reply_markup=main_menu())
        return
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(message, f"✅ הפעולה בוצעה. יתרת המזומן: {balance:.2f}", reply_markup=main_menu())


def _prepare_agent_goal_confirmation(message, amount):
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        goal_command(message)
        return
    msg = bot.reply_to(
        message,
        f"🤖 להגדיר יעד פיננסי של {amount:,.2f} ₪? כתוב <b>כן</b> לאישור.",
    )
    bot.register_next_step_handler(msg, _confirm_agent_goal, amount)


def _confirm_agent_goal(message, amount):
    if _redirect_if_menu_action(message):
        return
    if (message.text or "").strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "הגדרת היעד בוטלה.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    connect_firebase.set_financial_goal(uid, amount)
    valuation = _get_fast_valuation(uid)[0]
    bot.reply_to(message, "✅ היעד נשמר.\n\n" + _goal_message(valuation), reply_markup=main_menu())


def _prepare_agent_profile_confirmation(message, field, value):
    allowed = {
        "display_name", "risk_profile", "investment_horizon",
        "investment_goal", "base_currency",
    }
    field = str(field or "").strip()
    value = str(value or "").strip()
    if field not in allowed or not value:
        bot.reply_to(message, "לא הצלחתי לזהות איזה פרט אישי לעדכן. נסה /profile.", reply_markup=main_menu())
        return
    if field == "risk_profile" and value not in {"conservative", "balanced", "aggressive"}:
        bot.reply_to(message, "רמת הסיכון חייבת להיות שמרנית, מאוזנת או אגרסיבית.", reply_markup=main_menu())
        return
    if field == "investment_horizon" and value not in {"short", "medium", "long"}:
        bot.reply_to(message, "טווח ההשקעה חייב להיות קצר, בינוני או ארוך.", reply_markup=main_menu())
        return
    if field == "base_currency":
        value = value.upper()
        if value not in {"ILS", "USD", "EUR"}:
            bot.reply_to(message, "מטבע הבסיס יכול להיות ILS, USD או EUR.", reply_markup=main_menu())
            return
    field_labels = {
        "display_name": "שם", "risk_profile": "רמת סיכון",
        "investment_horizon": "טווח השקעה", "investment_goal": "מטרת השקעה",
        "base_currency": "מטבע בסיס",
    }
    msg = bot.reply_to(
        message,
        f"🤖 לעדכן {field_labels[field]} ל־<b>{html.escape(value)}</b>?\n"
        "כדי לבצע כתוב <b>כן</b>. כל תשובה אחרת תבטל.",
    )
    bot.register_next_step_handler(msg, _confirm_agent_profile, field, value)


def _confirm_agent_profile(message, field, value):
    if _redirect_if_menu_action(message):
        return
    if (message.text or "").strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "העדכון בוטל.", reply_markup=main_menu())
        return
    try:
        connect_firebase.update_user_profile(get_user_id(message), {field: value})
    except ValueError as e:
        bot.reply_to(message, f"❌ {html.escape(str(e))}", reply_markup=main_menu())
        return
    bot.reply_to(message, "✅ הפרופיל האישי עודכן.", reply_markup=main_menu())


def _prepare_agent_email_confirmation(message, email_address):
    email_address = str(email_address or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email_address):
        bot.reply_to(message, "לא זיהיתי כתובת אימייל תקינה. נסה /email.", reply_markup=main_menu())
        return
    msg = bot.reply_to(
        message,
        f"🤖 לשמור את האימייל <b>{html.escape(email_address)}</b>?\n"
        "כדי לבצע כתוב <b>כן</b>. כל תשובה אחרת תבטל.",
    )
    bot.register_next_step_handler(msg, _confirm_agent_email, email_address)


def _confirm_agent_email(message, email_address):
    if _redirect_if_menu_action(message):
        return
    if (message.text or "").strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "שמירת האימייל בוטלה.", reply_markup=main_menu())
        return
    connect_firebase.set_user_email(get_user_id(message), email_address)
    bot.reply_to(message, "✅ האימייל נשמר.", reply_markup=main_menu())


def _run_verified_free_text_answer(message, question):
    uid = get_user_id(message)
    thinking = bot.reply_to(message, "🤖 בודק את הנתונים ומאמת את התשובה...")
    try:
        valuation, _ = _get_fast_valuation(uid)
        holdings_for_search = [(t, h.get("name")) for t, h in valuation["holdings"].items()]
        market_context = ai_recommendation.search_market_context(holdings_for_search)
        profile = connect_firebase.get_user_profile(uid)
        answer = ai_recommendation.answer_question(valuation, market_context, question, profile)
    except Exception as e:
        answer = f"לא הצלחתי לענות כרגע ({_safe_error(e)})."
    try:
        bot.delete_message(message.chat.id, thinking.message_id)
    except Exception:
        pass
    _send_long_text(message.chat.id, answer, reply_markup=main_menu())


@bot.message_handler(func=lambda m: True)
def ai_fallback_handler(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    pasted_holdings = portfolio_import.parse_pasted_holdings_local(message.text or "")
    if pasted_holdings:
        _prepare_bulk_buy_confirm(message, pasted_holdings)
        return
    if _looks_like_pasted_positions(message.text or ""):
        _handle_pasted_buy_text(message, message.text or "")
        return
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model=FALLBACK_MODEL,
            messages=[{"role": "user", "content": _ai_fallback_prompt(uid, message.text)}],
            temperature=0.2,
            max_completion_tokens=800,
            reasoning_effort="low",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(_extract_json_text(response.choices[0].message.content))
    except Exception as e:
        bot.reply_to(message, f"לא הבנתי את ההודעה. שלח ❓ עזרה לרשימת הפקודות.\n({html.escape(_safe_error(e))})", reply_markup=main_menu())
        return

    action = parsed.get("action")
    if action == "portfolio":
        portfolio_command(message)
    elif action == "cake":
        cake_command(message)
    elif action == "tax":
        tax_command(message)
    elif action == "cash":
        _prepare_agent_cash_confirmation(message, parsed.get("operation"), parsed.get("amount"))
    elif action == "savings":
        savings_command(message)
    elif action == "financial_asset":
        financial_asset_command(message)
    elif action == "goal":
        _prepare_agent_goal_confirmation(message, parsed.get("amount"))
    elif action == "edit_cost_basis":
        portfolio = connect_firebase.get_portfolio(uid)
        resolved = _resolve_ticker_or_reply(message, portfolio, str(parsed.get("ticker") or ""))
        if resolved and resolved != "__AMBIGUOUS__" and parsed.get("price") is not None:
            _prepare_edit_price_confirm(message, resolved, parsed.get("price"), portfolio)
        elif resolved != "__AMBIGUOUS__":
            bot.reply_to(message, "חסר שם נייר או מחיר בסיס חדש. אפשר להשתמש בכפתור ✏️ עריכת מחיר בסיס.", reply_markup=main_menu())
    elif action == "analyze" and parsed.get("ticker"):
        threading.Thread(
            target=_run_analysis_for_message,
            args=(message, str(parsed["ticker"])),
            daemon=True,
        ).start()
    elif action == "deep_analysis":
        thinking_command(message)
    elif action == "import":
        import_command(message)
    elif action == "profile":
        _prepare_agent_profile_confirmation(message, parsed.get("field"), parsed.get("value"))
    elif action == "email":
        _prepare_agent_email_confirmation(message, parsed.get("email"))
    elif action == "help":
        menu_help(message)
    elif action == "sell_all":
        _prepare_sell_all_confirm(message)
    elif action in ("buy", "sell"):
        _ai_confirm_trade(message, action, parsed)
    elif action == "reply":
        threading.Thread(
            target=_run_verified_free_text_answer,
            args=(message, message.text or ""),
            daemon=True,
        ).start()
    else:
        bot.reply_to(message, "לא הבנתי בדיוק מה ביקשת. שלח ❓ עזרה לרשימת הפקודות.", reply_markup=main_menu())


# --- מענה לשאלות AI שהגיעו מהאתר (דרך Firestore, ראה connect_firebase.watch_pending_ai_requests) ---
def _handle_web_ai_request(request_id, telegram_id, request_data, doc_ref):
    try:
        kind = request_data.get("kind", "question")
        question = request_data.get("question", "")
        if kind == "financial_assets":
            assets = connect_firebase.refresh_user_financial_assets(telegram_id, force=True)
            valuation = portfolio_service.get_portfolio_valuation(telegram_id)
            connect_firebase.save_valuation_snapshot(telegram_id, valuation)
            answer = _format_savings_message(assets)
            connect_firebase.answer_ai_request(doc_ref, answer)
            return
        if kind == "fundamental":
            symbol = request_data.get("symbol") or question
            analysis = _build_fundamental_analysis(telegram_id, symbol)
            answer = ai_recommendation.format_fundamental_report(analysis)
            connect_firebase.answer_ai_request(doc_ref, answer, analysis=analysis)
            return
        if kind == "thinking":
            analysis = _build_deep_portfolio_analysis(telegram_id)
            answer = ai_recommendation.format_deep_portfolio_report(
                analysis,
                analysis.get("analyzed_count", 0),
                analysis.get("failed_symbols", []),
            )
            connect_firebase.answer_ai_request(doc_ref, answer, analysis=analysis)
            return

        valuation, _ = _get_fast_valuation(telegram_id)
        if not valuation["holdings"] and not valuation.get("financial_assets"):
            answer = "התיק ריק כרגע — אין נתונים להתבסס עליהם. אפשר להוסיף החזקה או מכשיר פיננסי."
        else:
            holdings_for_search = [(t, h.get("name")) for t, h in valuation["holdings"].items()]
            market_context = ai_recommendation.search_market_context(holdings_for_search)
            profile = connect_firebase.get_user_profile(telegram_id)
            answer = ai_recommendation.answer_question(valuation, market_context, question, profile)
    except Exception as e:
        answer = f"מצטער, לא הצלחתי לענות כרגע ({_safe_error(e)}). נסה שוב מאוחר יותר."
    connect_firebase.answer_ai_request(doc_ref, answer)


def _handle_web_portfolio_request(request_id, telegram_id, request_data, doc_ref):
    """The browser may request a mutation, but only this server validates and applies it."""
    try:
        created_at = request_data.get("created_at")
        if created_at and hasattr(created_at, "timestamp") and time.time() - created_at.timestamp() > 15 * 60:
            raise ValueError("הבקשה ישנה מדי. רענן את האתר ונסה שוב.")
        request_type = request_data.get("type")
        if request_type == "buy":
            ticker = str(request_data.get("ticker") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9.=_^-]{1,30}", ticker):
                raise ValueError("הטיקר מכיל תווים לא נתמכים.")
            holding = connect_firebase.record_buy(
                telegram_id,
                ticker,
                request_data.get("quantity"),
                request_data.get("buy_price"),
                str(request_data.get("name") or "").strip() or None,
                buy_fx_rate=request_data.get("buy_fx_rate"),
                broker=str(request_data.get("broker") or "").strip() or None,
            )
            result = {"ticker": ticker, **holding}
            message = "הקנייה נוספה לתיק וסונכרנה עם הבוט."
        elif request_type == "excel_parse":
            rows_json = str(request_data.get("rows_json") or "")
            if not 2 < len(rows_json) < 250000:
                raise ValueError("נתוני הגיליון גדולים מדי לזיהוי.")
            try:
                rows = json.loads(rows_json)
            except json.JSONDecodeError as exc:
                raise ValueError("מבנה נתוני הגיליון אינו תקין.") from exc
            clean = portfolio_import.parse_rows_holdings_ai(rows)
            if not 1 <= len(clean) <= 200:
                raise ValueError("לא זוהו החזקות תקינות בגיליון.")
            result = {"holdings": clean, "holdings_count": len(clean)}
            message = f"ה-AI זיהה את מבנה העמודות ומצא {len(clean)} החזקות. בדוק לפני הסנכרון."
        elif request_type == "image_parse":
            image_data_url = str(request_data.get("image_data_url") or "")
            match = re.fullmatch(r"data:image/(?:jpeg|png);base64,([A-Za-z0-9+/=]+)", image_data_url)
            if not match or len(image_data_url) >= 700000:
                raise ValueError("קובץ התמונה אינו תקין או גדול מדי.")
            try:
                image_bytes = base64.b64decode(match.group(1), validate=True)
            except Exception as exc:
                raise ValueError("לא ניתן לפענח את התמונה.") from exc
            if not 100 <= len(image_bytes) <= 525000:
                raise ValueError("גודל התמונה לאחר כיווץ אינו נתמך.")
            parsed_holdings = portfolio_import.parse_image_holdings(image_bytes)
            if not 1 <= len(parsed_holdings) <= 200:
                raise ValueError("לא זוהו החזקות ברורות בתמונה.")
            clean = []
            for item in parsed_holdings:
                ticker = str(item.get("ticker") or "").strip().upper()[:30]
                if not re.fullmatch(r"[A-Z0-9.=_^-]{1,30}", ticker):
                    continue
                clean.append({
                    "ticker": ticker,
                    "name": str(item.get("name") or "").strip()[:120],
                    "quantity": float(item.get("quantity")),
                    "buy_price": float(item.get("buy_price")),
                    **({"reported_total_cost": float(item.get("reported_total_cost"))} if item.get("reported_total_cost") is not None else {}),
                })
            if not clean:
                raise ValueError("לא זוהו שורות תקינות בתמונה.")
            result = {"holdings": clean, "holdings_count": len(clean)}
            message = f"זוהו {len(clean)} החזקות בתמונה. בדוק את התצוגה המקדימה לפני הסנכרון."
        elif request_type == "import":
            holdings = json.loads(str(request_data.get("payload_json") or "[]"))
            if not isinstance(holdings, list) or not 1 <= len(holdings) <= 200:
                raise ValueError("קובץ הייבוא חייב להכיל בין 1 ל־200 החזקות.")
            clean = []
            for item in holdings:
                if not isinstance(item, dict):
                    raise ValueError("מבנה שורה לא תקין בקובץ.")
                ticker = str(item.get("ticker") or "").strip().upper()[:30]
                if not re.fullmatch(r"[A-Z0-9.=_^-]{1,30}", ticker):
                    raise ValueError(f"טיקר לא תקין בקובץ: {ticker[:20]}")
                clean.append({
                    "ticker": ticker,
                    "name": str(item.get("name") or "").strip()[:120],
                    "quantity": float(item.get("quantity")),
                    "buy_price": float(item.get("buy_price")),
                    **({"reported_total_cost": float(item.get("reported_total_cost"))} if item.get("reported_total_cost") is not None else {}),
                })
            sync_result = connect_firebase.sync_portfolio_from_import(telegram_id, clean)
            counts = sync_result["counts"]
            result = {
                "holdings_count": counts["total"],
                "summary": counts,
                "added": [{"ticker": item["ticker"], "name": item.get("name")} for item in sync_result["added"]],
                "updated": [{"ticker": item["ticker"], "name": item.get("name")} for item in sync_result["updated"]],
                "removed": [{"ticker": item["ticker"], "name": item.get("name")} for item in sync_result["removed"]],
            }
            message = (
                f"הסנכרון הושלם: {counts['added']} נוספו, {counts['updated']} עודכנו, "
                f"{counts['removed']} נמכרו/הוסרו ו־{counts['unchanged']} נשארו ללא שינוי."
            )
        else:
            raise ValueError("סוג בקשה לא נתמך.")
        if request_type not in {"image_parse", "excel_parse"}:
            valuation = portfolio_service.get_portfolio_valuation(telegram_id)
            connect_firebase.save_valuation_snapshot(telegram_id, valuation)
        connect_firebase.answer_portfolio_request(doc_ref, "completed", message, result)
    except Exception as exc:
        connect_firebase.answer_portfolio_request(
            doc_ref, "rejected", f"הפעולה לא בוצעה: {_safe_error(exc)}"
        )


def _handle_web_signup(request_id, uid, request_data, doc_ref):
    try:
        telegram_id = connect_firebase.complete_web_signup(uid)
        connect_firebase.mark_web_signup_done(doc_ref, telegram_id)
    except Exception as exc:
        connect_firebase.mark_web_signup_failed(doc_ref, _safe_error(exc))


def _start_ai_request_listener():
    """A freshly-deployed Firestore collection-group index can take a few
    minutes to finish building; querying before it's ready raises
    FailedPrecondition. Retry instead of requiring a manual bot restart once
    it becomes ready. Runs in its own daemon thread — non-blocking."""
    while True:
        try:
            next(iter(connect_firebase.db.collection_group("ai_requests").limit(1).stream()), None)
            connect_firebase.watch_pending_ai_requests(_handle_web_ai_request)
            print("AI request listener (web relay) started.")
            return
        except Exception as e:
            print(f"AI request listener not ready yet ({_safe_error(e)}); retrying in 30s...")
            time.sleep(30)


def _start_portfolio_request_listener():
    while True:
        try:
            next(iter(connect_firebase.db.collection_group("portfolio_requests").limit(1).stream()), None)
            connect_firebase.watch_pending_portfolio_requests(_handle_web_portfolio_request)
            print("Portfolio request listener (secure web relay) started.")
            return
        except Exception as exc:
            print(f"Portfolio request listener not ready ({_safe_error(exc)}); retrying in 30s...")
            time.sleep(30)


def _start_web_signup_listener():
    while True:
        try:
            next(iter(connect_firebase.db.collection("web_signups").limit(1).stream()), None)
            connect_firebase.watch_pending_web_signups(_handle_web_signup)
            print("Web signup listener (continue without Telegram) started.")
            return
        except Exception as exc:
            print(f"Web signup listener not ready ({_safe_error(exc)}); retrying in 30s...")
            time.sleep(30)


def _start_savings_refresh_loop():
    while True:
        try:
            for uid in connect_firebase.get_user_ids_with_financial_assets():
                connect_firebase.refresh_user_financial_assets(uid)
                valuation = portfolio_service.get_portfolio_valuation(uid)
                connect_firebase.save_valuation_snapshot(uid, valuation)
        except Exception as exc:
            print(f"Savings refresh warning: {_safe_error(exc)}")
        time.sleep(6 * 60 * 60)


def _start_health_check_server():
    """Render's free Web Service tier requires the process to bind $PORT and
    answer HTTP requests to be considered alive; without this the bot process
    (which otherwise only makes outbound Telegram/Firestore connections)
    would be treated as down. Not needed for local runs (PORT is unset)."""
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass  # keep the health-check pings out of the bot's own logs

    ThreadingHTTPServer(("0.0.0.0", int(port)), HealthHandler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=_start_health_check_server, daemon=True).start()
    threading.Thread(target=_start_ai_request_listener, daemon=True).start()
    threading.Thread(target=_start_portfolio_request_listener, daemon=True).start()
    threading.Thread(target=_start_web_signup_listener, daemon=True).start()
    threading.Thread(target=_start_savings_refresh_loop, daemon=True).start()

    print("Telegram bot is running...")
    # infinity_polling() already retries getUpdates()-level network errors
    # internally; this loop is a second layer of defense in case something
    # outside that (or outside the exception_handler above) ever crashes it
    # anyway, so the bot restarts itself instead of silently staying dead.
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception as e:
            print(f"infinity_polling crashed, restarting in 5s: {_safe_error(e)}")
            time.sleep(5)
