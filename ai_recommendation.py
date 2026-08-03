import json
import os

from groq import Groq
from tavily import TavilyClient

# llama-3.3-70b-versatile is deprecated (shuts down 2026-08-16); Groq's own
# recommended replacement is gpt-oss-120b. Check console.groq.com/docs/deprecations
# occasionally — Groq retires models roughly every few months.
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_VERIFIER_MODEL = "openai/gpt-oss-20b"


def search_market_context(holdings: list[tuple[str, str | None]]) -> str:
    """Pulls a couple of recent headlines per holding via Tavily (free-tier
    web search) so the LLM has something current to reason over instead of
    just its training data. `holdings` is a list of (ticker, name_or_None)
    pairs — searches by name when one is known, since a raw numeric security
    number (common on Israeli brokerage imports, e.g. "5112628") is not a
    meaningful search term the way a real name or stock symbol is. Kept small
    (2 results, 200 chars each) on purpose — the recommendation itself is
    meant to be short, so it doesn't need a wall of source text to draw from."""
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    snippets = []
    for ticker, name in holdings:
        query_term = name or ticker
        label = f"{name} ({ticker})" if name else ticker
        try:
            result = client.search(f"{query_term} stock news this week", max_results=2)
        except Exception as e:
            snippets.append(f"({label}: news search failed — {e})")
            continue
        for item in result.get("results", []):
            content = (item.get("content") or "")[:200]
            snippets.append(f"- [{label}] {item.get('title', '')}: {content}")
    return "\n".join(snippets) if snippets else "No recent news found."


def _holdings_summary(valuation: dict) -> str:
    holding_lines = []
    for ticker, h in valuation["holdings"].items():
        name = h.get("name")
        label = f"{name} ({ticker})" if name else ticker
        value_str = f"{h['market_value']:.2f}" if h["market_value"] is not None else "unknown"
        gain_str = f"{h['gain_loss']:.2f}" if h["gain_loss"] is not None else "unknown"
        holding_lines.append(
            f"- {label}: {h['quantity']} units, cost basis {h['cost_basis']:.2f}, "
            f"current value {value_str}, gain/loss {gain_str}"
        )
    return "\n".join(holding_lines)


def _financial_assets_summary(valuation: dict) -> str:
    lines = []
    for asset in (valuation.get("financial_assets") or {}).values():
        lines.append(
            f"- {asset.get('name')}: estimated balance {asset.get('estimated_balance', asset.get('reported_balance', 0))}, "
            f"reported balance date {asset.get('balance_as_of')}, latest public report {asset.get('latest_report_period')}, "
            f"latest reported month return {asset.get('monthly_return_pct')}%, 12-month reported return {asset.get('return_12m_pct')}%"
        )
    return "\n".join(lines) if lines else "No long-term savings instruments recorded."


def _call_groq_with_retry(prompt: str, validate=None) -> str:
    """gpt-oss-120b is a reasoning model: its internal "thinking" tokens are
    drawn from the same max_completion_tokens budget as the visible answer,
    and how much it spends thinking is variable — measured runs on this
    exact prompt ranged from ~160 to ~600 reasoning tokens. At effort
    "medium" with a 600-token budget, roughly 1 in 3 calls burned the whole
    budget on reasoning and returned an empty answer. Retry with a lower
    reasoning effort and more headroom before giving up, rather than ever
    silently returning a blank answer."""
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    attempts = [("medium", 1500), ("low", 1500)]
    last_response = None
    for reasoning_effort, max_tokens in attempts:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,  # lower than the API default (1) — steady, factual
                              # tone, not creative variation
            max_completion_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        last_response = response
        text = (response.choices[0].message.content or "").strip()
        if text and (validate is None or validate(text)):
            return text

    finish_reason = last_response.choices[0].finish_reason if last_response else "unknown"
    raise RuntimeError(
        f"Groq returned an empty answer after retrying (finish_reason={finish_reason}) "
        "— reasoning tokens likely exhausted the budget both times."
    )


