# Rough capital-gains estimate only — NOT tax advice. Uses Israel's flat 25%
# rate for individuals on nominal gain; does not do CPI/inflation adjustment
# (real vs. nominal gain), doesn't offset losses against other positions, and
# doesn't account for foreign withholding tax credits. Good enough for "roughly
# what would I owe if I sold this now", not for filing.
CAPITAL_GAINS_RATE = 0.25


def estimate_sale_tax(quantity: float, buy_price: float, sell_price: float) -> dict:
    quantity = float(quantity)
    proceeds = quantity * float(sell_price)
    cost = quantity * float(buy_price)
    gain = proceeds - cost
    estimated_tax = max(gain, 0) * CAPITAL_GAINS_RATE
    return {
        "proceeds": proceeds,
        "cost": cost,
        "gain": gain,
        "estimated_tax": estimated_tax,
        "net_after_tax": proceeds - estimated_tax,
    }
