"""Trade payload builder for step 04, with one route per injection mode.

The mode is set in utils/flag.py:

  SCENARIO       one row of data/osttra_test_scenarios/, named by flag.SCENARIO
  DYNAMIC        a payload generated from run state and generated ids
  SINGLE_FILE    api/json_payload_trade_injection/single_trade.json
  MULTIPLE_FILE  one trade of multiple_trade.json, picked by TRADE_INDEX
  IMPORT         as MULTIPLE_FILE; runner.py imports the day's trades first
  IMPORT_EXCEL   as IMPORT for a trade the Excel names for a test case, else
                 that case's scenario; runner.py names both per trade. A case
                 with no scenario file runs its trades as SINGLE_FILE

Only DYNAMIC builds the trade from the parties and currencies step 04 reads
from run state. The file and scenario routes inject the payload's own, and its
business fields are written back to run state for the later steps. This module
also loads and validates scenario files and resolves their {{TOKEN}}
placeholders.

Sections, in order: constants; step 04's entry point, get_trade_payload, and
one build_*_payload function per route; payload files and run state; product
type; scenario files (loading and validation, then what a scenario runs); and
{{TOKEN}} resolution.
"""

import difflib
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from config.steps import compare_tokens, get_step
from utils.flag import resolve_injection_mode, resolve_scenario_id, scenario_source
from utils.state_helper import StateHelper


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

PAYLOAD_DIR = Path(__file__).resolve().parent.parent / 'json_payload_trade_injection'

# One JSON file per row of the test sheet, each holding its trade in the same
# wrapper shape the DYNAMIC route builds. Selected with INJECTION_MODE =
# 'SCENARIO' and SCENARIO = '<id>' in utils/flag.py.
#
# A file pins only the values the sheet states. Whatever the sheet leaves open
# (trade ids, references, timestamps, guids, dates) is a {{TOKEN}} resolved per
# run, so a scenario never injects a stale date or a trade id already used.
SCENARIO_DIR = Path(__file__).resolve().parent.parent.parent / 'data' / 'osttra_test_scenarios'

# Index of the scenario trade this process injects. The runner sets it per trade
# so the simulator is called once per trade, as TRADE_INDEX does for
# MULTIPLE_FILE. Defined here, not in utils/flag.py: flag.py picks the payload
# source, this module indexes into it.
SCENARIO_TRADE_INDEX_ENV = 'SCENARIO_TRADE_INDEX'

# JSON identity shared by every trade in a duplicate set. A duplicate must carry
# the same tradeId, tradeSideId and uniqueTransactionId as the trade it copies,
# but each pipeline run generates its own, so the runner generates one identity
# per set and passes it here. It is generated per run rather than pinned in the
# scenario file, so one run's set never duplicates the previous run's trades.
SCENARIO_SHARED_IDS_ENV = 'SCENARIO_SHARED_IDS'

# The keys each level of a scenario file may use. The loader reads only the keys
# it knows, so a mistyped one ("delaysBefore", "nettingtotal") would silently
# drop its setting and the row would run as a weaker test; any other key is
# rejected instead. The "scenario" block describes the test sheet's row and is
# not checked. nettingTotal's keys are NETTING_TOTAL_FIELDS, plus the optional
# NETTING_TOTAL_GROUP_AMOUNTS.
SCENARIO_KEYS = ('scenario', 'trade', 'trades', 'runUpTo', 'nettingTotal', 'continues',
                 'cancelTrade', 'cancelSettlement', 'expect', 'skip')
ENTRY_KEYS = ('trade', 'label', 'repeat', 'runUpTo', 'expect', 'delayBefore', 'identifiers',
              'cancel', 'cancelSettlement')

# The step a cancelled trade's steps follow (runner.py's CANCEL_STEPS):
# steps 04 cancel, 05ct_a and 05ct_b run straight after step 05b.
CANCEL_CHECKS_AFTER = '5b'

# The step a cancelled settlement's steps follow by default, and the earliest
# they may (runner.py's SETTLEMENT_CANCEL_STEPS): steps 13cs_a and 13cs_b run
# straight after step 13c, whether it runs or the row skips it, or after the
# later step the object form's "after" names (15b in TC038, once the
# settlement is complete). Steps 15cs_a and 15cs_b run straight after them, or
# after the later step its "checkAfter" names. Its "expect" declares the
# outcome of any of the four, and its "delayBefore" how long to wait before
# any of them (30 seconds before 13cs_a in TC037).
SETTLEMENT_CANCEL_AFTER = '13c'
SETTLEMENT_CANCEL_KEYS = ('after', 'checkAfter', 'expect', 'delayBefore')
SETTLEMENT_CANCEL_STEP_TOKENS = ('13cs_a', '13cs_b')
SETTLEMENT_CHECK_STEP_TOKENS = ('15cs_a', '15cs_b')

# A netting row's cancelTrade: which trade to cancel, the group step it is
# cancelled after, and the outcome declared for any of the cancel steps
# (runner.py's CANCEL_STEPS tokens).
CANCEL_TRADE_KEYS = ('trade', 'after', 'expect')
CANCEL_STEP_TOKENS = ('4cancel', '5ct_a', '5ct_b')

# The last group step before the netting total (07c), the only check of the
# group's net after a cancel takes a trade out of it. A netting row whose
# runUpTo is this step stops its group here, without the total: steps 07 / 07b
# are then the only check of its net, as after a trade cancelled after step 06b.
NETTING_TOTAL_AFTER = '7b'

# Values "identifiers" may take: "shared" makes every copy a duplicate of one trade.
IDENTIFIER_MODES = ('shared',)

# Keys a nettingTotal must state: whose trades, how many, and the currencies
# their netting group holds. The amounts are the sum utils/sum_storer.py stores
# from the injected trades.
NETTING_TOTAL_FIELDS = (
    'tradeCount', 'party', 'counterParty', 'tradeCurrencyPair', 'productType',
    'baseCurrency', 'referenceCurrency',
)

# An optional nettingTotal key: {currency: amount}, what the whole netting
# group must net to as the test sheet states it, other cases' trades that share
# the group included (TC013), or 0 in each currency for a group whose only
# trade is cancelled (TC045, TC050). Step 07c checks the group's balance against
# it, unsigned, in place of the stored sum.
NETTING_TOTAL_GROUP_AMOUNTS = 'groupAmounts'

# Step at which runner.py starts a netting group check: step 06, finding the
# group from the first of its trades.
NETTING_GROUP_START_STEP = '6'

# Step at which runner.py resumes a netting group check after its netting
# total: step 08, closing the netting group.
CONTINUATION_RESUME_STEP = '8'

# {{NAME}}: uppercase letters, digits and underscores, plus the sign and digits
# of the T+N / T-N date offsets.
TOKEN_RE = re.compile(r'\{\{([A-Z0-9_+\-]+)\}\}')
OFFSET_TOKEN_RE = re.compile(r'^T([+-])(\d+)$')

# DYNAMIC builds the trade itself, so its product type is read from productType
# in run state, which runner.py seeds from .state.example.json. The static file
# modes carry productType in their own JSON, which is injected as is.
PRODUCT_TYPES = ['FxSpot', 'FxForward']
DEFAULT_PRODUCT_TYPE = 'FxSpot'

# The ledger reports the injected product as productType "FX" plus a
# productSubType. FxSpot -> SPOT is confirmed against the live ledger; FxForward
# -> FORWARD follows the same naming. Asserting the mapping makes a different
# spelling fail as a named mismatch.
PRODUCT_SUB_TYPES = {
    'FxSpot': 'SPOT',
    'FxForward': 'FORWARD',
}

# metaData.status step 04's cancel variant (tests/test_04_cancel_trade.py)
# sends in place of the payload's own "New". Every route's payload carries it.
CANCELLED_STATUS = 'Cancelled'


# ---------------------------------------------------------------------------
# STEP 04'S ENTRY POINT: ONE ROUTE PER INJECTION MODE
# ---------------------------------------------------------------------------