def _call_groq_json_with_retry(prompt: str, model: str = GROQ_MODEL, validate=None) -> dict:
    """Uses Groq JSON mode and still validates locally before trusting it.

    `validate`, if given, must return True for the parsed dict to be
    accepted. Without it, a reasoning model that spends its whole budget
    "thinking" can return syntactically-valid JSON with the actual answer
    field left empty, and this would return that empty result on the
    first attempt instead of retrying with a fresh reasoning budget."""
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    last_error = None
    for reasoning_effort in ("medium", "low"):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_completion_tokens=3200,
                reasoning_effort=reasoning_effort,
                response_format={"type": "json_object"},
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                value = json.loads(text)
                if isinstance(value, dict) and (validate is None or validate(value)):
                    return value
                last_error = RuntimeError(f"Response failed validation: {value!r}")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Groq did not return valid structured JSON: {last_error}")


def _profile_summary(profile: dict | None) -> str:
    profile = profile or {}
    return (
        f"Name: {profile.get('display_name') or 'not provided'}; "
        f"risk profile: {profile.get('risk_profile') or 'balanced'}; "
        f"horizon: {profile.get('investment_horizon') or 'medium'}; "
        f"goal: {profile.get('investment_goal') or 'long_term_growth'}; "
        f"base currency: {profile.get('base_currency') or 'ILS'}"
    )


def verify_recommendation(draft: str, trusted_facts: str, profile: dict | None = None) -> str:
    """Second-pass fact check required for every generated recommendation.

    The verifier receives the trusted facts again, not merely the first draft,
    and must return a corrected final answer. This catches invented numbers,
    overconfident language, contradictions, and unsuitable risk framing.
    """
    prompt = f"""You are the FINAL fact-checker for an investment assistant.
The trusted facts below are the only source of truth. Review the draft line by
line. Correct any number, unsupported claim, contradiction, stale inference,
or language that sounds like guaranteed/licensed financial advice. Keep the
answer useful and decisive, but frame it as an educational screening view.

Investor profile:
{_profile_summary(profile)}

TRUSTED FACTS:
{trusted_facts}

DRAFT TO VERIFY:
{draft}

Return JSON only:
{{"approved": true/false, "issues": ["short issue"], "final_text": "corrected Hebrew answer"}}
The final_text must be in Hebrew, concise, and must not introduce any fact that
is absent from TRUSTED FACTS. If the draft is correct, preserve it closely."""
    try:
        checked = _call_groq_json_with_retry(
            prompt, model=GROQ_VERIFIER_MODEL,
            validate=lambda v: "מסקנה" in str(v.get("final_text") or ""),
        )
        final_text = str(checked.get("final_text") or "").strip()
        if final_text:
            return final_text
    except Exception:
        pass
    # The verifier model occasionally returns valid-but-empty JSON (or fails
    # outright). Falling back to the unverified draft still gets the user a
    # useful note instead of silently dropping their weekly email entirely.
    return draft


