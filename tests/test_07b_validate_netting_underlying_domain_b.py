# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 07b: find the injected trade's legs in its Domain B netting group.

The Domain B counterpart of step 07. Domain B sees the same trade from the
counterparty's side, so the trading party and counterparty are swapped. Lists
the legs, on the trade's value date, of the group step 06b saved
(nettingGroupIdStringB), falls back to the other Domain B groups of the
trade's party pair under its netting rule on its value date, and writes the
group that holds the trade back to state for steps 08b onward. Every listing is
read page by page, so no page limit can hide the trade. The tests then check
the matched leg's fields, dates, rule and amounts as step 07 does. The net of any other trades' legs in the group is
saved to state as nettingGroupOthersNetB, for step 12b to add to the injected
trade's.

A trade the run has cancelled once it was netted, with no live trade left for
state to name (a row whose cancelTrade cancels its only trade after step 06b),
is marked tradeCancelled in state and has no live legs to check. Only the group
step 06b found is read, until each of the trade's legs there is CANCELLED or
gone, so its cashflow is out of the group's net; the leg field checks skip.
The group must then net to 0 in every currency. TC045 and TC050 cancel their
only trade after step 07b instead, so this step checks its legs live and 07c
checks the 0 balance.
"""
import types
from decimal import Decimal

import pytest

from api.payloads.netting_payload import get_party_netting_group_list_payload
from api.payloads.trade_payload import expected_product_sub_type
from api.payloads.underlying_payload import get_underlying_list_payload
from api.services.base_service import is_ok
from api.services.netting_service import NettingService
from api.services.underlying_service import UnderlyingService
from utils.describe import amounts_line, trade_line
from utils.state_helper import StateHelper, resolve_rule_id
from utils.trade_direction import CASH_SIDES, base_currency, expected_sides, leg_amount, net_line
from utils.wait import GIVE_UP, wait_until

# Underlyings are the slowest records to appear after netting, so this step
# allows 30 seconds, checked every 5.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
UNDERLYING_TIMEOUT_SECONDS = 30
UNDERLYING_POLL_SECONDS = 5

# The status of a cancelled trade's leg in its netting group: the cashflow is
# marked cancelled and leaves the group's net, as step 07c reads it.
CANCELLED_LEG_STATUS = 'CANCELLED'


def same_amount(group_value, injected_value):
    """Return True if a group leg amount and an injected amount are the same number.

    Compared as Decimal, not float, as step 05b does, so 1001 injected and
    1001.0 returned match exactly. An unparseable value, None included, returns
    False so the calling assertion fails with its own message.
    """
    try:
        return Decimal(str(group_value)) == Decimal(str(injected_value))
    except (ArithmeticError, TypeError, ValueError):
        return False


# ============================================================================
# TEST SUITE: VALIDATE UNDERLYING TRADE IN NETTING GROUP (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Find the trade's legs in its Domain B group once for every test in this module.

    Returns without calling the API when the token, trade id or value date is
    missing; the pre-flight tests then report which one.
    """
    c = types.SimpleNamespace(
        res_status=None, response_body=None, matched_underlying_trade=None, leg_amounts={},
        leg_sides={}, legs=[], cancelled_legs=[], group_net={}, other_legs=[], unreadable_legs=[],
    )

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('tokenB') is not None

    # Domain B uses its own group id (from step 06b). There is no fallback to
    # Domain A's, which belongs to a different group.
    c.expected_netting_group_id = state.get('nettingGroupIdStringB')
    c.expected_trade_id = state.get('injectedTradeId')
    c.expected_party = state.get('reqPartyId')
    c.expected_counter_party = state.get('counterPartyId')
    # Spot and forward trades, and different party pairs, net under different
    # rules, so the rule is resolved from the injected trade.
    c.expected_rule_id = resolve_rule_id(state)
    # The trade's dates, recorded by step 04 from the injected payload. Legs are
    # listed on the value date alone, and each leg must carry both dates.
    c.target_date = state.get('valueDate')
    c.expected_trade_date = state.get('tradeDate')
    # Recorded by step 04 from the injected payload, so it is right in every
    # injection mode.
    c.injected_product_type = state.get('productType')
    # The injected trade's currencies and amounts. Step 05b checks the ledger
    # against the same values; here each must appear on the trade's legs.
    c.expected_base_currency = base_currency(state)
    c.expected_reference_currency = state.get('referenceCurrency')
    c.expected_base_amount = state.get('baseCurrencyAmount')
    c.expected_reference_amount = state.get('referenceCurrencyAmount')
    # Domain B lists the legs from the counterparty's side, the reverse of
    # Domain A: RECEIVABLE in the currency the counterparty buys, PAYABLE in the
    # one it sells.
    c.expected_sides = expected_sides(state, c.expected_counter_party, CASH_SIDES)

    # Names the trade in every assertion message. c.leg, the matched leg the
    # field checks read, is set once the trade is found.
    c.trade = trade_line(state)
    c.leg = None

    # Set by runner.py once the run has cancelled this trade and no live one is
    # left for state to name (a cancelTrade after step 06b): its legs must have
    # left the net instead.
    c.trade_cancelled = bool(state.get('tradeCancelled'))

    # Cleared until this run reads the group, so step 12b never expects a net
    # this step did not read.
    StateHelper.save_state({'nettingGroupOthersNetB': None, 'nettingGroupOthersLegsB': None})

    if not (state.get('tokenB') and c.expected_trade_id and c.target_date):
        return c

    underlying_service_b = UnderlyingService(session, domain='B', token=state['tokenB'])
    netting_service_b = NettingService(session, domain='B', token=state['tokenB'])

    def read_all_pages(list_call, build_payload):
        """Return (the body of the listing with every page's items, status code).

        Reads page after page, by offset, until the response's totalItems have
        all arrived or a page comes back empty, so a busy group or day cannot
        push the trade past the first page. The body is None if a call did not
        return 2xx; the status is the last call's, so the 2xx test can report it.
        """
        items = []
        while True:
            res = list_call(build_payload(len(items)))
            if not is_ok(res):
                return None, res.status_code
            data = res.json()
            page = data.get('items')
            # A body with no items list is returned as it is, for the schema test.
            if not isinstance(page, list):
                return data, res.status_code
            items += page
            if not page or len(items) >= (data.get('totalItems') or 0):
                data['items'] = items
                return data, res.status_code

    def find_trade_in_group(netting_group_id):
        """Return (the trade's first leg in the group or None, body, status code).

        Lists every page of the group's legs on the trade's value date, the only
        date its legs can carry. The body is None unless every call returned 2xx.
        """
        # Stray whitespace in the id would make the EQUAL filter match no legs.
        clean_group_id = netting_group_id.strip() if netting_group_id else netting_group_id

        # Same builder and paging as step 07, so both domains filter and page
        # identically.
        data, status = read_all_pages(
            underlying_service_b.get_all_underlyings,
            lambda offset: get_underlying_list_payload(
                clean_group_id, c.target_date, c.target_date, offset=offset))

        match = None
        if data is not None and isinstance(data.get('items'), list):
            match = next(
                (item for item in data['items']
                 if item.get('foId') and c.expected_trade_id in item['foId']),
                None,
            )

        return match, data, status

    # A cancelled trade is looked for only in the group step 06b found: the
    # fallback search below finds a group by the trade's live legs, which it no
    # longer has. The netting service applies the cancel after the ledgers do,
    # so the group is re-read until each of the trade's legs is CANCELLED or gone.
    if c.trade_cancelled:
        if not c.expected_netting_group_id:
            return c

        def out_of_net():
            _, data, status = find_trade_in_group(c.expected_netting_group_id)
            c.res_status, c.response_body = status, data
            if data is None or not isinstance(data.get('items'), list):
                return GIVE_UP
            c.cancelled_legs = [
                item for item in data['items']
                if item.get('foId') and c.expected_trade_id in item['foId']
            ]
            return all(leg.get('status') == CANCELLED_LEG_STATUS for leg in c.cancelled_legs)

        wait_until(out_of_net,
                   f'cancelled trade [{c.expected_trade_id}] to leave Domain B netting group '
                   f'[{c.expected_netting_group_id}]\'s net',
                   timeout=UNDERLYING_TIMEOUT_SECONDS, interval=UNDERLYING_POLL_SECONDS)
        print(f'INFO: Cancelled trade [{c.expected_trade_id}] in Domain B group '
              f'[{c.expected_netting_group_id}]: '
              + (', '.join(f'{leg.get("assetId")} {leg.get("status")}' for leg in c.cancelled_legs)
                 or 'no legs left'))

        # What the group nets to with the trade cancelled, from the
        # counterparty's side. A CANCELLED leg adds nothing, as in step 07c;
        # any other leg, of this trade or another run's sharing the group, counts.
        live_legs = [item for item in (c.response_body or {}).get('items') or []
                     if item.get('status') != CANCELLED_LEG_STATUS]
        for item in live_legs:
            value = leg_amount(item)
            if value is None:
                c.unreadable_legs.append(str(item.get('foId')))
                continue
            c.group_net[item.get('assetId')] = c.group_net.get(item.get('assetId'), Decimal(0)) + value
        # Each foId once: a trade has a leg per currency under the same foId.
        c.other_legs = sorted({str(item.get('foId')) for item in live_legs
                               if not (item.get('foId') and c.expected_trade_id in item['foId'])})
        if c.response_body is not None:
            print(f'INFO: Domain B group [{c.expected_netting_group_id}] nets to: '
                  f'{net_line(c.expected_counter_party, c.group_net)}.')
        return c

    # One search pass: the group step 06b found, then the party pair's other
    # Domain B groups under the trade's rule on its value date. Returns the
    # matched leg, or None so wait_until tries again.
    def search_for_trade():

        # --------------------------------------------------------------------
        # PHASE 1: Fast check against expected Netting Group from 6B
        # --------------------------------------------------------------------
        if c.expected_netting_group_id:
            match, data, status = find_trade_in_group(c.expected_netting_group_id)
            c.res_status = status
            print(f'INFO: Domain B Underlying Trades API responded with status: {c.res_status}')
            c.response_body = data

            if match:
                c.matched_underlying_trade = match
                print(f'INFO: Trade [{c.expected_trade_id}] verified in Domain B group '
                      f'[{c.expected_netting_group_id}].')
                return match

        # --------------------------------------------------------------------
        # PHASE 2: Fallback search across the party pair's groups under the
        # rule on the value date, every page of them
        # --------------------------------------------------------------------
        if c.expected_rule_id:
            # Domain B sees the group from the counterparty's side: tradingParty
            # is the trade's counterparty, counterParty its party.
            ng_data, _ = read_all_pages(
                netting_service_b.get_all_netting_groups,
                lambda offset: get_party_netting_group_list_payload(
                    c.target_date, c.expected_counter_party, c.expected_party,
                    rule_id=c.expected_rule_id, offset=offset))

            if ng_data is not None:
                # The server matches the rule ignoring case, so it is checked
                # exactly here.
                matching_groups = [
                    item for item in (ng_data.get('items') or [])
                    if (item.get('configSnapshot') or {}).get('nettingRuleId') == c.expected_rule_id
                ]

                for group in matching_groups:
                    # Already searched in phase 1.
                    if group.get('nettingGroupId') == c.expected_netting_group_id:
                        continue

                    print(f'INFO: Iterating... Checking fallback Domain B group '
                          f'[{group["nettingGroupId"]}]...')
                    match, data, status = find_trade_in_group(group['nettingGroupId'])
                    c.res_status = status
                    print(f'INFO: Domain B Underlying Trades (fallback group '
                          f'[{group["nettingGroupId"]}]) API responded with status: {c.res_status}')
                    c.response_body = data

                    if match:
                        c.matched_underlying_trade = match
                        c.expected_netting_group_id = group['nettingGroupId']

                        # Steps 08b onward and the Excel row read the group id from
                        # state, so it must name the group that holds the trade.
                        StateHelper.save_state({
                            'nettingGroupIdStringB': group['nettingGroupId'],
                        })

                        print(f'INFO: Trade [{c.expected_trade_id}] found! State securely '
                              f'overwritten with Group [{group["nettingGroupId"]}].')
                        return match

        return None

    wait_until(search_for_trade,
               f'trade [{c.expected_trade_id}] in a Domain B netting group',
               timeout=UNDERLYING_TIMEOUT_SECONDS, interval=UNDERLYING_POLL_SECONDS)

    if not c.matched_underlying_trade:
        print(f'ERROR: Trade [{c.expected_trade_id}] was NOT found in Domain B within '
              f'{UNDERLYING_TIMEOUT_SECONDS}s. Status: {c.res_status}')
    else:
        c.leg = (f'Domain B netting group [{c.expected_netting_group_id}], leg '
                 f'[{c.matched_underlying_trade.get("foId")}]')
        # One leg per currency: assetId is the currency, assetValue its amount.
        # response_body is the listing of the group that matched, since the
        # search returns as soon as it finds the trade.
        c.legs = [
            item for item in (c.response_body or {}).get('items') or []
            if item.get('foId') and c.expected_trade_id in item['foId']
        ]
        c.leg_amounts = {item.get('assetId'): item.get('assetValue') for item in c.legs}
        # And each currency's direction, RECEIVABLE or PAYABLE.
        c.leg_sides = {item.get('assetId'): item.get('direction') for item in c.legs}
        print(f'INFO: Trade [{c.expected_trade_id}] legs in Domain B group '
              f'[{c.expected_netting_group_id}]: {c.leg_amounts} {c.leg_sides}')

        # The group settles every trade it holds, not this one alone, so step
        # 12b expects its settlement candidates to net to this trade as injected
        # plus every other leg listed here. Their net, from the counterparty's
        # side (received positive, paid negative), is saved for it; {} when the
        # trade is alone. Not saved if any of those legs cannot be read, so a
        # partial net is never expected. See step 07.
        others = [
            item for item in (c.response_body or {}).get('items') or []
            if not (item.get('foId') and c.expected_trade_id in item['foId'])
        ]
        others_net = {}
        for item in others:
            value = leg_amount(item)
            if value is None:
                others_net = None
                print(f'ERROR: Leg [{item.get("foId")}] in Domain B group '
                      f'[{c.expected_netting_group_id}] has an unreadable amount or direction, '
                      'so the group\'s net is unknown.')
                break
            others_net[item.get('assetId')] = others_net.get(item.get('assetId'), Decimal(0)) + value
        if others_net is not None:
            StateHelper.save_state({
                'nettingGroupOthersNetB': {str(ccy): str(value) for ccy, value in others_net.items()},
                # Each foId once: a trade has a leg per currency under the same foId.
                'nettingGroupOthersLegsB': sorted({str(item.get('foId')) for item in others}),
            })
            if others:
                print(f'INFO: Domain B group [{c.expected_netting_group_id}] also holds '
                      f'{len(others)} leg(s) of other trades: '
                      f'{net_line(c.expected_counter_party, others_net)}.')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def skip_without_leg(ctx):
    """Skip a leg field check when there is no live leg to read: the trade was cancelled, or not found."""
    if ctx.trade_cancelled:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was cancelled, so it has no live leg to check.')
    if not ctx.matched_underlying_trade:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was not found in a Domain B netting group.')