def get_trade_payload(party_id, counter_party_id, currency_pair_format, dealt_currency,
                      reference_currency, t, ids, iso_timestamp, guid1, guid2):
    """Build the trade payload for the active injection mode.

    The mode comes only from utils/flag.py; .env has no say. Every route returns
    a one-element list. Routes that take the trade from a file or a scenario
    copy its identifiers into `ids`, so step 04 records what was injected.

    :param t: timestamps from TestUtils.generate_timestamps(); t['dateStr'] is
        the run date.
    :param ids: trade identifiers generated by step 04; updated in place.
    """
    # ROUTE 0: SCENARIO - one row of data/osttra_test_scenarios/
    # resolve_scenario_id() returns None in every other mode, so those fall
    # through to the routes below.
    #
    # The scenario file defines the whole trade, so the state-derived parties
    # and currencies passed in are unused here; build_scenario_payload() writes
    # the scenario's own back to state for steps 05 onward.
    #
    # IMPORT_EXCEL sets TRADE_INDEX for a trade the Excel names, which comes
    # from multiple_trade.json (ROUTE 2) even though its case has a scenario:
    # the scenario then supplies only the flow. It injects nothing else: a case
    # the Excel names no trade for (N/A) is skipped, not given its scenario's
    # own trade.
    scenario_id = resolve_scenario_id()
    mode = resolve_injection_mode()
    excel_trade = mode == 'IMPORT_EXCEL' and str(os.environ.get('TRADE_INDEX') or '').strip() != ''

    # Without TRADE_INDEX there is no Excel trade to pick: the scenario route
    # would generate one, and ROUTE 2 would inject trade 0 of whatever
    # multiple_trade.json last held.
    if mode == 'IMPORT_EXCEL' and not excel_trade:
        raise ValueError(
            'IMPORT_EXCEL injects only the Excel\'s trades, under runner.py, which names '
            'each by TRADE_INDEX; none is set.'
        )

    if scenario_id and not excel_trade:
        return build_scenario_payload(scenario_id, t, ids, iso_timestamp, guid1, guid2)

    # ROUTE 1: SINGLE STATIC FILE
    if mode == 'SINGLE_FILE':
        return build_single_file_payload(ids)

    # ROUTE 2: MULTIPLE STATIC FILE (loop driven by runner.py). IMPORT and
    # IMPORT_EXCEL inject the same file; they differ only in how runner.py fills it.
    if mode in ('MULTIPLE_FILE', 'IMPORT', 'IMPORT_EXCEL'):
        return build_multiple_file_payload(ids, scenario_id)

    # ROUTE 3: DYNAMIC PAYLOAD (default)
    return build_dynamic_payload(party_id, counter_party_id, currency_pair_format, dealt_currency,
                                 reference_currency, t, ids, iso_timestamp, guid1, guid2)


def build_single_file_payload(ids):
    """Build the SINGLE_FILE route's payload: the first trade of single_trade.json.

    :returns: a one-element list holding the trade, like every route.
    """
    selected_trade = load_trades('single_trade.json')[0]
    adopt_static_ids(selected_trade, ids)
    return [selected_trade]


def build_multiple_file_payload(ids, scenario_id):
    """Build the MULTIPLE_FILE / IMPORT / IMPORT_EXCEL route's payload: one trade of multiple_trade.json.

    The trade is the one TRADE_INDEX names, which runner.py sets per trade.

    :param scenario_id: the IMPORT_EXCEL case's scenario, or None.
    :returns: a one-element list holding the trade, like every route.
    """
    trades = load_trades('multiple_trade.json')
    # runner.py sets TRADE_INDEX per trade; a non-integer becomes -1 and
    # fails the range check below.
    raw_index = os.environ.get('TRADE_INDEX', '0')

    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        index = -1

    if index < 0 or index >= len(trades):
        raise ValueError(
            f'TRADE_INDEX={raw_index} is out of range for multiple_trade.json '
            f'({len(trades)} trades, valid range 0-{len(trades) - 1}).'
        )

    selected_trade = trades[index]
    adopt_static_ids(selected_trade, ids)

    # An Excel trade in a case with a scenario is that case's trade, so
    # state describes it before injection, as for a scenario's own trade:
    # a trade the simulator rejects still reports its parties and pair.
    if scenario_id:
        sync_scenario_state(selected_trade)

    return [selected_trade]


def build_dynamic_payload(party_id, counter_party_id, currency_pair_format, dealt_currency,
                          reference_currency, t, ids, iso_timestamp, guid1, guid2):
    """Build the DYNAMIC route's payload from the parties and currencies in run state.

    :returns: a one-element list holding the trade, like every route.
    """
    # Resolved before building so a bad productType fails here, naming the key,
    # rather than as a rejected injection downstream.
    product_type = resolve_product_type()

    # The pair is the trade's two currencies in alphabetical order (CNHUSD, as
    # the netting rule's CNH-USD); it does not say which is the base, so a seed
    # pair of USDCNH is sent as CNHUSD. A seed pair naming other currencies
    # fails here rather than as a trade no netting rule matches.
    currencies = sorted(c for c in (dealt_currency, reference_currency) if c)
    seed_pair = str(currency_pair_format or '').upper()
    if len(currencies) != 2 or sorted((seed_pair[:3], seed_pair[3:])) != currencies:
        raise ValueError(
            f".state.example.json: tradeCurrencyPair '{currency_pair_format}' does not name "
            f"dealtCurrency '{dealt_currency}' and referenceCurrency '{reference_currency}'."
        )
    currency_pair = ''.join(currencies)
    # Later steps match the netting rule on state's pair, so it must be the one sent.
    StateHelper.save_state({'tradeCurrencyPair': currency_pair})

    return [
        {
            'wrapper': {
                'metaData': {
                    'determinedCounterpartyId': counter_party_id,
                    'determinedPartyId': party_id,
                    'ingestionTimeStamp': iso_timestamp,
                    'osttraTradeId': guid1,
                    'partyPerspective': party_id,
                    'positionStatus': 'Active',
                    'processingStatus': 'Confirmed',
                    'status': 'New',
                    'version': 0,
                },
                'sourcePlatform': [
                    {
                        'eventType': 'Trade',
                        'matchId': guid2,
                        'messageId': guid2,
                        'name': 'TradeServ',
                        'receiptTimeStamp': iso_timestamp,
                        'sequence': 1,
                        'submissionSource': 'WEB',
                        'submissionTimeStamp': iso_timestamp,
                        'submissionUserId': 'Stephen Toal',
                        'tradeId': ids['tradeId'],
                        'tradeSideId': ids['tradeSideId'],
                        'tradeStatus': 'CONFIRMED',
                        'type': 'TradeCapture',
                        'uniqueTransactionId': ids['uniqueTransactionId'],
                        'version': '-1',
                    }
                ],
                'tradeData': {
                    'assetClass': 'FX',
                    'documentationDetail': {'contractualDefinitions': ['ISDA1998FX']},
                    'fx': {
                        'activeStatus': 'Active',
                        'confirmationTimeStamp': iso_timestamp,
                        'fxCash': {
                            'baseCurrency': dealt_currency,
                            'currencyPair': currency_pair,
                            # Written for the CNHUSD pair in state: the party
                            # buys 689 CNH (base) and pays 95 USD (reference).
                            'nearLeg': {
                                'baseCurrencyAmount': 689,
                                'baseCurrencyBuyer': party_id,
                                'baseCurrencySeller': counter_party_id,
                                'dealtCurrency': dealt_currency,
                                'partyDirection': 'Buy',
                                'quoteBasis': 'RefCcyPerBaseCcy',
                                'rate': 1.1,
                                'referenceCurrencyAmount': 95,
                                'valueDate': t['dateStr'],
                            },
                            'referenceCurrency': reference_currency,
                        },
                        'productType': product_type,
                        'tradeDate': t['dateStr'],
                        'transactionType': 'Trade',
                    },
                    'partyDetail': [
                        {'partyId': party_id, 'partyRole': 'Party', 'partyTradeId': ids['partyTradeId']},
                        {'partyId': counter_party_id, 'partyRole': 'Counterparty', 'partyTradeId': ids['counterpartyTradeId']},
                        {'partyId': party_id, 'partyRole': 'Submitter'},
                    ],
                    'settlementDetail': {
                        'pvpFlag': 'PVP',
                        'pvpRegistrationStatus': 'Pre-Accepted',
                        'settlementStatus': False,
                    },
                },
            }
        }
    ]