def generate_recommendation(valuation: dict, market_context: str, profile: dict | None = None) -> str:
    """Feeds the user's actual holdings/prices (numbers we trust — computed by
    portfolio_service, not the LLM) plus fresh news snippets (from Tavily) to
    Groq, and asks it to write a short, plain-language recommendation note."""
    prompt = f"""You are a cautious investment assistant writing a SHORT note for a
retail investor. Use only the data given below plus the live web-search news
snippets — do not invent prices or news.

Their current portfolio (each holding shown as "name (ticker)" when a name is
known — ALWAYS refer to a holding by its name in your answer; a bare numeric
ticker/security-number like "5112628" means nothing to this user):
{_holdings_summary(valuation)}

Long-term savings instruments (monthly public data, not live balances):
{_financial_assets_summary(valuation)}

Total cost basis: {valuation['total_cost']:.2f}
Total current value: {valuation['total_value']:.2f}
Free cash: {valuation.get('cash_balance', 0):.2f}
Total account value including cash: {valuation.get('account_total_value', valuation['total_value']):.2f}
Total long-term savings estimate: {valuation.get('savings_total_value', 0):.2f}
Total financial assets: {valuation.get('total_financial_value', valuation.get('account_total_value', 0)):.2f}
Total gain/loss: {valuation['total_gain_loss']:.2f} ({valuation['total_gain_loss_pct']:.1f}%)
Price coverage complete: {valuation.get('pricing_complete', True)}

Investor profile:
{_profile_summary(profile)}

Recent news snippets from a live web search about their holdings:
{market_context}

Write in Hebrew, MAX 130 words total (excluding the final line), structured exactly as:
- 2-3 short bullet points: how the portfolio is doing overall, plus anything
  notable from the news snippets above that's relevant to their holdings —
  by name, never by a bare ticker/security number.
- One bullet calling out the SINGLE most significant holding right now
  (biggest position, biggest mover, or the one with the most relevant news)
  by name, with one short, specific, educational observation about it —
  general/educational framing (e.g. concentration risk, a relevant news
  development, worth watching), not a specific "buy/sell" instruction.
- One bullet titled "כדאי לבדוק:" with ONE concrete, specific, personalized
  educational suggestion the investor could look into this week — grounded
  ONLY in their actual data above (e.g. a holding whose weight now exceeds a
  healthy share of the priced portfolio given their risk profile, free cash
  sitting idle relative to their stated goal/horizon, a savings instrument
  with no recent update, or a holding with unusually large news-driven moves
  worth a closer read). Be specific (name the holding/amount/%), not generic
  advice like "diversify your portfolio". Still education, not a directive.
- One final line starting with "מסקנה:" — a single-sentence, calm, factual
  takeaway (not a specific "buy/sell" instruction, since you are not a
  licensed financial advisor). This line is REQUIRED — never omit it.

Be tight — no filler, no repeated numbers, no generic disclaimers beyond the
implicit caution in "מסקנה". Never write a bare numeric ticker/security-number
alone anywhere in your answer — always pair it with (or replace it by) its name."""

    draft = _call_groq_with_retry(prompt, validate=lambda t: "מסקנה" in t)
    trusted_facts = (
        _holdings_summary(valuation)
        + "\nSavings:\n" + _financial_assets_summary(valuation)
        + f"\nTotals: cost={valuation['total_cost']:.2f}, value={valuation['total_value']:.2f}, "
          f"cash={valuation.get('cash_balance', 0):.2f}, gain_loss={valuation['total_gain_loss']:.2f}, "
          f"gain_loss_pct={valuation['total_gain_loss_pct']:.1f}.\nNews:\n{market_context}"
    )
    return verify_recommendation(draft, trusted_facts, profile)


