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
from types import SimpleNamespace

import pandas as pd

import Finance_bot as FB
import connect_firebase
import price_service
import ai_recommendation
import portfolio_import
import tax_service
import portfolio_service

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


def fake_get_current_price_full(ticker):
    return FAKE_PRICES.get(ticker.strip().upper())


def fake_get_current_price(ticker):
    r = fake_get_current_price_full(ticker)
    return r["price"] if r else None


def fake_get_current_prices_full(tickers):
    return {t: fake_get_current_price_full(t) for t in tickers}


# ---------------------------------------------------------------------------
# Fake ai_recommendation — canned strings, no Tavily/Groq calls
# ---------------------------------------------------------------------------
def fake_search_market_context(tickers):
    return "FAKE market context (test double, no real Tavily/Groq call)."


def fake_generate_recommendation(valuation, market_context):
    return "FAKE recommendation (test double)."


def fake_answer_question(valuation, market_context, question):
    return "FAKE answer (test double)."


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


fb_groq_holder = {"content": "", "call_count": 0}
import_groq_holder = {"content": "", "call_count": 0}


# ---------------------------------------------------------------------------
# connect_firebase write spies — real Firestore calls still happen (so tests
# can verify end-to-end via connect_firebase.get_portfolio), but every call is
# logged so read-only commands (e.g. /tax) can be asserted to never write.
# ---------------------------------------------------------------------------
_write_call_log = []
_orig_add_holding = connect_firebase.add_holding
_orig_reduce_holding = connect_firebase.reduce_holding
_orig_add_transaction = connect_firebase.add_transaction


def spy_add_holding(*a, **kw):
    _write_call_log.append(("add_holding", a, kw))
    return _orig_add_holding(*a, **kw)


def spy_reduce_holding(*a, **kw):
    _write_call_log.append(("reduce_holding", a, kw))
    return _orig_reduce_holding(*a, **kw)


def spy_add_transaction(*a, **kw):
    _write_call_log.append(("add_transaction", a, kw))
    return _orig_add_transaction(*a, **kw)


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

    ai_recommendation.search_market_context = fake_search_market_context
    ai_recommendation.generate_recommendation = fake_generate_recommendation
    ai_recommendation.answer_question = fake_answer_question

    FB.Groq = make_fake_groq_class(fb_groq_holder)
    portfolio_import.Groq = make_fake_groq_class(import_groq_holder)

    connect_firebase.add_holding = spy_add_holding
    connect_firebase.reduce_holding = spy_reduce_holding
    connect_firebase.add_transaction = spy_add_transaction


