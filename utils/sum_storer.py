"""Sum storer: the per-currency sum of the trades a scenario nets into one group.

TC010, TC011, TC012, TC013 and TC018 each book several trades and check them as one
netting group. runner.py starts a fresh sum when such a row starts and, after
each trade's own steps, adds the payload step 04 injected for it. Steps 07c, 12
and 12b then require the netting group to hold exactly the stored sum, so what
they expect is always what was really sent, never a figure typed into the
scenario file.

Currency mapping. Each amount is stored under the currency the payload pairs it
with - nearLeg.baseCurrencyAmount under fxCash.baseCurrency and
nearLeg.referenceCurrencyAmount under fxCash.referenceCurrency - never by
position, and never under dealtCurrency, which can name either. A trade is
refused, and the refusal kept in the sum so step 07c fails on it, when:

- the payload is not exactly one trade, or has a farLeg, which would go
  uncounted;
- currencyPair is not baseCurrency followed by referenceCurrency, the two
  currencies are the same, or dealtCurrency is neither of them;
- an amount is not a positive JSON number;
- the base currency buyer and seller are not the party and the counterparty,
  one each, or partyDirection disagrees with them;
- its trade id is not the one step 04 had accepted, or is already in the sum;
- its party, counterparty, currency pair, product or value date differ from the
  first trade's, so it could not net into the same group.

Amounts are read as Decimal straight from the JSON text and added as Decimal,
so ten trades of 6746.74 sum to exactly 67467.40. They are stored as strings.
The sum is recomputed from every trade on each add, never adjusted in place.

A row that cancels one of its trades once they are netted (TC034's
cancelTrade) takes it out of the sum with cancel_trade(), so steps 07c, 12 and
12b expect the net of the trades still live. Its id is kept under 'cancelled'.

Two sums are stored per currency:

- gross: the amounts as booked, added up whichever way they move. This is
  "the sum of the values", shown in reports and assertion messages.
- net, per party: what that party receives (positive) or pays (negative)
  across all the trades, as utils/trade_direction.booked_net() works it out.
  Domain A sees the group from the party's side and Domain B from the
  counterparty's, so each domain checks the net under its own party. The two
  nets are exact opposites.

Stored in .state/sum-<scenario>.json, owner-only like the run state beside it.
"""

import json
import os
from decimal import Decimal

from utils.state_helper import STATE_DIR
from utils.trade_direction import booked_net, net_line, payload_side_problems

# The fields every trade in one netting group must share. A trade that differs
# in any of them nets into another group, so it cannot be part of this sum.
NETTING_KEY = ('party', 'counterParty', 'tradeCurrencyPair', 'productType', 'valueDate')

# The trade fields stored as amounts: written as strings, read back as Decimal.
AMOUNT_FIELDS = ('baseCurrencyAmount', 'referenceCurrencyAmount')


class SumError(ValueError):
    """A trade the sum cannot take, with the reason."""


def sum_path(scenario_id):
    """Return the stored sum's file path for a scenario id."""
    return STATE_DIR / f'sum-{scenario_id}.json'