def generate_fundamental_recommendation(
    analysis: dict,
    market_context: str,
    profile: dict | None = None,
) -> dict:
    """Produces a personalized asset verdict, then independently verifies it."""
    facts = json.dumps(analysis, ensure_ascii=False, default=str)
    prompt = f"""You are a cautious fundamental investment analyst. Analyze the
stock or fund using ONLY the structured facts below and the supplied recent-news
snippets. The deterministic screening score is a signal, not permission to
invent certainty. Personalize suitability to the investor profile.
If entry_guidance is present, respect its exact prices, source label and status.
Explain the conditions briefly; never replace it with a price generated by AI.

Investor profile: {_profile_summary(profile)}
STRUCTURED FACTS: {facts}
RECENT NEWS: {market_context}

Return JSON only with exactly these keys:
{{
  "verdict": "attractive|watch|cautious|avoid_for_now",
  "confidence": 0,
  "headline": "short Hebrew headline",
  "summary": "2-3 concise Hebrew sentences",
  "positives": ["Hebrew point", "Hebrew point"],
  "risks": ["Hebrew risk", "Hebrew risk"],
  "suitability": "one Hebrew sentence tied to this investor's profile",
  "what_to_watch": "one Hebrew sentence",
  "decision": "clear Hebrew educational conclusion on whether it is attractive to consider now",
  "disclaimer": "הערכה לימודית בלבד, לא ייעוץ השקעות."
}}
Use a 0-100 integer confidence. Never invent a ratio, price, forecast, holding,
or news item. For limited data quality, lower confidence and say so."""
    first = _call_groq_json_with_retry(prompt)

    verifier_prompt = f"""You are a senior investment-analysis verifier. The
first analyst produced a structured recommendation. Re-check every claim against
the trusted facts and news below. Correct unsupported claims and ensure the
verdict fits the deterministic score, data quality, risks, and investor profile.
Any price statement must exactly match entry_guidance in the trusted facts.
Do not merely approve it. Return the complete corrected object.

Investor profile: {_profile_summary(profile)}
TRUSTED FACTS: {facts}
TRUSTED NEWS SNIPPETS: {market_context}
FIRST ANALYST OUTPUT: {json.dumps(first, ensure_ascii=False)}

Return JSON only with exactly the same keys as the first output, plus:
"verified": true and "verification_notes": ["short Hebrew correction/check"].
All user-facing prose must be Hebrew. Do not introduce new facts."""
    required = {"verdict", "headline", "summary", "positives", "risks", "suitability", "decision"}
    checked = _call_groq_json_with_retry(
        verifier_prompt, model=GROQ_VERIFIER_MODEL,
        validate=lambda v: required.issubset(v) and bool(str(v.get("summary") or "").strip()),
    )
    checked["verified"] = True
    checked["confidence"] = max(0, min(100, int(checked.get("confidence", 50))))
    checked["disclaimer"] = "הערכה לימודית בלבד, לא ייעוץ השקעות."
    return checked


def _score_visual(score) -> str:
    if score is None:
        return "אין נתון"
    score = max(0, min(100, float(score)))
    filled = int(round(score / 10))
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 80:
        label = "🟢 מצוין"
    elif score >= 65:
        label = "🟢 טוב"
    elif score >= 50:
        label = "🟡 בינוני"
    elif score >= 35:
        label = "🟠 חלש"
    else:
        label = "🔴 חלש מאוד"
    return f"{bar} {score:.0f}/100 · {label}"


def _metric_value(value, suffix="") -> str:
    if value is None:
        return "אין נתון"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}{suffix}"
    return str(value)


def _fundamental_score_sections(analysis: dict) -> tuple[str, str]:
    breakdown = analysis.get("score_breakdown") or {}
    category_labels = {
        "valuation": "תמחור ומכפילים",
        "quality": "איכות ורווחיות",
        "growth": "צמיחה",
        "financial_health_and_risk": "בריאות פיננסית וסיכון",
        "cost": "עלויות",
        "performance": "ביצועים",
        "diversification": "פיזור",
        "risk": "סיכון",
    }
    category_lines = [
        f"• {category_labels.get(key, key)}\n  {_score_visual(score)}"
        for key, score in breakdown.items()
    ]

    metrics = analysis.get("metrics") or {}
    scores = analysis.get("metric_scores") or {}
    if analysis.get("asset_type") == "fund":
        definitions = [
            ("expense_ratio", "דמי ניהול", metrics.get("expense_ratio_pct"), "%"),
            ("return_1y", "תשואה שנה", metrics.get("return_1y_pct"), "%"),
            ("return_3y", "תשואה שנתית 3 שנים", metrics.get("return_3y_annualized_pct"), "%"),
            ("return_5y", "תשואה שנתית 5 שנים", metrics.get("return_5y_annualized_pct"), "%"),
            ("top_10_weight", "משקל 10 הגדולות", metrics.get("top_10_weight_pct"), "%"),
            ("largest_sector_weight", "ריכוז בסקטור הגדול", metrics.get("largest_sector_weight_pct"), "%"),
            ("volatility", "תנודתיות שנתית", metrics.get("volatility_1y_pct"), "%"),
            ("max_drawdown", "ירידה מרבית", metrics.get("max_drawdown_5y_pct"), "%"),
        ]
    else:
        pe = metrics.get("forward_pe") or metrics.get("trailing_pe")
        definitions = [
            ("pe_ratio", "מכפיל רווח", pe, "x"),
            ("price_to_book", "מכפיל הון", metrics.get("price_to_book"), "x"),
            ("enterprise_to_ebitda", "EV/EBITDA", metrics.get("enterprise_to_ebitda"), "x"),
            ("return_on_equity", "תשואה על ההון", metrics.get("return_on_equity_pct"), "%"),
            ("profit_margin", "שולי רווח נקי", metrics.get("profit_margin_pct"), "%"),
            ("operating_margin", "שולי רווח תפעולי", metrics.get("operating_margin_pct"), "%"),
            ("revenue_growth", "צמיחת הכנסות", metrics.get("revenue_growth_pct"), "%"),
            ("earnings_growth", "צמיחת רווח", metrics.get("earnings_growth_pct"), "%"),
            ("debt_to_equity", "חוב להון", metrics.get("debt_to_equity"), ""),
            ("current_ratio", "יחס שוטף", metrics.get("current_ratio"), ""),
            ("volatility", "תנודתיות שנתית", metrics.get("volatility_1y_pct"), "%"),
            ("max_drawdown", "ירידה מרבית", metrics.get("max_drawdown_5y_pct"), "%"),
        ]
    metric_lines = [
        f"• {label}: {_metric_value(value, suffix)}\n  {_score_visual(scores.get(key))}"
        for key, label, value, suffix in definitions
    ]
    return "\n".join(category_lines), "\n".join(metric_lines)