def build_scenario_payload(scenario_id, t, ids, iso_timestamp, guid1, guid2):
    """Build the SCENARIO route's payload: load, resolve tokens, adopt ids, sync state.

    :returns: a one-element list holding the resolved trade, like every route.
    """
    scenario = load_scenario(scenario_id)
    meta = scenario.get('scenario') or {}
    index, entry = select_scenario_entry(scenario_id)

    today = datetime.strptime(t['dateStr'], '%Y-%m-%d').date()
    tokens = build_scenario_tokens(t, ids, iso_timestamp, guid1, guid2)
    trade = resolve_scenario_tokens(entry['trade'], tokens, today, scenario_id)

    # Step 04 reports and saves identifiers from its own `ids`. Copy in the ids
    # actually injected (a shared identity, or a literal id a file pins) so the
    # trade is not injected under one id and tracked under another.
    adopt_static_ids(trade, ids)

    sync_scenario_state(trade)

    total = len(scenario_trades(scenario_id))
    where = f' trade {index + 1} of {total}' if total > 1 else ''
    label = entry.get('label')

    print(f'SCENARIO {scenario_id} [{meta.get("traceabilityId") or "no traceability id"}] '
          f'-> {meta.get("highLevelScenario") or "no description"}')
    print(f'SCENARIO {scenario_id}{where}'
          f'{f" [{label}]" if label else ""}: '
          f'expectation {meta.get("expectation") or "not stated"}.')

    return [trade]


# ---------------------------------------------------------------------------
# PAYLOAD FILES AND RUN STATE
# ---------------------------------------------------------------------------

