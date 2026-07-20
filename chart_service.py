import io

import matplotlib
matplotlib.use("Agg")  # headless backend; must be set before importing pyplot
import matplotlib.pyplot as plt

# Chart labels stay in English/tickers: matplotlib doesn't shape Hebrew (RTL)
# text correctly without extra handling. The Hebrew summary belongs in the
# Telegram caption text instead, which Telegram renders fine.

def generate_portfolio_pie_chart(holdings: dict) -> io.BytesIO:
    labels = [ticker for ticker, h in holdings.items() if h["market_value"]]
    values = [h["market_value"] for h in holdings.values() if h["market_value"]]

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.set_title("Portfolio Allocation")
    ax.axis("equal")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)  # avoid leaking figures across calls in a long-running bot process
    buf.seek(0)
    return buf