def _format_price(value, currency=None) -> str:
    if value is None:
        return "אין נתון"
    suffix = f" {currency}" if currency else ""
    return f"{float(value):,.2f}{suffix}"


def _entry_guidance_section(analysis: dict) -> str:
    guidance = analysis.get("entry_guidance") or {}
    if not guidance:
        return "🎯 טווח כניסה: אין מספיק נתונים אמינים לחישוב."
    currency = guidance.get("currency")
    comparison = guidance.get("current_vs_reference_pct")
    comparison_text = "אין נתון" if comparison is None else f"{float(comparison):+.1f}%"
    low = guidance.get("entry_zone_low")
    high = guidance.get("entry_zone_high")
    zone = "אין מספיק נתונים" if low is None or high is None else (
        f"{_format_price(low, currency)}–{_format_price(high, currency)}"
    )
    conditions = "\n".join(f"• {item}" for item in guidance.get("conditions_he", [])[:4])
    return "\n".join(part for part in [
        "🎯 מתי ובאיזה מחיר לשקול קנייה",
        f"מחיר נוכחי: {_format_price(guidance.get('current_price'), currency)}",
        f"מחיר ייחוס: {_format_price(guidance.get('reference_price'), currency)}",
        f"מקור הייחוס: {guidance.get('reference_label_he', 'לא זמין')}",
        f"המחיר הנוכחי מול הייחוס: {comparison_text}",
        f"טווח כניסה לימודי: {zone}",
        f"מצב: {guidance.get('status_label_he', 'אין מספיק נתונים')}",
        conditions,
        str(guidance.get("methodology_he") or ""),
    ] if part)


