# Rough capital-gains estimate only — NOT tax advice. Uses Israel's flat 25%
# rate for individuals on nominal gain; does not do CPI/inflation adjustment
# (real vs. nominal gain), doesn't offset losses against other positions, and
# doesn't account for foreign withholding tax credits. Good enough for "roughly
# what would I owe if I sold this now", not for filing.
CAPITAL_GAINS_RATE = 0.25
AS_TRADE_RATE = 0.00059
AS_TRADE_TASE_MINIMUM = 1.90
AS_TRADE_TASE_FUND_MINIMUM = 16.0
AS_TRADE_FOREIGN_PER_SHARE_USD = 0.01
AS_TRADE_FOREIGN_MINIMUM_USD = 4.90
AS_TRADE_FOREIGN_FRACTIONAL_FLAT_USD = 2.50


def estimate_trade_commission(
    quantity: float,
    gross_value_ils: float,
    *,
    market: str = "tase",
    instrument_type: str = "security",
    fx_rate_to_ils: float = 1.0,
) -> dict:
    """Estimate the user's Altshuler Shaham Trade annex commission.

    The annex pasted by the user overrides the public tariff for this estimate:
    TASE 0.059%, minimum ILS 1.90 for ordinary securities, tracking mutual
    funds and ETFs traded in the continuous session; ILS 16 for other mutual
    funds. Foreign securities cost USD 0.01/share with a USD 4.90 minimum,
    while a fractional-security transaction of up to one unit costs USD 2.50.
    """
    quantity = float(quantity)
    gross_value_ils = max(float(gross_value_ils), 0.0)
    market = str(market or "tase").lower()
    instrument_type = str(instrument_type or "security").lower()
    if market == "foreign":
        if quantity <= 1.0:
            amount_usd = AS_TRADE_FOREIGN_FRACTIONAL_FLAT_USD
            return {
                "amount_ils": amount_usd * max(float(fx_rate_to_ils or 1.0), 0.0),
                "amount_quote": amount_usd,
                "quote_currency": "USD",
                "label": "מסחר בשבר של עד יחידה אחת — ‎$2.50 לפי הנספח",
            }
        amount_usd = max(quantity * AS_TRADE_FOREIGN_PER_SHARE_USD, AS_TRADE_FOREIGN_MINIMUM_USD)
        return {
            "amount_ils": amount_usd * max(float(fx_rate_to_ils or 1.0), 0.0),
            "amount_quote": amount_usd,
            "quote_currency": "USD",
            "label": "1 סנט למניה, מינימום ‎$4.90 לפי הנספח",
        }
    minimum = AS_TRADE_TASE_FUND_MINIMUM if instrument_type == "other_mutual_fund" else AS_TRADE_TASE_MINIMUM
    amount = max(gross_value_ils * AS_TRADE_RATE, minimum)
    if instrument_type == "etf":
        label = "0.059%, מינימום 1.90 ₪ לקרן סל בת״א במסלול רציף"
    elif instrument_type == "tracking_fund":
        label = "0.059%, מינימום 1.90 ₪ לקרן נאמנות מחקה בת״א"
    elif instrument_type == "other_mutual_fund":
        label = "0.059%, מינימום 16.00 ₪ לקרן נאמנות רגילה בת״א"
    else:
        label = "0.059%, מינימום 1.90 ₪ לנייר בת״א שאינו קרן"
    return {
        "amount_ils": amount,
        "amount_quote": amount,
        "quote_currency": "ILS",
        "label": label,
    }


def estimate_sale_from_amounts(proceeds: float, cost: float, sale_commission: float = 0.0) -> dict:
    proceeds = float(proceeds)
    cost = float(cost)
    sale_commission = max(float(sale_commission or 0), 0.0)
    gross_gain = proceeds - cost
    taxable_gain = proceeds - sale_commission - cost
    estimated_tax = max(taxable_gain, 0) * CAPITAL_GAINS_RATE
    return {
        "proceeds": proceeds,
        "cost": cost,
        "gain": gross_gain,
        "sale_commission": sale_commission,
        "taxable_gain": taxable_gain,
        "estimated_tax": estimated_tax,
        "net_after_tax": proceeds - estimated_tax,
        "net_after_tax_and_fees": proceeds - sale_commission - estimated_tax,
        "net_gain_after_tax_and_fees": taxable_gain - estimated_tax,
    }


def estimate_sale_tax(
    quantity: float,
    buy_price: float,
    sell_price: float,
    unit_scale: float = 1.0,
    sale_commission: float = 0.0,
) -> dict:
    quantity = float(quantity)
    unit_scale = float(unit_scale)
    proceeds = quantity * float(sell_price) * unit_scale
    cost = quantity * float(buy_price) * unit_scale
    return estimate_sale_from_amounts(proceeds, cost, sale_commission)
