import os

from groq import Groq
from tavily import TavilyClient

# llama-3.3-70b-versatile is deprecated (shuts down 2026-08-16); Groq's own
# recommended replacement is gpt-oss-120b. Check console.groq.com/docs/deprecations
# occasionally — Groq retires models roughly every few months.
GROQ_MODEL = "openai/gpt-oss-120b"


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


def _call_groq_with_retry(prompt: str) -> str:
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
        if text:
            return text

    finish_reason = last_response.choices[0].finish_reason if last_response else "unknown"
    raise RuntimeError(
        f"Groq returned an empty answer after retrying (finish_reason={finish_reason}) "
        "— reasoning tokens likely exhausted the budget both times."
    )


def generate_recommendation(valuation: dict, market_context: str) -> str:
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

Total cost basis: {valuation['total_cost']:.2f}
Total current value: {valuation['total_value']:.2f}
Total gain/loss: {valuation['total_gain_loss']:.2f} ({valuation['total_gain_loss_pct']:.1f}%)

Recent news snippets from a live web search about their holdings:
{market_context}

Write in Hebrew, MAX 100 words total (excluding the final line), structured exactly as:
- 2-3 short bullet points: how the portfolio is doing overall, plus anything
  notable from the news snippets above that's relevant to their holdings —
  by name, never by a bare ticker/security number.
- One bullet calling out the SINGLE most significant holding right now
  (biggest position, biggest mover, or the one with the most relevant news)
  by name, with one short, specific, educational observation about it —
  general/educational framing (e.g. concentration risk, a relevant news
  development, worth watching), not a specific "buy/sell" instruction.
- One final line starting with "מסקנה:" — a single-sentence, calm, factual
  takeaway (not a specific "buy/sell" instruction, since you are not a
  licensed financial advisor).

Be tight — no filler, no repeated numbers, no generic disclaimers beyond the
implicit caution in "מסקנה". Never write a bare numeric ticker/security-number
alone anywhere in your answer — always pair it with (or replace it by) its name."""

    return _call_groq_with_retry(prompt)


def answer_question(valuation: dict, market_context: str, question: str) -> str:
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

Total cost basis: {valuation['total_cost']:.2f}
Total current value: {valuation['total_value']:.2f}
Total gain/loss: {valuation['total_gain_loss']:.2f} ({valuation['total_gain_loss_pct']:.1f}%)

Recent news snippets from a live web search about their holdings:
{market_context}

Their question: "{question}"

Answer in Hebrew, MAX 80 words, tight and direct — answer the actual question
first, add only the most relevant supporting detail. Never write a bare
numeric ticker/security-number alone — always use its name when one is given
above. Not a specific "buy/sell" instruction, since you are not a licensed
financial advisor. If the question isn't really about their portfolio, answer
briefly and helpfully anyway."""

    return _call_groq_with_retry(prompt)