def format_fundamental_report(analysis: dict) -> str:
    ai = analysis.get("ai") or {}
    verdict_labels = {
        "attractive": "🟢 מעניין לבדיקה לקנייה",
        "watch": "🟡 מתאים למעקב",
        "cautious": "🟠 זהירות / המתנה",
        "avoid_for_now": "🔴 לא אטרקטיבי כרגע",
    }
    positives = "\n".join(f"✅ {item}" for item in ai.get("positives", [])[:3])
    risks = "\n".join(f"⚠️ {item}" for item in ai.get("risks", [])[:3])
    category_scores, metric_scores = _fundamental_score_sections(analysis)
    entry_guidance = _entry_guidance_section(analysis)
    return "\n".join(part for part in [
        f"📈 {analysis.get('name')} ({analysis.get('symbol')})",
        f"ציון פונדמנטלי: {analysis.get('score')}/100 · איכות נתונים: {analysis.get('data_quality')}",
        f"מסקנה: {verdict_labels.get(ai.get('verdict'), ai.get('verdict', 'לא זמין'))}",
        f"ביטחון: {ai.get('confidence', 0)}%",
        "",
        entry_guidance,
        "",
        "🏆 ציונים לפי קטגוריה:",
        category_scores,
        "",
        "🔬 ציוני מדדים:",
        metric_scores,
        "",
        str(ai.get("summary") or ""),
        positives,
        risks,
        f"🎯 התאמה אישית: {ai.get('suitability', '')}",
        f"👀 מה לעקוב: {ai.get('what_to_watch', '')}",
        f"🧭 {ai.get('decision', '')}",
        "",
        "הניתוח עבר בדיקת AI שנייה מול הנתונים המקוריים.",
        "הערכה לימודית בלבד, לא ייעוץ השקעות.",
    ] if part)


def _compact_asset_analyses(asset_analyses: list[dict]) -> list[dict]:
    """Keeps only trusted decision-relevant fields for portfolio synthesis."""
    return [{
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "asset_type": item.get("asset_type"),
        "score": item.get("score"),
        "data_quality": item.get("data_quality"),
        "screening_verdict": item.get("screening_verdict"),
        "score_breakdown": item.get("score_breakdown"),
        "metrics": {
            key: (item.get("metrics") or {}).get(key)
            for key in (
                "current_price", "trailing_pe", "forward_pe", "expense_ratio_pct",
                "return_on_equity_pct", "revenue_growth_pct", "profit_margin_pct",
                "debt_to_equity", "return_1y_pct", "volatility_1y_pct",
            ) if (item.get("metrics") or {}).get(key) is not None
        },
    } for item in asset_analyses]