# --- PRE-FLIGHT CHECKS ---
def test_domain_b_auth_token_present(ctx):
    """Pre-flight: Auth token is present in state"""
    assert ctx.token_present, \
        ('Domain B token is missing: .state/default.json has no "tokenB". '
         f'Did step 01 run?\n{ctx.trade}')


def test_netting_group_id_present(ctx):
    """Pre-flight: Expected Netting Group ID is present in state"""
    assert ctx.expected_netting_group_id is not None, \
        ('nettingGroupIdStringB is missing from state, so there is no Domain B netting '
         f'group to look in. Did step 06b find one?\n{ctx.trade}')


def test_trade_id_present(ctx):
    """Pre-flight: Expected Trade ID is present in state"""
    assert ctx.expected_trade_id is not None, \
        ('injectedTradeId is missing from state, so there is no trade to look for in a '
         f'Domain B netting group. Did step 04 run and get a 2xx?\n{ctx.trade}')


def test_expected_rule_id_present(ctx):
    """Pre-flight: Expected Rule ID is present in state"""
    # Without a rule id the fallback search cannot run, so a trade that landed
    # in a different group would be reported as missing.
    assert ctx.expected_rule_id is not None, \
        ('Rule ID is missing - the injected trade\'s entry in data/trade_scenarios.py '
         'NETTING_RULES is listed more than once with different ruleIds.'
         f'\n{ctx.trade}')


