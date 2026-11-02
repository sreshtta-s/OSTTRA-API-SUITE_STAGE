# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 06: find the Domain A netting group for the injected trade.

Lists the Domain A netting groups on the trade's value date (recorded by step
04), widening the date range if needed, and takes the first whose configSnapshot
names the trade's netting rule. Its nettingGroupId is saved to run state as
nettingGroupIdString for the later Domain A steps; step 07 replaces it if the
trade's legs turn out to be in another group.
"""
import types
from datetime import datetime, timedelta, timezone

import pytest

from api.payloads.netting_payload import get_netting_group_list_payload
from api.services.base_service import is_ok
from api.services.netting_service import NettingService
from utils.api_logger import log_ids
from utils.describe import trade_line
from utils.helpers import is_number
from utils.state_helper import StateHelper, resolve_rule_id
from utils.wait import GIVE_UP, wait_until

# How long the ledger gets to route the trade into a netting group, and how often
# to check (at 0s and 5s with these values).
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
NETTING_TIMEOUT_SECONDS = 5
NETTING_POLL_SECONDS = 5


# ============================================================================
# TEST SUITE: VALIDATE NETTING GROUP
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Find the Domain A netting group once for every test in this module."""
    c = types.SimpleNamespace(
        res_status=None, response_body=None, matched_netting_group=None,
        extracted_numeric_id=None, extracted_string_id=None, server_error=None,
    )

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in failure
    # messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None

    # Spot and forward trades, and different party pairs, net under different
    # rules, so the rule is resolved from the injected trade. None (conflicting
    # ruleIds in NETTING_RULES) fails test_expected_rule_id_present.
    c.expected_rule_id = resolve_rule_id(state)
    c.expected_party = state.get('reqPartyId')
    c.expected_counter_party = state.get('counterPartyId')

    # The group is filed under the trade's value date, which step 04 records from
    # the injected payload. Today (UTC) is used only if state has none.
    c.target_date = state.get('valueDate') or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Names the trade in every assertion message. c.group, the group each check
    # was made on, gains the group's ids once it is found; c.window is the wider
    # date range searched when the value date alone matches nothing.
    c.trade = trade_line(state)
    c.group = 'Domain A netting group'
    c.window = None

    # Without a token or a rule there is nothing to query; the pre-flight tests
    # report which one is missing.
    if not (state.get('token') and c.expected_rule_id):
        return c

    # Each check asks for the value date alone first and, only if no group matches,
    # this wider window (yesterday to 30 days ahead of today) in case the group is
    # filed under a neighbouring date.
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    window_end = (now + timedelta(days=30)).strftime('%Y-%m-%d')
    c.window = f'{window_start} .. {window_end}'

    netting_service = NettingService(session, domain='A', token=state['token'])

    def query_groups(start_date, end_date):
        """Return the first group under the rule with a value date in the range.

        Records the status, body and any rejection on the namespace for the tests.

        :returns: the matching group; None if no group matches yet (or the body
            has no items list); GIVE_UP if the call was rejected (retrying would
            not help).
        """
        res = netting_service.get_all_netting_groups(
            get_netting_group_list_payload(start_date, end_date))
        c.res_status = res.status_code

        if not is_ok(res):
            c.server_error = res.text
            return GIVE_UP

        c.response_body = res.json()

        if not isinstance(c.response_body.get('items'), list):
            return None

        # A group records the rule it nets under in configSnapshot. Whether the
        # trade itself is in the group is checked against its legs in step 07.
        return next(
            (item for item in c.response_body['items']
             if (item.get('configSnapshot') or {}).get('nettingRuleId') == c.expected_rule_id),
            None,
        )

    # The ledger routes the trade into its group asynchronously, so the group is
    # often missing on the first check. A rejected listing stops the wait at once.
    def find_netting_group():
        match = query_groups(c.target_date, c.target_date)

        if match is None:
            print(f'INFO: No group on [{c.target_date}]; widening to '
                  f'[{window_start} .. {window_end}]')
            match = query_groups(window_start, window_end)

        if match is GIVE_UP:
            return GIVE_UP

        c.matched_netting_group = match
        return match

    wait_until(find_netting_group,
               f'netting group for Rule ID [{c.expected_rule_id}] on Domain A',
               timeout=NETTING_TIMEOUT_SECONDS, interval=NETTING_POLL_SECONDS)

    print(f'INFO: Domain A Netting Groups API responded with status: {c.res_status}')

    if c.server_error is not None:
        print(f'API Request Failed with status: {c.res_status}')
        print(f'Server Rejection Reason: {c.server_error}')
    elif c.matched_netting_group:
        c.extracted_numeric_id = c.matched_netting_group.get('id')
        c.extracted_string_id = c.matched_netting_group.get('nettingGroupId')
        c.group = f'Domain A netting group [{c.extracted_string_id}] (id {c.extracted_numeric_id})'

        # Later Domain A steps (07, 07c, 08, 09) read this group id.
        StateHelper.save_state({
            'nettingGroupIdString': c.extracted_string_id,
        })
        log_ids('STEP 06 netting group (A)',
                nettingGroupId=c.extracted_string_id,
                nettingGroupIdNumeric=c.extracted_numeric_id,
                nettingRuleId=c.expected_rule_id)
        print(f'Successfully mapped and saved Netting Group [{c.extracted_string_id}] to state.')
    else:
        print(f'Netting Group containing Rule ID [{c.expected_rule_id}] '
              'was NOT found in the ledger response.')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

# --- PRE-FLIGHT CHECKS ---
def test_auth_token_present(ctx):
    """Verify auth token is present in state"""
    assert ctx.token_present, \
        ('Domain A token is missing: .state/default.json has no "token". '
         f'Did step 01 run?\n{ctx.trade}')


