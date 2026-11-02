# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 05b: find the injected trade on the Domain B ledger and check its fields.

The counterparty's view of the trade step 05 checks on Domain A. Reads the
Domain B token and the injected trade's id, counterparty, currencies and leg
amounts from run state, pages the trade-management ledger until the trade
appears, then asserts the ledger record against what step 04 injected. Writes
nothing back to state.
"""
import re
import types
from decimal import Decimal

import pytest

from api.payloads.trade_payload import CANCELLED_STATUS, expected_product_sub_type
from api.services.transaction_service import TransactionService, describe_lookup
from utils.describe import trade_line
from utils.helpers import is_number
from utils.state_helper import StateHelper
from utils.trade_direction import LEDGER_SIDES, base_currency, expected_sides
from utils.wait import GIVE_UP, wait_until

ISO_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')


def same_amount(ledger_value, injected_value):
    """Return True if a ledger amount and an injected amount are the same number.

    The ledger returns amounts as strings ("95") and the payload carries numbers
    (95), so a plain == never matches. Both are compared as Decimal, not float,
    so the match is exact: 10000.6 and "10000.60" are equal.

    An unparseable value, None included, returns False, so the calling
    assertion fails with its own message instead of raising.
    """
    try:
        return Decimal(str(ledger_value)) == Decimal(str(injected_value))
    except (ArithmeticError, TypeError, ValueError):
        return False

# How long the ledger gets to show the injected trade, and how often to check
# (every 5s for up to 60s). In past runs about half the trades were not yet
# listed at the first check, and the search by id answers at once, so the wait
# itself has to cover that lag. Checks stop as soon as the trade appears.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
LEDGER_TIMEOUT_SECONDS = 60
LEDGER_POLL_SECONDS = 5


# ============================================================================
# TEST SUITE: VALIDATE TRADE ON LEDGER (DOMAIN B)
# NOTE: Party perspective is flipped - Domain B sees counterPartyId as
#       the trading party and reqPartyId as the counter party.
# NOTE: The UTI is not asserted here. Domain B returns sourceReferenceId as
#       None - the UTI does not propagate to the counterparty ledger, so the
#       trade is identified by Trade ID alone. Step 05 asserts it on Domain A.
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Find the trade on the Domain B ledger once for every test in this module."""
    c = types.SimpleNamespace(
        res_status=None, response_body=None, matched_trade=None, lookup_detail=None,
    )

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token = state.get('tokenB')
    c.token_present = token is not None
    c.expected_trade_id = state.get('injectedTradeId')

    # The counterparty's ledger lists counterPartyId as the trading party.
    c.expected_party = state.get('counterPartyId')
    c.injected_product_type = state.get('productType')   # recorded by step 04
    # "Cancelled" once step 04 cancel has resent the trade. The runner cancels
    # after 05b (TC032's "cancel": true), so only a direct pytest run sees it.
    c.injected_status = state.get('injectedStatus')

    # The currencies the injected payload carried. Under DYNAMIC the payload is
    # generated from these state values; in the file and scenario modes step 04
    # overwrites them from the injected JSON. Step 04 records the leg amounts
    # off the payload it sent, in every mode.
    #
    # Domain B reports these exactly as Domain A does: the counterparty's view
    # flips the party fields, not which currency or amount sits on which leg,
    # so the checks below match step 05's.
    c.expected_base_currency = base_currency(state)
    c.expected_reference_currency = state.get('referenceCurrency')
    c.expected_currency_pair = state.get('tradeCurrencyPair')
    c.expected_base_amount = state.get('baseCurrencyAmount')
    c.expected_reference_amount = state.get('referenceCurrencyAmount')

    # What the counterparty's view does flip is each leg's direction: the side
    # the counterparty takes in each currency, the reverse of the party's.
    c.expected_sides = expected_sides(state, c.expected_party, LEDGER_SIDES)

    # Step 04 records the trade date off the payload it sent (fx.tradeDate).
    # It belongs to the trade, not the viewing party, so Domain B matches A.
    c.expected_trade_date = state.get('tradeDate')

    # Names the trade in every assertion message. c.record says where each
    # field check was made, with the ledger record's own ids once it is found.
    c.trade = trade_line(state)
    c.record = 'Domain B ledger'

    if not token or not c.expected_trade_id:
        print('WARN: Skipping Domain B. Missing tokenB or injectedTradeId.')
        return c

    txn_service = TransactionService(session, domain='B', token=token)

    # The ledger ingests asynchronously, so the search repeats until the trade
    # appears or the timeout is spent. Each attempt searches by the trade id and
    # reads every page of results, so a miss means the ledger holds no record
    # with that id. c.lookup holds the latest attempt, reported even when the
    # wait gives up.
    def look_for_trade():
        c.lookup = txn_service.find_trade_by_trade_id(c.expected_trade_id,
                                                      trade_date=c.expected_trade_date)
        # A failed call will not recover within the timeout, so stop and let
        # the status assertions report it.
        if not c.lookup.ok:
            return GIVE_UP
        return c.lookup if c.lookup.trade else None

    c.lookup = None
    wait_until(look_for_trade, f'Domain B ledger lookup for trade [{c.expected_trade_id}]',
               timeout=LEDGER_TIMEOUT_SECONDS, interval=LEDGER_POLL_SECONDS)

    lookup = c.lookup
    c.res_status = lookup.status
    c.response_body = lookup.first_page
    c.matched_trade = lookup.trade
    c.lookup_detail = describe_lookup(lookup)
    if c.matched_trade:
        c.record = (f'Domain B ledger record [tradeRecordId '
                    f'{c.matched_trade.get("tradeRecordId")}, transactionId '
                    f'{c.matched_trade.get("transactionId")}]')

    print(f'INFO: Domain B Trade Validation API responded with status: {c.res_status}')
    if lookup.ok:
        found = 'FOUND' if c.matched_trade else 'NOT FOUND'
        print(f'INFO: Trade ID [{c.expected_trade_id}] -> {found} on Domain B; {c.lookup_detail}')
    else:
        print(f'ERROR: Domain B lookup failed ({c.res_status}): {lookup.error_text}')

    return c