def test_value_date_and_parties_present(ctx):
    """Pre-flight: The trade's value date and parties are present in state"""
    # Legs are listed on the value date, and the fallback search lists the party
    # pair's groups on it, so without these a trade that landed in a different
    # group would be reported as missing.
    missing = [name for name, value in (
        ('valueDate', ctx.target_date), ('reqPartyId', ctx.expected_party),
        ('counterPartyId', ctx.expected_counter_party),
    ) if not value]
    assert not missing, \
        (f'{", ".join(missing)} missing from state, so the trade\'s Domain B netting '
         f'groups cannot be searched. Did step 04 run and get a 2xx?\n{ctx.trade}')


# --- API HEALTH & SCHEMA ---
def test_endpoint_returns_2xx(ctx):
    """API: Endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')
    assert 200 <= ctx.res_status < 300, \
        (f'Domain B underlying listing for trade [{ctx.expected_trade_id}] should '
         f'return a 2xx status, but returned {ctx.res_status}. Netting group searched '
         f'first: [{ctx.expected_netting_group_id}].\n{ctx.trade}')


def test_response_has_items_array(ctx):
    """Schema: Ledger response contains a valid items array"""
    if not ctx.response_body:
        pytest.skip('Response data is unavailable.')
    items = ctx.response_body.get('items')
    assert isinstance(items, list), \
        (f'Domain B underlying listing for netting group '
         f'[{ctx.expected_netting_group_id}] should carry an "items" list, but "items" '
         f'is {type(items).__name__}.\n{ctx.trade}')


# --- TRADE EXISTENCE ---
def test_trade_exists_in_netting_group(ctx):
    """Routing: Trade exists within the assigned Netting Group"""
    if ctx.trade_cancelled:
        pytest.skip(f'Trade [{ctx.expected_trade_id}] was cancelled, so it is not looked for '
                    'live: test_cancelled_trade_is_out_of_net checks its legs left the net.')
    # Fails rather than skips: a trade missing from its group is the defect this
    # step exists to catch.
    if ctx.token_present and ctx.expected_trade_id and ctx.target_date:
        problem = (f'Trade [{ctx.expected_trade_id}] has no leg in Domain B netting group '
                   f'[{ctx.expected_netting_group_id}], nor in any other Domain B group of '
                   f'[{ctx.expected_counter_party} / {ctx.expected_party}] under rule '
                   f'[{ctx.expected_rule_id}] on value date [{ctx.target_date}].')
    else:
        problem = ('Domain B netting groups were not searched for the trade: a pre-flight '
                   'check above failed.')
    assert ctx.matched_underlying_trade is not None, f'{problem}\n{ctx.trade}'


def test_cancelled_trade_is_out_of_net(ctx):
    """Routing: A cancelled trade's legs are CANCELLED or gone from the Netting Group"""
    if not ctx.trade_cancelled:
        pytest.skip('The trade is not cancelled, so its legs are checked live.')
    assert ctx.response_body is not None, \
        (f'Domain B netting group [{ctx.expected_netting_group_id}] was not read, so whether '
         f'cancelled trade [{ctx.expected_trade_id}] left its net is unknown: a check above '
         f'says why.\n{ctx.trade}')
    live = [f'{leg.get("foId")} ({leg.get("assetId")} {leg.get("status")})'
            for leg in ctx.cancelled_legs if leg.get('status') != CANCELLED_LEG_STATUS]
    assert not live, \
        (f'Trade [{ctx.expected_trade_id}] was cancelled, so its cashflow should be out of '
         f'Domain B netting group [{ctx.expected_netting_group_id}]\'s net, its legs '
         f'{CANCELLED_LEG_STATUS} or gone, but these were still live when the wait ended: '
         f'{", ".join(live)}.\n{ctx.trade}')


