"""Convert received trade events into injectable trade payloads.

Mirrors utils/tradeParser.js; used by parse_trades.py. The party identities are
kept from the source event and a past value date moves to today. Most other
fields are copied from the source event, with a fixed default where the source
omits them.
"""

from datetime import datetime

from utils.helpers import TestUtils


def extract_wrapper(node):
    """Return the trade wrapper nested inside a received event, or None.

    Accepts the full publishEvent envelope, any level inside it (eventPayload,
    current, { wrapper }), or a bare wrapper, recognised by having both
    tradeData and sourcePlatform.
    """
    if not isinstance(node, dict):
        return None
    if node.get('publishEvent'):
        return extract_wrapper(node['publishEvent'])
    if node.get('eventPayload'):
        return extract_wrapper(node['eventPayload'])
    if node.get('current'):
        return extract_wrapper(node['current'])
    if node.get('wrapper'):
        return node['wrapper']
    if node.get('tradeData') and node.get('sourcePlatform'):
        return node
    return None


def _parse_date(value):
    """Parse a YYYY-MM-DD date, returning None if it is missing or malformed."""
    try:
        return datetime.strptime(str(value), '%Y-%m-%d')
    except (TypeError, ValueError):
        return None


def resolve_value_date(source_value_date, today):
    """Return the source value date, or today if it has passed or is unreadable.

    A past value date cannot settle, so it becomes today; a missing or
    malformed one does too. A future date is kept exactly as the source had it.
    tradeDate is not handled here: it always keeps the source value, because it
    records when the trade was struck.

    :param today: today's date as YYYY-MM-DD.
    """
    v = _parse_date(source_value_date)
    now = _parse_date(today)
    if v is None or (now is not None and v < now):
        return today
    return source_value_date


def _flatten(raw):
    """Flatten one event, a list of events, or nested lists; drop empty entries."""
    if isinstance(raw, list):
        out = []
        for item in raw:
            out.extend(_flatten(item))
        return out
    return [raw] if raw else []