def generate_deep_portfolio_recommendation(
    valuation: dict,
    asset_analyses: list[dict],
    market_context: str,
    profile: dict | None = None,
) -> dict:
    """Full-portfolio thinking mode with a mandatory second audit pass."""
    compact_holdings = {
        symbol: {
            key: holding.get(key)
            for key in (
                "name", "quantity", "buy_price", "current_price", "market_value",
                "gain_loss", "day_change_pct", "week_change_pct", "month_change_pct",
                "year_change_pct", "price_source",
            )
        }
        for symbol, holding in (valuation.get("holdings") or {}).items()
    }
    facts_payload = {
        "portfolio": {
            "holdings": compact_holdings,
            "securities_value": valuation.get("total_value", 0),
            "cash": valuation.get("cash_balance", 0),
            "account_value": valuation.get("account_total_value", 0),
            "priced_cost": valuation.get("priced_cost", valuation.get("total_cost", 0)),
            "gain_loss": valuation.get("total_gain_loss", 0),
            "gain_loss_pct": valuation.get("total_gain_loss_pct", 0),
            "pricing_complete": valuation.get("pricing_complete", True),
            "unpriced_tickers": valuation.get("unpriced_tickers", []),
            "long_term_savings": list((valuation.get("financial_assets") or {}).values()),
            "savings_total": valuation.get("savings_total_value", 0),
            "total_financial_value": valuation.get("total_financial_value", valuation.get("account_total_value", 0)),
            "financial_goal": valuation.get("financial_goal") or {},
            "financial_goal_progress_pct": valuation.get("financial_goal_progress_pct"),
        },
        "fundamental_screening": _compact_asset_analyses(asset_analyses),
    }
    trusted_facts = json.dumps(facts_payload, ensure_ascii=False, default=str)
    prompt = f"""You are running DEEP PORTFOLIO THINKING MODE for a retail investor.
Analyze the entire account as one system, not as isolated tickers. Use ONLY the
trusted facts and news supplied below. Consider allocation/concentration, free
cash, priced vs unpriced holdings, gain/loss, each holding's deterministic
fundamental score, data quality, business/fund quality, risk, diversification,
and fit to the investor profile.

Investor profile: {_profile_summary(profile)}
TRUSTED PORTFOLIO FACTS: {trusted_facts}
RECENT NEWS: {market_context[:2400]}

Give clear educational direction. You may say maintain, monitor, research,
consider reducing concentration, or consider gradual entry—but never claim a
guaranteed outcome or pretend to be a licensed adviser. Do not invent target
weights, prices, assets, tax facts, or news.

Return JSON only with exactly these keys:
{{
  "overall_verdict": "strong|healthy_but_watch|needs_changes|high_risk",
  "confidence": 0,
  "executive_summary": "3-5 clear Hebrew sentences",
  "portfolio_strengths": ["Hebrew point"],
  "portfolio_risks": ["Hebrew point"],
  "holding_actions": [{{"symbol":"...","name":"...","stance":"maintain|watch|research|consider_reduce|insufficient_data","reason":"short Hebrew reason"}}],
  "allocation_actions": ["prioritized Hebrew action"],
  "cash_plan": "Hebrew guidance based on the actual cash balance and profile",
  "next_steps": ["first step", "second step", "third step"],
  "review_triggers": ["specific factual trigger to re-check the portfolio"],
  "disclaimer": "הערכה לימודית בלבד, לא ייעוץ השקעות."
}}
Confidence is a 0-100 integer. Include every holding exactly once in
holding_actions. If a holding has limited/missing data, say insufficient_data
instead of guessing."""
    first = _call_groq_json_with_retry(prompt)

    verifier_prompt = f"""You are the senior auditor for DEEP PORTFOLIO THINKING
MODE. Rebuild and correct the first analyst's result against the original facts.
Check that every holding appears exactly once, all numbers/claims are supported,
cash and concentration are treated correctly, missing data is disclosed, and
the actions fit the investor's risk/horizon without becoming guaranteed or
licensed financial advice.

Investor profile: {_profile_summary(profile)}
TRUSTED FACTS: {trusted_facts}
TRUSTED NEWS: {market_context[:2400]}
FIRST RESULT: {json.dumps(first, ensure_ascii=False)}

Return the complete corrected JSON with the exact same keys, plus
"verified": true and "verification_notes": ["short Hebrew audit note"].
Do not introduce any fact or asset not present in the trusted inputs."""
    required = {
        "overall_verdict", "confidence", "executive_summary", "portfolio_strengths",
        "portfolio_risks", "holding_actions", "allocation_actions", "cash_plan", "next_steps",
    }
    checked = _call_groq_json_with_retry(
        verifier_prompt, model=GROQ_VERIFIER_MODEL,
        validate=lambda v: required.issubset(v) and bool(str(v.get("executive_summary") or "").strip()),
    )
    checked["verified"] = True
    checked["confidence"] = max(0, min(100, int(checked.get("confidence", 50))))
    checked["disclaimer"] = "הערכה לימודית בלבד, לא ייעוץ השקעות."
    return checked