# ---------------------------------------------------------------------------
# Firestore cleanup for the test user
# ---------------------------------------------------------------------------
def cleanup_test_user():
    try:
        user_ref = connect_firebase.db.collection("users").document(TEST_UID)
        for doc in user_ref.collection("transactions").stream():
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

    add_holding_calls = [c for c in _write_call_log if c[0] == "add_holding"]
    add_tx_calls = [c for c in _write_call_log if c[0] == "add_transaction"]
    assert len(add_holding_calls) == 1, "expected exactly one add_holding call"
    assert add_holding_calls[0][1] == (TEST_UID, "BUYTST", 3.0, 200.0)
    assert len(add_tx_calls) == 1, "expected exactly one add_transaction call"
    assert add_tx_calls[0][1] == (TEST_UID, "BUYTST", 3.0, 200.0, "buy")

    assert "נרשם" in last_output_text() and "BUYTST" in last_output_text()

    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert "BUYTST" in portfolio
    assert portfolio["BUYTST"]["quantity"] == 3.0
    assert portfolio["BUYTST"]["buy_price"] == 200.0


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
    reduce_calls = [c for c in _write_call_log if c[0] == "reduce_holding"]
    assert len(reduce_calls) == 1 and reduce_calls[0][1] == (TEST_UID, "SELLTST", 4.0)
    assert "נמכר" in last_output_text()

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
    assert not any(c[0] in ("reduce_holding", "add_transaction") for c in _write_call_log), (
        "an over-quantity sell must not touch Firestore"
    )
    portfolio = connect_firebase.get_portfolio(TEST_UID)
    assert portfolio["EXCDTST"]["quantity"] == 2.0, "holding must be unchanged after rejected sell"

    # --- bulk "sell all" flow, triggered by 'הכל' at the ticker step ---
    connect_firebase.add_holding(TEST_UID, "SAALL1", 5, 10)
    connect_firebase.add_holding(TEST_UID, "SAALL2", 2, 20)
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
    reduce_tickers = {c[1][1] for c in _write_call_log if c[0] == "reduce_holding"}
    assert {"SAALL1", "SAALL2"}.issubset(reduce_tickers)
    assert "נמכרו" in last_output_text()

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

    expected = tax_service.estimate_sale_tax(8, 50, 70.0)
    out = last_output_text()
    assert f"{expected['gain']:+.2f}" in out
    assert f"{expected['estimated_tax']:.2f}" in out
    assert f"{expected['net_after_tax']:.2f}" in out

    assert len(_write_call_log) == 0, "the /tax simulator must never write to Firestore"


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
    assert ("פירוט התיק" in out) or ("תיק ריק" in out), "expected portfolio-shaped output"

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
    assert any(c["method"] == "send_photo" for c in recorder.calls) or "ריק" in out, (
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
    assert ("פירוט התיק" in out) or ("תיק ריק" in out)


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

    import_groq_holder["content"] = json.dumps([{"ticker": "XYZ", "quantity": 7, "buy_price": 42.5}])
    import_groq_holder["call_count"] = 0
    holdings2 = portfolio_import.parse_excel_holdings(buf2.getvalue())

    assert import_groq_holder["call_count"] >= 1, "unrecognized headers must go through parse_excel_holdings_ai"
    assert holdings2 == [{"ticker": "XYZ", "quantity": 7.0, "buy_price": 42.5}]


def test_ai_fallback_dispatch():
    ensure_authenticated()

    # --- portfolio ---
    recorder.calls.clear()
    fb_groq_holder["content"] = json.dumps({"action": "portfolio"})
    FB.ai_fallback_handler(make_message("כמה שווה התיק שלי"))
    out = last_output_text()
    assert ("פירוט התיק" in out) or ("תיק ריק" in out)

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
    assert any(c[0] == "add_holding" and c[1] == (TEST_UID, "AIBUY1", 5.0, 33.0) for c in _write_call_log)
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
    fb_groq_holder["content"] = json.dumps({"action": "reply", "text": "זו תשובה לדוגמה מה-AI"})
    FB.ai_fallback_handler(make_message("מי אתה?"))
    assert "זו תשובה לדוגמה מה-AI" in last_output_text()

    # --- malformed / non-JSON model output -> graceful error, not a crash ---
    recorder.calls.clear()
    fb_groq_holder["content"] = "this is not json at all { garbage"
    FB.ai_fallback_handler(make_message("שלום"))
    assert "לא הבנתי את ההודעה" in last_output_text()


def test_pure_functions():
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

    # portfolio_service.compute_portfolio_value edge cases
    portfolio = {"NOPRICE": {"quantity": 10, "buy_price": 5}}
    valuation = portfolio_service.compute_portfolio_value(portfolio, {})
    h = valuation["holdings"]["NOPRICE"]
    assert h["market_value"] is None
    assert h["gain_loss"] is None
    assert h["day_change_pct"] is None

    portfolio2 = {"NOPREVCLOSE": {"quantity": 4, "buy_price": 10}}
    prices2 = {"NOPREVCLOSE": {"price": 12.0, "day_change_pct": None}}
    valuation2 = portfolio_service.compute_portfolio_value(portfolio2, prices2)
    h2 = valuation2["holdings"]["NOPREVCLOSE"]
    assert h2["market_value"] == 48.0
    assert h2["gain_loss"] == 8.0
    assert h2["day_change_pct"] is None
    assert h2["day_change_value"] is None


# ===========================================================================
# Runner
# ===========================================================================
ALL_TESTS = [
    test_password_auth,
    test_buy_flow,
    test_sell_flow,
    test_tax_simulator_flow,
    test_mid_flow_redirect,
    test_menu_completeness,
    test_excel_import,
    test_ai_fallback_dispatch,
    test_pure_functions,
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