# --- PRE-FLIGHT ---
def test_domain_b_auth_token_present(ctx):
    """Verify Domain B auth token is present in state"""
    assert ctx.token_present, \
        ('Domain B token is missing: .state/default.json has no "tokenB". '
         f'Did step 01 run?\n{ctx.trade}')


def test_injected_trade_id_present(ctx):
    """Verify injected Trade ID is present in state"""
    assert ctx.expected_trade_id is not None, \
        ('injectedTradeId is missing from state, so there is no trade to look up on '
         f'the Domain B ledger. Did step 04 run and get a 2xx?\n{ctx.trade}')


# --- API HEALTH ---
def test_request_returns_2xx(ctx):
    """Verify request returns a 2xx success status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert 200 <= ctx.res_status < 300, \
        (f'Domain B ledger search for trade [{ctx.expected_trade_id}] should return a '
         f'2xx status, but returned {ctx.res_status}. '
         f'Reason: {ctx.lookup.error_text or "none given"}\n{ctx.trade}')


# --- TRADE EXISTENCE ---
def test_trade_found_on_ledger(ctx):
    """Verify injected Trade ID is found on the ledger"""
    # Fails rather than skips: a trade missing from the ledger is the defect
    # this step exists to catch.
    assert ctx.matched_trade is not None, \
        (f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger. '
         f'{ctx.lookup_detail or "The ledger was not searched: a pre-flight check above failed."}\n{ctx.trade}')
    assert ctx.matched_trade['sourceTradeId'] == ctx.expected_trade_id, \
        (f'{ctx.record}: sourceTradeId should be the injected trade id '
         f'[{ctx.expected_trade_id}], but is [{ctx.matched_trade["sourceTradeId"]}].'
         f'\n{ctx.trade}')


def test_trade_appears_exactly_once(ctx):
    """Verify exactly one ledger record carries the injected Trade ID"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')

    count = len(ctx.lookup.matches)
    transaction_ids = ', '.join(str(m.get('transactionId')) for m in ctx.lookup.matches)
    assert count == 1, \
        (f'Trade [{ctx.expected_trade_id}] should appear on the Domain B ledger '
         f'exactly once, but {count} records carry it (transactionIds: '
         f'{transaction_ids}). A second record means a trade that should have been '
         f'rejected as a duplicate was kept. {ctx.lookup_detail}\n{ctx.trade}')