def _write(scenario_id, document):
    """Write a scenario's sum via a temp file and rename, owner-only (0o600)."""
    path = sum_path(scenario_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(document, indent=2), encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def _read(scenario_id):
    """Return a scenario's sum as stored (amounts as strings), or None if missing or unreadable."""
    path = sum_path(scenario_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def start(scenario_id, recorded_at):
    """Start an empty sum for a scenario's run, replacing any earlier one."""
    _write(scenario_id, {
        'scenario': scenario_id,
        'recordedAt': recorded_at,
        'trades': [],
        'problems': [],
        'gross': {},
        'net': {},
    })


def _amount(value, field):
    """Return a payload amount as a Decimal, or raise SumError.

    Only a JSON number is taken: a string, a boolean or null may be read
    differently by the ledger, so the sum would not be what was booked.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise SumError(f'{field} is {value!r}, not a number')
    amount = Decimal(value)
    if not amount.is_finite() or amount <= 0:
        raise SumError(f'{field} is {value}, not a positive amount')
    return amount


def _text(value, field):
    """Return a non-blank string payload field, or raise SumError."""
    if not isinstance(value, str) or not value.strip():
        raise SumError(f'{field} is {value!r}')
    return value


def payload_trade(payload_text):
    """Read the one trade a step 04 payload injected, with each amount under its own currency.

    :param payload_text: the payload as step 04 recorded it (a list holding one
        trade wrapper).
    :returns: the trade keyed as in run state, amounts as Decimal.
    :raises SumError: when the payload is not one consistent trade.
    """
    try:
        payload = json.loads(payload_text or '', parse_float=Decimal)
    except json.JSONDecodeError as err:
        raise SumError(f'the injected payload is not valid JSON ({err})') from None

    trades = payload if isinstance(payload, list) else [payload]
    if len(trades) != 1 or not isinstance(trades[0], dict):
        raise SumError(f'the injected payload holds {len(trades)} trades, not one')

    wrapper = trades[0].get('wrapper') or {}
    source = (wrapper.get('sourcePlatform') or [{}])[0] or {}
    meta = wrapper.get('metaData') or {}
    fx = (wrapper.get('tradeData') or {}).get('fx') or {}
    fx_cash = fx.get('fxCash') or {}
    near_leg = fx_cash.get('nearLeg')

    if not isinstance(near_leg, dict):
        raise SumError('the payload has no fxCash.nearLeg')
    if fx_cash.get('farLeg'):
        raise SumError('the payload has a farLeg, which the sum does not count')

    base = _text(fx_cash.get('baseCurrency'), 'fxCash.baseCurrency')
    reference = _text(fx_cash.get('referenceCurrency'), 'fxCash.referenceCurrency')
    pair = fx_cash.get('currencyPair')
    if base == reference:
        raise SumError(f'baseCurrency and referenceCurrency are both {base}')
    # The pair is what the netting rule matches on and the two currency fields
    # are what the amounts belong to, so they must say the same thing.
    if pair != f'{base}{reference}':
        raise SumError(f'currencyPair is {pair!r}, but baseCurrency {base} and referenceCurrency '
                       f'{reference} make {base}{reference}')
    dealt = near_leg.get('dealtCurrency')
    if dealt is not None and dealt not in (base, reference):
        raise SumError(f'dealtCurrency {dealt!r} is neither {base} nor {reference}')

    party = _text(meta.get('determinedPartyId'), 'metaData.determinedPartyId')
    counter_party = _text(meta.get('determinedCounterpartyId'), 'metaData.determinedCounterpartyId')
    problems = payload_side_problems(near_leg, party, counter_party)
    if problems:
        raise SumError('; '.join(problems))

    return {
        'tradeId': _text(source.get('tradeId'), 'sourcePlatform[0].tradeId'),
        'party': party,
        'counterParty': counter_party,
        'tradeCurrencyPair': pair,
        'productType': _text(fx.get('productType'), 'fx.productType'),
        'valueDate': _text(near_leg.get('valueDate'), 'nearLeg.valueDate'),
        'baseCurrency': base,
        'referenceCurrency': reference,
        'baseCurrencyAmount': _amount(near_leg.get('baseCurrencyAmount'), 'nearLeg.baseCurrencyAmount'),
        'referenceCurrencyAmount': _amount(near_leg.get('referenceCurrencyAmount'),
                                           'nearLeg.referenceCurrencyAmount'),
        'baseCurrencyBuyer': near_leg.get('baseCurrencyBuyer'),
        'baseCurrencySeller': near_leg.get('baseCurrencySeller'),
    }


def _decimals(trade):
    """Return a stored trade with its amounts as Decimal."""
    return {**trade, **{field: Decimal(trade[field]) for field in AMOUNT_FIELDS}}


def _totals(trades):
    """Return (gross, net) for the trades: gross per currency, net per party per currency."""
    gross = {}
    for trade in trades:
        for currency, field in ((trade['baseCurrency'], 'baseCurrencyAmount'),
                                (trade['referenceCurrency'], 'referenceCurrencyAmount')):
            gross[currency] = gross.get(currency, Decimal(0)) + trade[field]

    net = {}
    if trades:
        for party in (trades[0]['party'], trades[0]['counterParty']):
            net[party] = booked_net(trades, party)
    return gross, net


def add_trade(scenario_id, payload_text, accepted_trade_id):
    """Add one injected trade to a scenario's sum and recompute it.

    :param payload_text: the payload step 04 injected for the trade.
    :param accepted_trade_id: the trade id step 04 saved to state, which it does
        only for a trade the simulator accepted.
    :returns: the trade as added.
    :raises SumError: when the trade cannot be added. The reason is also kept
        in the sum, so the netting group check fails on it.
    """
    document = _read(scenario_id)
    if document is None:
        raise SumError(f'no sum was started for {scenario_id} at {sum_path(scenario_id)}')

    try:
        trade = payload_trade(payload_text)
        if trade['tradeId'] != accepted_trade_id:
            raise SumError(f'the injected payload is trade {trade["tradeId"]}, but step 04 '
                           f'accepted {accepted_trade_id}')

        trades = [_decimals(t) for t in document['trades']]
        if any(t['tradeId'] == trade['tradeId'] for t in trades):
            raise SumError(f'trade {trade["tradeId"]} is already in the sum')
        if trades:
            first = trades[0]
            differs = [f'{key} {trade[key]} (the first trade has {first[key]})'
                       for key in NETTING_KEY if trade[key] != first[key]]
            if differs:
                raise SumError('it cannot net into the same group as the trades before it: '
                               + ', '.join(differs))
    except SumError as err:
        document['problems'].append(f'{accepted_trade_id}: {err}')
        _write(scenario_id, document)
        raise

    trades.append(trade)
    _store_trades(scenario_id, document, trades)
    return trade


def cancel_trade(scenario_id, trade_id):
    """Take a cancelled trade out of a scenario's sum and recompute it.

    A cancelled trade's cashflow leaves the net of its netting group, so the
    group must then hold the sum of the trades still live.

    :raises SumError: when there is no sum, or the trade is not in it.
    """
    document = _read(scenario_id)
    if document is None:
        raise SumError(f'no sum was started for {scenario_id} at {sum_path(scenario_id)}')

    trades = [_decimals(t) for t in document['trades']]
    cancelled = next((t for t in trades if t['tradeId'] == trade_id), None)
    if cancelled is None:
        raise SumError(f'trade {trade_id} is not in the sum, so it cannot be taken out')

    document['cancelled'] = [*(document.get('cancelled') or []), trade_id]
    _store_trades(scenario_id, document, [t for t in trades if t is not cancelled],
                  zero=cancelled)


def _store_trades(scenario_id, document, trades, zero=None):
    """Write `trades` and their recomputed totals into a scenario's sum.

    :param zero: a trade just taken out of the sum. When no trade is left
        (TC050 cancels its only one), the sum nets to 0 in that trade's
        currencies, for both its parties, rather than to nothing, so the
        group is still checked against it.
    """
    gross, net = _totals(trades)
    if zero and not trades:
        gross = {currency: Decimal(0) for currency in (zero['baseCurrency'], zero['referenceCurrency'])}
        net = {party: dict(gross) for party in (zero['party'], zero['counterParty'])}
    document['trades'] = [{**t, **{f: str(t[f]) for f in AMOUNT_FIELDS}} for t in trades]
    document['gross'] = {ccy: str(value) for ccy, value in gross.items()}
    document['net'] = {party: {ccy: str(value) for ccy, value in (amounts or {}).items()}
                       for party, amounts in net.items()}
    _write(scenario_id, document)


def load_sum(scenario_id):
    """Return a scenario's stored sum with every amount as Decimal, or None if there is none.

    :returns: {'scenario', 'recordedAt', 'trades', 'problems', 'gross':
        {currency: Decimal}, 'net': {party: {currency: Decimal}}}.
    """
    document = _read(scenario_id)
    if document is None:
        return None
    try:
        return {
            **document,
            'trades': [_decimals(t) for t in document.get('trades') or []],
            'gross': {ccy: Decimal(v) for ccy, v in (document.get('gross') or {}).items()},
            'net': {party: {ccy: Decimal(v) for ccy, v in amounts.items()}
                    for party, amounts in (document.get('net') or {}).items()},
        }
    except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
        return None


def net_for(stored, party):
    """Return the stored net from `party`'s side, {currency: Decimal}, or None if it has none."""
    return ((stored or {}).get('net') or {}).get(party)


def sum_problems(stored, scenario_id, trade_ids, trade_count, currencies):
    """Return what keeps a stored sum from being the expected sum of a netting group.

    It must exist, have refused no trade, hold exactly `trade_count` trades, be
    the sum of exactly the trades `trade_ids` names (the run's record), and be
    in exactly `currencies` (the row's nettingTotal currencies).

    :returns: a list of problems, empty when the sum can be relied on.
    """
    if stored is None:
        return [f'there is no stored sum for {scenario_id} at {sum_path(scenario_id)}: run '
                f'{scenario_id} again, since its trades add to the sum as they are booked']

    problems = list(stored.get('problems') or [])
    summed = [t['tradeId'] for t in stored['trades']]
    if len(summed) != trade_count:
        problems.append(f'the sum holds {len(summed)} trades, but the row books {trade_count}')
    missing = sorted(set(trade_ids) - set(summed))
    extra = sorted(set(summed) - set(trade_ids))
    if missing:
        problems.append(f'recorded trade(s) missing from the sum: {", ".join(missing)}')
    if extra:
        problems.append(f'trade(s) in the sum that the run did not record: {", ".join(extra)}')
    if set(stored['gross']) != set(currencies):
        problems.append(f'the sum is in {", ".join(sorted(stored["gross"])) or "no currency"}, but '
                        f'the row nets {", ".join(sorted(currencies))}')
    return problems


def sum_line(stored):
    """Describe a stored sum: '10 trades, sum 67467.40 CNH, 10010 USD; BNKA-HK receives ...'."""
    if not stored:
        return 'no stored sum'
    gross = ', '.join(f'{v} {ccy}' for ccy, v in sorted(stored['gross'].items())) or 'nothing'
    trades = stored['trades']
    # With every trade cancelled, the net is still kept, under both parties.
    party = trades[0]['party'] if trades else next(iter(stored.get('net') or {}), None)
    net = net_for(stored, party)
    cancelled = len(stored.get('cancelled') or [])
    return (f'{len(trades)} trades' + (f' ({cancelled} cancelled out)' if cancelled else '')
            + f', sum {gross}'
            + (f'; {net_line(party, net)}' if net is not None else ''))
