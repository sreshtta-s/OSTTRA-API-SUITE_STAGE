# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 04: inject a trade through the simulator and record it in run state.

The payload depends on the injection mode (utils/flag.py): DYNAMIC builds it
from the parties and currencies in .state/default.json, SINGLE_FILE,
MULTIPLE_FILE and IMPORT load it from api/json_payload_trade_injection, and SCENARIO from
data/osttra_test_scenarios. IMPORT_EXCEL loads a test case's Excel trade as
IMPORT does, or that case's scenario trade. The trade id, UTI, product type, dates and leg
amounts are read off the injected payload and, on a 2xx, saved for later steps.

Run directly with pytest, SINGLE_FILE and MULTIPLE_FILE first parse their
*_unparsed.json into the *_trade.json they inject, as runner.py does in its
Phase 0.
"""
import json
import os
import types
from datetime import datetime, timezone

import pytest

import parse_trades
from api.payloads.trade_payload import (
    PRODUCT_TYPES,
    get_payload_state_fields,
    get_trade_payload,
    resolve_product_type,
    set_trade_status,
)
from api.services.base_service import is_ok
from api.services.trade_service import TradeService
from data.trade_scenarios import SCENARIOS
from utils.api_logger import log_ids
from utils.describe import trade_line
from utils.flag import resolve_injection_mode, resolve_scenario_id
from utils.helpers import TestUtils
from utils.reporter import write_injected_payload
from utils.state_helper import StateHelper
from utils.trade_direction import payload_side_problems

# Supplies only the expected injection status; the trade comes from the payload.
data = SCENARIOS[0]


# ============================================================================
# TEST SUITE: INJECT TRADE (GRANULAR POSTMAN STYLE)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Build and inject the trade once for every test in this module."""
    return inject(session)