def load_trades(file_name):
    """Load a static payload file as a flat list of trades.

    The hand-maintained files come in three shapes, all flattened:

      { wrapper }          a bare object
      [ { wrapper } ]      single-element array   (single_trade.json)
      [ [ { wrapper } ] ]  array of arrays        (multiple_trade.json)

    Raises if the file is missing, is not valid JSON, or holds no trades.
    """
    file_path = PAYLOAD_DIR / file_name

    if not file_path.exists():
        raise FileNotFoundError(
            f'INJECTION_MODE={resolve_injection_mode()} requires {file_name}, '
            f'but it is missing at {file_path}'
        )

    try:
        parsed = json.loads(file_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as err:
        raise ValueError(f'{file_name} is not valid JSON: {err}') from err

    trades = flatten_trades(parsed)

    if not trades:
        raise ValueError(f'{file_name} parsed successfully but contains no trades.')

    return trades


def flatten_trades(node):
    """Flatten a trade file of any nesting depth into a list, dropping empty items.

    Public because runner.py flattens multiple_trade.json with it too, so each
    TRADE_INDEX it hands out selects the trade it announces.
    """
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(flatten_trades(item))
        return out
    return [node] if node else []


def adopt_static_ids(trade, ids):
    """Copy the payload's own trade identifiers into `ids`, in place.

    Step 04 logs and saves identifiers from `ids`, so this keeps what it
    records in line with the ids actually injected. Does nothing when the
    payload has no sourcePlatform entry.
    """
    source_platforms = ((trade or {}).get('wrapper') or {}).get('sourcePlatform') or []
    source = source_platforms[0] if source_platforms else None
    if not source:
        return

    ids['tradeId'] = source.get('tradeId')
    ids['uniqueTransactionId'] = source.get('uniqueTransactionId')
    ids['tradeSideId'] = source.get('tradeSideId')

    parties = (trade['wrapper'].get('tradeData') or {}).get('partyDetail')
    if isinstance(parties, list):
        if len(parties) > 0 and parties[0]:
            ids['partyTradeId'] = parties[0].get('partyTradeId')
        if len(parties) > 1 and parties[1]:
            ids['counterpartyTradeId'] = parties[1].get('partyTradeId')


def set_trade_status(trades, status):
    """Set wrapper.metaData.status on every trade of a payload, in place.

    Safe on any route's payload: each builds its trades afresh, from a file
    read or a resolved copy of the scenario, so nothing cached is changed.
    """
    for trade in trades:
        trade['wrapper'].setdefault('metaData', {})['status'] = status


def get_payload_state_fields(trade):
    """Return the trade's business fields from its payload, keyed as in run state.

    Used where the payload, not state, defines the trade (the static files and
    scenarios), so downstream steps validate the trade actually injected rather
    than the seeded values. Fields absent from the payload are omitted so they
    never blank out state.
    """
    wrapper = (trade or {}).get('wrapper')
    if not wrapper:
        return {}

    source_platforms = wrapper.get('sourcePlatform') or []
    source = source_platforms[0] if source_platforms else {}
    trade_data = wrapper.get('tradeData') or {}
    fx = trade_data.get('fx') or {}
    fx_cash = fx.get('fxCash') or {}
    near_leg = fx_cash.get('nearLeg') or {}
    meta_data = wrapper.get('metaData') or {}

    fields = {
        'injectedTradeId':     source.get('tradeId'),
        'uniqueTransactionId': source.get('uniqueTransactionId'),
        'reqPartyId':          meta_data.get('determinedPartyId'),
        'counterPartyId':      meta_data.get('determinedCounterpartyId'),
        'productType':         fx.get('productType'),
        'tradeCurrencyPair':   fx_cash.get('currencyPair'),
        'referenceCurrency':   fx_cash.get('referenceCurrency'),
        'dealtCurrency':       near_leg.get('dealtCurrency') or fx_cash.get('baseCurrency'),
        # Mirrored so the report's Trade Date column shows this trade's own date
        # rather than falling back to the value date.
        'tradeDate':           fx.get('tradeDate'),
        'valueDate':           near_leg.get('valueDate'),
    }

    return {k: v for k, v in fields.items() if v is not None}


def sync_scenario_state(trade):
    """Write the scenario trade's business fields to run state before injection.

    A scenario sets its own parties, currencies, product and dates, so state
    seeded from .state/default.json does not describe the trade. Step 04
    mirrors the same fields only after a 2xx; writing them here first means
    the later steps and the Excel row describe the scenario trade even if it
    is rejected.

    injectedTradeId and uniqueTransactionId are left out: step 04 writes them
    only once the server accepts the trade, so state never holds a trade id for
    a rejected trade.

    :returns: the fields written.
    """
    fields = get_payload_state_fields(trade)
    for owned_by_step_04 in ('injectedTradeId', 'uniqueTransactionId'):
        fields.pop(owned_by_step_04, None)

    if fields:
        StateHelper.save_state(fields)

    return fields


# ---------------------------------------------------------------------------
# PRODUCT TYPE
# ---------------------------------------------------------------------------

def resolve_product_type():
    """Return the configured product type: the scenario's, else productType from state.

    An unset or empty productType falls back to FxSpot, matching the Excel
    column default in runner.py. An unrecognised value raises, as utils/flag.py
    does for a mistyped mode, rather than injecting a trade the ledger rejects
    for a reason that is hard to trace back to a typo.

    An active scenario's product type comes from its own file and is not checked
    against PRODUCT_TYPES; step 04 compares the injected product with it.
    """
    scenario_id = resolve_scenario_id()
    if scenario_id:
        return scenario_product_type(scenario_id)

    configured = (StateHelper.get_state() or {}).get('productType')

    if configured is None or configured == '':
        return DEFAULT_PRODUCT_TYPE

    if configured not in PRODUCT_TYPES:
        raise ValueError(
            f".state.example.json: productType '{configured}' is not a supported "
            f'product type. Expected one of: {", ".join(PRODUCT_TYPES)}.'
        )

    return configured


def scenario_product_type(scenario_id):
    """Return the productType carried by the selected scenario trade's payload.

    Read from the trade, not the scenario metadata, so there is one source of
    truth. Deliberately not checked against PRODUCT_TYPES: a scenario may pin an
    unsupported product to prove it is rejected.
    """
    _, entry = select_scenario_entry(scenario_id)
    fx = ((entry['trade']['wrapper'].get('tradeData') or {}).get('fx')) or {}
    product_type = fx.get('productType')

    if not product_type:
        raise ValueError(
            f'Scenario {scenario_id} has no tradeData.fx.productType, so there '
            'is nothing for step 04 to record or for step 06 to pick a netting '
            'rule with.'
        )

    return product_type


def expected_product_sub_type(injected_product_type):
    """Return the productSubType the ledger should report for an injected product.

    Takes the product type step 04 read from the injected payload, so it is
    right in every mode. Returns None for a product with no known mapping.
    """
    return PRODUCT_SUB_TYPES.get(injected_product_type)


# ---------------------------------------------------------------------------
# SCENARIO FILES: LOADING AND VALIDATION
# ---------------------------------------------------------------------------

def available_scenarios():
    """Return every scenario id in SCENARIO_DIR, for listing in error messages."""
    if not SCENARIO_DIR.is_dir():
        return []
    return sorted(path.stem for path in SCENARIO_DIR.glob('*.json'))


def load_scenario(scenario_id):
    """Load one scenario file and validate its shape.

    Every error names the file, and a missing file lists the scenarios that do
    exist, so a mistyped SCENARIO fails here with an obvious cause.

    :returns: the parsed document, with each trade entry's step tokens
        (runUpTo, expect, delayBefore) normalised to the runner's spelling.
    """
    path = SCENARIO_DIR / f'{scenario_id}.json'

    if not path.exists():
        known = ', '.join(available_scenarios()) or 'none found'
        raise FileNotFoundError(
            f"{scenario_source()} '{scenario_id}' has no scenario file at "
            f'{path}. Available scenarios: {known}.'
        )

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as err:
        raise ValueError(f'{path.name} is not valid JSON: {err}') from err

    if not isinstance(document, dict):
        raise ValueError(
            f'{path.name} must be an object with a "scenario" block and either '
            f'a "trade" or a "trades" key, not a {type(document).__name__}.'
        )

    _reject_unknown_keys(document, SCENARIO_KEYS, path.name)

    # A top-level runUpTo is the netting group's; with no nettingTotal nothing
    # reads it, so a row meant to stop early would run every step.
    if document.get('runUpTo') is not None and document.get('nettingTotal') is None:
        raise ValueError(
            f'{path.name}: a top-level "runUpTo" applies only to a row with a nettingTotal. '
            'To stop a trade early, put runUpTo on its entry in "trades".'
        )

    # cancelTrade cancels one of the trades a row nets together once they are
    # in their group, so only a row that runs its own group's steps can use it.
    if document.get('cancelTrade') is not None and (document.get('nettingTotal') is None
                                                    or document.get('continues') is not None):
        raise ValueError(
            f'{path.name}: "cancelTrade" cancels one of the row\'s own trades once they net '
            'into one group, so it needs a nettingTotal and a "trades" list, and cannot be '
            'used on a continuation.'
        )

    # A top-level cancelSettlement cancels the netting group's settlement, so
    # only a row that runs its own group's steps up to step 13c can use it. An
    # empty object is falsy but still asks for the cancel, so only an absent
    # key or false lets a row without a group through.
    if document.get('cancelSettlement') not in (None, False) and (
            document.get('nettingTotal') is None or document.get('continues') is not None):
        raise ValueError(
            f'{path.name}: a top-level "cancelSettlement" cancels the settlement of the row\'s '
            'own netting group, so it needs a nettingTotal and cannot be used on a '
            'continuation. To cancel one trade\'s settlement, put it on its entry in "trades".'
        )
    _validate_cancel_settlement(document, path.name, document.get('runUpTo'), group=True)

    # A top-level expect or skip names steps of the row's own netting group,
    # which nothing else runs, so they would silently not apply.
    for key in ('expect', 'skip'):
        if document.get(key) is not None and (document.get('nettingTotal') is None
                                              or document.get('continues') is not None):
            raise ValueError(
                f'{path.name}: a top-level "{key}" names steps of the row\'s own netting group, '
                'so it needs a nettingTotal and cannot be used on a continuation.'
                + (' To declare a trade\'s own step outcomes, put expect on its entry in '
                   '"trades".' if key == 'expect' else '')
            )

    # A continuation (a row that checks the trades an earlier row booked) books
    # nothing itself: `continues` names the row it checks and `nettingTotal`
    # says what to check.
    if document.get('continues') is not None:
        _validate_continuation(document, path)
        return document

    # A row that injects one trade writes `trade`; a row that injects several
    # writes `trades`, and the runner gives each trade (after repeats are
    # expanded) its own pipeline run, so the simulator is called once per trade.
    entries = document.get('trades')

    if entries is None:
        if not isinstance((document.get('trade') or {}).get('wrapper'), dict):
            raise ValueError(f'{path.name} has no "trade.wrapper" object to inject.')
        if document.get('nettingTotal') is not None:
            raise ValueError(
                f'{path.name}: a nettingTotal checks several trades in one netting group, '
                'so it needs a "trades" list, not a single "trade".'
            )
        return document

    if not isinstance(entries, list) or not entries:
        raise ValueError(f'{path.name} has a "trades" key with no entries to inject.')

    for index, entry in enumerate(entries):
        where = f'{path.name} trades[{index}]'
        if not isinstance(entry, dict):
            raise ValueError(f'{where} must be an object.')
        _reject_unknown_keys(entry, ENTRY_KEYS, where)
        if not isinstance((entry.get('trade') or {}).get('wrapper'), dict):
            raise ValueError(f'{where} has no "trade.wrapper" object to inject.')

        # Anything but "shared" would be ignored, and the copies would quietly
        # get their own trade ids instead of being duplicates of one trade.
        identifiers = entry.get('identifiers')
        if identifiers is not None and identifiers not in IDENTIFIER_MODES:
            raise ValueError(
                f'{where}: "identifiers" must be {" or ".join(map(json.dumps, IDENTIFIER_MODES))}, '
                f'got {identifiers!r}.'
            )

        # Number of times to inject this entry. Each copy is a distinct trade
        # with the same economics; see _expand_repeats().
        repeat = entry.get('repeat', 1)
        if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
            raise ValueError(
                f'{where}: "repeat" must be a whole number of trades, 1 or more, '
                f'got {repeat!r}.'
            )

        # The last step this trade runs. The runner matches it exactly, so a
        # typo ("05b") would never match and the trade would run on to 15b.
        run_up_to = entry.get('runUpTo')
        if run_up_to is not None:
            run_up_to = entry['runUpTo'] = _pipeline_step(run_up_to, where, 'runUpTo')

        # {step token: 'pass'|'fail'} the runner reconciles step results with.
        # The runner ignores any other outcome or step, so a typo would report
        # an expected failure as a real one; it is rejected here instead.
        expect = entry.get('expect') or {}
        if not isinstance(expect, dict):
            raise ValueError(f'{where}: "expect" must be an object of step -> pass|fail.')
        for token, outcome in expect.items():
            if outcome not in ('pass', 'fail'):
                raise ValueError(
                    f'{where}: expect["{token}"] must be "pass" or "fail", '
                    f'got {outcome!r}.'
                )
        entry['expect'] = _step_keys(expect, where, 'expect', run_up_to)

        # {step token: seconds} the runner waits before starting that step, for
        # rows that test a late-arriving message. A mistyped token would never
        # match a step, so the delay would silently not happen and the row
        # would pass without testing the late arrival.
        delays = entry.get('delayBefore') or {}
        if not isinstance(delays, dict):
            raise ValueError(f'{where}: "delayBefore" must be an object of step -> seconds.')
        for token, seconds in delays.items():
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ValueError(
                    f'{where}: delayBefore["{token}"] must be a positive number of '
                    f'seconds, got {seconds!r}.'
                )
        entry['delayBefore'] = _step_keys(delays, where, 'delayBefore', run_up_to)

        # true makes the runner resend the trade as Cancelled straight after
        # step 05b and then check both ledgers show it cancelled. A string
        # "true" would be truthy, so only a JSON boolean is accepted; and a
        # trade stopped before 05b would never run the cancel or its checks.
        cancel = entry.get('cancel', False)
        if not isinstance(cancel, bool):
            raise ValueError(f'{where}: "cancel" must be true or false, got {cancel!r}.')
        if cancel and run_up_to and compare_tokens(run_up_to, CANCEL_CHECKS_AFTER) < 0:
            raise ValueError(
                f'{where}: "cancel" cancels the trade after step {CANCEL_CHECKS_AFTER}, but '
                f'runUpTo "{run_up_to}" stops it before then.'
            )

        # true (or an object) makes the runner cancel the trade's settlement on
        # both domains straight after step 13c, or the later step it names.
        _validate_cancel_settlement(entry, where, run_up_to)

    # A row that nets its trades together (TC010 to TC013, TC018) checks their
    # netting group once all of them have run, and takes it on to its runUpTo.
    if document.get('nettingTotal') is not None:
        _validate_own_netting_group(document, path, entries)

    return document


def _validate_cancel_settlement(mapping, where, run_up_to, group=False):
    """Validate a "cancelSettlement", on a run that reaches its steps, normalising it in place.

    It is false, true, or an object with any of:

    - "after": the step the cancel (13cs_a, 13cs_b) follows, 13c or later;
      13c by default. TC038 cancels after 15b, once the settlement is complete.
    - "checkAfter": the step the cancelled checks (15cs_a, 15cs_b) follow, no
      earlier than "after"; "after" by default. TC036 checks after 15b, once
      steps 15 and 15b have read the ledger.
    - "expect": {step: 'pass'|'fail'} for any of the four. A cancel step
      declared to fail means the cancel is refused (a NACK), so there is
      nothing cancelled to check: the checks are not run, and naming them, in
      "expect", "checkAfter" or "delayBefore", is an error.
    - "delayBefore": {step: seconds} the runner waits before starting any of
      the four. TC037 holds 13cs_a 30 seconds after step 14.

    A string "true" would be truthy, so nothing else is accepted; and a run
    stopped before "after" or "checkAfter" would never cancel or check. A
    trade with no runUpTo runs every step, but a netting row with none stops
    its group after the netting total, so `group` makes a missing runUpTo an
    error too.

    True and the object are both stored as {'after': step token, 'checkAfter':
    step token, or None when the cancel is refused, 'expect': dict,
    'delayBefore': dict}.
    """
    cancel = mapping.get('cancelSettlement', False)
    if cancel is False:
        return
    if cancel is True:
        cancel = {}
    if not isinstance(cancel, dict):
        raise ValueError(f'{where}: "cancelSettlement" must be true, false or an object of '
                         f'{", ".join(SETTLEMENT_CANCEL_KEYS)}, got {cancel!r}.')
    _reject_unknown_keys(cancel, SETTLEMENT_CANCEL_KEYS, f'{where} cancelSettlement')

    after = SETTLEMENT_CANCEL_AFTER
    if cancel.get('after') is not None:
        after = _pipeline_step(cancel['after'], where, 'cancelSettlement.after')
        if compare_tokens(after, SETTLEMENT_CANCEL_AFTER) < 0:
            raise ValueError(
                f'{where}: cancelSettlement.after is step "{after}", but a settlement is '
                f'cancelled no earlier than after step {SETTLEMENT_CANCEL_AFTER}.'
            )

    expect = cancel.get('expect') or {}
    if not isinstance(expect, dict):
        raise ValueError(f'{where}: cancelSettlement.expect must be an object of step -> pass|fail.')
    steps = SETTLEMENT_CANCEL_STEP_TOKENS + SETTLEMENT_CHECK_STEP_TOKENS
    for token, outcome in expect.items():
        if token not in steps:
            raise ValueError(f'{where}: cancelSettlement.expect names "{token}", which is not one '
                             f'of the settlement cancel steps ({", ".join(steps)}).')
        if outcome not in ('pass', 'fail'):
            raise ValueError(f'{where}: cancelSettlement.expect["{token}"] must be "pass" or '
                             f'"fail", got {outcome!r}.')

    # A mistyped token would never match a step, so the wait would silently
    # not happen.
    delays = cancel.get('delayBefore') or {}
    if not isinstance(delays, dict):
        raise ValueError(f'{where}: cancelSettlement.delayBefore must be an object of step -> seconds.')
    for token, seconds in delays.items():
        if token not in steps:
            raise ValueError(f'{where}: cancelSettlement.delayBefore names "{token}", which is not '
                             f'one of the settlement cancel steps ({", ".join(steps)}).')
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError(f'{where}: cancelSettlement.delayBefore["{token}"] must be a positive '
                             f'number of seconds, got {seconds!r}.')

    refused = any(expect.get(token) == 'fail' for token in SETTLEMENT_CANCEL_STEP_TOKENS)
    if refused:
        named = [token for token in SETTLEMENT_CHECK_STEP_TOKENS if token in expect or token in delays]
        if cancel.get('checkAfter') is not None:
            named.insert(0, 'checkAfter')
        if named:
            raise ValueError(
                f'{where}: cancelSettlement declares the cancel refused, so steps '
                f'{" and ".join(SETTLEMENT_CHECK_STEP_TOKENS)} are not run, but it names '
                f'{", ".join(named)}. Remove {"it" if len(named) == 1 else "them"}.'
            )
        check_after = None
    else:
        check_after = after
        if cancel.get('checkAfter') is not None:
            check_after = _pipeline_step(cancel['checkAfter'], where, 'cancelSettlement.checkAfter')
            if compare_tokens(check_after, after) < 0:
                raise ValueError(
                    f'{where}: cancelSettlement.checkAfter is step "{check_after}", but the '
                    f'settlement is not cancelled until after step {after}.'
                )
    mapping['cancelSettlement'] = {'after': after, 'checkAfter': check_after, 'expect': expect,
                                   'delayBefore': delays}

    if run_up_to is None and not group:
        return
    if run_up_to is None:
        raise ValueError(
            f'{where}: "cancelSettlement" cancels the group\'s settlement after step '
            f'{after}, but with no runUpTo the group stops after its netting '
            f'total. Set runUpTo to {after} or later.'
        )
    stop = str(run_up_to).strip().lower()
    if compare_tokens(stop, after) < 0:
        raise ValueError(
            f'{where}: "cancelSettlement" cancels the settlement after step '
            f'{after}, but runUpTo "{run_up_to}" stops it before then.'
        )
    if check_after and compare_tokens(stop, check_after) < 0:
        raise ValueError(
            f'{where}: cancelSettlement.checkAfter is step "{check_after}", after runUpTo '
            f'"{run_up_to}", so the cancelled checks would never run.'
        )


def _reject_unknown_keys(mapping, allowed, where):
    """Raise ValueError naming each key of `mapping` not in `allowed`, with the nearest valid one."""
    unknown = [key for key in mapping if key not in allowed]
    if not unknown:
        return
    named = []
    for key in unknown:
        near = difflib.get_close_matches(str(key), allowed, n=1, cutoff=0.6)
        named.append(f'"{key}"' + (f' (did you mean "{near[0]}"?)' if near else ''))
    raise ValueError(f'{where}: unknown key {", ".join(named)}. '
                     f'Valid keys: {", ".join(allowed)}.')


def _pipeline_step(token, where, key):
    """Return a step token as the runner spells it ("5B" -> "5b").

    Raises if the token names no step, or a step the per-trade pipeline never
    runs (01 auth, and the disabled 02 / 03), since it could never match.
    """
    name = str(token).strip().lower()
    try:
        step = get_step(name)
    except ValueError:
        raise ValueError(f'{where}: {key} names unknown step "{token}".') from None
    if name == '1' or step.get('enabled') is False:
        raise ValueError(f'{where}: {key} names step "{token}", which the trade pipeline does not run.')
    return name


def _step_keys(mapping, where, key, run_up_to):
    """Return `mapping` with its step keys normalised, rejecting any after `run_up_to`.

    A step after runUpTo never runs, so its expected outcome or delay would
    silently never apply.
    """
    normalised = {}
    for token, value in mapping.items():
        name = _pipeline_step(token, where, key)
        if name in normalised:
            raise ValueError(f'{where}: {key} names step "{name}" more than once.')
        if run_up_to and compare_tokens(name, run_up_to) > 0:
            raise ValueError(
                f'{where}: {key} names step "{token}", after runUpTo "{run_up_to}", '
                'so it would never run.'
            )
        normalised[name] = value
    return normalised


def _validate_continuation(document, path):
    """Validate a continuation row, which books no trade of its own.

    It must name an existing scenario in `continues`, carry no `trade` or
    `trades` that could be injected by mistake, state every NETTING_TOTAL_FIELDS
    value in `nettingTotal`, and, if it sets `runUpTo`, name a known step no
    earlier than CONTINUATION_RESUME_STEP.
    """
    source = document.get('continues')
    if not isinstance(source, str) or not source.strip():
        raise ValueError(
            f'{path.name}: "continues" must name the scenario it continues, such as "TC010".'
        )
    if not (SCENARIO_DIR / f'{source.strip().upper()}.json').exists():
        known = ', '.join(available_scenarios()) or 'none found'
        raise ValueError(
            f'{path.name} continues {source}, which has no scenario file. '
            f'Available scenarios: {known}.'
        )
    if document.get('trade') is not None or document.get('trades') is not None:
        raise ValueError(
            f'{path.name} continues {source} and books nothing itself, so it cannot '
            'also carry "trade" or "trades".'
        )

    if not isinstance(document.get('nettingTotal'), dict):
        raise ValueError(f'{path.name}: a continuation needs a "nettingTotal" object saying what to check.')
    _validate_netting_total(document, path)


def _validate_own_netting_group(document, path, entries):
    """Validate a row that books several trades and checks them as one netting group.

    Its trades must all stop before step NETTING_GROUP_START_STEP, since the
    group's steps run once for all of them rather than once per trade, and
    nettingTotal.tradeCount must be the number of trades the row books, so a
    changed `repeat` cannot leave the total checking a different count.
    """
    if not isinstance(document.get('nettingTotal'), dict):
        raise ValueError(f'{path.name}: "nettingTotal" must be an object saying what to check.')

    for index, entry in enumerate(entries):
        run_up_to = entry.get('runUpTo')
        if run_up_to is None or compare_tokens(run_up_to, NETTING_GROUP_START_STEP) >= 0:
            raise ValueError(
                f'{path.name} trades[{index}]: the row checks its trades as one netting group, '
                f'which runs steps {NETTING_GROUP_START_STEP} onward once for all of them, so '
                f'each trade needs a runUpTo before step {NETTING_GROUP_START_STEP}, such as "5b".'
            )

    booked = sum(entry.get('repeat', 1) for entry in entries)
    count = document['nettingTotal'].get('tradeCount')
    if count != booked:
        raise ValueError(
            f'{path.name}: nettingTotal.tradeCount is {count!r}, but the row books {booked} trades.'
        )

    _validate_netting_total(document, path)
    _validate_cancel_trade(document, path, booked)
    _validate_group_steps(document, path)

    # Every trade must be one the group nets: the parties, pair, product and
    # currencies nettingTotal names. Checked here so a mistyped trade fails
    # before anything is booked, rather than as a split group after 40 trades.
    total = document['nettingTotal']
    for index, entry in enumerate(entries):
        differs = netting_total_differences(total, entry['trade']['wrapper'])
        if differs:
            raise ValueError(
                f'{path.name} trades[{index}] cannot net into the group nettingTotal describes: '
                + ', '.join(differs) + '.'
            )


def _validate_cancel_trade(document, path, booked):
    """Validate a netting row's cancelTrade, normalising its step tokens in place.

    `trade` is numbered as the run announces it ("trade 10 of 10"), counting
    repeats. `after` is a group step from 06 to the row's runUpTo (07b when it
    has none), which the cancel follows. `expect` declares the cancel steps'
    outcomes; one declared to fail means the cancel is refused (a NACK), so
    nothing changes. A cancel that goes through takes the trade out of the
    group's net, which only the netting total after step 07b checks, so it
    must come no later. Any slip here would run the row as a plain netting row
    or cancel at a step that never comes, so each is rejected.
    """
    cancel = document.get('cancelTrade')
    if cancel is None:
        return
    where = f'{path.name} cancelTrade'
    if not isinstance(cancel, dict):
        raise ValueError(f'{where} must be an object: {{"trade": <number>, "after": "<step>"}}, '
                         'with an optional "expect" of cancel step -> pass|fail.')
    _reject_unknown_keys(cancel, CANCEL_TRADE_KEYS, where)

    trade = cancel.get('trade')
    if isinstance(trade, bool) or not isinstance(trade, int) or not 1 <= trade <= booked:
        raise ValueError(f'{where}: "trade" must be the number of one of the row\'s {booked} '
                         f'trades, 1 to {booked}, got {trade!r}.')

    if cancel.get('after') is None:
        raise ValueError(f'{where} has no "after": the group step to cancel the trade after.')
    after = cancel['after'] = _pipeline_step(cancel['after'], where, 'after')
    run_up_to = document.get('runUpTo')
    last = str(run_up_to).strip().lower() if run_up_to is not None else NETTING_TOTAL_AFTER
    if compare_tokens(after, NETTING_GROUP_START_STEP) < 0 or compare_tokens(after, last) > 0:
        raise ValueError(f'{where}: "after" is step "{after}", but the group\'s steps run from '
                         f'{NETTING_GROUP_START_STEP} to {last}.')

    expect = cancel.get('expect') or {}
    if not isinstance(expect, dict):
        raise ValueError(f'{where}: "expect" must be an object of cancel step -> pass|fail.')
    for token, outcome in expect.items():
        if token not in CANCEL_STEP_TOKENS:
            raise ValueError(f'{where}: expect names "{token}", which is not one of the cancel '
                             f'steps ({", ".join(CANCEL_STEP_TOKENS)}).')
        if outcome not in ('pass', 'fail'):
            raise ValueError(f'{where}: expect["{token}"] must be "pass" or "fail", got {outcome!r}.')
    cancel['expect'] = expect

    if 'fail' not in expect.values() and compare_tokens(after, NETTING_TOTAL_AFTER) > 0:
        raise ValueError(
            f'{where}: a cancel that goes through takes the trade out of the group\'s net, '
            f'which only the netting total after step {NETTING_TOTAL_AFTER} checks, so "after" '
            f'must be {NETTING_TOTAL_AFTER} or earlier. If the cancel is refused, declare the '
            'cancel steps that fail in "expect".'
        )


def _validate_group_steps(document, path):
    """Validate a netting row's top-level expect and skip, normalising their step tokens in place.

    Both name the group's own steps, up to its runUpTo (07b when it has none).
    `expect` ({step: 'pass'|'fail'}) may name any of them, from step
    NETTING_GROUP_START_STEP. `skip` ({step: reason}) names steps the group does
    not run, each reported as not run with its reason; it starts at
    CONTINUATION_RESUME_STEP, since steps 06 to 07b find the group the netting
    total checks. A skipped step has no outcome, so no step may be in both. A
    slip would otherwise run a step the row means to skip, or report an
    expected failure as a real one.
    """
    run_up_to = document.get('runUpTo')
    last = str(run_up_to).strip().lower() if run_up_to is not None else NETTING_TOTAL_AFTER

    for key, first, shape in (('expect', NETTING_GROUP_START_STEP, 'step -> pass|fail'),
                              ('skip', CONTINUATION_RESUME_STEP, 'step -> reason')):
        mapping = document.get(key) or {}
        if not isinstance(mapping, dict):
            raise ValueError(f'{path.name}: "{key}" must be an object of {shape}.')
        mapping = document[key] = _step_keys(mapping, path.name, key, last)
        early = [token for token in mapping if compare_tokens(token, first) < 0]
        if early:
            raise ValueError(
                f'{path.name}: {key} names step {", ".join(early)}, but the group\'s {key} '
                f'applies from step {first}.'
            )

    for token, outcome in document['expect'].items():
        if outcome not in ('pass', 'fail'):
            raise ValueError(f'{path.name}: expect["{token}"] must be "pass" or "fail", '
                             f'got {outcome!r}.')
    for token, reason in document['skip'].items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f'{path.name}: skip["{token}"] must be the reason the step is not '
                             f'run, got {reason!r}.')

    both = [token for token in document['skip'] if token in document['expect']]
    if both:
        raise ValueError(f'{path.name}: step {", ".join(both)} is both skipped and given an '
                         'expected outcome, but a skipped step has none.')