def test_cancelled_group_nets_to_zero(ctx):
    """Net: With its trade cancelled, the Netting Group nets to 0 in every currency"""
    if not ctx.trade_cancelled:
        pytest.skip('The trade is not cancelled, so the group nets to its live trades.')
    if ctx.response_body is None:
        pytest.skip('The netting group was not read: test_cancelled_trade_is_out_of_net says why.')
    assert not ctx.unreadable_legs, \
        (f'Domain B netting group [{ctx.expected_netting_group_id}] has live legs whose amount '
         f'or direction cannot be read, so whether it nets to 0 is unknown: '
         f'{", ".join(ctx.unreadable_legs)}.\n{ctx.trade}')
    moving = {ccy: value for ccy, value in ctx.group_net.items() if value}
    assert not moving, \
        (f'Trade [{ctx.expected_trade_id}] was cancelled, leaving the run no live trade in '
         f'Domain B netting group [{ctx.expected_netting_group_id}], so the group should net '
         f'to 0, but {net_line(ctx.expected_counter_party, moving)}. Live legs of other trades '
         f'in it: {", ".join(ctx.other_legs) or "none"}.\n{ctx.trade}')


# --- DEEP BUSINESS LOGIC ASSERTIONS ---
def test_asset_class_is_cash(ctx):
    """Business Logic: Asset class resolves correctly as CASH"""
    skip_without_leg(ctx)
    asset_class = ctx.matched_underlying_trade.get('assetClass')
    assert asset_class == 'CASH', \
        f'{ctx.leg}: assetClass should be CASH, but is [{asset_class}].\n{ctx.trade}'