def parse_trade(raw):
    """Build one injectable trade payload from a received trade event.

    :raises ValueError: if the event has no trade wrapper, tradeId or
        uniqueTransactionId.
    """
    wrapper = extract_wrapper(raw)
    if not wrapper:
        raise ValueError(
            'Could not locate a trade wrapper. Expected '
            'publishEvent.eventPayload.current.wrapper or { wrapper }.'
        )

    meta = wrapper.get('metaData') or {}
    source_platforms = wrapper.get('sourcePlatform') or []
    src_platform = source_platforms[0] if source_platforms else {}
    trade_data = wrapper.get('tradeData') or {}
    doc = trade_data.get('documentationDetail') or {}
    fx = trade_data.get('fx') or {}
    fx_cash = fx.get('fxCash') or {}
    near_leg = fx_cash.get('nearLeg') or {}
    settlement = trade_data.get('settlementDetail') or {}
    perf = meta.get('performanceStats') or {}
    src_parties = trade_data.get('partyDetail')
    src_parties = src_parties if isinstance(src_parties, list) else []

    src_party = next((p for p in src_parties if p.get('partyRole') == 'Party'), {})
    src_counterparty = next((p for p in src_parties if p.get('partyRole') == 'Counterparty'), {})

    # The trade id and UTI must come from the source event; nothing is generated
    # in their place.
    trade_id = src_platform.get('tradeId') or meta.get('sourcePlatformTradeId')
    if not trade_id:
        raise ValueError(
            'Source trade has no tradeId (checked sourcePlatform[0].tradeId '
            'and metaData.sourcePlatformTradeId).'
        )
    if not src_platform.get('uniqueTransactionId'):
        raise ValueError(f'Source trade {trade_id} has no uniqueTransactionId.')

    # The parties are taken from the source event as received.
    party_id = src_party.get('partyId') or meta.get('determinedPartyId')
    counterparty_id = src_counterparty.get('partyId') or meta.get('determinedCounterpartyId')
    if not party_id or not counterparty_id:
        raise ValueError(f'Source trade {trade_id} has no party or counterparty id.')

    today = TestUtils.generate_timestamps()['dateStr']
    value_date = resolve_value_date(near_leg.get('valueDate'), today)

    return {
        'wrapper': {
            'metaData': {
                'determinedCounterpartyId': counterparty_id,
                'determinedCounterpartyName': src_counterparty.get('partyName') or meta.get('determinedCounterpartyName'),
                'determinedPartyId': party_id,
                'determinedPartyName': src_party.get('partyName') or meta.get('determinedPartyName'),
                # The received event has no ingestion time; its receipt time is
                # the closest equivalent.
                'ingestionTimeStamp': perf.get('receiptTimeStamp') or src_platform.get('receiptTimeStamp'),
                # The received event carries no OSTTRA trade id.
                'osttraTradeId': None,
                'partyPerspective': party_id,
                'positionStatus': meta.get('positionStatus') or 'Active',
                'processingStatus': meta.get('processingStatus') or 'Confirmed',
                'status': meta.get('status') or 'New',
                'version': 0,
            },
            'sourcePlatform': [
                {
                    'eventType': src_platform.get('eventType') or 'Trade',
                    'matchId': src_platform.get('matchId'),
                    'messageId': src_platform.get('messageId'),
                    'name': src_platform.get('name') or 'TradeServ',
                    'receiptTimeStamp': src_platform.get('receiptTimeStamp'),
                    'sequence': src_platform.get('sequence'),
                    'submissionSource': src_platform.get('submissionSource'),
                    'submissionTimeStamp': src_platform.get('submissionTimeStamp'),
                    'submissionUserId': src_platform.get('submissionUserId'),
                    'tradeId': trade_id,
                    'tradeSideId': src_platform.get('tradeSideId') or meta.get('tradeSideId'),
                    'tradeStatus': src_platform.get('tradeStatus') or 'CONFIRMED',
                    'type': src_platform.get('type') or 'TradeCapture',
                    'uniqueTransactionId': src_platform.get('uniqueTransactionId'),
                    'version': src_platform.get('version') or '-1',
                }
            ],
            'tradeData': {
                'assetClass': trade_data.get('assetClass') or 'FX',
                'documentationDetail': {
                    'calculationAgent': doc.get('calculationAgent') or '',
                    'contractualDefinitions': doc.get('contractualDefinitions') or ['ISDA1998FX'],
                    'masterAgreementType': doc.get('masterAgreementType') or '',
                },
                'fx': {
                    'activeStatus': fx.get('activeStatus') or 'Active',
                    'confirmationTimeStamp': fx.get('confirmationTimeStamp'),
                    'fxCash': {
                        'baseCurrency': fx_cash.get('baseCurrency'),
                        'currencyPair': fx_cash.get('currencyPair'),
                        'nearLeg': {
                            'baseCurrencyAmount': near_leg.get('baseCurrencyAmount'),
                            'baseCurrencyBuyer': near_leg.get('baseCurrencyBuyer'),
                            'baseCurrencySeller': near_leg.get('baseCurrencySeller'),
                            'dealtCurrency': near_leg.get('dealtCurrency') or fx_cash.get('baseCurrency'),
                            'partyDirection': near_leg.get('partyDirection'),
                            'quoteBasis': near_leg.get('quoteBasis') or 'RefCcyPerBaseCcy',
                            'rate': near_leg.get('rate'),
                            'referenceCurrencyAmount': near_leg.get('referenceCurrencyAmount'),
                            'valueDate': value_date,
                        },
                        'referenceCurrency': fx_cash.get('referenceCurrency'),
                    },
                    'productType': fx.get('productType') or meta.get('productType'),
                    'tradeDate': fx.get('tradeDate') or meta.get('tradeDate'),
                    'transactionType': fx.get('transactionType') or 'Trade',
                },
                'partyDetail': [
                    {
                        'partyFullLegalName': src_party.get('partyFullLegalName'),
                        'partyId': party_id,
                        'partyInstitutionName': src_party.get('partyInstitutionName'),
                        'partyName': src_party.get('partyName'),
                        'partyRole': 'Party',
                        'partySubmissionTimeStamp': src_party.get('partySubmissionTimeStamp'),
                        'partyTradeId': src_party.get('partyTradeId'),
                    },
                    {
                        'partyFullLegalName': src_counterparty.get('partyFullLegalName'),
                        'partyId': counterparty_id,
                        'partyInstitutionName': src_counterparty.get('partyInstitutionName'),
                        'partyName': src_counterparty.get('partyName'),
                        'partyRole': 'Counterparty',
                        'partyTradeId': src_counterparty.get('partyTradeId'),
                    },
                    {
                        'partyId': party_id,
                        'partyRole': 'Submitter',
                    },
                ],
                'settlementDetail': {
                    'pvpFlag': settlement.get('pvpFlag') or 'PVP',
                    'pvpRegistrationStatus': settlement.get('pvpRegistrationStatus') or 'Pre-Accepted',
                    'settlementStatus': settlement.get('settlementStatus') is True,
                },
            },
        }
    }


def parse_trades(raw):
    """Parse one event, a list of events, or nested lists into trade payloads."""
    return [parse_trade(item) for item in _flatten(raw)]
