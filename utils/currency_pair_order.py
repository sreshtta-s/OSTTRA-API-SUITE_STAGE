"""Post-parse step: put every parsed trade's currency pair in alphabetical order.

Netting rules are configured with the pair in alphabetical order (CNH-USD), so
a trade is injected the same way round: USDCNH becomes CNHUSD. parse_trades.py
runs this after parsing, just before it writes single_trade.json /
multiple_trade.json.

The flip keeps the trade's economics: each amount stays with its currency, the
base currency buyer and seller swap, and partyDirection flips with them, so
each party still pays and receives the same amounts. dealtCurrency names the
currency of the amount that was dealt, so it stays as it is: a USDCNH trade
dealt in 1000 USD is still dealt in USD once it is CNHUSD. rate is left as
received, as every trade in the pack carries it.

One exception, for amounts entered in the alphabetical pair's order: a flipped
trade whose LARGER_AMOUNT_CURRENCY carries the smaller of its two amounts keeps
the amounts where they are, so the base amount stays the base amount. The
TC018 USDCNH trades arrive as USD 67400 / CNH 10000 for the sheet's 67400 CNH
against 10000 USD; flipped with the amounts following their labels they would
book 10000 CNH / 67400 USD and not net to zero against the CNHUSD pair. The
buyer, seller and partyDirection still flip as for any other trade, and
dealtCurrency moves to the other currency with the amount it named (USD 67400
dealt becomes CNH 67400 dealt).

To remove it, delete order_currency_pairs from POST_PARSE_STEPS in
parse_trades.py, and its import; nothing else uses this module.
"""

from decimal import Decimal

OPPOSITE_DIRECTION = {'Buy': 'Sell', 'Sell': 'Buy'}

# The currency of a pair that is worth less per unit, so a trade must carry
# more of it than of the other: about 6.7 CNH to the USD and 7.8 to the EUR. A
# flipped trade in a pair listed here whose amounts say otherwise was entered
# in the alphabetical order, and keeps its amounts in place. Pairs not listed
# always keep each amount with its currency.
LARGER_AMOUNT_CURRENCY = {
    frozenset({'CNH', 'USD'}): 'CNH',
    frozenset({'CNH', 'EUR'}): 'CNH',
}


def _amount(value):
    """Return a payload amount as a Decimal, or None if it cannot be read."""
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def entered_in_flipped_order(leg, base, reference):
    """Return True if a leg's amounts were entered for the pair the other way round.

    That is, the currency LARGER_AMOUNT_CURRENCY names for the pair carries the
    smaller amount. False when the pair is not listed or an amount cannot be
    read, so the amounts then follow their currencies as usual.
    """
    larger = LARGER_AMOUNT_CURRENCY.get(frozenset({base, reference}))
    base_amount = _amount(leg.get('baseCurrencyAmount'))
    reference_amount = _amount(leg.get('referenceCurrencyAmount'))
    if not larger or base_amount is None or reference_amount is None:
        return False
    if larger == base:
        return base_amount < reference_amount
    return reference_amount < base_amount


def order_pair(trade):
    """Flip one trade's pair into alphabetical order, in place.

    :returns: (the pair as received, True if any leg's amounts were kept in
        place) if it was flipped, else None.
    """
    trade_data = ((trade or {}).get('wrapper') or {}).get('tradeData') or {}
    fx_cash = (trade_data.get('fx') or {}).get('fxCash') or {}
    base, reference = fx_cash.get('baseCurrency'), fx_cash.get('referenceCurrency')
    if not base or not reference or base <= reference:
        return None

    received = fx_cash.get('currencyPair')
    fx_cash['baseCurrency'], fx_cash['referenceCurrency'] = reference, base
    fx_cash['currencyPair'] = f'{reference}{base}'

    kept_amounts = False
    for leg_key in ('nearLeg', 'farLeg'):
        leg = fx_cash.get(leg_key)
        if not leg:
            continue
        kept = entered_in_flipped_order(leg, base, reference)
        if kept:
            kept_amounts = True
        else:
            leg['baseCurrencyAmount'], leg['referenceCurrencyAmount'] = (
                leg.get('referenceCurrencyAmount'), leg.get('baseCurrencyAmount'))
        leg['baseCurrencyBuyer'], leg['baseCurrencySeller'] = (
            leg.get('baseCurrencySeller'), leg.get('baseCurrencyBuyer'))
        leg['partyDirection'] = OPPOSITE_DIRECTION.get(leg.get('partyDirection'), leg.get('partyDirection'))
        # dealtCurrency follows the dealt amount: amounts that move with their
        # currencies leave it as it is, and amounts kept in place take the
        # other currency's label, so it takes that label too.
        if kept and leg.get('dealtCurrency') == base:
            leg['dealtCurrency'] = reference
        elif kept and leg.get('dealtCurrency') == reference:
            leg['dealtCurrency'] = base

    return received, kept_amounts


def order_currency_pairs(trades):
    """Return `trades` with every currency pair in alphabetical order.

    Prints one line naming each trade that was flipped, and whether its amounts
    were kept in place.
    """
    flipped = []
    for trade in trades:
        result = order_pair(trade)
        if result:
            received, kept_amounts = result
            trade_id = trade['wrapper']['sourcePlatform'][0].get('tradeId')
            pair = trade['wrapper']['tradeData']['fx']['fxCash']['currencyPair']
            flipped.append(f'{trade_id} {received} -> {pair}'
                           + (f' (amounts entered in {pair} order, kept in place)' if kept_amounts else ''))

    print(f'STEP  currency pairs ordered: {", ".join(flipped) if flipped else "none to flip"}')
    return trades