def test_expected_rule_id_present(ctx):
    """Verify expected Rule ID is present in state"""
    assert ctx.expected_rule_id is not None, \
        ('Rule ID is missing - the injected trade\'s entry in data/trade_scenarios.py '
         'NETTING_RULES is listed more than once with different ruleIds.'
         f'\n{ctx.trade}')


# --- API HEALTH & SCHEMA ---
def test_endpoint_returns_2xx(ctx):
    """Verify endpoint returns a 2xx success status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')
    assert 200 <= ctx.res_status < 300, \
        (f'Domain A netting group listing (value date [{ctx.target_date}]) should '
         f'return a 2xx status, but returned {ctx.res_status}. '
         f'Reason: {ctx.server_error or "none given"}\n{ctx.trade}')


def test_response_has_items_array(ctx):
    """Verify ledger response contains a valid items array"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    items = ctx.response_body.get('items')
    assert isinstance(items, list), \
        (f'Domain A netting group listing (value date [{ctx.target_date}]) should carry '
         f'an "items" list, but "items" is {type(items).__name__}.\n{ctx.trade}')


# --- ROUTING EXISTENCE ---
def test_netting_group_exists_for_rule(ctx):
    """Verify netting group utilizing the expected Rule ID exists in ledger"""
    # Fails rather than skips, as step 06b does: an empty response is a group
    # not found, and skipping would let the step pass without one.
    if ctx.res_status:
        searched = f'value date [{ctx.target_date}]' + (
            f' or in the window [{ctx.window}]' if ctx.window else '')
        problem = (f'No Domain A netting group under rule [{ctx.expected_rule_id}] was found '
                   f'for {searched}.')
    else:
        problem = 'Domain A netting groups were not listed: a pre-flight check above failed.'
    assert ctx.matched_netting_group is not None, f'{problem}\n{ctx.trade}'


# --- EXTRACTED IDS VALIDATION ---
def test_numeric_netting_group_id_valid(ctx):
    """Verify extracted numeric netting group ID is valid"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    assert is_number(ctx.extracted_numeric_id) and ctx.extracted_numeric_id > 0, \
        (f'{ctx.group}: id should be a number above 0, but is '
         f'[{ctx.extracted_numeric_id!r}].\n{ctx.trade}')


def test_string_netting_group_id_nomenclature(ctx):
    """Verify extracted string netting group ID follows NG- nomenclature"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    assert isinstance(ctx.extracted_string_id, str) and 'NG-' in ctx.extracted_string_id, \
        (f'{ctx.group}: nettingGroupId should be a string following the NG- '
         f'nomenclature, but is [{ctx.extracted_string_id!r}].\n{ctx.trade}')


# --- DEEP BUSINESS LOGIC ASSERTIONS ---
def test_config_snapshot_rule_mapping(ctx):
    """Verify config snapshot retains accurate Rule ID mapping"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    rule_id = (ctx.matched_netting_group.get('configSnapshot') or {}).get('nettingRuleId')
    assert rule_id == ctx.expected_rule_id, \
        (f'{ctx.group}: configSnapshot.nettingRuleId should be the trade\'s rule '
         f'[{ctx.expected_rule_id}], but is [{rule_id}].\n{ctx.trade}')


def test_trading_party_matches_origin(ctx):
    """Verify trading party precisely matches the origin initiator"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    trading_party = ctx.matched_netting_group.get('tradingParty')
    assert trading_party == ctx.expected_party, \
        (f'{ctx.group}: tradingParty should be the injected party '
         f'[{ctx.expected_party}], but is [{trading_party}].\n{ctx.trade}')


def test_counter_party_matches_recipient(ctx):
    """Verify counter party precisely matches the destination recipient"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    counter_party = ctx.matched_netting_group.get('counterParty')
    assert counter_party == ctx.expected_counter_party, \
        (f'{ctx.group}: counterParty should be the injected counterparty '
         f'[{ctx.expected_counter_party}], but is [{counter_party}].\n{ctx.trade}')


def test_netting_strategy(ctx):
    """Verify netting strategy dynamically resolves to BILATERAL_ASSET_PAIR"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    strategy = ctx.matched_netting_group.get('nettingStrategy')
    assert strategy == 'BILATERAL_ASSET_PAIR', \
        (f'{ctx.group}: nettingStrategy should be BILATERAL_ASSET_PAIR, but is '
         f'[{strategy}].\n{ctx.trade}')


def test_settlement_system_is_baton(ctx):
    """Verify settlement system architecture is designated as BATON"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    settlement_system = ctx.matched_netting_group.get('settlementSystem')
    assert settlement_system == 'BATON', \
        f'{ctx.group}: settlementSystem should be BATON, but is [{settlement_system}].\n{ctx.trade}'


def test_auto_affirmation_is_boolean(ctx):
    """Verify auto-affirmation conforms to configuration rules (Boolean)"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    auto_affirmation = (ctx.matched_netting_group.get('configSnapshot') or {}).get(
        'enabledForAutoAffirmation')
    assert isinstance(auto_affirmation, bool), \
        (f'{ctx.group}: configSnapshot.enabledForAutoAffirmation should be true or '
         f'false, but is [{auto_affirmation!r}].\n{ctx.trade}')


def test_netting_group_status(ctx):
    """Verify netting group status implies ledger ingestion success"""
    if not ctx.matched_netting_group:
        pytest.skip(f'No Domain A netting group was found under rule [{ctx.expected_rule_id}].')
    valid_statuses = ['CONFIRMED', 'PENDING_AFFIRMATION', 'OPEN_NET']
    status = ctx.matched_netting_group.get('status')
    assert status in valid_statuses, \
        (f'{ctx.group}: status should be one of {", ".join(valid_statuses)}, but is '
         f'[{status}].\n{ctx.trade}')