def _validate_netting_total(document, path):
    """Validate the netting group check a row declares: its nettingTotal and runUpTo.

    nettingTotal must state every NETTING_TOTAL_FIELDS value, and no amounts:
    the amounts the group must hold are the sum utils/sum_storer.py stores as
    the trades are booked, so a typed figure would be ignored. The one exception
    is NETTING_TOTAL_GROUP_AMOUNTS, the whole group's amounts, which must give a
    positive amount, or 0, for exactly its two currencies. runUpTo, if set, must name
    a known step no earlier than CONTINUATION_RESUME_STEP, since the runner
    resumes there after the total, or, on a row that nets its own trades,
    NETTING_TOTAL_AFTER, which stops the group before the total. A continuation
    exists to check the total, so it cannot stop before it.
    """
    total = document['nettingTotal']
    missing = [field for field in NETTING_TOTAL_FIELDS if total.get(field) in (None, '')]
    if missing:
        raise ValueError(f'{path.name}: nettingTotal is missing {", ".join(missing)}.')
    typed = [field for field in ('baseAmount', 'referenceAmount') if field in total]
    if typed:
        raise ValueError(
            f'{path.name}: nettingTotal states {", ".join(typed)}, but the group is checked '
            'against the sum utils/sum_storer.py stores from the injected trades. Remove '
            f'{"it" if len(typed) == 1 else "them"}.'
        )
    _reject_unknown_keys(total, NETTING_TOTAL_FIELDS + (NETTING_TOTAL_GROUP_AMOUNTS,),
                         f'{path.name} nettingTotal')
    if total['baseCurrency'] == total['referenceCurrency'] \
            or total['tradeCurrencyPair'] != f'{total["baseCurrency"]}{total["referenceCurrency"]}':
        raise ValueError(
            f'{path.name}: nettingTotal.tradeCurrencyPair is {total["tradeCurrencyPair"]!r}, but '
            f'its baseCurrency {total["baseCurrency"]!r} and referenceCurrency '
            f'{total["referenceCurrency"]!r} do not make that pair.'
        )

    amounts = total.get(NETTING_TOTAL_GROUP_AMOUNTS)
    if amounts is not None:
        currencies = {total['baseCurrency'], total['referenceCurrency']}
        if not isinstance(amounts, dict) or set(amounts) != currencies:
            raise ValueError(
                f'{path.name}: nettingTotal.{NETTING_TOTAL_GROUP_AMOUNTS} must give an amount for '
                f'each of {", ".join(sorted(currencies))} and no other currency, got {amounts!r}.'
            )
        # A string or boolean would compare unequal to every net, and NaN to itself.
        wrong = [f'{ccy} {value!r}' for ccy, value in amounts.items()
                 if isinstance(value, bool) or not isinstance(value, (int, float))
                 or not math.isfinite(value) or value < 0]
        if wrong:
            raise ValueError(
                f'{path.name}: nettingTotal.{NETTING_TOTAL_GROUP_AMOUNTS} amounts must be positive '
                f'numbers or 0: {", ".join(wrong)}.'
            )

    run_up_to = document.get('runUpTo')
    if run_up_to is not None:
        try:
            get_step(run_up_to)
        except ValueError:
            raise ValueError(f'{path.name}: runUpTo names unknown step "{run_up_to}".') from None
        stop = str(run_up_to).strip().lower()
        if stop == NETTING_TOTAL_AFTER and document.get('continues') is None:
            return
        if compare_tokens(stop, CONTINUATION_RESUME_STEP) < 0:
            raise ValueError(
                f'{path.name}: runUpTo must be step {CONTINUATION_RESUME_STEP} or later'
                + ('.' if document.get('continues') is not None else
                   f', or {NETTING_TOTAL_AFTER} to stop the group before the netting total.'))