def format_deep_portfolio_report(result: dict, analyzed_count: int, failed_symbols: list[str]) -> str:
    verdicts = {
        "strong": "🟢 תיק חזק יחסית",
        "healthy_but_watch": "🟡 תיק בריא, עם נקודות למעקב",
        "needs_changes": "🟠 נדרשים שינויים",
        "high_risk": "🔴 רמת סיכון גבוהה",
    }
    strengths = "\n".join(f"✅ {item}" for item in result.get("portfolio_strengths", [])[:5])
    risks = "\n".join(f"⚠️ {item}" for item in result.get("portfolio_risks", [])[:5])
    actions = []
    stance_labels = {
        "maintain": "שמירה/מעקב רגיל",
        "watch": "מעקב הדוק",
        "research": "מחקר נוסף",
        "consider_reduce": "לשקול הפחתת ריכוזיות",
        "insufficient_data": "אין מספיק נתונים",
    }
    for item in result.get("holding_actions", []):
        label = item.get("name") or item.get("symbol") or "נייר"
        stance = stance_labels.get(item.get("stance"), item.get("stance", "מעקב"))
        actions.append(f"• {label} ({item.get('symbol', '')}) — {stance}: {item.get('reason', '')}")
    allocation = "\n".join(f"{index}. {item}" for index, item in enumerate(result.get("allocation_actions", [])[:5], 1))
    next_steps = "\n".join(f"{index}. {item}" for index, item in enumerate(result.get("next_steps", [])[:5], 1))
    missing_note = f"\n⚠️ ניתוח פונדמנטלי נכשל עבור: {', '.join(failed_symbols)}" if failed_symbols else ""
    return "\n".join(part for part in [
        "🧠 מצב חשיבה עמוקה — ניתוח התיק המלא",
        f"נותחו {analyzed_count} החזקות · ביטחון {result.get('confidence', 0)}%",
        verdicts.get(result.get("overall_verdict"), result.get("overall_verdict", "")),
        missing_note,
        "",
        str(result.get("executive_summary") or ""),
        "",
        "נקודות חוזקה:", strengths,
        "",
        "סיכונים מרכזיים:", risks,
        "",
        "המלצה לכל החזקה:", "\n".join(actions),
        "",
        "שינויים לפי סדר עדיפות:", allocation,
        "",
        f"💵 תוכנית מזומן: {result.get('cash_plan', '')}",
        "",
        "צעדים הבאים:", next_steps,
        "",
        "✓ ההמלצה נבדקה מחדש מול כל הנתונים על ידי מעבר AI שני.",
        "הערכה לימודית בלבד, לא ייעוץ השקעות.",
    ] if part)


def answer_question(
    valuation: dict,
    market_context: str,
    question: str,
    profile: dict | None = None,
) -> str:
    """Same grounding as generate_recommendation (real holdings + live news),
    but answers an arbitrary free-text question instead of writing a fixed-
    shape note. Used by both the Telegram AI fallback's "reply" action and the
    website's AI Q&A (relayed through Firestore — see connect_firebase.py's
    watch_pending_ai_requests)."""
    prompt = f"""You are a cautious investment assistant answering a retail investor's
question about their own portfolio. Use only the data given below plus the
live web-search news snippets — do not invent prices or news.

Their current portfolio (each holding shown as "name (ticker)" when a name is
known — ALWAYS refer to a holding by its name in your answer; a bare numeric
ticker/security-number like "5112628" means nothing to this user):
{_holdings_summary(valuation)}

Long-term savings instruments (monthly public data, not live balances):
{_financial_assets_summary(valuation)}

Total cost basis: {valuation['total_cost']:.2f}
Total current value: {valuation['total_value']:.2f}
Total gain/loss: {valuation['total_gain_loss']:.2f} ({valuation['total_gain_loss_pct']:.1f}%)
Free cash: {valuation.get('cash_balance', 0):.2f}
Total financial assets including savings: {valuation.get('total_financial_value', valuation.get('account_total_value', 0)):.2f}
Financial goal: {json.dumps(valuation.get('financial_goal') or {}, ensure_ascii=False, default=str)}
Investor profile: {_profile_summary(profile)}

Recent news snippets from a live web search about their holdings:
{market_context}

Their question: "{question}"

Answer in Hebrew, MAX 80 words, tight and direct — answer the actual question
first, add only the most relevant supporting detail. Never write a bare
numeric ticker/security-number alone — always use its name when one is given
above. Not a specific "buy/sell" instruction, since you are not a licensed
financial advisor. If the question isn't really about their portfolio, answer
briefly and helpfully anyway."""

    draft = _call_groq_with_retry(prompt)
    trusted_facts = (
        _holdings_summary(valuation)
        + "\nSavings:\n" + _financial_assets_summary(valuation)
        + f"\nTotals: value={valuation['total_value']:.2f}, cash={valuation.get('cash_balance', 0):.2f}, "
          f"gain_loss={valuation['total_gain_loss']:.2f}.\nNews:\n{market_context}\nQuestion: {question}"
    )
    return verify_recommendation(draft, trusted_facts, profile)
