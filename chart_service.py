import io

import matplotlib
matplotlib.use("Agg")  # headless backend; must be set before importing pyplot
import matplotlib.pyplot as plt
from bidi.algorithm import get_display


def _visual_label(holding: dict) -> str:
    """Use the asset name only—never expose a numeric security id in the pie."""
    name = str(holding.get("name") or "נייר ללא שם").strip()
    if len(name) > 42:
        name = name[:39].rstrip() + "…"
    return get_display(name)


def build_portfolio_chart_labels(holdings: dict) -> list[str]:
    return [
        _visual_label(holding)
        for holding in holdings.values()
        if (holding.get("market_value") or 0) > 0
    ]

def generate_portfolio_pie_chart(holdings: dict) -> io.BytesIO:
    priced = [holding for holding in holdings.values() if (holding.get("market_value") or 0) > 0]
    labels = [_visual_label(holding) for holding in priced]
    values = [holding["market_value"] for holding in priced]

    fig, ax = plt.subplots(figsize=(9, 6.5))
    wedges, _texts, percent_texts = ax.pie(
        values,
        labels=None,
        autopct="%1.1f%%",
        startangle=90,
        pctdistance=0.72,
        wedgeprops={"linewidth": 1.2, "edgecolor": "white"},
    )
    for percent in percent_texts:
        percent.set_fontsize(10)
        percent.set_fontweight("bold")
    ax.legend(
        wedges,
        labels,
        title=get_display("שמות הנכסים"),
        loc="center left",
        bbox_to_anchor=(1.0, 0.5),
        frameon=False,
        prop={"family": "DejaVu Sans", "size": 10},
        title_fontproperties={"family": "DejaVu Sans", "size": 11, "weight": "bold"},
    )
    ax.set_title(get_display("חלוקת תיק ההשקעות"), fontfamily="DejaVu Sans", fontsize=15, fontweight="bold")
    ax.axis("equal")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)  # avoid leaking figures across calls in a long-running bot process
    buf.seek(0)
    return buf
