"""
Automated integration tests for Finance_bot.py's message-handling logic.

Run with:
    .venv/Scripts/python.exe test_bot_flows.py

Design
------
- Imports Finance_bot as a real module (so the real `bot` TeleBot instance,
  the real command handlers, and the real _redirect_if_menu_action fix are
  all exercised as-is) but NEVER calls bot.infinity_polling() and never
  touches the network-facing Telegram API: bot.reply_to / send_message /
  send_photo / delete_message / register_next_step_handler / clear_step_handler
  are all monkeypatched to fakes that just record calls.
- price_service and ai_recommendation are monkeypatched to fixed/canned
  values so nothing hits yfinance, Playwright/Globes, Tavily, or Groq for
  real.
- Finance_bot.Groq and portfolio_import.Groq (the imported *classes*) are
  monkeypatched so instantiating them returns a fake client whose
  .chat.completions.create(...) returns a configurable canned response.
- A single dedicated fake Telegram user id (TEST_UID) is used for every
  test. Every Firestore document/subcollection created for that id is
  deleted again at the end, in a `finally` block, so nothing is left behind
  in the real project even if an assertion fails.

No pytest dependency (not in requirements.txt) — plain asserts, grouped into
test_*() functions, each run in its own try/except by main().
"""

import io
import json
import sys
import time
from types import SimpleNamespace

import pandas as pd

import Finance_bot as FB
import connect_firebase
import price_service
import ai_recommendation
import chart_service
import portfolio_import
import tax_service
import portfolio_service
import fundamental_service
import finance_engine
import savings_service

# ---------------------------------------------------------------------------
# Fixed test identity — NEVER the real user's real linked Telegram account.
# ---------------------------------------------------------------------------
TEST_UID = "TEST_AUTOMATED_998877"
TEST_CHAT_ID = 998877877  # arbitrary int, only used as a dict key by our fakes


# ---------------------------------------------------------------------------
# Fake telegram Message plumbing
# ---------------------------------------------------------------------------
class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


_next_msg_id = [10000]


class FakeMessage:
    def __init__(self, text=None, chat_id=TEST_CHAT_ID, user_id=TEST_UID, document=None, photo=None):
        self.text = text
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id)
        self.message_id = _next_msg_id[0]
        _next_msg_id[0] += 1
        self.document = document
        self.photo = photo


def make_message(text, chat_id=TEST_CHAT_ID, user_id=TEST_UID):
    return FakeMessage(text=text, chat_id=chat_id, user_id=user_id)


def ensure_authenticated():
    FB.authenticated_users.add(TEST_UID)


# ---------------------------------------------------------------------------
# Recorder + fake bot methods (bot.reply_to / send_message / send_photo /
# delete_message / register_next_step_handler / clear_step_handler)
# ---------------------------------------------------------------------------
class _Recorder:
    def __init__(self):
        self.calls = []

    def add(self, method, **kw):
        self.calls.append({"method": method, **kw})


recorder = _Recorder()
next_step_store = {}  # chat_id -> (callback, args, kwargs), mirrors telebot's real next-step backend


def fake_reply_to(message, text, **kwargs):
    recorder.add("reply_to", chat_id=message.chat.id, text=text, kwargs=kwargs)
    return FakeMessage(text=text, chat_id=message.chat.id, user_id=message.from_user.id)


def fake_send_message(chat_id, text, **kwargs):
    recorder.add("send_message", chat_id=chat_id, text=text, kwargs=kwargs)
    return FakeMessage(text=text, chat_id=chat_id, user_id=TEST_UID)


def fake_send_photo(chat_id, photo, **kwargs):
    recorder.add("send_photo", chat_id=chat_id, photo=photo, kwargs=kwargs)
    return FakeMessage(text=kwargs.get("caption"), chat_id=chat_id, user_id=TEST_UID)


def fake_delete_message(chat_id, message_id, **kwargs):
    recorder.add("delete_message", chat_id=chat_id, message_id=message_id)
    return True


def fake_register_next_step_handler(msg, callback, *args, **kwargs):
    next_step_store[msg.chat.id] = (callback, args, kwargs)


def fake_clear_step_handler(msg):
    next_step_store.pop(msg.chat.id, None)
    recorder.add("clear_step_handler", chat_id=msg.chat.id)


def last_output_text():
    """Text of the most recent reply_to/send_message, or caption of the most
    recent send_photo — whichever happened last."""
    for c in reversed(recorder.calls):
        if c["method"] in ("reply_to", "send_message"):
            return c["text"] or ""
        if c["method"] == "send_photo":
            return c["kwargs"].get("caption", "") or ""
    return ""


def label_for(handler_name):
    """Looks up a main-menu button's exact label text by the handler name it
    should redirect to, from Finance_bot's own _MENU_BUTTON_HANDLER_NAMES —
    avoids hand-retyping emoji/Hebrew strings that could subtly mismatch."""
    for label, name in FB._MENU_BUTTON_HANDLER_NAMES.items():
        if name == handler_name:
            return label
    raise KeyError(handler_name)


# ---------------------------------------------------------------------------
# Fake price_service — fixed prices, no yfinance/Playwright network calls
# ---------------------------------------------------------------------------
FAKE_PRICES = {
    "AAPL": {"price": 200.0, "day_change_pct": 1.5},
    "QQQ": {"price": 690.46, "day_change_pct": -0.2},
    "BRK.B": {"price": 100.0, "day_change_pct": 0.1},
    "BUYTST": {"price": 210.0, "day_change_pct": 0.5},
    "SELLTST": {"price": 120.0, "day_change_pct": -0.5},
    "EXCDTST": {"price": 55.0, "day_change_pct": 1.0},
    "SAALL1": {"price": 15.0, "day_change_pct": 2.0},
    "SAALL2": {"price": 25.0, "day_change_pct": -1.0},
    "TAXTST": {"price": 70.0, "day_change_pct": 3.0},
    "MIDSELL": {"price": 12.0, "day_change_pct": 0.2},
    "MIDTAX": {"price": 11.0, "day_change_pct": -0.2},
    "AIBUY1": {"price": 33.0, "day_change_pct": 0.0},
    "AISELL1": {"price": 25.0, "day_change_pct": 0.0},
}


def fake_get_current_price_full(ticker, name=None):
    return FAKE_PRICES.get(ticker.strip().upper())


def fake_get_current_price(ticker, name=None):
    r = fake_get_current_price_full(ticker, name)
    return r["price"] if r else None


def fake_get_current_prices_full(tickers, names=None):
    names = names or {}
    return {t: fake_get_current_price_full(t, names.get(t)) for t in tickers}