# --- SCHEMA & PAGINATION ---
def test_response_has_trades_array(ctx):
    """Verify response contains a valid trades array"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    trades = ctx.response_body.get('trades')
    assert isinstance(trades, list), \
        (f'Domain B ledger page 0 (search for trade [{ctx.expected_trade_id}]) should '
         f'carry a "trades" list, but "trades" is {type(trades).__name__}.\n{ctx.trade}')


def test_total_count_valid(ctx):
    """Verify pagination totalCount is a valid number >= 0"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    total_count = ctx.response_body.get('totalCount')
    assert is_number(total_count) and total_count >= 0, \
        (f'Domain B ledger page 0 (search for trade [{ctx.expected_trade_id}]): '
         f'totalCount should be a number >= 0, but is [{total_count!r}].\n{ctx.trade}')


def test_page_size_valid(ctx):
    """Verify pagination pageSize is a valid number > 0"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    page_size = ctx.response_body.get('pageSize')
    assert is_number(page_size) and page_size > 0, \
        (f'Domain B ledger page 0 (search for trade [{ctx.expected_trade_id}]): '
         f'pageSize should be a number above 0, but is [{page_size!r}].\n{ctx.trade}')


def test_current_page_valid(ctx):
    """Verify pagination currentPage is a valid number >= 0"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    current_page = ctx.response_body.get('currentPage')
    assert is_number(current_page) and current_page >= 0, \
        (f'Domain B ledger page 0 (search for trade [{ctx.expected_trade_id}]): '
         f'currentPage should be a number >= 0, but is [{current_page!r}].\n{ctx.trade}')


def test_has_more_is_boolean(ctx):
    """Verify pagination hasMore is a boolean flag"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    has_more = ctx.response_body.get('hasMore')
    assert isinstance(has_more, bool), \
        (f'Domain B ledger page 0 (search for trade [{ctx.expected_trade_id}]): '
         f'hasMore should be true or false, but is [{has_more!r}].\n{ctx.trade}')


# --- SYSTEM IDENTIFIERS ---
def test_trade_record_id_valid(ctx):
    """Verify tradeRecordId is a valid numerical ID > 0"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    record_id = ctx.matched_trade.get('tradeRecordId')
    assert is_number(record_id) and record_id > 0, \
        (f'{ctx.record}: tradeRecordId should be a number above 0, but is '
         f'[{record_id!r}].\n{ctx.trade}')


def test_transaction_id_populated(ctx):
    """Verify transactionId is a populated string"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    transaction_id = ctx.matched_trade.get('transactionId')
    assert isinstance(transaction_id, str) and len(transaction_id) > 0, \
        (f'{ctx.record}: transactionId should be a non-empty string, but is '
         f'[{transaction_id!r}].\n{ctx.trade}')


def test_leg1_id_valid(ctx):
    """Verify leg1Id is a valid numerical ID > 0"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    leg1_id = ctx.matched_trade.get('leg1Id')
    assert is_number(leg1_id) and leg1_id > 0, \
        (f'{ctx.record}: leg1Id should be a number above 0, but is '
         f'[{leg1_id!r}].\n{ctx.trade}')