def test_settlement_mode_is_net(ctx):
    """Business Logic: Settlement Mode resolves correctly as NET"""
    skip_without_leg(ctx)
    settlement_mode = ctx.matched_underlying_trade.get('settlementMode')
    assert settlement_mode == 'NET', \
        f'{ctx.leg}: settlementMode should be NET, but is [{settlement_mode}].\n{ctx.trade}'


def test_product_category_matches_injection(ctx):
    """Business Logic: Product Category matches the injected product type"""
    skip_without_leg(ctx)

    # productType is always FX in this suite; productSubType follows the injected
    # FxSpot / FxForward, so it is derived from state rather than fixed.
    product_type = ctx.matched_underlying_trade.get('productType')
    assert product_type == 'FX', \
        (f'{ctx.leg}: productType should be FX for the injected '
         f'{ctx.injected_product_type}, but is [{product_type}].\n{ctx.trade}')

    expected_sub_type = expected_product_sub_type(ctx.injected_product_type)
    assert expected_sub_type is not None, (
        f"No productSubType mapping for injected product type '{ctx.injected_product_type}'. "
        'Did step 04 run, and is the product type one the suite supports?'
        f'\n{ctx.trade}'
    )

    sub_type = ctx.matched_underlying_trade.get('productSubType')
    assert sub_type == expected_sub_type, \
        (f'{ctx.leg}: productSubType should be [{expected_sub_type}] for the injected '
         f'{ctx.injected_product_type}, but is [{sub_type}].\n{ctx.trade}')