def netting_total_differences(total, wrapper):
    """Return how a trade differs from the netting group a nettingTotal describes.

    Public because data/osttra_test_scenarios/trade_reader.py checks an
    IMPORT_EXCEL case's parsed trades with it before booking any of them.

    :returns: one 'key value (nettingTotal has value)' per field that differs;
        empty when the trade nets into the group.
    """
    meta = wrapper.get('metaData') or {}
    fx = (wrapper.get('tradeData') or {}).get('fx') or {}
    fx_cash = fx.get('fxCash') or {}
    booked_as = {
        'party': meta.get('determinedPartyId'),
        'counterParty': meta.get('determinedCounterpartyId'),
        'tradeCurrencyPair': fx_cash.get('currencyPair'),
        'productType': fx.get('productType'),
        'baseCurrency': fx_cash.get('baseCurrency'),
        'referenceCurrency': fx_cash.get('referenceCurrency'),
    }
    return [f'{key} {value!r} (nettingTotal has {total[key]!r})'
            for key, value in booked_as.items() if value != total[key]]


# ---------------------------------------------------------------------------
# SCENARIO FILES: WHAT A SCENARIO RUNS
# ---------------------------------------------------------------------------

def scenario_trades(scenario_id):
    """Return every trade entry a scenario injects, as a list.

    Normalises the `trade` and `trades` layouts to one shape. Each entry holds
    its `trade` wrapper plus optional runner keys (`label`, `runUpTo`, `expect`,
    `delayBefore`, `identifiers`, `cancel`). Repeats are expanded here so the runner and
    step 04 index the same list. A continuation injects nothing and returns [].
    """
    document = load_scenario(scenario_id)
    if document.get('continues') is not None:
        return []

    entries = document.get('trades')

    if isinstance(entries, list):
        return _expand_repeats(entries)

    return [{'trade': document['trade']}]