def test_created_at_is_iso(ctx):
    """Verify createdAt strictly follows ISO-8601 format"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    created_at = ctx.matched_trade.get('createdAt')
    assert ISO_TIMESTAMP.match(str(created_at)), \
        (f'{ctx.record}: createdAt should be an ISO-8601 timestamp '
         f'(YYYY-MM-DDTHH:MM:SS...), but is [{created_at}].\n{ctx.trade}')


# --- FLIPPED BILATERAL LOGIC ---
def test_trading_party_is_flipped(ctx):
    """Verify tradingParty reflects the Domain B perspective (flipped - counterPartyId)"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    trading_party = ctx.matched_trade.get('tradingParty')
    assert trading_party == ctx.expected_party, \
        (f'{ctx.record}: tradingParty should be the injected counterparty '
         f'[{ctx.expected_party}], since Domain B sees the trade from the '
         f'counterparty\'s side, but is [{trading_party}].\n{ctx.trade}')
# --------------------------------


def test_source_is_tradeserv(ctx):
    """Verify source platform is identified as TradeServ"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    source = ctx.matched_trade.get('source')
    assert source == 'TradeServ', \
        f'{ctx.record}: source should be TradeServ, but is [{source}].\n{ctx.trade}'


def test_product_type_is_fx(ctx):
    """Verify productType is strictly mapped as FX"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    product_type = ctx.matched_trade.get('productType')
    assert product_type == 'FX', \
        (f'{ctx.record}: productType should be FX for the injected '
         f'{ctx.injected_product_type}, but is [{product_type}].\n{ctx.trade}')


def test_product_sub_type_reflects_injection(ctx):
    """Verify productSubType reflects the injected product type"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    # The ledger summary does not always carry productSubType, so this skips
    # when it is absent. Step 07 makes the mandatory check on the netting
    # group's underlying record.
    if 'productSubType' not in ctx.matched_trade:
        pytest.skip('Ledger summary does not expose productSubType.')

    expected_sub_type = expected_product_sub_type(ctx.injected_product_type)
    sub_type = ctx.matched_trade['productSubType']
    assert sub_type == expected_sub_type, \
        (f'{ctx.record}: productSubType should be [{expected_sub_type}] for the '
         f'injected {ctx.injected_product_type}, but is [{sub_type}].\n{ctx.trade}')


def test_type_is_trade(ctx):
    """Verify primary transaction type is TRADE"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    trade_type = ctx.matched_trade.get('type')
    assert trade_type == 'TRADE', \
        f'{ctx.record}: type should be TRADE, but is [{trade_type}].\n{ctx.trade}'


# --- ASSET CLASSES ---
def test_leg1_primary_asset_class(ctx):
    """Verify leg1PrimaryAssetClass maps to CASH"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    asset_class = ctx.matched_trade.get('leg1PrimaryAssetClass')
    assert asset_class == 'CASH', \
        f'{ctx.record}: leg1PrimaryAssetClass should be CASH, but is [{asset_class}].\n{ctx.trade}'


def test_leg1_primary_asset_type(ctx):
    """Verify leg1PrimaryAssetType maps to FIAT"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    asset_type = ctx.matched_trade.get('leg1PrimaryAssetType')
    assert asset_type == 'FIAT', \
        f'{ctx.record}: leg1PrimaryAssetType should be FIAT, but is [{asset_type}].\n{ctx.trade}'


