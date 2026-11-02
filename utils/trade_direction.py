"""Which way each currency of a trade moves, as each domain sees it.

A trade's payload names the party that buys its base currency
(nearLeg.baseCurrencyBuyer) and the party that sells it (baseCurrencySeller).
The buyer receives the base currency and pays the reference currency; the
seller does the reverse. Each domain states the trade from its own trading
party's side: Domain A from the trade's party (reqPartyId), Domain B from its
counterparty (counterPartyId), so the same trade reads the opposite way round
on the two domains.

The ledger (steps 05 / 05b) states the side as BUY / SELL on each leg asset;
netting underlyings (07 / 07b / 07c) and settlement candidates (12 / 12b) as
RECEIVABLE / PAYABLE per currency. Checked on both domains on 2026-10-06 for a
TC010 trade in which BNKA-HK buys 6746.74 CNH from BNKB-HK for 1001 USD:

    Domain A (BNKA-HK)   CNH BUY  RECEIVABLE   USD SELL PAYABLE
    Domain B (BNKB-HK)   CNH SELL PAYABLE      USD BUY  RECEIVABLE

partyDirection is the trading party's side of the base currency: Buy when it
is the base currency buyer. Every received TradeServ event states it that way,
and utils/currency_pair_order.py and utils/party_id_mapping.py keep it so when
they flip a pair or swap the sides. payload_side_problems() checks a payload
for it before injection.

The trade dicts here are keyed as in run state: baseCurrency (or, failing
that, dealtCurrency), referenceCurrency, baseCurrencyAmount,
referenceCurrencyAmount, baseCurrencyBuyer and baseCurrencySeller.
"""

from decimal import Decimal

# "The party receives this currency" -> the word each API uses for it.
LEDGER_SIDES = {True: 'BUY', False: 'SELL'}
CASH_SIDES = {True: 'RECEIVABLE', False: 'PAYABLE'}

# The sign a netting leg's amount takes from its trading party's side.
CASH_SIGNS = {'RECEIVABLE': 1, 'PAYABLE': -1}

# partyDirection for "the trading party buys the base currency".
PARTY_DIRECTIONS = {True: 'Buy', False: 'Sell'}


def _amount(value):
    """Return a value as a Decimal, or None if it cannot be parsed."""
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def base_currency(trade):
    """Return the trade's base currency, falling back to its dealt currency."""
    return trade.get('baseCurrency') or trade.get('dealtCurrency')


def buys_base(trade, party):
    """Return True if `party` buys the trade's base currency, False if it sells it.

    None when the trade does not name `party` as exactly one of its base
    currency buyer and seller, so no side can be expected of it.
    """
    buyer, seller = trade.get('baseCurrencyBuyer'), trade.get('baseCurrencySeller')
    if not party or buyer == seller:
        return None
    if party == buyer:
        return True
    if party == seller:
        return False
    return None


def expected_sides(trade, party, vocabulary):
    """Return {currency: side} for the trade's two currencies from `party`'s side.

    :param vocabulary: LEDGER_SIDES or CASH_SIDES.
    :returns: {} when the trade does not say which way they move for `party`.
    """
    receives_base = buys_base(trade, party)
    base, reference = base_currency(trade), trade.get('referenceCurrency')
    if receives_base is None or not base or not reference or base == reference:
        return {}
    return {base: vocabulary[receives_base], reference: vocabulary[not receives_base]}


def booked_net(trades, party):
    """Return {currency: amount} `party` nets across `trades`, received positive, paid negative.

    :returns: None if any trade does not say which way its currencies move for
        `party`, or lacks an amount, since the net would then be a guess.
    """
    net = {}
    for trade in trades:
        receives_base = buys_base(trade, party)
        base, reference = base_currency(trade), trade.get('referenceCurrency')
        base_amount = _amount(trade.get('baseCurrencyAmount'))
        reference_amount = _amount(trade.get('referenceCurrencyAmount'))
        if receives_base is None or None in (base, reference, base_amount, reference_amount):
            return None
        sign = 1 if receives_base else -1
        net[base] = net.get(base, Decimal(0)) + sign * base_amount
        net[reference] = net.get(reference, Decimal(0)) - sign * reference_amount
    return net


def leg_amount(leg):
    """Return a netting leg's amount from its trading party's side, paid negative.

    :returns: None if the amount cannot be read or the direction is neither
        RECEIVABLE nor PAYABLE.
    """
    value = _amount(leg.get('assetValue'))
    sign = CASH_SIGNS.get(leg.get('direction'))
    if value is None or sign is None:
        return None
    return sign * value


def sides_of_net(net, vocabulary):
    """Return {currency: side} for each currency a net position moves; a zero net has none."""
    return {currency: vocabulary[value > 0] for currency, value in (net or {}).items() if value}


def net_line(party, net):
    """Describe a net position: 'BNKA-HK receives 67467.40 CNH, pays 10010 USD'."""
    parts = []
    for currency, value in sorted((net or {}).items(), key=lambda kv: str(kv[0])):
        if value > 0:
            parts.append(f'receives {value} {currency}')
        elif value < 0:
            parts.append(f'pays {-value} {currency}')
        else:
            parts.append(f'nets 0 {currency}')
    return f'{party} {", ".join(parts) or "moves nothing"}'


def side_line(trade):
    """Describe who buys which currency: 'BNKA-HK buys CNH, BNKB-HK buys USD', or None."""
    buyer, seller = trade.get('baseCurrencyBuyer'), trade.get('baseCurrencySeller')
    base, reference = base_currency(trade), trade.get('referenceCurrency')
    if not (buyer and seller and base and reference):
        return None
    return f'{buyer} buys {base}, {seller} buys {reference}'


def payload_side_problems(near_leg, party, counterparty):
    """Return what is inconsistent about which way a payload leg's currencies move.

    The base currency buyer and seller must be the trade's party and
    counterparty, one each, and partyDirection must be the party's side of the
    base currency. Received TradeServ events always agree on all three.

    :returns: a list of problems, empty when the leg is consistent.
    """
    buyer, seller = near_leg.get('baseCurrencyBuyer'), near_leg.get('baseCurrencySeller')
    direction = near_leg.get('partyDirection')

    if buyer == seller or {buyer, seller} != {party, counterparty}:
        return [f'baseCurrencyBuyer [{buyer}] and baseCurrencySeller [{seller}] should be '
                f'the party [{party}] and the counterparty [{counterparty}], one each']

    party_buys = buyer == party
    if direction != PARTY_DIRECTIONS[party_buys]:
        return [f'partyDirection is [{direction}], but the party [{party}] is the base '
                f'currency {"buyer" if party_buys else "seller"}, so it should be '
                f'[{PARTY_DIRECTIONS[party_buys]}]']

    return []