def test_source_is_trade_management(ctx):
    """Business Logic: Origin source identifies as TRADE_MANAGEMENT"""
    skip_without_leg(ctx)
    source = ctx.matched_underlying_trade.get('source')
    assert source == 'TRADE_MANAGEMENT', \
        f'{ctx.leg}: source should be TRADE_MANAGEMENT, but is [{source}].\n{ctx.trade}'


def test_settlement_system_is_baton(ctx):
    """Business Logic: Settlement System architecture confirms BATON"""
    skip_without_leg(ctx)
    settlement_system = ctx.matched_underlying_trade.get('settlementSystem')
    assert settlement_system == 'BATON', \
        f'{ctx.leg}: settlementSystem should be BATON, but is [{settlement_system}].\n{ctx.trade}'


def test_confirmation_status(ctx):
    """Business Logic: Confirmation status implies successful netting inclusion"""
    skip_without_leg(ctx)
    valid_statuses = ['CONFIRMED', 'AFFIRMED']
    status = ctx.matched_underlying_trade.get('status')
    assert status in valid_statuses, \
        (f'{ctx.leg}: status should be CONFIRMED or AFFIRMED, but is [{status}].'
         f'\n{ctx.trade}')


# Domain B sees the trade from the counterparty's side, so its trading party is
# the injected counterparty and its counterparty the injected party.
def test_trading_party_is_flipped(ctx):
    """Business Logic: Trading Party precisely matches the injected recipient (Flipped for Domain B)"""
    skip_without_leg(ctx)
    trading_party = ctx.matched_underlying_trade.get('tradingParty')
    assert trading_party == ctx.expected_counter_party, \
        (f'{ctx.leg}: tradingParty should be the injected counterparty '
         f'[{ctx.expected_counter_party}], since Domain B sees the trade from the '
         f'counterparty\'s side, but is [{trading_party}].\n{ctx.trade}')