def test_leg1_primary_asset_value_populated(ctx):
    """Verify leg1PrimaryAssetValue is a populated string"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    value = ctx.matched_trade.get('leg1PrimaryAssetValue')
    assert isinstance(value, str) and len(value) > 0, \
        (f'{ctx.record}: leg1PrimaryAssetValue (the '
         f'{ctx.matched_trade.get("leg1PrimaryAssetId")} amount) should be a non-empty '
         f'string, but is [{value!r}].\n{ctx.trade}')


# --- CURRENCIES (DOMAIN B: SAME AS INJECTED) ---
# The ledger flattens the trade onto one leg with a primary and a secondary
# asset, in the order the payload carried them: the base currency
# primary, the reference currency secondary. The order is the same on both
# domains: which currency sits on which leg belongs to the trade, not to the
# viewing party, so these checks match step 05's.

def test_leg1_primary_asset_is_base_currency(ctx):
    """Verify leg1PrimaryAssetId is the base currency that was injected"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if not ctx.expected_base_currency:
        pytest.skip('baseCurrency is absent from state; nothing to compare against.')
    primary = ctx.matched_trade.get('leg1PrimaryAssetId')
    assert primary == ctx.expected_base_currency, \
        (f'{ctx.record}: leg1PrimaryAssetId should be the injected base currency '
         f'[{ctx.expected_base_currency}], the same as Domain A, but is [{primary}].'
         f'\n{ctx.trade}')


def test_leg1_secondary_asset_is_reference_currency(ctx):
    """Verify leg1SecondaryAssetId is the reference currency that was injected"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if not ctx.expected_reference_currency:
        pytest.skip('referenceCurrency is absent from state; nothing to compare against.')
    secondary = ctx.matched_trade.get('leg1SecondaryAssetId')
    assert secondary == ctx.expected_reference_currency, \
        (f'{ctx.record}: leg1SecondaryAssetId should be the injected reference currency '
         f'[{ctx.expected_reference_currency}], the same as Domain A, but is '
         f'[{secondary}].\n{ctx.trade}')


def test_leg1_currency_pair_matches_injection(ctx):
    """Verify the two leg assets together are the injected currency pair"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if not ctx.expected_currency_pair:
        pytest.skip('tradeCurrencyPair is absent from state; nothing to compare against.')

    # tradeCurrencyPair is unseparated (CNHUSD) and names the two currencies in
    # alphabetical order, not base first, so both sides are compared in that
    # order. Which leg carries the base is checked by the two tests above.
    pair = ctx.expected_currency_pair
    primary = ctx.matched_trade.get('leg1PrimaryAssetId')
    secondary = ctx.matched_trade.get('leg1SecondaryAssetId')
    ledger_pair = ''.join(sorted((str(primary), str(secondary))))

    assert ledger_pair == ''.join(sorted((pair[:3], pair[3:]))), \
        (f'{ctx.record}: the two leg assets should be the two currencies of the '
         f'injected currency pair [{pair}], the same as Domain A, but are '
         f'[{primary}] (primary) and [{secondary}] (secondary).\n{ctx.trade}')


# --- AMOUNTS (DOMAIN B: SAME AS INJECTED) ---
# Both amounts are the ones step 04 read off the payload it sent, so these
# checks hold in every injection mode. Each amount sits on its currency's leg,
# and the legs do not swap between domains, so these checks match step 05's.

def test_leg1_primary_amount_matches_injected_base(ctx):
    """Verify leg1PrimaryAssetValue is the injected base currency amount"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if ctx.expected_base_amount is None:
        pytest.skip('baseCurrencyAmount is absent from state; nothing to compare against.')

    ledger_amount = ctx.matched_trade.get('leg1PrimaryAssetValue')
    assert same_amount(ledger_amount, ctx.expected_base_amount), \
        (f'{ctx.record}: leg1PrimaryAssetValue should be the injected base amount '
         f'[{ctx.expected_base_amount}] {ctx.expected_base_currency}, the same as '
         f'Domain A, but is [{ledger_amount}] '
         f'{ctx.matched_trade.get("leg1PrimaryAssetId")}.\n{ctx.trade}')


def test_leg1_secondary_amount_matches_injected_reference(ctx):
    """Verify leg1SecondaryAssetValue is the injected reference currency amount"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if ctx.expected_reference_amount is None:
        pytest.skip('referenceCurrencyAmount is absent from state; nothing to compare against.')

    ledger_amount = ctx.matched_trade.get('leg1SecondaryAssetValue')
    assert same_amount(ledger_amount, ctx.expected_reference_amount), \
        (f'{ctx.record}: leg1SecondaryAssetValue should be the injected reference '
         f'amount [{ctx.expected_reference_amount}] {ctx.expected_reference_currency}, '
         f'the same as Domain A, but is [{ledger_amount}] '
         f'{ctx.matched_trade.get("leg1SecondaryAssetId")}.\n{ctx.trade}')


