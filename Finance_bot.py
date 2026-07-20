import json
import os
import sys
import threading
import time

import telebot
from dotenv import load_dotenv
from groq import Groq
from telebot import types

import ai_recommendation
import chart_service
import connect_firebase
import portfolio_import
import portfolio_service
import price_service
import tax_service

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

Password = os.environ.get("BOT_ACCESS_PASSWORD", "1")


class LogAndContinueExceptionHandler(telebot.ExceptionHandler):
    """Without this, any unhandled exception in a message handler — including
    a plain transient network blip on a bot.send_message() call — bubbles up
    through telebot's worker pool and kills the entire polling loop, taking
    the whole bot offline until someone manually restarts it (observed for
    real: a ConnectionResetError during the exit-button handler did exactly
    this). Returning True here marks the exception as handled, so telebot
    logs it and keeps polling instead of dying."""

    def handle(self, exception):
        print(f"Handler error (bot stays alive): {exception}")
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

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", exception_handler=LogAndContinueExceptionHandler())


# --- עזרי תצוגה ---
def _gain_arrow(amount) -> str:
    if amount is None:
        return "⚪"
    if amount > 0:
        return "🟢▲"
    if amount < 0:
        return "🔴▼"
    return "⚪"


def _day_change_str(day_change_pct) -> str:
    if day_change_pct is None:
        return ""
    arrow = "📈" if day_change_pct > 0 else ("📉" if day_change_pct < 0 else "➖")
    return f" {arrow}{day_change_pct:+.1f}% היום"


def _period_change_str(period_change_pct, period_label) -> str:
    """period_label says what period_change_pct actually measures — "השבוע"
    (week-over-week) via yfinance, or "מתחילת החודש" (month-to-date) via the
    Globes fallback, since the two price sources don't expose the same
    timeframe (see price_service.get_current_price_full)."""
    if period_change_pct is None:
        return ""
    arrow = "📈" if period_change_pct > 0 else ("📉" if period_change_pct < 0 else "➖")
    label = period_label or "בתקופה"
    return f" · {arrow}{period_change_pct:+.1f}% {label}"


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
    name = (details or {}).get("name")
    ticker_part = _bidi_isolate(ticker)
    return f"{_bidi_isolate(name)} ({ticker_part})" if name else ticker_part


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
        if query_lower and query_lower in (details.get("name") or "").lower()
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
    if not holdings:
        return "תיק ריק. הגיע הזמן לקנות! 🚀"
    status_msg = "📊 <b>פירוט התיק שלך:</b>\n"
    for ticker, h in holdings.items():
        label = _display_label(ticker, h)
        gain = h.get("gain_loss")
        gain_str = f" {_gain_arrow(gain)} {gain:+.2f}₪" if gain is not None else ""
        status_msg += (
            f"🔹 {label}{gain_str}"
            f"{_day_change_str(h.get('day_change_pct'))}"
            f"{_period_change_str(h.get('period_change_pct'), h.get('period_label'))}\n"
        )
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
    markup.row("➕ קניה חדשה", "➖ מכירה")
    markup.row("📥 ייבוא תיק", "🧮 סימולטור מס")
    markup.row("🤖 המלצת AI", "🌐 חיבור לאתר")
    markup.row("🚪 יציאה")
    markup.row("❓ עזרה")
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
    "➖ מכירה": "sell_command",
    "📥 ייבוא תיק": "import_command",
    "🧮 סימולטור מס": "tax_command",
    "🤖 המלצת AI": "menu_ai_recommendation",
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
        valuation = portfolio_service.get_portfolio_valuation(uid)
        if valuation["holdings"]:
            connect_firebase.save_valuation_snapshot(uid, valuation)
        bot.send_message(chat_id, format_portfolio_message(valuation), parse_mode="HTML")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ לא הצלחתי לעדכן מחירים חיים כרגע ({e}).")


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


def buy_step_ticker(message):
    if _redirect_if_menu_action(message):
        return
    ticker = message.text.strip().upper()
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
    msg = bot.reply_to(message, "באיזה מחיר קנית ליחידה?")
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