def test_counter_party_is_flipped(ctx):
    """Business Logic: Counter Party precisely matches the injected origin (Flipped for Domain B)"""
    skip_without_leg(ctx)
    counter_party = ctx.matched_underlying_trade.get('counterParty')
    assert counter_party == ctx.expected_party, \
        (f'{ctx.leg}: counterParty should be the injected party '
         f'[{ctx.expected_party}], since Domain B sees the trade from the '
         f'counterparty\'s side, but is [{counter_party}].\n{ctx.trade}')


def test_root_netting_group_id_matches(ctx):
    """Business Logic: Root Netting Group ID matches the requested group parameters"""
    skip_without_leg(ctx)
    group_id = ctx.matched_underlying_trade.get('nettingGroupId')
    assert group_id == ctx.expected_netting_group_id, \
        (f'{ctx.leg}: nettingGroupId should be the group the leg is listed under, '
         f'[{ctx.expected_netting_group_id}], but is [{group_id}].\n{ctx.trade}')


# --- DATES AND RULE (AS INJECTED / AS NETTING_RULES NAMES IT) ---
def test_value_date_matches_trade(ctx):
    """Business Logic: The trade's legs carry the injected value date"""
    skip_without_leg(ctx)
    got = {leg.get('assetId'): leg.get('valueDate') for leg in ctx.legs}
    assert all(value == ctx.target_date for value in got.values()), \
        (f'Domain B netting group [{ctx.expected_netting_group_id}]: the legs of trade '
         f'[{ctx.expected_trade_id}] should carry the injected value date '
         f'[{ctx.target_date}], but carry {amounts_line(got)}.\n{ctx.trade}')


def test_trade_date_matches_trade(ctx):
    """Business Logic: The trade's legs carry the injected trade date"""
    skip_without_leg(ctx)
    if not ctx.expected_trade_date:
        pytest.skip('tradeDate is absent from state; nothing to compare against.')
    # A leg's tradeDate is a timestamp ('2026-10-14 00:00:00.000000000'); its
    # date part is compared.
    expected = str(ctx.expected_trade_date)[:10]
    got = {leg.get('assetId'): str(leg.get('tradeDate') or '')[:10] for leg in ctx.legs}
    assert all(value == expected for value in got.values()), \
        (f'Domain B netting group [{ctx.expected_netting_group_id}]: the legs of trade '
         f'[{ctx.expected_trade_id}] should carry the injected trade date [{expected}], '
         f'but carry {amounts_line(got)}.\n{ctx.trade}')