# --- TRADE DATE (DOMAIN B: SAME AS INJECTED) ---
# Every injection mode sends tradeDate as YYYY-MM-DD, the format the ledger
# returns, so the two compare as plain strings. The date does not change with
# the viewing party, so this check matches step 05's.

def test_trade_date_matches_injected(ctx):
    """Verify tradeDate is the trade date that was injected"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if not ctx.expected_trade_date:
        pytest.skip('tradeDate is absent from state; nothing to compare against.')

    ledger_trade_date = ctx.matched_trade.get('tradeDate')
    assert ledger_trade_date == ctx.expected_trade_date, \
        (f'{ctx.record}: tradeDate should be the injected trade date '
         f'[{ctx.expected_trade_date}], the same as Domain A, but is '
         f'[{ledger_trade_date}].\n{ctx.trade}')


# --- DIRECTION (DOMAIN B: FLIPPED TO THE COUNTERPARTY'S SIDE) ---
# Each leg's direction is the side the counterparty takes in that currency, the
# reverse of what step 05 expects on Domain A: when the party buys the base
# currency, Domain B shows the base leg SELL and the reference leg BUY. Each
# leg is looked up by its own asset id, as in step 05.

def check_leg_direction(ctx, leg):
    """Assert one ledger leg's direction is the side the counterparty takes in its currency."""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    currency = ctx.matched_trade.get(f'{leg}AssetId')
    expected = ctx.expected_sides.get(currency)
    if expected is None:
        pytest.skip(f'No side to expect of [{ctx.expected_party}] in [{currency}]: state does '
                    'not name it as the base currency buyer or seller of a trade in that currency.')
    direction = ctx.matched_trade.get(f'{leg}Direction')
    assert direction == expected, \
        (f'{ctx.record}: {leg}Direction (the {currency} leg) should be [{expected}], the '
         f'side the counterparty [{ctx.expected_party}] takes in {currency} - the reverse '
         f'of Domain A - but is [{direction}].\n{ctx.trade}')


def test_leg1_primary_direction(ctx):
    """Verify leg1PrimaryDirection is the counterparty's side (BUY or SELL) of that currency"""
    check_leg_direction(ctx, 'leg1Primary')


def test_leg1_secondary_direction(ctx):
    """Verify leg1SecondaryDirection is the counterparty's side (BUY or SELL) of that currency"""
    check_leg_direction(ctx, 'leg1Secondary')


# --- STATUSES ---
def test_affirmation_status(ctx):
    """Verify affirmation status is NEW, AFFIRMED, or CONFIRMED"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    if ctx.injected_status == CANCELLED_STATUS:
        pytest.skip('Step 04 cancel resent this trade as Cancelled before this step ran; '
                    'steps 05ct_a and 05ct_b check its status.')
    status = ctx.matched_trade.get('status')
    assert status in ['AFFIRMED', 'CONFIRMED', 'NEW'], \
        (f'{ctx.record}: affirmation status should be NEW, AFFIRMED or CONFIRMED, '
         f'but is [{status}].\n{ctx.trade}')


def test_settlement_status_sequenced(ctx):
    """Verify settlement status is properly sequenced"""
    if not ctx.matched_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found on the Domain B ledger.')
    settlement_status = ctx.matched_trade.get('settlementStatus')
    assert settlement_status in ['NOT_STARTED', 'PENDING_SETTLEMENT', 'SETTLED'], \
        (f'{ctx.record}: settlementStatus should be NOT_STARTED, PENDING_SETTLEMENT '
         f'or SETTLED, but is [{settlement_status}].\n{ctx.trade}')