def _refresh_valuation_snapshot_async(uid):
    """The website reads users/{id}.last_valuation — a cached snapshot only
    refreshed when the bot computes a fresh valuation (/portfolio, /cake,
    login, the weekly job). Without this, the site shows stale holdings right
    after a /buy, /sell, or import until the user happens to view /portfolio
    or /cake again. Runs in the background (pricing can be slow — some
    holdings only resolve via the much slower Globes scrape) so it never
    delays the action's own confirmation reply."""
    def _job():
        try:
            valuation = portfolio_service.get_portfolio_valuation(uid)
            connect_firebase.save_valuation_snapshot(uid, valuation)
        except Exception as e:
            print(f"Background valuation snapshot refresh failed for {uid}: {e}")
    threading.Thread(target=_job, daemon=True).start()


def _finish_buy(message, ticker, qty, price):
    try:
        ticker, qty, price = str(ticker).upper(), float(qty), float(price)
    except ValueError:
        bot.reply_to(message, "כמות/מחיר לא תקינים, נסה /buy שוב.")
        return
    uid = get_user_id(message)
    connect_firebase.add_holding(uid, ticker, qty, price)
    connect_firebase.add_transaction(uid, ticker, qty, price, "buy")
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(message, f"✅ נרשם: {qty} {ticker} במחיר {price}", reply_markup=main_menu())


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
    msg = bot.reply_to(message, "באיזה מחיר אתה מוכר ליחידה?")
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


def _prepare_sell_confirm(message, ticker, qty, price):
    try:
        ticker, qty, price = str(ticker).upper(), float(qty), float(price)
    except ValueError:
        bot.reply_to(message, "כמות/מחיר לא תקינים, נסה /sell שוב.", reply_markup=main_menu())
        return

    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    existing = portfolio.get(ticker)
    held_qty = existing.get("quantity", 0) if existing else 0
    if not existing or qty > held_qty:
        bot.reply_to(message, f"❌ אין מספיק {ticker} בתיק למכירה (יש {held_qty}).", reply_markup=main_menu())
        return

    buy_price = existing.get("buy_price", 0)
    tax = tax_service.estimate_sale_tax(qty, buy_price, price)
    lines = [
        f"📤 מכירה: {qty} {_display_label(ticker, existing)} במחיר {price} ליחידה",
        f"💰 תמורה: {tax['proceeds']:.2f}",
        f"📊 רווח/הפסד: {tax['gain']:+.2f}",
        f"💸 מס משוער (הערכה בלבד — 25% שטוח, לא ייעוץ מס): {tax['estimated_tax']:.2f}",
        f"✅ נטו משוער אחרי מס: {tax['net_after_tax']:.2f}",
        "\nלאשר ולמכור? השב <b>כן</b> לאישור, או כל דבר אחר לביטול.",
    ]
    msg = bot.reply_to(message, "\n".join(lines))
    bot.register_next_step_handler(msg, _finish_sell, ticker, qty, price)


def _finish_sell(message, ticker, qty, price):
    if _redirect_if_menu_action(message):
        return
    if message.text.strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "המכירה בוטלה.", reply_markup=main_menu())
        return
    uid = get_user_id(message)
    try:
        connect_firebase.reduce_holding(uid, ticker, qty)
    except ValueError as e:
        bot.reply_to(message, f"❌ {e}", reply_markup=main_menu())
        return
    connect_firebase.add_transaction(uid, ticker, qty, price, "sell")
    _refresh_valuation_snapshot_async(uid)
    bot.reply_to(message, f"✅ נמכר: {qty} {ticker} במחיר {price}", reply_markup=main_menu())


