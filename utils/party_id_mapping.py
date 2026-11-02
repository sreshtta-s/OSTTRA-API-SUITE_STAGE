"""Post-parse step: rename party ids in parsed trades to the hyphenated spelling.

Received TradeServ events spell the test banks with an underscore (BNKA_HK),
while data/trade_scenarios.py NETTING_RULES and the scenario files use a hyphen
(BNKA-HK), so a parsed trade would otherwise find no netting rule.
parse_trades.py runs this after parsing, just before it writes
single_trade.json / multiple_trade.json.

Once the ids are renamed, a trade whose trading party is in
SWAP_TO_COUNTERPARTY is restated from the other side, so that party becomes
the counterparty; see swap_to_counterparty().

To extend it, add an entry to PARTY_ID_RENAMES or SWAP_TO_COUNTERPARTY. To
remove the swap alone, delete its call at the end of rename_party_ids(). To
remove the whole step, delete rename_party_ids from POST_PARSE_STEPS in
parse_trades.py, and its import; nothing else uses this module.
"""

# Received spelling -> spelling written to the injection files. Only a value
# that is exactly one of these keys is renamed, so a string that merely
# contains one, such as a composite key, is left alone.
PARTY_ID_RENAMES = {
    'BNKA_ABC': 'BNKA-ABC',
    'BNKA_HK': 'BNKA-HK',
    'BNKA_MY': 'BNKA-MY',
    'BNKA_NY': 'BNKA-NY',
    'BNK_MEGA': 'BNK-MEGA',
    'BNKB_HK': 'BNKB-HK',
    'BNKB_MY': 'BNKB-MY',
    'BNKB_NY': 'BNKB-NY',
    'BNK_GIGA': 'BNK-GIGA',
}

# Parties that belong on the counterparty side, in the renamed spelling. A
# trade whose trading party is one of these has its two sides swapped.
SWAP_TO_COUNTERPARTY = {'BNKB-HK', 'BNKB-MY', 'BNKB-NY'}

# partyDetail fields that identify a party, and so move with it on a swap. The
# role and the submission timestamp stay on their entry, where the parser
# writes them.
PARTY_IDENTITY_FIELDS = ('partyFullLegalName', 'partyId', 'partyInstitutionName', 'partyName', 'partyTradeId')

OPPOSITE_DIRECTION = {'Buy': 'Sell', 'Sell': 'Buy'}


def _rename(node, counts):
    """Return a copy of `node` with every PARTY_ID_RENAMES value renamed, tallying each."""
    if isinstance(node, dict):
        return {key: _rename(value, counts) for key, value in node.items()}
    if isinstance(node, list):
        return [_rename(item, counts) for item in node]
    if isinstance(node, str) and node in PARTY_ID_RENAMES:
        counts[node] = counts.get(node, 0) + 1
        return PARTY_ID_RENAMES[node]
    return node


def rename_party_ids(trades):
    """Return `trades` with every PARTY_ID_RENAMES value renamed, wherever it occurs.

    Walks each whole trade, so the id is renamed in every field that holds it:
    metaData, partyDetail, the base currency buyer and seller, and the
    calculation agent. Prints one line saying what was renamed, then runs
    swap_to_counterparty() on the result.
    """
    counts = {}
    renamed = [_rename(trade, counts) for trade in trades]

    if counts:
        summary = ', '.join(f'{old} -> {PARTY_ID_RENAMES[old]} x{n}' for old, n in counts.items())
        print(f'STEP  party ids renamed: {summary}')
    else:
        print('STEP  party ids renamed: none found')

    # Delete this line to keep each trade's trading party as received.
    renamed = swap_to_counterparty(renamed)

    return renamed


def _swap_sides(trade):
    """Restate one trade with its counterparty as the trading party, in place.

    Swaps the party and counterparty in metaData and partyDetail, points
    partyPerspective and the submitter at the new trading party, and flips
    partyDirection, which is stated from the trading party's side. The base
    currency buyer and seller are left alone, so the economics do not change.
    """
    wrapper = trade['wrapper']
    meta = wrapper['metaData']
    meta['determinedPartyId'], meta['determinedCounterpartyId'] = (
        meta.get('determinedCounterpartyId'), meta.get('determinedPartyId'))
    meta['determinedPartyName'], meta['determinedCounterpartyName'] = (
        meta.get('determinedCounterpartyName'), meta.get('determinedPartyName'))
    meta['partyPerspective'] = meta['determinedPartyId']

    trade_data = wrapper.get('tradeData') or {}
    parties = trade_data.get('partyDetail') or []
    party = next((p for p in parties if p.get('partyRole') == 'Party'), None)
    counterparty = next((p for p in parties if p.get('partyRole') == 'Counterparty'), None)
    if party is not None and counterparty is not None:
        for field in PARTY_IDENTITY_FIELDS:
            party[field], counterparty[field] = counterparty.get(field), party.get(field)
    for entry in parties:
        if entry.get('partyRole') == 'Submitter':
            entry['partyId'] = meta['determinedPartyId']

    fx_cash = (trade_data.get('fx') or {}).get('fxCash') or {}
    for leg_key in ('nearLeg', 'farLeg'):
        leg = fx_cash.get(leg_key)
        if leg and leg.get('partyDirection') in OPPOSITE_DIRECTION:
            leg['partyDirection'] = OPPOSITE_DIRECTION[leg['partyDirection']]


def swap_to_counterparty(trades):
    """Return `trades` with each SWAP_TO_COUNTERPARTY trading party moved to the counterparty side.

    A trade whose counterparty is on the list too is left as it is, since the
    swap would only put another listed party in front. Prints one line saying
    how many trades were swapped, per party.
    """
    counts = {}
    for trade in trades:
        meta = trade['wrapper']['metaData']
        party, counterparty = meta.get('determinedPartyId'), meta.get('determinedCounterpartyId')
        if party in SWAP_TO_COUNTERPARTY and counterparty not in SWAP_TO_COUNTERPARTY:
            _swap_sides(trade)
            counts[party] = counts.get(party, 0) + 1

    if counts:
        summary = ', '.join(f'{party} x{n}' for party, n in counts.items())
        print(f'STEP  trading party moved to counterparty: {summary}')
    else:
        print('STEP  trading party moved to counterparty: none found')

    return trades