# ---------------------------------------------------------------------------
# Fake ai_recommendation — canned strings, no Tavily/Groq calls
# ---------------------------------------------------------------------------
def fake_search_market_context(tickers):
    return "FAKE market context (test double, no real Tavily/Groq call)."


def fake_generate_recommendation(valuation, market_context, profile=None):
    return "FAKE recommendation (test double)."


def fake_answer_question(valuation, market_context, question, profile=None):
    return "FAKE answer (test double)."


def fake_generate_deep_recommendation(valuation, analyses, market_context, profile=None):
    return {
        "overall_verdict": "healthy_but_watch",
        "confidence": 88,
        "executive_summary": "FAKE deep portfolio summary.",
        "portfolio_strengths": ["פיזור קיים"],
        "portfolio_risks": ["יש לעקוב אחר ריכוזיות"],
        "holding_actions": [
            {"symbol": a["symbol"], "name": a.get("name"), "stance": "watch", "reason": "בדיקת מערכת"}
            for a in analyses
        ],
        "allocation_actions": ["בדוק את המשקל של כל החזקה"],
        "cash_plan": "שמור כרית מזומן התואמת לפרופיל.",
        "next_steps": ["בדוק ריכוזיות", "עדכן יעד", "בדוק שוב"],
        "verified": True,
    }


# ---------------------------------------------------------------------------
# Fake Groq — used for both Finance_bot.Groq (ai_fallback_handler) and
# portfolio_import.Groq (excel/image AI fallback), each with its own holder
# so tests can configure/inspect them independently.
# ---------------------------------------------------------------------------
class FakeGroqResponse:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")]


class FakeGroqCompletions:
    def __init__(self, holder):
        self._holder = holder

    def create(self, *args, **kwargs):
        self._holder["call_count"] += 1
        self._holder["last_kwargs"] = kwargs
        return FakeGroqResponse(self._holder["content"])


class FakeGroqChat:
    def __init__(self, holder):
        self.completions = FakeGroqCompletions(holder)


class FakeGroqClient:
    def __init__(self, holder, *args, **kwargs):
        self.chat = FakeGroqChat(holder)


def make_fake_groq_class(holder):
    def _factory(*args, **kwargs):
        return FakeGroqClient(holder)
    return _factory


fb_groq_holder = {"content": "", "call_count": 0, "last_kwargs": None}
import_groq_holder = {"content": "", "call_count": 0, "last_kwargs": None}


# ---------------------------------------------------------------------------
# connect_firebase write spies — real Firestore calls still happen (so tests
# can verify end-to-end via connect_firebase.get_portfolio), but every call is
# logged so read-only commands (e.g. /tax) can be asserted to never write.
# ---------------------------------------------------------------------------
_write_call_log = []
_orig_add_holding = connect_firebase.add_holding
_orig_reduce_holding = connect_firebase.reduce_holding
_orig_add_transaction = connect_firebase.add_transaction
_orig_record_buy = connect_firebase.record_buy
_orig_record_sell = connect_firebase.record_sell


def spy_add_holding(*a, **kw):
    _write_call_log.append(("add_holding", a, kw))
    return _orig_add_holding(*a, **kw)


def spy_reduce_holding(*a, **kw):
    _write_call_log.append(("reduce_holding", a, kw))
    return _orig_reduce_holding(*a, **kw)


def spy_add_transaction(*a, **kw):
    _write_call_log.append(("add_transaction", a, kw))
    return _orig_add_transaction(*a, **kw)


def spy_record_buy(*a, **kw):
    _write_call_log.append(("record_buy", a, kw))
    return _orig_record_buy(*a, **kw)


def spy_record_sell(*a, **kw):
    _write_call_log.append(("record_sell", a, kw))
    return _orig_record_sell(*a, **kw)


def apply_patches():
    FB.bot.reply_to = fake_reply_to
    FB.bot.send_message = fake_send_message
    FB.bot.send_photo = fake_send_photo
    FB.bot.delete_message = fake_delete_message
    FB.bot.register_next_step_handler = fake_register_next_step_handler
    FB.bot.clear_step_handler = fake_clear_step_handler

    price_service.get_current_price = fake_get_current_price
    price_service.get_current_price_full = fake_get_current_price_full
    price_service.get_current_prices_full = fake_get_current_prices_full
    price_service.get_fx_rate = lambda source, target: 3.5 if source != target else 1.0

    ai_recommendation.search_market_context = fake_search_market_context
    ai_recommendation.generate_recommendation = fake_generate_recommendation
    ai_recommendation.answer_question = fake_answer_question
    ai_recommendation.generate_deep_portfolio_recommendation = fake_generate_deep_recommendation

    FB.Groq = make_fake_groq_class(fb_groq_holder)
    portfolio_import.Groq = make_fake_groq_class(import_groq_holder)

    connect_firebase.add_holding = spy_add_holding
    connect_firebase.reduce_holding = spy_reduce_holding
    connect_firebase.add_transaction = spy_add_transaction
    connect_firebase.record_buy = spy_record_buy
    connect_firebase.record_sell = spy_record_sell


# ---------------------------------------------------------------------------
# Firestore cleanup for the test user
# ---------------------------------------------------------------------------
def cleanup_test_user():
    try:
        user_ref = connect_firebase.db.collection("users").document(TEST_UID)
        for subcollection in (
            "transactions", "cash_transactions", "analyses", "ai_requests",
            "financial_assets", "portfolio_requests",
        ):
            for doc in user_ref.collection(subcollection).stream():
                doc.reference.delete()
        user_ref.delete()
    except Exception as e:
        print(f"WARNING: cleanup of test user {TEST_UID} failed: {e}")


def verify_cleanup():
    doc = connect_firebase.db.collection("users").document(TEST_UID).get()
    return not doc.exists


# ===========================================================================
# Tests
# ===========================================================================
def test_password_auth():
    FB.authenticated_users.discard(TEST_UID)

    # Wrong password
    recorder.calls.clear()
    FB.process_password_step(make_message("___WRONG_PASSWORD_TEST_998877___"))
    assert TEST_UID not in FB.authenticated_users, "wrong password must not authenticate the user"
    assert "סיסמה שגויה" in last_output_text(), "expected a wrong-password rejection message"

    # Correct password
    recorder.calls.clear()
    FB.process_password_step(make_message(FB.Password))
    assert TEST_UID in FB.authenticated_users, "correct password must authenticate the user"
    texts = " ".join(c.get("text") or "" for c in recorder.calls)
    assert "סיסמה נכונה" in texts, "expected a correct-password confirmation message"
    assert "ברוך הבא" in texts, "expected a welcome message after login"