def test_leg_rule_matches_group(ctx):
    """Business Logic: The trade's legs net under the trade's netting rule"""
    skip_without_leg(ctx)
    if not ctx.expected_rule_id:
        pytest.skip('No rule id to compare against: see the Rule ID pre-flight check.')
    got = {leg.get('assetId'): leg.get('rulesetId') for leg in ctx.legs}
    assert all(value == ctx.expected_rule_id for value in got.values()), \
        (f'Domain B netting group [{ctx.expected_netting_group_id}]: the legs of trade '
         f'[{ctx.expected_trade_id}] should carry the trade\'s rule '
         f'[{ctx.expected_rule_id}] as rulesetId, but carry {amounts_line(got)}.\n{ctx.trade}')


# --- AMOUNTS (AS INJECTED) ---
# The base amount sits on the base currency leg and the reference amount on the
# reference currency leg, the same pairing step 05b checks on the ledger.
def test_base_currency_amount_in_group(ctx):
    """Business Logic: The trade's base currency leg in the group carries the injected base amount"""
    skip_without_leg(ctx)
    if ctx.expected_base_amount is None:
        pytest.skip('baseCurrencyAmount is absent from state; nothing to compare against.')
    currency = ctx.expected_base_currency
    group_amount = ctx.leg_amounts.get(currency)
    assert same_amount(group_amount, ctx.expected_base_amount), \
        (f'Domain B netting group [{ctx.expected_netting_group_id}]: the {currency} leg '
         f'of trade [{ctx.expected_trade_id}] should carry the injected base amount '
         f'[{ctx.expected_base_amount}] {currency}, but the trade\'s legs are '
         f'{amounts_line(ctx.leg_amounts)}.\n{ctx.trade}')


def test_reference_currency_amount_in_group(ctx):
    """Business Logic: The trade's reference currency leg in the group carries the injected reference amount"""
    skip_without_leg(ctx)
    if ctx.expected_reference_amount is None:
        pytest.skip('referenceCurrencyAmount is absent from state; nothing to compare against.')
    currency = ctx.expected_reference_currency
    group_amount = ctx.leg_amounts.get(currency)
    assert same_amount(group_amount, ctx.expected_reference_amount), \
        (f'Domain B netting group [{ctx.expected_netting_group_id}]: the {currency} leg '
         f'of trade [{ctx.expected_trade_id}] should carry the injected reference amount '
         f'[{ctx.expected_reference_amount}] {currency}, but the trade\'s legs are '
         f'{amounts_line(ctx.leg_amounts)}.\n{ctx.trade}')


# --- DIRECTIONS (DOMAIN B: FLIPPED TO THE COUNTERPARTY'S SIDE) ---
# The counterparty receives the currency the party sells and pays the one the
# party buys, so each leg's direction is the reverse of step 07's on Domain A.
def test_leg_directions_are_counterparty_side(ctx):
    """Business Logic: The trade's legs are RECEIVABLE / PAYABLE as the counterparty buys / sells each currency"""
    skip_without_leg(ctx)
    if not ctx.expected_sides:
        pytest.skip(f'No sides to expect of [{ctx.expected_counter_party}]: state does not name '
                    'it as the base currency buyer or seller.')
    got = {currency: ctx.leg_sides.get(currency) for currency in ctx.expected_sides}
    assert got == ctx.expected_sides, \
        (f'Domain B netting group [{ctx.expected_netting_group_id}]: the legs of trade '
         f'[{ctx.expected_trade_id}] should be {amounts_line(ctx.expected_sides)} from the '
         f'counterparty [{ctx.expected_counter_party}]\'s side - the reverse of Domain A - '
         f'but are {amounts_line(got)}.\n{ctx.trade}')


# --- OPEN NET VALIDATIONS ---
def test_net_is_not_dissolved(ctx):
    """Business Logic: Underlying details confirm the net is active (not dissolved)"""
    skip_without_leg(ctx)
    details = ctx.matched_underlying_trade.get('underlyingDetails')
    assert details is not None, \
        f'{ctx.leg}: underlyingDetails should be present, but the leg has none.\n{ctx.trade}'
    assert details.get('dissolved') is False, \
        (f'{ctx.leg}: underlyingDetails.dissolved should be false, as the net should '
         f'remain open, but is [{details.get("dissolved")!r}].\n{ctx.trade}')