def _prepare_sell_all_confirm(message):
    uid = get_user_id(message)
    portfolio = connect_firebase.get_portfolio(uid)
    if not portfolio:
        bot.reply_to(message, "התיק כבר ריק — אין מה למכור.", reply_markup=main_menu())
        return

    lines = ["📤 <b>מכירת כל התיק:</b>"]
    sells = []  # [(ticker, qty, price), ...] — locked in now so the confirm step sells at these exact prices
    total_proceeds = total_gain = total_tax = 0.0
    for ticker, details in portfolio.items():
        qty = details.get("quantity", 0)
        buy_price = details.get("buy_price", 0)
        price = price_service.get_current_price(ticker)
        label = _display_label(ticker, details)
        if price is None:
            lines.append(f"⚠️ {label}: לא נמצא מחיר נוכחי — לא ייכלל במכירה.")
            continue
        tax = tax_service.estimate_sale_tax(qty, buy_price, price)
        sells.append((ticker, qty, price))
        total_proceeds += tax["proceeds"]
        total_gain += tax["gain"]
        total_tax += tax["estimated_tax"]
        lines.append(f"🔹 {label}: {qty} יח' × {price} = {_gain_arrow(tax['gain'])} {tax['gain']:+.2f}")

    if not sells:
        bot.reply_to(message, "לא הצלחתי לקבל מחיר נוכחי לאף החזקה. נסה שוב מאוחר יותר.", reply_markup=main_menu())
        return

    lines += [
        "",
        f"💰 סה\"כ תמורה: {total_proceeds:.2f}",
        f"📊 סה\"כ רווח/הפסד: {total_gain:+.2f}",
        f"💸 מס משוער כולל (הערכה בלבד — 25% שטוח, לא ייעוץ מס): {total_tax:.2f}",
        f"✅ נטו משוער: {total_proceeds - total_tax:.2f}",
        "\nלאשר ולמכור את הכל? השב <b>כן</b> לאישור, או כל דבר אחר לביטול.",
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
    for ticker, qty, price in sells:
        try:
            connect_firebase.reduce_holding(uid, ticker, qty)
        except ValueError:
            continue  # holding changed since the quote — skip it, don't fail the whole batch
        connect_firebase.add_transaction(uid, ticker, qty, price, "sell")
        sold.append(ticker)
    if sold:
        _refresh_valuation_snapshot_async(uid)
        bot.reply_to(message, f"✅ נמכרו {len(sold)} החזקות: {', '.join(sold)}", reply_markup=main_menu())
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
    price_hint = f" (מחיר נוכחי: {current_price})" if current_price else ""
    msg = bot.reply_to(message, f"באיזה מחיר תרצה לסמלץ מכירה?{price_hint} (מספר, או 'נוכחי')")
    bot.register_next_step_handler(msg, tax_sim_step_price, ticker, existing, qty, current_price)


def tax_sim_step_price(message, ticker, existing, qty, current_price):
    if _redirect_if_menu_action(message):
        return
    text = message.text.strip()
    try:
        sell_price = current_price if text in ("נוכחי", "current") and current_price else float(text)
    except ValueError:
        bot.reply_to(message, "מחיר לא תקין, נסה /tax שוב.", reply_markup=main_menu())
        return
    if sell_price is None:
        bot.reply_to(message, "לא הצלחתי לקבל מחיר נוכחי — הזן מחיר ידנית עם /tax.", reply_markup=main_menu())
        return

    buy_price = existing.get("buy_price", 0)
    tax = tax_service.estimate_sale_tax(qty, buy_price, sell_price)
    lines = [
        f"🧮 <b>סימולציית מס — {_display_label(ticker, existing)}</b>",
        f"{qty} יח' × {sell_price} = תמורה {tax['proceeds']:.2f}",
        f"עלות מקורית: {tax['cost']:.2f}",
        f"{_gain_arrow(tax['gain'])} רווח/הפסד: {tax['gain']:+.2f}",
        f"💸 מס משוער (25% שטוח, הערכה בלבד — לא ייעוץ מס): {tax['estimated_tax']:.2f}",
        f"✅ נטו משוער אחרי מס: {tax['net_after_tax']:.2f}",
        "\nזו רק סימולציה — שום דבר לא נמכר בפועל. למכירה אמיתית: /sell",
    ]
    bot.reply_to(message, "\n".join(lines), reply_markup=main_menu())


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
    if not filename.lower().endswith((".xlsx", ".xls")):
        bot.reply_to(message, "אני תומך רק בקבצי Excel (.xlsx). לעזרה שלח /import", reply_markup=main_menu())
        return

    bot.reply_to(message, "📊 קורא את הקובץ...")
    try:
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        holdings = portfolio_import.parse_excel_holdings(file_bytes)
    except Exception as e:
        bot.reply_to(message, f"❌ שגיאה בייבוא הקובץ: {e}", reply_markup=main_menu())
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
        bot.reply_to(message, f"❌ שגיאה בזיהוי התמונה: {e}", reply_markup=main_menu())
        return

    _show_import_preview(message, holdings)


def _show_import_preview(message, holdings):
    if not holdings:
        bot.reply_to(message, "לא זוהו החזקות תקינות. נסה קובץ/תמונה ברורים יותר.", reply_markup=main_menu())
        return

    lines = ["📋 <b>נמצאו ההחזקות הבאות:</b>"]
    for h in holdings:
        label = _display_label(h["ticker"], h)
        lines.append(f"🔹 {label}: {h['quantity']} יחידות במחיר {h['buy_price']}")
    lines.append("\nלאשר ולייבא? השב <b>כן</b> לאישור, או כל דבר אחר לביטול.")

    msg = bot.reply_to(message, "\n".join(lines))
    bot.register_next_step_handler(msg, confirm_import_step, holdings)


def confirm_import_step(message, holdings):
    if _redirect_if_menu_action(message):
        return
    if message.text.strip() not in CONFIRM_WORDS:
        bot.reply_to(message, "הייבוא בוטל. אפשר לנסות שוב עם /import.", reply_markup=main_menu())
        return

    uid = get_user_id(message)
    for h in holdings:
        connect_firebase.add_holding(uid, h["ticker"], h["quantity"], h["buy_price"], name=h.get("name"))
        connect_firebase.add_transaction(uid, h["ticker"], h["quantity"], h["buy_price"], "import")
    _refresh_valuation_snapshot_async(uid)

    bot.reply_to(
        message,
        f"✅ יובאו {len(holdings)} החזקות בהצלחה! שלח /portfolio לצפייה.",
        reply_markup=main_menu(),
    )


# --- הצגת תיק ---
@bot.message_handler(commands=["portfolio"])
def portfolio_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    valuation = portfolio_service.get_portfolio_valuation(uid)
    if valuation["holdings"]:
        connect_firebase.save_valuation_snapshot(uid, valuation)
    bot.reply_to(message, format_portfolio_message(valuation), reply_markup=main_menu())


# --- עוגת ההשקעות ---
@bot.message_handler(commands=["cake"])
def cake_command(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    valuation = portfolio_service.get_portfolio_valuation(uid)
    if not valuation["holdings"]:
        bot.reply_to(message, "התיק ריק — אין מה לצייר עדיין. נסה /buy קודם.", reply_markup=main_menu())
        return

    connect_firebase.save_valuation_snapshot(uid, valuation)

    chart_buf = chart_service.generate_portfolio_pie_chart(valuation["holdings"])
    total_gain = valuation["total_gain_loss"]
    day_change = valuation.get("total_day_change_value", 0.0)
    day_arrow = "📈" if day_change > 0 else ("📉" if day_change < 0 else "➖")
    caption = (
        f"💰 שווי כולל: {valuation['total_value']:.2f}\n"
        f"📥 עלות כוללת: {valuation['total_cost']:.2f}\n"
        f"{_gain_arrow(total_gain)} רווח/הפסד: {total_gain:+.2f} "
        f"({valuation['total_gain_loss_pct']:.1f}%)\n"
        f"{day_arrow} שינוי היום: {day_change:+.2f}"
    )
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


@bot.message_handler(func=lambda m: m.text == "➖ מכירה")
def menu_sell(message):
    sell_command(message)


@bot.message_handler(func=lambda m: m.text == "📥 ייבוא תיק")
def menu_import(message):
    import_command(message)


@bot.message_handler(func=lambda m: m.text == "🧮 סימולטור מס")
def menu_tax_sim(message):
    tax_command(message)


@bot.message_handler(func=lambda m: m.text == "🤖 המלצת AI")
def menu_ai_recommendation(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    thinking = bot.reply_to(message, "🤖 מחפש חדשות עדכניות ברשת וחושב... (כמה שניות)")
    try:
        valuation = portfolio_service.get_portfolio_valuation(uid)
        if not valuation["holdings"]:
            reply_text = "התיק ריק — אין על מה להמליץ עדיין. נסה ➕ קניה חדשה קודם."
        else:
            holdings_for_search = [(t, h.get("name")) for t, h in valuation["holdings"].items()]
            market_context = ai_recommendation.search_market_context(holdings_for_search)
            recommendation = ai_recommendation.generate_recommendation(valuation, market_context)
            reply_text = (
                f"🤖 <b>המלצת AI</b> <i>(כולל חיפוש חדשות עדכני ברשת)</i>\n\n{recommendation}\n\n"
                "💬 אפשר גם לשאול אותי כל שאלה על התיק שלך בהודעה חופשית."
            )
    except Exception as e:
        reply_text = f"❌ לא הצלחתי להפיק המלצה כרגע: {e}"

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
        "/import - ייבוא תיק שלם מאקסל או תמונה\n"
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
        f"{_display_label(ticker, d)}: {d.get('quantity')} יח' (מחיר קנייה ממוצע {d.get('buy_price')})"
        for ticker, d in portfolio.items()
    ) or "התיק ריק"

    return f"""אתה העוזר של Finance Bot, בוט טלגרם לניהול תיק השקעות אישי בעברית.
המשתמש שלח הודעה שלא תואמת אף פקודה מוכרת בבוט (לא /buy, /sell, /portfolio וכו').
אתה לא מבצע שום פעולה בעצמך — רק מבין את הכוונה ומחזיר JSON; הבוט עצמו יבצע את הפעולה,
ותמיד יציג למשתמש אישור לפני שמירה בפועל.

התיק הנוכחי של המשתמש (חלק מהניירות מזוהים במספר נייר בלבד, יחד עם השם שלהם בסוגריים): {holdings_summary}

הודעת המשתמש: "{user_text}"

החזר אך ורק JSON תקין (בלי טקסט נוסף, בלי הסברים), באחת מהצורות הבאות בלבד:
- קנייה: {{"action": "buy", "ticker": "...", "quantity": <מספר או null>, "price": <מספר או null>}}
- מכירה של החזקה ספציפית (ticker יכול להיות מספר הנייר או השם שלו כפי שמופיע למעלה): {{"action": "sell", "ticker": "...", "quantity": <מספר או null>, "price": <מספר או null>}}
- מכירה של כל התיק (כל ההחזקות): {{"action": "sell_all"}}
- הצגת התיק: {{"action": "portfolio"}}
- גרף/עוגת התיק: {{"action": "cake"}}
- שאלה מסוג "כמה מס אשלם אם אמכור" / בקשה לסימולציית מס (בלי למכור בפועל): {{"action": "tax"}}
- כל דבר אחר (שאלה על התיק, שאלה כללית, "מי אתה", בקשה לא ברורה): {{"action": "reply", "text": "תשובה קצרה וברורה בעברית, מבוססת רק על הנתונים שלמעלה"}}"""


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


@bot.message_handler(func=lambda m: True)
def ai_fallback_handler(message):
    if not _require_auth(message):
        return
    uid = get_user_id(message)
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model=FALLBACK_MODEL,
            messages=[{"role": "user", "content": _ai_fallback_prompt(uid, message.text)}],
            temperature=0.2,
            max_completion_tokens=800,
            reasoning_effort="low",
        )
        parsed = json.loads(_extract_json_text(response.choices[0].message.content))
    except Exception as e:
        bot.reply_to(message, f"לא הבנתי את ההודעה. שלח ❓ עזרה לרשימת הפקודות.\n({e})", reply_markup=main_menu())
        return

    action = parsed.get("action")
    if action == "portfolio":
        portfolio_command(message)
    elif action == "cake":
        cake_command(message)
    elif action == "tax":
        tax_command(message)
    elif action == "sell_all":
        _prepare_sell_all_confirm(message)
    elif action in ("buy", "sell"):
        _ai_confirm_trade(message, action, parsed)
    elif action == "reply" and parsed.get("text"):
        bot.reply_to(message, parsed["text"], reply_markup=main_menu())
    else:
        bot.reply_to(message, "לא הבנתי בדיוק מה ביקשת. שלח ❓ עזרה לרשימת הפקודות.", reply_markup=main_menu())


# --- מענה לשאלות AI שהגיעו מהאתר (דרך Firestore, ראה connect_firebase.watch_pending_ai_requests) ---
def _handle_web_ai_request(request_id, telegram_id, question, doc_ref):
    try:
        valuation = portfolio_service.get_portfolio_valuation(telegram_id)
        if not valuation["holdings"]:
            answer = "התיק ריק כרגע — אין נתונים להתבסס עליהם. אפשר להוסיף החזקה עם /buy בבוט."
        else:
            holdings_for_search = [(t, h.get("name")) for t, h in valuation["holdings"].items()]
            market_context = ai_recommendation.search_market_context(holdings_for_search)
            answer = ai_recommendation.answer_question(valuation, market_context, question)
    except Exception as e:
        answer = f"מצטער, לא הצלחתי לענות כרגע ({e}). נסה שוב מאוחר יותר."
    connect_firebase.answer_ai_request(doc_ref, answer)


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
            print(f"AI request listener not ready yet ({e}); retrying in 30s...")
            time.sleep(30)


if __name__ == "__main__":
    threading.Thread(target=_start_ai_request_listener, daemon=True).start()

    print("Telegram bot is running...")
    # infinity_polling() already retries getUpdates()-level network errors
    # internally; this loop is a second layer of defense in case something
    # outside that (or outside the exception_handler above) ever crashes it
    # anyway, so the bot restarts itself instead of silently staying dead.
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception as e:
            print(f"infinity_polling crashed, restarting in 5s: {e}")
            time.sleep(5)