def _expand_repeats(entries):
    """Expand each entry with `repeat` into that many entries, one simulator call each.

    Each copy generates its own identifiers, so ten copies are ten distinct
    trades with the same economics (identifiers "shared" instead makes copies
    duplicates of one trade). When there is more than one copy, each is
    labelled "<label> n of N" so the report shows which copy a step belongs to.
    """
    expanded = []
    for entry in entries:
        count = entry.get('repeat', 1)
        for n in range(count):
            copy = {key: value for key, value in entry.items() if key != 'repeat'}
            if count > 1:
                copy['label'] = f'{entry.get("label") or "trade"} {n + 1} of {count}'
            expanded.append(copy)
    return expanded


def resolve_scenario_trade_index():
    """Return the index of the scenario trade this process injects.

    Read from SCENARIO_TRADE_INDEX, which the runner sets per trade. Unset means
    0, the only trade of a single-trade scenario. A non-integer returns -1 so
    select_scenario_entry() rejects it as out of range.
    """
    raw = os.environ.get(SCENARIO_TRADE_INDEX_ENV)

    if raw is None or str(raw).strip() == '':
        return 0

    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return -1


def select_scenario_entry(scenario_id):
    """Return (index, entry) for the scenario trade this process injects.

    An out-of-range index raises rather than being clamped: clamping would
    inject a different trade from the one the runner reports on.
    """
    entries = scenario_trades(scenario_id)
    index = resolve_scenario_trade_index()

    if index < 0 or index >= len(entries):
        raise ValueError(
            f'{SCENARIO_TRADE_INDEX_ENV}={os.environ.get(SCENARIO_TRADE_INDEX_ENV)!r} '
            f'is out of range for scenario {scenario_id} ({len(entries)} '
            f'trade(s), valid range 0-{len(entries) - 1}).'
        )

    return index, entries[index]