def inject(session, status=None):
    """Build the trade for the active injection mode, inject it and record it in run state.

    Shared with step 04's cancel variant (tests/test_04_cancel_trade.py), which
    differs only in the status it passes.

    :param status: a metaData.status to send in place of the payload's own
        ("New"), or None to send the payload as built.
    :returns: the namespace every test in this module reads.
    """
    c = types.SimpleNamespace(res_status=None, injected={}, rejection=None)

    # --- PARSE THE RAW TRADE FILE (direct pytest runs only) ---
    # Under runner.py (BLOB_FILE set) Phase 0 has already parsed it. Stops on
    # failure: injecting the stale file would silently re-test the last trade.
    mode = resolve_injection_mode()
    if mode in parse_trades.JOBS and not os.environ.get('BLOB_FILE'):
        if parse_trades.main(['parse_trades.py', mode]) != 0:
            pytest.fail(f'Parsing failed for {mode}; not injecting the stale '
                        f'{parse_trades.JOBS[mode]["out"]}.')

    # --- LOAD PARTIES AND CURRENCIES FROM STATE ---
    state = StateHelper.get_state()

    party_id = state.get('reqPartyId')
    counter_party_id = state.get('counterPartyId')
    trade_currency_pair = state.get('tradeCurrencyPair')
    dealt_currency = state.get('dealtCurrency')
    reference_currency = state.get('referenceCurrency')

    # --- GENERATE IDS AND TIMESTAMPS ---
    timestamps = TestUtils.generate_timestamps()
    c.trade_ids = TestUtils.generate_trade_identifiers()
    # A status resends the trade step 04 injected, so it takes step 04's ids:
    # under new ones it would cancel nothing. The file routes inject their
    # file's own ids either way.
    if status:
        injected_ids = state.get('injectedTradeIds')
        if not injected_ids:
            pytest.fail(f'There is no trade to resend as {status}: state has no '
                        'injectedTradeIds. Did step 04 run and get a 2xx?')
        c.trade_ids.update(injected_ids)
    # One clock read, so the milliseconds belong to the same instant as the
    # seconds they are appended to.
    now = datetime.now(timezone.utc)
    iso_timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'
    guid1 = TestUtils.generate_uuid()
    guid2 = TestUtils.generate_uuid()

    # --- BUILD THE PAYLOAD ---
    trade_payload = get_trade_payload(
        party_id,
        counter_party_id,
        trade_currency_pair,
        dealt_currency,
        reference_currency,
        timestamps,
        c.trade_ids,
        iso_timestamp,
        guid1,
        guid2,
    )

    # Set before the payload is printed and recorded, so the report shows the
    # status actually sent.
    if status:
        set_trade_status(trade_payload, status)

    print('Trade Payload: ', json.dumps(trade_payload))

    # Recorded for the Excel report's Injected Payload column: DYNAMIC and
    # SCENARIO trades are built at run time, so there is no source file to quote.
    write_injected_payload(json.dumps(trade_payload, indent=2))

    # --- EXTRACT PAYLOAD DATA FOR DOWNSTREAM VALIDATION ---
    wrapper = (trade_payload[0] or {}).get('wrapper') or {}
    source_platforms = wrapper.get('sourcePlatform') or []
    trade_data = wrapper.get('tradeData') or {}
    fx = trade_data.get('fx') or {}

    near_leg = (fx.get('fxCash') or {}).get('nearLeg') or {}

    c.extracted_trade_id = (source_platforms[0] or {}).get('tradeId') if source_platforms else None
    c.product_type = fx.get('productType')
    # Dates are read off the injected payload, like productType, so they are
    # right in every mode: later steps use this value date rather than today's,
    # and a DYNAMIC run's report date columns would otherwise be blank or stale.
    injected_trade_date = fx.get('tradeDate')
    injected_value_date = near_leg.get('valueDate')

    # Leg amounts come from the injected payload too: steps 05/05b (ledger) and
    # 07/07b (netting group legs) assert against them, and every mode supplies
    # its own, so any other source would check against a number never sent.
    injected_base_amount = near_leg.get('baseCurrencyAmount')
    injected_reference_amount = near_leg.get('referenceCurrencyAmount')

    # Who buys which currency, read off the payload in every mode like the
    # amounts: every later step derives from these which side of each currency
    # the party (Domain A) and the counterparty (Domain B) should see.
    c.near_leg = near_leg
    c.sides = {
        'baseCurrency': (fx.get('fxCash') or {}).get('baseCurrency'),
        'baseCurrencyBuyer': near_leg.get('baseCurrencyBuyer'),
        'baseCurrencySeller': near_leg.get('baseCurrencySeller'),
    }

    # Outside DYNAMIC the injected JSON (a static file or a scenario) is the
    # source of truth, so its fields override the state values read above.
    c.injected = {} if resolve_injection_mode() == 'DYNAMIC' else get_payload_state_fields(trade_payload[0])

    party_id = c.injected.get('reqPartyId', party_id)
    counter_party_id = c.injected.get('counterPartyId', counter_party_id)
    # Read off the payload in every mode: DYNAMIC sends the seed's pair in
    # alphabetical order, which may not be how state spelled it.
    trade_currency_pair = (fx.get('fxCash') or {}).get('currencyPair') or trade_currency_pair
    c.party_id, c.counter_party_id = party_id, counter_party_id

    # The trade as later steps will know it, for every assertion message below:
    # the same fields, merged in the same order, as the state saved on a 2xx.
    c.trade = trade_line({
        'injectedTradeId': c.extracted_trade_id,
        'uniqueTransactionId': c.trade_ids['uniqueTransactionId'],
        'reqPartyId': party_id,
        'counterPartyId': counter_party_id,
        'tradeCurrencyPair': trade_currency_pair,
        'productType': c.product_type,
        'dealtCurrency': dealt_currency,
        'referenceCurrency': reference_currency,
        'baseCurrencyAmount': injected_base_amount,
        'referenceCurrencyAmount': injected_reference_amount,
        'tradeDate': injected_trade_date,
        'valueDate': injected_value_date,
        **c.sides,
        **c.injected,
    })

    print(f'Preparing to submit Trade ID -> {c.extracted_trade_id} for '
          f'[{party_id}] vs [{counter_party_id}] ({trade_currency_pair})')
    print(f'Unique Transaction ID (UTI) -> {c.trade_ids["uniqueTransactionId"]}')

    # --- SEND THE REQUEST TO THE SIMULATOR ---
    trade_service = TradeService(session)
    res = trade_service.inject_trade(trade_payload)

    c.res_status = res.status_code
    print(f'INFO: Trade Injection API responded with status: {c.res_status}')

    # --- STATE PERSISTENCE ---
    # Saved only for an accepted trade (2xx), so state never holds the id of a
    # trade that never reached the ledger.
    if is_ok(res):
        StateHelper.save_state({
            'uniqueTransactionId': c.trade_ids['uniqueTransactionId'],
            'injectedTradeId': c.extracted_trade_id,
            # Every id the trade was injected under, for the cancel variant to
            # resend it with.
            'injectedTradeIds': dict(c.trade_ids),
            # The status sent in place of the payload's own: None for step 04,
            # "Cancelled" for step 04 cancel. Steps 05 / 05b, if run after it,
            # leave a cancelled trade's status check to steps 05ct_a / 05ct_b.
            'injectedStatus': status,
            # The product actually sent, whichever mode supplied it.
            'productType': c.product_type,
            'tradeDate': injected_trade_date,
            'valueDate': injected_value_date,
            # Expected leg amounts for steps 05/05b and 07/07b.
            'baseCurrencyAmount': injected_base_amount,
            'referenceCurrencyAmount': injected_reference_amount,
            # Who buys which currency, for the direction checks from step 05 on.
            **c.sides,
            # Empty under DYNAMIC; otherwise the injected JSON's own fields.
            **c.injected,
        })
        log_ids('STEP 04 injected trade',
                tradeId=c.extracted_trade_id,
                uniqueTransactionId=c.trade_ids['uniqueTransactionId'],
                productType=c.product_type,
                valueDate=injected_value_date,
                baseCurrencyAmount=injected_base_amount,
                referenceCurrencyAmount=injected_reference_amount)
        print('Trade UTI successfully saved to state for Step 5 Ledger Validation.')
    else:
        c.rejection = res.text
        print('WARN: Trade injection failed. State not saved. Step 5 may skip or fail.')
        print(f'SERVER REJECTION REASON: {res.text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES (Every assertion is an independent validation)
# ============================================================================

# --- API HEALTH VALIDATION ---
def test_endpoint_returns_2xx(ctx):
    """API: Endpoint returns a 2xx success status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert 200 <= ctx.res_status < 300, \
        (f'Trade injection should return a 2xx status, but the simulator returned '
         f'{ctx.res_status}. Reason: {ctx.rejection or "none given"}\n{ctx.trade}')


def test_injection_status_matches_expected(ctx):
    """Verify trade injection status matches expected status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert ctx.res_status == data['expectedTradeStatus'], \
        (f'Trade injection should return HTTP {data["expectedTradeStatus"]}, but the '
         f'simulator returned {ctx.res_status}. Reason: {ctx.rejection or "none given"}'
         f'\n{ctx.trade}')


# --- PAYLOAD STRUCTURE VALIDATION ---
def test_trade_id_exists_in_payload(ctx):
    """Verify trade ID exists in the generated payload mapping"""
    assert ctx.extracted_trade_id is not None, \
        (f'The injected payload carries no trade id at '
         f'[0].wrapper.sourcePlatform[0].tradeId.\n{ctx.trade}')


def test_trade_id_nomenclature(ctx):
    """Verify trade ID adheres to MSERV- nomenclature"""
    if not ctx.extracted_trade_id:
        pytest.skip('Trade ID missing, cannot validate nomenclature.')
    assert 'MSERV' in ctx.extracted_trade_id, \
        (f'Trade id [{ctx.extracted_trade_id}] should follow the MSERV- nomenclature, '
         f'but does not contain MSERV.\n{ctx.trade}')


def test_trade_id_minimum_length(ctx):
    """Verify trade ID meets minimum ledger character limits (>10)"""
    if not ctx.extracted_trade_id:
        pytest.skip('Trade ID missing, cannot validate length.')
    assert len(ctx.extracted_trade_id) > 10, \
        (f'Trade id [{ctx.extracted_trade_id}] is {len(ctx.extracted_trade_id)} '
         f'characters long; the ledger needs more than 10.\n{ctx.trade}')


def test_product_type_matches_configuration(ctx):
    """Verify product type matches the configured product type"""
    # DYNAMIC (productType in .state/default.json) and SCENARIO (the scenario
    # file) declare a product type, so the injected trade must match it. That
    # includes a product outside PRODUCT_TYPES that a scenario pins on purpose.
    # The static file modes declare none, so only the value itself is checked.
    # An IMPORT_EXCEL trade is checked against its test case's scenario, or,
    # in a case with no scenario file, as an IMPORT trade.
    mode = resolve_injection_mode()
    if mode in ('SINGLE_FILE', 'MULTIPLE_FILE', 'IMPORT') \
            or (mode == 'IMPORT_EXCEL' and not resolve_scenario_id()):
        assert ctx.product_type in PRODUCT_TYPES, \
            (f'Injected product type [{ctx.product_type}] should be one of '
             f'{", ".join(PRODUCT_TYPES)} ({mode} mode).\n{ctx.trade}')
        return

    configured = resolve_product_type()
    assert ctx.product_type == configured, \
        (f'Injected product type [{ctx.product_type}] should match the product type '
         f'{mode} mode configures, [{configured}].\n{ctx.trade}')


def test_currency_sides_are_consistent(ctx):
    """Verify the payload's base currency buyer, seller and partyDirection agree on who buys which currency"""
    # Steps 05 onward expect each currency's side from the buyer and seller, so
    # a payload that contradicts itself (a reversed trade whose partyDirection
    # was not flipped with its buyer and seller) is reported here, not as a
    # ledger defect. Received TradeServ events always agree.
    problems = payload_side_problems(ctx.near_leg, ctx.party_id, ctx.counter_party_id)
    assert not problems, \
        (f'The injected nearLeg says two different things about who buys '
         f'{ctx.sides["baseCurrency"]}: {"; ".join(problems)}.\n{ctx.trade}')