def test_buy_flow():
    ensure_authenticated()
    _write_call_log.clear()
    recorder.calls.clear()
    next_step_store.clear()

    FB.buy_command(make_message("/buy"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.buy_step_ticker

    cb(make_message("buytst"), *args, **kwargs)  # lowercase on purpose — handler must uppercase it
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.buy_step_quantity
    assert args[0] == "BUYTST"

    cb(make_message("3"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.buy_step_price

    cb(make_message("200"), *args, **kwargs)

    record_calls = [c for c in _write_call_log if c[0] == "record_buy"]
    assert len(record_calls) == 1, "expected exactly one atomic record_buy call"
    assert record_calls[0][1] == (TEST_UID, "BUYTST", 3.0, 200.0)

    assert "נרשם" in last_output_text() and "BUYTST" in last_output_text()

    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert "BUYTST" in portfolio
    assert portfolio["BUYTST"]["quantity"] == 3.0
    assert portfolio["BUYTST"]["buy_price"] == 200.0

    # Firestore update paths used to split BRK.B into nested BRK -> B maps.
    # The atomic whole-map write must preserve the literal dotted ticker.
    FB._finish_buy(make_message("/buy BRK.B 1 100"), "BRK.B", 1, 100)
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert "BRK.B" in portfolio and "BRK" not in portfolio

    # A copied brokerage table must be understood in one paste. In particular,
    # use average price (234.87/581.34), not current price or total cost basis.
    pasted = """**מוצר פיננסי** **פוזיציה** **אחרון** **שינוי %** **בסיס עלות** **שווי שוק** **מחיר ממוצע**
**AAPL**
APPLE INC
0.1 325.69 +1.25% 23.49
USD
32.50
USD
234.87
USD
+0.41
USD
+9.09
USD
**QQQ**
INVESCO QQQ TRUST SERIES 1
0.13 690.46 -0.22% 75.57
USD
89.97
USD
581.34
USD
-0.19
USD
+14.20
USD"""
    recorder.calls.clear()
    next_step_store.clear()
    FB.buy_command(make_message("/buy"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message(pasted), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._confirm_bulk_buy
    detected = args[0]
    assert args[1] is True, "a pasted brokerage table must replace matching positions, not add them again"
    assert [(item["ticker"], item["quantity"], item["buy_price"]) for item in detected] == [
        ("AAPL", 0.1, 234.87), ("QQQ", 0.13, 581.34),
    ]
    assert [item["reported_total_cost"] for item in detected] == [23.49, 75.57]
    cb(make_message("כן"), *args, **kwargs)
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert portfolio["AAPL"]["quantity"] == 0.1 and portfolio["AAPL"]["buy_price"] == 234.87
    assert portfolio["QQQ"]["quantity"] == 0.13 and portfolio["QQQ"]["buy_price"] == 581.34
    assert portfolio["AAPL"]["reported_total_cost"] == 23.49
    assert portfolio["QQQ"]["reported_total_cost"] == 75.57

    # Pasting the same broker snapshot again must be idempotent. It previously
    # doubled 0.1 AAPL -> 0.2 and 0.13 QQQ -> 0.26.
    recorder.calls.clear()
    next_step_store.clear()
    FB.buy_command(make_message("/buy"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message(pasted), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("כן"), *args, **kwargs)
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert portfolio["AAPL"]["quantity"] == 0.1
    assert portfolio["QQQ"]["quantity"] == 0.13


def test_sell_flow():
    ensure_authenticated()

    # --- normal sell within held quantity ---
    connect_firebase.add_holding(TEST_UID, "SELLTST", 10, 100)
    _write_call_log.clear()
    recorder.calls.clear()
    next_step_store.clear()

    FB.sell_command(make_message("/sell"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.sell_step_ticker

    cb(make_message("SELLTST"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.sell_step_quantity

    cb(make_message("4"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.sell_step_price

    cb(make_message("120"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._finish_sell
    # confirmation preview should show proceeds/gain/tax numbers
    preview = last_output_text()
    assert "תמורה" in preview and "מס משוער" in preview

    cb(make_message("כן"), *args, **kwargs)
    sell_calls = [c for c in _write_call_log if c[0] == "record_sell"]
    assert len(sell_calls) == 1 and sell_calls[0][1] == (TEST_UID, "SELLTST", 4.0, 120.0)
    final_sale = last_output_text()
    assert "המכירה נרשמה" in final_sale
    assert "רווח ממומש" in final_sale and "+80.00" in final_sale and "+20.0%" in final_sale
    assert "עמלת מסחר צפויה" in final_sale
    assert "רווח נטו משוער אחרי עמלה ומס" in final_sale and "+58.58" in final_sale

    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert portfolio["SELLTST"]["quantity"] == 6.0

    # --- reject a sell that exceeds held quantity ---
    connect_firebase.add_holding(TEST_UID, "EXCDTST", 2, 50)
    _write_call_log.clear()
    recorder.calls.clear()
    next_step_store.clear()

    FB.sell_command(make_message("/sell"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("EXCDTST"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("5"), *args, **kwargs)  # only 2 held
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("60"), *args, **kwargs)

    assert "אין מספיק" in last_output_text()
    assert not any(c[0] in ("record_sell", "reduce_holding", "add_transaction") for c in _write_call_log), (
        "an over-quantity sell must not touch Firestore"
    )
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert portfolio["EXCDTST"]["quantity"] == 2.0, "holding must be unchanged after rejected sell"

    # --- bulk "sell all" flow, triggered by 'הכל' at the ticker step ---
    connect_firebase.add_holding(TEST_UID, "SAALL1", 5, 10)
    connect_firebase.add_holding(TEST_UID, "SAALL2", 2, 20)
    # Sell-all intentionally uses the synchronized snapshot so its confirmation
    # is instant and never blocks on market/browser calls.
    connect_firebase.save_valuation_snapshot(
        TEST_UID, portfolio_service.get_portfolio_valuation(TEST_UID)
    )
    _write_call_log.clear()
    recorder.calls.clear()
    next_step_store.clear()

    FB.sell_command(make_message("/sell"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.sell_step_ticker

    cb(make_message("הכל"), *args, **kwargs)
    # sell_step_ticker should have routed straight into _prepare_sell_all_confirm,
    # registering _finish_sell_all as the next step (not sell_step_quantity).
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._finish_sell_all
    preview = last_output_text()
    assert "מכירת כל התיק" in preview
    assert "SAALL1" in preview and "SAALL2" in preview

    cb(make_message("כן"), *args, **kwargs)
    reduce_tickers = {c[1][1] for c in _write_call_log if c[0] == "record_sell"}
    assert {"SAALL1", "SAALL2"}.issubset(reduce_tickers)
    assert "נמכר כל התיק" in last_output_text()
    assert "רווח ממומש כולל" in last_output_text()

    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert "SAALL1" not in portfolio and "SAALL2" not in portfolio, "sell-all must fully liquidate both holdings"


def test_tax_simulator_flow():
    ensure_authenticated()
    connect_firebase.add_holding(TEST_UID, "TAXTST", 8, 50)
    _write_call_log.clear()  # the seed line above is not part of what we're testing
    recorder.calls.clear()
    next_step_store.clear()

    FB.tax_command(make_message("/tax"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.tax_sim_step_ticker

    cb(make_message("TAXTST"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.tax_sim_step_quantity

    cb(make_message("הכל"), *args, **kwargs)  # "all" shortcut -> qty should resolve to held_qty (8)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.tax_sim_step_price

    cb(make_message("נוכחי"), *args, **kwargs)  # "current price" shortcut -> fake price 70.0

    out = last_output_text()
    assert "+160.00" in out
    assert "עמלת מסחר צפויה: 1.90" in out
    assert "39.52" in out or "39.53" in out
    assert "518.58" in out

    assert len(_write_call_log) == 0, "the /tax simulator must never write to Firestore"


def test_sell_all_manual_price_fallback():
    """An unresolvable holding must be priced manually, never silently skipped."""
    time.sleep(1.0)  # let prior test's daemon snapshot refresh finish cleanly
    cleanup_test_user()
    connect_firebase.create_user_document(TEST_UID)
    ensure_authenticated()
    connect_firebase.add_holding(TEST_UID, "NOQUOTE1", 2, 10, name="Unquoted Test Asset")
    connect_firebase.save_valuation_snapshot(
        TEST_UID, portfolio_service.get_portfolio_valuation(TEST_UID)
    )
    recorder.calls.clear()
    next_step_store.clear()

    FB.sell_command(make_message("/sell"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("הכל"), *args, **kwargs)

    deadline = time.time() + 2
    while time.time() < deadline:
        pending = next_step_store.get(TEST_CHAT_ID)
        if pending and pending[0] is FB._manual_sell_all_price_step:
            break
        time.sleep(0.01)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._manual_sell_all_price_step
    assert "מחיר" in last_output_text()

    cb(make_message("42"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._finish_sell_all
    assert "כל התיק" in last_output_text() and "Unquoted Test Asset" in last_output_text()
    assert "NOQUOTE1" not in last_output_text(), "known asset names should replace raw tickers"

    cb(make_message("כן"), *args, **kwargs)
    assert not connect_firebase.get_portfolio(TEST_UID)


def test_cash_and_profile():
    ensure_authenticated()
    assert connect_firebase.set_cash_balance(TEST_UID, 1000) == 1000
    assert connect_firebase.adjust_cash_balance(TEST_UID, 250) == 1250
    assert connect_firebase.adjust_cash_balance(TEST_UID, -200) == 1050
    assert connect_firebase.get_cash_balance(TEST_UID) == 1050
    try:
        connect_firebase.adjust_cash_balance(TEST_UID, -2000)
        raise AssertionError("cash balance must never go negative")
    except ValueError:
        pass

    profile = connect_firebase.update_user_profile(TEST_UID, {
        "display_name": "Test Investor",
        "risk_profile": "aggressive",
        "investment_horizon": "long",
        "investment_goal": "growth",
        "base_currency": "USD",
    })
    assert profile["display_name"] == "Test Investor"
    assert connect_firebase.get_user_profile(TEST_UID)["base_currency"] == "USD"


def test_names_monthly_display_and_cost_basis_edit():
    ensure_authenticated()
    connect_firebase.record_buy(TEST_UID, "410393", 2, 10, tx_type="buy")
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    valuation = portfolio_service.compute_portfolio_value(portfolio, {
        "410393": {
            "price": 12, "day_change_pct": 0.5,
            "week_change_pct": 1.25, "month_change_pct": -2.25,
            "year_change_pct": 7.5,
            "period_change_pct": -2.25, "period_label": "בחודש האחרון",
            "source": "Globes",
        }
    })
    text = FB.format_portfolio_message(valuation)
    assert "SPDR Gold MiniShares Trust" in text
    assert "410393" not in text, "numeric security id must be hidden when a name is known"
    assert "יום:" in text and "שבוע:" in text and "חודש:" in text and "שנה:" in text
    assert "-2.2%" in text and "מקור מחיר: Globes" in text

    recorder.calls.clear()
    next_step_store.clear()
    FB.edit_price_command(make_message("/editprice 410393 11.5"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._confirm_edit_price
    assert "מחיר חדש: 11.50" in last_output_text()
    cb(make_message("כן"), *args, **kwargs)
    updated = connect_firebase.get_portfolio(TEST_UID)["410393"]
    assert updated["quantity"] == 2
    assert updated["buy_price"] == 11.5
    connect_firebase.record_sell(TEST_UID, "410393", 2, 12)

    imported = [{"ticker": "SYNC1", "quantity": 4, "buy_price": 25, "name": "Sync Test"}]
    connect_firebase.replace_portfolio_from_import(TEST_UID, imported)
    connect_firebase.replace_portfolio_from_import(TEST_UID, imported)
    synced = connect_firebase.get_portfolio(TEST_UID)["SYNC1"]
    assert synced["quantity"] == 4, "re-importing the same file must not double quantity"
    assert synced["buy_price"] == 25
    connect_firebase.record_sell(TEST_UID, "SYNC1", 4, 25)


def test_mid_flow_redirect():
    ensure_authenticated()

    # --- 1) mid-/buy, redirected by the "📊 התיק שלי" menu button ---
    recorder.calls.clear()
    next_step_store.clear()
    FB.buy_command(make_message("/buy"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("REDIRTST"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.buy_step_quantity

    label = label_for("portfolio_command")
    cb(make_message(label), *args, **kwargs)  # menu button instead of a quantity number

    assert TEST_CHAT_ID not in next_step_store, "pending /buy flow was not cancelled"
    assert any(c["method"] == "clear_step_handler" for c in recorder.calls)
    out = last_output_text()
    assert "כמות לא תקינה" not in out, "old bug: menu text was parsed as a quantity"
    assert ("פירוט התיק" in out) or ("ללא ניירות" in out), "expected portfolio-shaped output"

    # --- 2) mid-/sell, redirected by the "🥧 עוגת ההשקעות" menu button ---
    connect_firebase.add_holding(TEST_UID, "MIDSELL", 5, 10)
    recorder.calls.clear()
    next_step_store.clear()
    FB.sell_command(make_message("/sell"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("MIDSELL"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.sell_step_quantity

    label = label_for("cake_command")
    cb(make_message(label), *args, **kwargs)  # menu button instead of a quantity number

    assert TEST_CHAT_ID not in next_step_store, "pending /sell flow was not cancelled"
    assert any(c["method"] == "clear_step_handler" for c in recorder.calls)
    out = last_output_text()
    assert "כמות לא תקינה" not in out, "old bug: menu text was parsed as a quantity"
    assert any(c["method"] == "send_photo" for c in recorder.calls) or "ריק" in out or "לא הצלחתי לקבל מחיר" in out, (
        "expected cake-shaped output (a chart, or the empty-portfolio message)"
    )

    # --- 3) mid-/tax, redirected by the "➕ קניה חדשה" menu button (re-registers a NEW flow) ---
    connect_firebase.add_holding(TEST_UID, "MIDTAX", 5, 10)
    recorder.calls.clear()
    next_step_store.clear()
    FB.tax_command(make_message("/tax"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("MIDTAX"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.tax_sim_step_quantity

    label = label_for("buy_command")
    cb(make_message(label), *args, **kwargs)  # menu button instead of a quantity number

    out = last_output_text()
    assert "כמות לא תקינה" not in out, "old bug: menu text was parsed as a quantity"
    assert "טיקר" in out, "expected buy_command's ticker prompt"
    assert any(c["method"] == "clear_step_handler" for c in recorder.calls)
    assert TEST_CHAT_ID in next_step_store, "buy_command should have registered its own next step"
    new_cb, _, _ = next_step_store[TEST_CHAT_ID]
    assert new_cb is FB.buy_step_ticker, "redirect must dispatch into the real buy flow, not just cancel"

    # --- 4) bonus: mid-/buy, redirected by a slash *command* (not just a menu button) ---
    recorder.calls.clear()
    next_step_store.clear()
    FB.buy_command(make_message("/buy"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    cb(make_message("REDIRTST2"), *args, **kwargs)
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.buy_step_quantity

    cb(make_message("/portfolio"), *args, **kwargs)  # a command, not a menu button, mid-flow
    out = last_output_text()
    assert "כמות לא תקינה" not in out
    assert ("פירוט התיק" in out) or ("ללא ניירות" in out)


def test_menu_completeness():
    markup = FB.main_menu()
    labels = [btn["text"] for row in markup.keyboard for btn in row]
    assert labels, "main_menu() produced no buttons"

    for label in labels:
        fake_msg = make_message(label)
        matched = None
        for h in FB.bot.message_handlers:
            func = h["filters"].get("func")
            if func is None:
                continue
            if func(fake_msg):
                matched = h["function"]
                break
        assert matched is not None, f"no @bot.message_handler matches menu button {label!r} (dead button)"
        assert matched.__name__ != "ai_fallback_handler", (
            f"menu button {label!r} falls through all the way to ai_fallback_handler (dead button)"
        )


def test_excel_import():
    # --- fast exact-header-match path (real Israeli brokerage export shape) ---
    df = pd.DataFrame({
        "שם נייר": ["טבע", "בנק הפועלים"],
        "מספר נייר": ["629014", "662577"],
        "שער אחרון": [1005.0, 2002.5],
        "כמות": [50, 20],
        "שער עלות": [900.0, 1800.0],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)

    import_groq_holder["call_count"] = 0
    holdings = portfolio_import.parse_excel_holdings(buf.getvalue())

    assert import_groq_holder["call_count"] == 0, "exact-header-match path must never call the AI fallback"
    assert len(holdings) == 2
    by_ticker = {h["ticker"]: h for h in holdings}
    assert by_ticker["629014"]["quantity"] == 50.0
    assert by_ticker["629014"]["buy_price"] == 900.0
    assert by_ticker["662577"]["quantity"] == 20.0
    assert by_ticker["662577"]["buy_price"] == 1800.0

    # Broker exports may put account/date metadata above the actual table and
    # append recommendation, rating, value and notes columns. The importer must
    # locate the semantic header row instead of assuming row 1 is the header.
    metadata_rows = [
        ["תיק עדכני"],
        ["חשבון", "123456"],
        ["תאריך", "24/07/2026"],
        [], [], [],
        ["שם נייר", "מספר נייר", "שער אחרון", "כמות", "שינוי יומי ב-%", "שער עלות", "המלצת AI", "דרוג AI", "שווי אחזקה (₪)", "הערה אישית"],
        ["קרן בדיקה", "5112628", 410.0, 764, 1.2, 357.08, "מעקב", 72, 313240, "עמודה נוספת"],
    ]
    metadata_buf = io.BytesIO()
    pd.DataFrame(metadata_rows).to_excel(metadata_buf, index=False, header=False, engine="openpyxl")
    parsed_metadata = portfolio_import.parse_excel_holdings(metadata_buf.getvalue())
    assert parsed_metadata == [{
        "ticker": "5112628", "quantity": 764.0, "buy_price": 357.08, "name": "קרן בדיקה",
    }]

    # A generic total-cost column must be divided by quantity; it is never a
    # per-unit price. This prevents hugely inflated cost basis on import.
    total_cost_df = pd.DataFrame({
        "מספר נייר": ["5112628"], "כמות": [100], "עלות": [35_708],
    })
    total_cost_buf = io.BytesIO()
    total_cost_df.to_excel(total_cost_buf, index=False, engine="openpyxl")
    parsed_total = portfolio_import.parse_excel_holdings(total_cost_buf.getvalue())
    assert parsed_total[0]["quantity"] == 100
    # Internally known TASE funds are stored in agorot so the monetary cost is
    # 100 units × 35,708 agorot × 0.01 = 35,708 ILS.
    assert parsed_total[0]["buy_price"] == 35_708
    assert parsed_total[0]["reported_total_cost"] == 35_708

    # --- unrecognized headers -> AI fallback path ---
    df2 = pd.DataFrame({
        "Some Weird Name Column": ["FooCorp"],
        "Random Code": ["XYZ"],
        "How Many": [7],
        "What It Cost": [42.5],
    })
    buf2 = io.BytesIO()
    df2.to_excel(buf2, index=False, engine="openpyxl")
    buf2.seek(0)

    import_groq_holder["content"] = json.dumps({
        "ticker_column": "Random Code",
        "quantity_column": "How Many",
        "price_column": "What It Cost",
        "name_column": "Some Weird Name Column",
    })
    import_groq_holder["call_count"] = 0
    holdings2 = portfolio_import.parse_excel_holdings(buf2.getvalue())

    assert import_groq_holder["call_count"] >= 1, "unrecognized headers must go through parse_excel_holdings_ai"
    assert holdings2 == [{"ticker": "XYZ", "quantity": 7.0, "buy_price": 42.5, "name": "FooCorp"}]
    request_kwargs = import_groq_holder["last_kwargs"]
    assert request_kwargs["max_completion_tokens"] <= 900
    assert len(request_kwargs["messages"][0]["content"]) < 15_000

    # A large workbook must still send only a compact column profile to AI and
    # parse every row locally, avoiding the old 8,000-token request failure.
    large_df = pd.DataFrame({
        "Security Code": [f"LARGE{i}" for i in range(500)],
        "Units Held": [i + 1 for i in range(500)],
        "What It Cost": [42.5 for _ in range(500)],
        **{f"Noise {i}": [f"value-{row}" for row in range(500)] for i in range(20)},
    })
    large_buf = io.BytesIO()
    large_df.to_excel(large_buf, index=False, engine="openpyxl")
    large_buf.seek(0)
    import_groq_holder["content"] = json.dumps({
        "ticker_column": "Security Code",
        "quantity_column": "Units Held",
        "price_column": "What It Cost",
        "name_column": None,
    })
    holdings3 = portfolio_import.parse_excel_holdings(large_buf.getvalue())
    assert len(holdings3) == 500
    large_kwargs = import_groq_holder["last_kwargs"]
    assert len(large_kwargs["messages"][0]["content"]) < 15_000


def test_ai_fallback_dispatch():
    ensure_authenticated()

    # --- portfolio ---
    recorder.calls.clear()
    fb_groq_holder["content"] = json.dumps({"action": "portfolio"})
    FB.ai_fallback_handler(make_message("כמה שווה התיק שלי"))
    out = last_output_text()
    assert ("פירוט התיק" in out) or ("ללא ניירות" in out)

    # --- cake ---
    recorder.calls.clear()
    fb_groq_holder["content"] = json.dumps({"action": "cake"})
    FB.ai_fallback_handler(make_message("תראה לי גרף"))
    assert any(c["method"] == "send_photo" for c in recorder.calls) or "ריק" in last_output_text()

    # --- tax ---
    recorder.calls.clear()
    next_step_store.clear()
    fb_groq_holder["content"] = json.dumps({"action": "tax"})
    FB.ai_fallback_handler(make_message("כמה מס אשלם אם אמכור"))
    out = last_output_text()
    assert ("טיקר" in out) or ("ריק" in out)

    # --- sell_all (dispatch only, do not confirm — full sell-all flow already covered elsewhere) ---
    recorder.calls.clear()
    next_step_store.clear()
    fb_groq_holder["content"] = json.dumps({"action": "sell_all"})
    FB.ai_fallback_handler(make_message("תמכור הכל"))
    out = last_output_text()
    assert ("מכירת כל התיק" in out) or ("ריק" in out)

    # --- buy, full fields -> confirmation prompt, then confirm -> real write ---
    recorder.calls.clear()
    next_step_store.clear()
    _write_call_log.clear()
    fb_groq_holder["content"] = json.dumps({"action": "buy", "ticker": "AIBUY1", "quantity": 5, "price": 33.0})
    FB.ai_fallback_handler(make_message("קניתי 5 מניות AIBUY1 ב-33"))
    assert "הבנתי" in last_output_text()
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._confirm_ai_buy
    cb(make_message("כן"), *args, **kwargs)
    assert any(c[0] == "record_buy" and c[1] == (TEST_UID, "AIBUY1", 5.0, 33.0) for c in _write_call_log)
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert "AIBUY1" in portfolio

    # --- buy, missing fields -> follow-up quantity question ---
    recorder.calls.clear()
    next_step_store.clear()
    fb_groq_holder["content"] = json.dumps({"action": "buy", "ticker": "AIBUY2", "quantity": None, "price": None})
    FB.ai_fallback_handler(make_message("אני רוצה לקנות AIBUY2"))
    assert "כמה יחידות" in last_output_text()
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB.buy_step_quantity
    assert args[0] == "AIBUY2"

    # --- sell, full fields (bonus, dispatch only) ---
    connect_firebase.add_holding(TEST_UID, "AISELL1", 10, 20)
    recorder.calls.clear()
    next_step_store.clear()
    fb_groq_holder["content"] = json.dumps({"action": "sell", "ticker": "AISELL1", "quantity": 2, "price": 25.0})
    FB.ai_fallback_handler(make_message("תמכור 2 AISELL1 ב-25"))
    assert "מכירה" in last_output_text()
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._finish_sell

    # --- reply ---
    recorder.calls.clear()
    fb_groq_holder["content"] = json.dumps({"action": "reply"})
    FB.ai_fallback_handler(make_message("מי אתה?"))
    deadline = time.time() + 5
    while "FAKE answer" not in last_output_text() and time.time() < deadline:
        time.sleep(0.01)
    assert "FAKE answer" in last_output_text()

    # --- cash bookkeeping: understand request, require confirmation, then write ---
    recorder.calls.clear()
    next_step_store.clear()
    before_cash = connect_firebase.get_cash_balance(TEST_UID)
    fb_groq_holder["content"] = json.dumps({"action": "cash", "operation": "deposit", "amount": 123})
    FB.ai_fallback_handler(make_message("תפקיד 123 למזומן"))
    assert "כדי לבצע" in last_output_text()
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._confirm_agent_cash
    cb(make_message("כן"), *args, **kwargs)
    assert connect_firebase.get_cash_balance(TEST_UID) == before_cash + 123

    # --- profile: normalized AI intent, confirmation, then synchronized write ---
    recorder.calls.clear()
    next_step_store.clear()
    fb_groq_holder["content"] = json.dumps({"action": "profile", "field": "risk_profile", "value": "aggressive"})
    FB.ai_fallback_handler(make_message("תשנה את רמת הסיכון שלי לאגרסיבית"))
    cb, args, kwargs = next_step_store[TEST_CHAT_ID]
    assert cb is FB._confirm_agent_profile
    cb(make_message("כן"), *args, **kwargs)
    assert connect_firebase.get_user_profile(TEST_UID)["risk_profile"] == "aggressive"

    # --- malformed / non-JSON model output -> graceful error, not a crash ---
    recorder.calls.clear()
    fb_groq_holder["content"] = "this is not json at all { garbage"
    FB.ai_fallback_handler(make_message("שלום"))
    assert "לא הבנתי את ההודעה" in last_output_text()


def test_pure_functions():
    assert price_service.resolve_broker_symbol("411462") == "IBIT"
    assert price_service.resolve_broker_symbol("410393") == "GLDM"
    savings_html = """
    <span>תקופת דיווח:</span><span>יוני 2026</span>
    <span>תשואה 12 חודשים</span><span>+22.97%</span>
    <h2>תשואה חודשית -</h2><span>12</span><span>חודשים אחרונים</span>
    <span>+4.58%</span><span>05/26</span><span>+6.78%</span><span>06/26</span>
    <h2>פרטי הקרן</h2><span>דמי ניהול ממוצעים</span><span>0.56%</span>
    """
    track = savings_service.parse_track_page(savings_html, "14864")
    assert track["report_period"] == "2026-06"
    assert track["monthly_return_pct"] == 6.78
    assert track["return_12m_pct"] == 22.97
    estimated = savings_service.value_financial_asset({
        "track_id": "14864", "reported_balance": 10_000,
        "balance_as_of": "2026-04-15", "monthly_contribution": 500,
        "total_contributed": 9_000, "auto_update": True,
    }, track)
    assert estimated["applied_report_periods"] == ["2026-05", "2026-06"]
    assert estimated["estimated_balance"] > 11_000
    assert estimated["estimated_gain_loss"] is not None

    malformed_globes_rows = {
        "השבוע 3.50% החודש 9.51%": "3.50% החודש 9.51%",
        "החודש 9.51%": "9.51%",
    }
    assert finance_engine._number(
        finance_engine._value_starting_with(malformed_globes_rows, "השבוע")
    ) == 3.5

    leaked = "https://api.telegram.org/bot123456789:ABC_def-GHI/getUpdates?token=secret"
    cleaned = FB._safe_error(leaked)
    assert "ABC_def" not in cleaned and "secret" not in cleaned

    chart_holdings = {
        "411462": {"name": "iShares Bitcoin Trust", "market_value": 100},
        "1215771": {"name": "אי.בי.אי. סל תא-ביטוח", "market_value": 50},
    }
    chart_labels = chart_service.build_portfolio_chart_labels(chart_holdings)
    assert len(chart_labels) == 2
    assert not any("411462" in label or "1215771" in label for label in chart_labels), (
        "pie-chart labels must use asset names, never numeric security ids"
    )
    chart_png = chart_service.generate_portfolio_pie_chart(chart_holdings).getvalue()
    assert chart_png.startswith(b"\x89PNG") and len(chart_png) > 10_000

    # tax_service.estimate_sale_tax edge cases
    zero = tax_service.estimate_sale_tax(10, 50, 50)
    assert zero["gain"] == 0
    assert zero["estimated_tax"] == 0
    assert zero["net_after_tax"] == zero["proceeds"] == 500.0

    loss = tax_service.estimate_sale_tax(5, 100, 80)
    assert loss["proceeds"] == 400.0
    assert loss["gain"] == -100.0
    assert loss["estimated_tax"] == 0, "a loss must never generate a positive estimated tax"
    assert loss["net_after_tax"] == 400.0

    frac = tax_service.estimate_sale_tax(2.5, 40, 44)
    assert frac["proceeds"] == 110.0
    assert frac["gain"] == 10.0
    assert frac["estimated_tax"] == 2.5
    assert frac["net_after_tax"] == 107.5

    agorot_tax = tax_service.estimate_sale_tax(100, 357.08, 439.28, 0.01)
    assert round(agorot_tax["proceeds"], 2) == 439.28
    assert round(agorot_tax["cost"], 2) == 357.08

    fractional_fee = tax_service.estimate_trade_commission(0.5, 500, market="foreign", fx_rate_to_ils=3.5)
    assert fractional_fee["amount_ils"] == 8.75
    assert fractional_fee["amount_quote"] == 2.5
    foreign_fee = tax_service.estimate_trade_commission(2, 2000, market="foreign", fx_rate_to_ils=3.5)
    assert round(foreign_fee["amount_ils"], 2) == 17.15
    local_fee = tax_service.estimate_trade_commission(10, 1000, market="tase", instrument_type="security")
    assert local_fee["amount_ils"] == 1.9
    fund_fee = tax_service.estimate_trade_commission(10, 1000, market="tase", instrument_type="etf")
    assert fund_fee["amount_ils"] == 1.9
    mutual_fund_fee = tax_service.estimate_trade_commission(10, 1000, market="tase", instrument_type="other_mutual_fund")
    assert mutual_fund_fee["amount_ils"] == 16.0
    with_fee = tax_service.estimate_sale_from_amounts(600, 400, 16)
    assert with_fee["taxable_gain"] == 184
    assert with_fee["estimated_tax"] == 46
    assert with_fee["net_after_tax_and_fees"] == 538

    # portfolio_service.compute_portfolio_value edge cases
    portfolio = {"NOPRICE": {"quantity": 10, "buy_price": 5}}
    valuation = portfolio_service.compute_portfolio_value(portfolio, {})
    h = valuation["holdings"]["NOPRICE"]
    assert h["market_value"] is None
    assert h["gain_loss"] is None
    assert h["day_change_pct"] is None
    assert valuation["total_gain_loss"] == 0, "an unpriced holding must not be treated as worthless"
    assert valuation["unpriced_cost"] == 50.0
    assert valuation["pricing_complete"] is False

    portfolio2 = {"NOPREVCLOSE": {"quantity": 4, "buy_price": 10}}
    prices2 = {"NOPREVCLOSE": {"price": 12.0, "day_change_pct": None}}
    valuation2 = portfolio_service.compute_portfolio_value(portfolio2, prices2)
    h2 = valuation2["holdings"]["NOPREVCLOSE"]
    assert h2["market_value"] == 48.0
    assert h2["gain_loss"] == 8.0
    assert h2["day_change_pct"] is None
    assert h2["day_change_value"] is None

    with_cash = portfolio_service.compute_portfolio_value(portfolio2, prices2, cash_balance=100)
    assert with_cash["cash_balance"] == 100.0
    assert with_cash["account_total_value"] == 148.0

    agorot = portfolio_service.compute_portfolio_value(
        {"1215771": {"quantity": 12, "buy_price": 12_480, "name": "Test Fund"}},
        {"1215771": {"price": 14_550, "price_unit": "agorot", "source": "Globes"}},
    )
    assert agorot["holdings"]["1215771"]["market_value"] == 1746.0
    assert round(agorot["holdings"]["1215771"]["cost_basis"], 2) == 1497.6
    assert round(agorot["holdings"]["1215771"]["gain_loss"], 2) == 248.4

    # Core data-integrity guards.
    for bad in (0, -1):
        try:
            connect_firebase._positive_number(bad, "כמות")
            raise AssertionError("non-positive values must be rejected")
        except ValueError:
            pass

    strong_score, _ = fundamental_service._score_stock({
        "forward_pe": 14, "price_to_book": 2.5, "enterprise_to_ebitda": 9,
        "return_on_equity_pct": 28, "profit_margin_pct": 24,
        "operating_margin_pct": 25, "free_cash_flow": 1,
        "revenue_growth_pct": 16, "earnings_growth_pct": 20,
        "return_3y_annualized_pct": 15, "debt_to_equity": 35,
        "current_ratio": 1.8, "volatility_1y_pct": 20,
        "max_drawdown_5y_pct": -22,
    })
    assert strong_score >= 70, "healthy fundamentals should receive a strong screening score"

    entry = fundamental_service.build_entry_guidance({
        "asset_type": "stock", "currency": "USD", "score": 78,
        "metrics": {"current_price": 100, "analyst_target_mean": 140},
    }, {"risk_profile": "balanced"})
    assert entry["reference_kind"] == "analyst_consensus"
    assert entry["entry_zone_high"] == 112.0
    assert entry["entry_zone_low"] == 100.8
    assert entry["status"] == "below_zone_verify", "100 is just below the balanced entry zone"

    missing_entry = fundamental_service.build_entry_guidance({
        "asset_type": "stock", "currency": "USD", "score": 80, "metrics": {"current_price": 100},
    }, {"risk_profile": "balanced"})
    assert missing_entry["status"] == "insufficient_data"
    assert missing_entry["reference_price"] is None


def test_fundamental_pipeline_without_network():
    original_resolve = fundamental_service.resolve_symbol
    original_history = fundamental_service._history_metrics
    original_globes = finance_engine.finance_engine_globes_data
    try:
        fundamental_service._analysis_cache.clear()
        fundamental_service.resolve_symbol = lambda query: ("FAKE", object(), {
            "symbol": "FAKE", "longName": "Fake Quality Corp", "quoteType": "EQUITY",
            "currency": "USD", "sector": "Technology", "marketCap": 1_000_000_000,
            "currentPrice": 100, "forwardPE": 14, "priceToBook": 2.5,
            "enterpriseToEbitda": 9, "returnOnEquity": 0.28,
            "profitMargins": 0.24, "operatingMargins": 0.25,
            "freeCashflow": 10_000_000, "revenueGrowth": 0.16,
            "earningsGrowth": 0.20, "debtToEquity": 35, "currentRatio": 1.8,
            "targetMeanPrice": 130,
        })
        fundamental_service._history_metrics = lambda ticker: {
            "latest_close": 100, "average_200d": 92, "high_52w": 110, "low_52w": 75,
            "return_1y_pct": 12, "return_3y_annualized_pct": 15,
            "return_5y_annualized_pct": 13, "volatility_1y_pct": 20,
            "max_drawdown_5y_pct": -22,
        }
        result = fundamental_service.analyze_asset("FAKE", force_refresh=True)
        assert result["symbol"] == "FAKE"
        assert result["asset_type"] == "stock"
        assert result["score"] >= 70
        assert result["data_quality"] in ("high", "medium")
        assert result["metric_scores"]["pe_ratio"] is not None
        assert 0 <= result["metric_scores"]["pe_ratio"] <= 100
        result["entry_guidance"] = fundamental_service.build_entry_guidance(
            result, {"risk_profile": "balanced"}
        )
        assert result["entry_guidance"]["reference_price"] == 130
        report = ai_recommendation.format_fundamental_report(result)
        assert "ציונים לפי קטגוריה" in report
        assert "מכפיל רווח" in report and "/100" in report
        assert "טווח כניסה לימודי" in report and "מחיר נוכחי" in report

        finance_engine.finance_engine_globes_data = lambda code: {
            "price": 439.28, "day_change_pct": 0.67, "week_change_pct": 1.47,
            "month_change_pct": 3.64, "year_change_pct": 13.39,
            "name": "אי.בי.אי. מחקה ת״א 125", "source": "Globes",
            "source_url": "https://www.globes.co.il/test",
        }
        israeli = fundamental_service.analyze_asset("5112628", force_refresh=True)
        assert israeli["asset_type"] == "fund"
        assert round(israeli["metrics"]["current_price"], 4) == 4.3928
        assert israeli["metrics"]["quoted_price_agorot"] == 439.28
        assert israeli["metrics"]["return_1y_pct"] == 13.39
        assert israeli["data_source"] == "Globes"
    finally:
        fundamental_service.resolve_symbol = original_resolve
        fundamental_service._history_metrics = original_history
        finance_engine.finance_engine_globes_data = original_globes
        fundamental_service._analysis_cache.clear()


def test_deep_portfolio_thinking_without_network():
    ensure_authenticated()
    connect_firebase.add_holding(TEST_UID, "AAPL", 2, 150)
    original_analyze = fundamental_service.analyze_asset
    try:
        fundamental_service.analyze_asset = lambda symbol: {
            "symbol": symbol,
            "name": f"Test {symbol}",
            "asset_type": "stock",
            "score": 72,
            "data_quality": "good",
            "screening_verdict": "positive",
            "score_breakdown": {},
            "metrics": {},
        }
        result = FB._build_deep_portfolio_analysis(TEST_UID)
        assert result["verified"] is True
        assert result["analyzed_count"] >= 1
        assert "AAPL" in result["portfolio_symbols"]
        stored = connect_firebase.get_user_data(TEST_UID).get("last_deep_analysis")
        assert stored and stored["overall_verdict"] == "healthy_but_watch"
        report = ai_recommendation.format_deep_portfolio_report(
            result, result["analyzed_count"], result["failed_symbols"]
        )
        assert "מצב חשיבה עמוקה" in report and "מעבר AI שני" in report
    finally:
        fundamental_service.analyze_asset = original_analyze


# ===========================================================================
# Runner
# ===========================================================================
ALL_TESTS = [
    test_password_auth,
    test_buy_flow,
    test_sell_flow,
    test_tax_simulator_flow,
    test_sell_all_manual_price_fallback,
    test_cash_and_profile,
    test_names_monthly_display_and_cost_basis_edit,
    test_mid_flow_redirect,
    test_menu_completeness,
    test_excel_import,
    test_ai_fallback_dispatch,
    test_pure_functions,
    test_fundamental_pipeline_without_network,
    test_deep_portfolio_thinking_without_network,
]


def main():
    apply_patches()
    cleanup_test_user()  # defensive: remove any leftover from a previous crashed run

    results = []
    try:
        for test_fn in ALL_TESTS:
            name = test_fn.__name__
            try:
                test_fn()
                results.append((name, True, None))
                print(f"PASS  {name}")
            except Exception as e:
                results.append((name, False, f"{type(e).__name__}: {e}"))
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    finally:
        cleanup_test_user()
        cleaned = verify_cleanup()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, err in results:
        status = "PASS" if ok else "FAIL"
        line = f"  [{status}] {name}"
        if err:
            line += f" — {err}"
        print(line)
    print(f"\n{passed}/{len(results)} tests passed.")
    print(f"Firestore cleanup for {TEST_UID}: {'OK (no leftover document)' if cleaned else 'FAILED — leftover document still exists!'}")

    if passed != len(results) or not cleaned:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