def scenario_netting_group(scenario_id):
    """Return the netting group a scenario checks as a whole, or None.

    Two kinds of row check one netting group holding several trades, rather
    than each trade on its own: a continuation (none at present), whose group
    holds the trades of the row it continues, and a row that books several
    trades and declares a nettingTotal (TC010 to TC013, TC018), whose group holds its
    own trades once each has run to its runUpTo. Steps 07c, 12 and 12b then
    expect the sum utils/sum_storer.py stored for those trades rather than the
    one trade state holds.

    :returns: {'tradesOf': upper-case id of the scenario whose recorded trades
        the group holds, 'nettingTotal': dict, 'runUpTo': lower-case step token
        or None, 'cancelTrade': {'trade': number from 1, 'after': lower-case
        step token, 'expect': {cancel step token: 'pass'|'fail'}} or None,
        'cancelSettlement': {'after': step token, 'checkAfter': step token or
        None, 'expect': dict, 'delayBefore': dict} to cancel the group's settlement (see
        _validate_cancel_settlement), or False, 'expect': {step token: 'pass'|'fail'}
        for the group's steps, 'skip': {step token: reason} for the group's
        steps it does not run}.
    """
    continuation = scenario_continuation(scenario_id)
    if continuation:
        return {'tradesOf': continuation['continues'],
                'nettingTotal': continuation['nettingTotal'],
                'runUpTo': continuation['runUpTo'],
                'cancelTrade': None,
                'cancelSettlement': False,
                'expect': {},
                'skip': {}}

    document = load_scenario(scenario_id)
    if document.get('nettingTotal') is None:
        return None
    run_up_to = document.get('runUpTo')
    return {
        'tradesOf': scenario_id.strip().upper(),
        'nettingTotal': document['nettingTotal'],
        'runUpTo': str(run_up_to).strip().lower() if run_up_to is not None else None,
        'cancelTrade': document.get('cancelTrade'),
        'cancelSettlement': document.get('cancelSettlement', False),
        'expect': document.get('expect') or {},
        'skip': document.get('skip') or {},
    }


def scenario_continuation(scenario_id):
    """Return a scenario's continuation settings, or None if it books its own trades.

    :returns: {'continues': upper-case scenario id, 'nettingTotal': dict,
        'runUpTo': lower-case step token or None}.
    """
    document = load_scenario(scenario_id)
    source = document.get('continues')
    if source is None:
        return None
    run_up_to = document.get('runUpTo')
    return {
        'continues': source.strip().upper(),
        'nettingTotal': document['nettingTotal'],
        'runUpTo': str(run_up_to).strip().lower() if run_up_to is not None else None,
    }


def continuation_of(scenario_id):
    """Return the id of the scenario that continues this one, or None.

    Only each file's `continues` key is read, so an unrelated scenario file
    that fails validation cannot stop this one from running.
    """
    for other in available_scenarios():
        try:
            document = json.loads((SCENARIO_DIR / f'{other}.json').read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        source = document.get('continues') if isinstance(document, dict) else None
        if isinstance(source, str) and source.strip().upper() == scenario_id:
            return other
    return None


# ---------------------------------------------------------------------------
# SCENARIO {{TOKEN}} RESOLUTION
# ---------------------------------------------------------------------------
# Every date token is derived from t['dateStr'], the run date step 04 gets from
# TestUtils.generate_timestamps() in utils/helpers.py. Nothing here reads the
# clock, so TODAY, NEXT_SUNDAY and T+N / T-N come from one instant and a run
# that crosses midnight cannot date one field yesterday and the next today.

def build_scenario_tokens(t, ids, iso_timestamp, guid1, guid2):
    """Build the named {{TOKEN}} values for one scenario trade.

    T+N / T-N are not listed: they are unbounded, so _resolve_token() derives
    them on demand from the same run date.

    A shared identity (see shared_scenario_ids()) overrides the generated `ids`,
    so every trade in a duplicate set carries the same identifiers.
    build_scenario_payload() then copies the injected ids back into the
    caller's `ids`, so step 04 records the identity actually injected.
    """
    today = datetime.strptime(t['dateStr'], '%Y-%m-%d').date()
    ids = {**ids, **(shared_scenario_ids() or {})}

    return {
        # Dates, from the run date step 04 passes in.
        'TODAY': t['dateStr'],
        'NEXT_SUNDAY': _next_sunday(today),
        # Identity: generated per run, or the duplicate set's shared identity.
        'TRADE_ID': ids['tradeId'],
        'TRADE_SIDE_ID': ids['tradeSideId'],
        'UTI': ids['uniqueTransactionId'],
        'PARTY_TRADE_ID': ids['partyTradeId'],
        'COUNTERPARTY_TRADE_ID': ids['counterpartyTradeId'],
        # Envelope: message timestamps and guids.
        'ISO_TIMESTAMP': iso_timestamp,
        'GUID1': guid1,
        'GUID2': guid2,
    }


def shared_scenario_ids():
    """Return the duplicate set's shared identity from SCENARIO_SHARED_IDS, or None.

    Missing or malformed content returns None rather than raising, and the
    caller keeps its own generated identifiers. The duplicate test is weaker for
    it, but the run still completes and the distinct trade id shows in the
    report.
    """
    raw = os.environ.get(SCENARIO_SHARED_IDS_ENV)

    if not raw or not str(raw).strip():
        return None

    try:
        shared = json.loads(raw)
    except json.JSONDecodeError:
        return None

    return shared if isinstance(shared, dict) and shared else None


def resolve_scenario_tokens(node, tokens, today, scenario_id):
    """Return a copy of `node` with every {{TOKEN}} in its strings resolved.

    Builds a new structure and never mutates `node`, so the loaded scenario
    document stays exactly as read from disk.
    """
    if isinstance(node, dict):
        return {key: resolve_scenario_tokens(value, tokens, today, scenario_id)
                for key, value in node.items()}

    if isinstance(node, list):
        return [resolve_scenario_tokens(item, tokens, today, scenario_id) for item in node]

    if isinstance(node, str):
        return TOKEN_RE.sub(
            lambda match: str(_resolve_token(match.group(1), tokens, today, scenario_id)),
            node,
        )

    # Numbers, booleans and nulls carry no tokens and pass through unchanged. A
    # null is kept, not dropped: a scenario may send a null amount on purpose.
    return node


def _resolve_token(name, tokens, today, scenario_id):
    """Return the value of one {{NAME}}: a named token or a T+N / T-N date.

    An unknown name raises, listing the tokens that are known.
    """
    if name in tokens:
        return tokens[name]

    offset = OFFSET_TOKEN_RE.match(name)
    if offset:
        sign, days = offset.groups()
        return _offset_date(today, int(days) * (-1 if sign == '-' else 1))

    raise ValueError(
        f'Scenario {scenario_id} uses an unknown token {{{{{name}}}}}. '
        f'Known tokens: {", ".join(sorted(tokens))}, T+N, T-N.'
    )


def _offset_date(today, days):
    """Return `today` shifted by `days` calendar days as YYYY-MM-DD (T+N / T-N).

    Calendar days, not business days: the sheet states tenors that way, and the
    settlement calendar is under test, so rolling the date here would hide the
    behaviour a holiday scenario exists to check.
    """
    return (today + timedelta(days=days)).strftime('%Y-%m-%d')


def _next_sunday(today):
    """Return the next Sunday strictly after `today` as YYYY-MM-DD.

    On a Sunday this is a week ahead, so a holiday scenario always targets a
    future non-business day instead of becoming a same-day trade once a week.
    weekday() numbers Sunday 6, so (6 - weekday) % 7 is the days until Sunday;
    `or 7` turns Sunday's 0 into a full week.
    """
    return (today + timedelta(days=(6 - today.weekday()) % 7 or 7)).strftime('%Y-%m-%d')
