"""One-line descriptions of the trade under test, for assertion messages.

Every assertion message in the suite ends with one of these lines, so a failure
in any step names the trade it concerns: its trade id and UTI, parties,
product, currency pair, amounts and dates, as step 04 recorded them in run
state. The message itself names the domain and the ledger record, netting group
or settlement the check was made on.
"""

from api.payloads.trade_payload import scenario_netting_group
from utils.flag import resolve_scenario_id
from utils.sum_storer import load_sum, sum_line
from utils.trade_direction import base_currency, side_line


def _amount(value, currency):
    """Return '<amount> <currency>', or None when the amount is not recorded."""
    if value is None:
        return None
    return f'{value} {currency}' if currency else str(value)


def _parties(party, counter_party):
    """Return 'party X, counterparty Y', leaving out whichever is not set."""
    return ', '.join(filter(None, (
        party and f'party {party}',
        counter_party and f'counterparty {counter_party}',
    )))


def trade_line(fields):
    """Describe one trade from its run-state fields, in one line.

    Takes state, or a dict keyed like it: injectedTradeId, uniqueTransactionId,
    reqPartyId, counterPartyId, productType, tradeCurrencyPair, dealtCurrency,
    baseCurrencyAmount, referenceCurrency, referenceCurrencyAmount,
    baseCurrencyBuyer, baseCurrencySeller, tradeDate and valueDate. A field
    that is not set is left out, apart from the trade id, which is always shown
    so no message loses track of its trade:

        Trade MSERV-2185719283 | UTI 549300... | party BNKA-NY, counterparty
        BNKB-NY | FxSpot CNHUSD | amounts 6746.74 CNH / 1001 USD | BNKA-NY buys
        CNH, BNKB-NY buys USD | trade date 2026-09-29 | value date 2026-10-01
    """
    f = fields or {}
    amounts = ' / '.join(filter(None, (
        _amount(f.get('baseCurrencyAmount'), base_currency(f)),
        _amount(f.get('referenceCurrencyAmount'), f.get('referenceCurrency')),
    )))
    parts = (
        f'Trade {f.get("injectedTradeId") or "(no trade id in state)"}',
        f.get('uniqueTransactionId') and f'UTI {f["uniqueTransactionId"]}',
        _parties(f.get('reqPartyId'), f.get('counterPartyId')),
        ' '.join(filter(None, (f.get('productType'), f.get('tradeCurrencyPair')))),
        amounts and f'amounts {amounts}',
        side_line(f),
        f.get('tradeDate') and f'trade date {f["tradeDate"]}',
        f.get('valueDate') and f'value date {f["valueDate"]}',
    )
    return ' | '.join(p for p in parts if p)


def netted_line(state):
    """Describe what a netting group nets: the injected trade, or a set of trades.

    Used from step 08 on, where the check is on the netting group or its
    settlement rather than on one trade. A row that checks several trades as
    one netting group (TC010 to TC013, TC018) has a group netting all of them while
    state holds only the first, so the line gives the sum utils/sum_storer.py
    stored for them instead. Every other run gets trade_line(state). A scenario
    that cannot be read falls back to trade_line too, since a description must
    never be what fails a test.
    """
    state = state or {}
    try:
        scenario_id = resolve_scenario_id()
        group = scenario_netting_group(scenario_id) if scenario_id else None
    except (OSError, ValueError):
        group = None
    if not group:
        return trade_line(state)

    total = group['nettingTotal']
    parts = (
        f'{total["tradeCount"]} {group["tradesOf"]} trades ({scenario_id} netting group)',
        _parties(total['party'], total['counterParty']),
        f'{total["productType"]} {total["tradeCurrencyPair"]}',
        f'stored sum: {sum_line(load_sum(group["tradesOf"]))}',
        state.get('valueDate') and f'value date {state["valueDate"]}',
    )
    return ' | '.join(p for p in parts if p)


def amounts_line(amounts):
    """Format {currency: amount} as '<amount> <currency>, ...' by currency, or 'none'."""
    return ', '.join(
        f'{value} {ccy}' for ccy, value in sorted(amounts.items(), key=lambda kv: str(kv[0]))
    ) or 'none'
