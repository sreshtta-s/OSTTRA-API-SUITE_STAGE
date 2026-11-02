# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 12: check the Domain A netting group is CONFIRMED.

Runs after steps 10, 11 and 11b have matched the group's proposals, or straight
after step 09b when the netting rule's mode is auto and they are skipped. Polls
the details of the group step 08 closed (nettingGroupIdString) on the injected
trade's value date until the ledger reports it CONFIRMED, and saves its status
to run state as nettingStatusA for the Excel row. The confirmed group's
settlement candidates, as the netting screen lists them, are then checked for
the injected currencies and amounts, signed by direction from the party's
side, plus those of any other trades step 07 found in the group (TC010 to
TC013: the whole group's net step 07c read, its stored sum included). Step 12b
does the same for Domain B.
"""
import types
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from api.payloads.netting_payload import (
    get_netting_group_details_payload,
    get_settlement_candidate_payload,
)
from api.payloads.trade_payload import scenario_netting_group
from api.services.base_service import is_ok
from api.services.netting_service import NettingService
from utils.api_logger import log_ids
from utils.describe import amounts_line, netted_line
from utils.flag import resolve_scenario_id
from utils.wait import GIVE_UP, wait_until
from utils.state_helper import StateHelper
from utils.sum_storer import load_sum, sum_line
from utils.trade_direction import base_currency, booked_net, leg_amount, net_line

# How long the group gets to reach CONFIRMED, and how often to check.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
STATUS_TIMEOUT_SECONDS = 30
STATUS_POLL_SECONDS = 3
# The match is acknowledged slightly before the status changes, so the first
# check waits a moment. The timeout starts after this pause.
STATUS_INITIAL_DELAY_SECONDS = 1
# How long the confirmed group's settlement candidates get to appear, and how
# often to check.
CANDIDATE_TIMEOUT_SECONDS = 30
CANDIDATE_POLL_SECONDS = 3



# ============================================================================
# TEST SUITE: VALIDATE NETTING GROUP CONFIRMED
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Fetch the Domain A netting group's details once for every test in this module."""
    c = types.SimpleNamespace(
        res_status=None, response_body=None, server_error=None, group_details=None,
        candidate_status=None, candidate_error=None, candidate_amounts={},
        candidate_net={}, unreadable_candidates=[], expected_net=None, own=None,
        value_date=None, returned_ids=None, no_net_reason=None,
    )

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None
    raw_id = state.get('nettingGroupIdString')
    c.target_netting_group_id = raw_id.strip() if raw_id else None

    # What the group's candidates should net to, per currency, from this
    # domain's own trading party's side: received positive, paid negative. A row
    # that checks several trades as one netting group (TC010 to TC013) expects
    # the whole group's net, which step 07c read from every leg of the group
    # steps 07 / 07b found and saved, since its settlement covers everything in
    # it. 07c checks that net against the sum utils/sum_storer.py stored for the
    # row's own trades, or, for a row that states groupAmounts (TC013), against
    # those, other trades in the group included. Every other run expects
    # the one trade step 04 injected plus the legs of any other trades step 07
    # found in its group, for the same reason. Either way each amount is keyed
    # by the currency the payload pairs it with, never by dealtCurrency, which
    # can name either. Step 12b expects the exact reverse on Domain B.
    scenario_id = resolve_scenario_id()
    group = scenario_netting_group(scenario_id) if scenario_id else None
    if group:
        total = group['nettingTotal']
        stored = load_sum(group['tradesOf'])
        c.own = total['party']
        group_net = state.get('nettingGroupNet')
        c.expected_net = ({ccy: Decimal(value) for ccy, value in group_net.items()}
                          if isinstance(group_net, dict) else None)
        c.expected_source = (f'whole netting group as step 07c read it, checked there against the '
                             f'{scenario_id} stored sum of {total["tradeCount"]} '
                             f'{group["tradesOf"]} trades ({sum_line(stored)})')
        c.expected_base_currency = total['baseCurrency']
        c.expected_reference_currency = total['referenceCurrency']
        c.no_net_reason = (f'step 07c recorded no net for the whole group to check the candidates '
                           f'against ({c.expected_source}). Did steps 07 / 07b find the group, '
                           'and could 07c read every leg in it?')
    else:
        # The injected trade's own net comes from what step 04 injected, not
        # from the group's legs, so a ledger that changed its amounts fails
        # here too. Another run's or case's trade with the same parties, pair,
        # product and value date may share the group, which settles it as well,
        # so the net of the other legs step 07 listed is added; it is {} when
        # the trade is alone in the group, and None when step 07 could not read
        # the group, leaving the group's net unknown.
        c.own = state.get('reqPartyId')
        trade_net = booked_net([state], c.own)
        others_net = state.get('nettingGroupOthersNet')
        others_net = ({ccy: Decimal(value) for ccy, value in others_net.items()}
                      if isinstance(others_net, dict) else None)
        others_legs = state.get('nettingGroupOthersLegs') or []
        if trade_net is None:
            c.expected_net = None
            c.no_net_reason = (f'state does not name [{c.own}] as the base currency buyer or '
                               'seller, so which way each currency should move is unknown. '
                               'Did step 04 run?')
        elif others_net is None:
            c.expected_net = None
            c.no_net_reason = ('step 07 recorded no net for the other trades in the group, so '
                               'what the group settles is unknown. Did step 07 find the trade, '
                               'and could every leg in its group be read?')
        else:
            c.expected_net = {ccy: trade_net.get(ccy, Decimal(0)) + others_net.get(ccy, Decimal(0))
                              for ccy in set(trade_net) | set(others_net)}
        c.expected_source = ('injected trade'
                             + (f' plus {len(others_legs)} leg(s) of other trades step 07 found '
                                f'in the group ({", ".join(others_legs)}: '
                                f'{net_line(c.own, others_net)})' if others_legs else ''))
        c.expected_base_currency = base_currency(state)
        c.expected_reference_currency = state.get('referenceCurrency')

    # Names what the group nets in every assertion message. c.group, the group
    # each check was made on, gains its settlement id once the details arrive.
    c.trade = netted_line(state)
    c.group = f'Domain A netting group [{c.target_netting_group_id}]'

    if not (state.get('token') and c.target_netting_group_id):
        print('WARN: Skipping API call. Token or Netting Group ID is missing from state.')
        return c

    # The group is filed under the trade's value date, which step 04 records from
    # the injected payload. Today (UTC) is used only if state has none.
    target_date = state.get('valueDate') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    c.value_date = target_date

    netting_service = NettingService(session, domain='A', token=state['token'])

    payload = get_netting_group_details_payload(c.target_netting_group_id, target_date)
    print(f'INFO: Fetching details for Netting Group [{c.target_netting_group_id}] '
          f'for date [{target_date}]...')

    # Stop at CONFIRMED, not at the first read that finds the group: it can read
    # PENDING_AFFIRMATION for a few seconds after its proposals are matched. A
    # group in any other status is still kept, so a timeout reports what it saw.
    def fetch_group_details():
        res = netting_service.get_netting_group_details(payload)
        c.res_status = res.status_code

        if not is_ok(res):
            c.server_error = res.text
            return GIVE_UP

        c.response_body = res.json()

        # The response is either {items: [...]} or the group itself. Only the group
        # asked for is accepted, so another group's commonGroupId never reaches
        # the settlement steps as settlementId.
        if isinstance(c.response_body, dict) and isinstance(c.response_body.get('items'), list):
            c.group_details = next(
                (item for item in c.response_body['items']
                 if item.get('nettingGroupId') == c.target_netting_group_id),
                None,
            )
        elif isinstance(c.response_body, dict) \
                and c.response_body.get('nettingGroupId') == c.target_netting_group_id:
            c.group_details = c.response_body

        return c.group_details if (c.group_details or {}).get('status') == 'CONFIRMED' else None

    wait_until(fetch_group_details,
               f'Netting Group [{c.target_netting_group_id}] to reach CONFIRMED on Domain A',
               timeout=STATUS_TIMEOUT_SECONDS, interval=STATUS_POLL_SECONDS,
               initial_delay=STATUS_INITIAL_DELAY_SECONDS)

    print(f'INFO: Domain A Netting Group Details API responded with status: {c.res_status}')

    if c.server_error is not None:
        print(f'ERROR: API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {c.server_error}')
    else:
        if not c.group_details and isinstance(c.response_body, dict) \
                and isinstance(c.response_body.get('items'), list):
            returned_ids = ', '.join(
                str(item.get('nettingGroupId')) for item in c.response_body['items']
            ) or 'none'
            c.returned_ids = returned_ids
            print(f'ERROR: Netting Group [{c.target_netting_group_id}] not present in the '
                  f'response. Groups returned: [{returned_ids}]')

        if c.group_details:
            c.group = (f'Domain A netting group [{c.target_netting_group_id}] (settlement id '
                       f'[{c.group_details.get("commonGroupId")}])')
            print(f'INFO: Successfully retrieved details. Status is [{c.group_details.get("status")}]')

            StateHelper.save_state({
                'settlementId': c.group_details.get('commonGroupId'),
                'nettingGroupIdString': c.group_details.get('nettingGroupId'),
                # Shown in the Excel row's Netting Grp State (A) column, replacing
                # the status step 09 saw.
                'nettingStatusA': c.group_details.get('status'),
            })
            log_ids('STEP 12 netting group confirmed (A)',
                    settlementId=c.group_details.get('commonGroupId'),
                    nettingGroupId=c.group_details.get('nettingGroupId'),
                    status=c.group_details.get('status'))
        else:
            print('WARN: Could not find Netting Group details in the response.')

    # A confirmed group has one settlement candidate per currency: assetId is the
    # currency and assetValue its net amount. They appear shortly after the
    # group is confirmed, so the list is re-read until every expected currency
    # is present. Before CONFIRMED no call is made, so the candidate tests skip.
    if (c.group_details or {}).get('status') == 'CONFIRMED':
        candidate_payload = get_settlement_candidate_payload(c.target_netting_group_id)
        # A currency the group nets to zero may have no candidate, so only the
        # currencies it moves are waited for.
        expected_currencies = ({ccy for ccy, value in c.expected_net.items() if value}
                               if c.expected_net is not None else
                               {ccy for ccy in (c.expected_base_currency,
                                                c.expected_reference_currency) if ccy})

        def fetch_candidates():
            res = netting_service.get_all_settlement_candidates(candidate_payload)
            c.candidate_status = res.status_code

            if not is_ok(res):
                c.candidate_error = res.text
                return GIVE_UP

            # Only this group's candidates are kept, so another group's amounts
            # never pass for this one's.
            items = [item for item in res.json().get('items') or []
                     if item.get('nettingGroupId') == c.target_netting_group_id]
            c.candidate_amounts = {item.get('assetId'): item.get('assetValue') for item in items}
            # Signed from this domain's party's side by each candidate's
            # direction (RECEIVABLE / PAYABLE), and added up per currency, so two
            # candidates in one currency cannot hide one another.
            c.candidate_net, c.unreadable_candidates = {}, []
            for item in items:
                value = leg_amount(item)
                if value is None:
                    c.unreadable_candidates.append(item)
                    continue
                c.candidate_net[item.get('assetId')] = \
                    c.candidate_net.get(item.get('assetId'), Decimal(0)) + value
            ready = c.candidate_amounts and expected_currencies <= c.candidate_amounts.keys()
            return c.candidate_amounts if ready else None

        wait_until(fetch_candidates,
                   f'settlement candidates of Netting Group [{c.target_netting_group_id}] '
                   'on Domain A',
                   timeout=CANDIDATE_TIMEOUT_SECONDS, interval=CANDIDATE_POLL_SECONDS)

        print(f'INFO: Domain A Settlement Candidate API responded with status: '
              f'{c.candidate_status}')
        if c.candidate_error is not None:
            print(f'SERVER REJECTION REASON: {c.candidate_error}')
        print(f'INFO: Netting Group [{c.target_netting_group_id}] candidates: '
              f'{c.candidate_amounts}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_preflight(ctx):
    """Pre-flight: Auth token and Netting Group ID are present"""
    assert ctx.token_present, \
        ('Domain A token is missing: .state/default.json has no "token". '
         f'Did step 01 run?\n{ctx.trade}')
    assert ctx.target_netting_group_id is not None, \
        ('nettingGroupIdString is missing from state, so there is no Domain A netting '
         f'group to check. Did step 08 close one?\n{ctx.trade}')


def test_endpoint_returns_200(ctx):
    """API: Endpoint returns a 200 Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert ctx.res_status == 200, \
        (f'Domain A netting group details for [{ctx.target_netting_group_id}] (value '
         f'date [{ctx.value_date}]) should return 200, but returned {ctx.res_status}. '
         f'Reason: {ctx.server_error or "none given"}\n{ctx.trade}')


def test_netting_group_in_response(ctx):
    """Data: Netting Group exists in ledger response"""
    # Fails rather than skips: a group missing from the response is a real defect.
    if ctx.res_status:
        problem = (f'Domain A netting group [{ctx.target_netting_group_id}] is not in the '
                   f'details response for value date [{ctx.value_date}]. Groups returned: '
                   f'[{ctx.returned_ids or "none"}].')
    else:
        problem = ('Domain A netting group details were not fetched: a pre-flight check '
                   'above failed.')
    assert ctx.group_details is not None, f'{problem}\n{ctx.trade}'
    assert ctx.group_details.get('nettingGroupId') == ctx.target_netting_group_id, \
        (f'The Domain A details name netting group '
         f'[{ctx.group_details.get("nettingGroupId")}], not the requested '
         f'[{ctx.target_netting_group_id}].\n{ctx.trade}')


def test_status_is_confirmed(ctx):
    """Business Logic: Status transitioned to CONFIRMED"""
    # Fails rather than skips when the details are missing: a status that could
    # not be read is not CONFIRMED.
    assert ctx.group_details is not None, \
        (f'{ctx.group}: status should be CONFIRMED, but it could not be read: the '
         f'group\'s details were not returned (see the checks above).\n{ctx.trade}')
    status = ctx.group_details.get('status')
    assert status == 'CONFIRMED', \
        (f'{ctx.group}: status should be CONFIRMED once its proposals are matched, but '
         f'is [{status}].\n{ctx.trade}')


# --- NET AMOUNTS (SETTLEMENT CANDIDATES) ---
# The confirmed group must hold a candidate in each currency it moves, and no
# other, each netting - signed by its direction, from Domain A's party's side -
# to the injected trade's amount plus any other trades' legs step 07 found in
# the group or, for a row that checks several trades as one group, to the whole
# group's net step 07c read. Step 12b expects the exact reverse on Domain B.
def skip_without_candidates(ctx):
    """Skip when the candidate call was not made; fail when it was rejected."""
    if not ctx.candidate_status:
        pytest.skip(f'Settlement candidates of {ctx.group} were not fetched: the group '
                    'did not reach CONFIRMED.')
    assert 200 <= ctx.candidate_status < 300, \
        (f'Settlement candidate listing for {ctx.group} should return a 2xx status, '
         f'but returned {ctx.candidate_status}. Reason: {ctx.candidate_error}\n{ctx.trade}')


def require_expected_net(ctx):
    """Fail when there is no net to expect, naming why."""
    assert ctx.expected_net is not None, f'{ctx.group}: {ctx.no_net_reason}\n{ctx.trade}'


def test_candidate_currencies_match(ctx):
    """Data: The group's settlement candidates are in the expected currencies"""
    skip_without_candidates(ctx)
    require_expected_net(ctx)
    moved = {ccy for ccy, value in ctx.expected_net.items() if value}
    got = set(ctx.candidate_amounts)
    assert moved <= got <= set(ctx.expected_net), \
        (f'{ctx.group}: settlement candidates should be in {", ".join(sorted(moved))} '
         f'(and at most {", ".join(sorted(ctx.expected_net))}), per the {ctx.expected_source}, '
         f'but the group holds {amounts_line(ctx.candidate_amounts)}.\n{ctx.trade}')


def check_candidate(ctx, currency):
    """Assert the group's candidates in `currency` net to the expected amount, signed by direction."""
    skip_without_candidates(ctx)
    require_expected_net(ctx)
    assert not ctx.unreadable_candidates, \
        (f'{ctx.group}: settlement candidate(s) whose amount or direction cannot be read: '
         f'{ctx.unreadable_candidates}.\n{ctx.trade}')
    expected = ctx.expected_net.get(currency, Decimal(0))
    got = ctx.candidate_net.get(currency, Decimal(0))
    assert got == expected, \
        (f'{ctx.group}: on its {currency} settlement candidate, {net_line(ctx.own, {currency: got})}, '
         f'but per the {ctx.expected_source}, {net_line(ctx.own, {currency: expected})}. All '
         f'candidates: {net_line(ctx.own, ctx.candidate_net)}.\n{ctx.trade}')


def test_base_currency_amount(ctx):
    """Business Logic: The base currency candidate nets to the expected amount, on this domain's party's side"""
    check_candidate(ctx, ctx.expected_base_currency)


def test_reference_currency_amount(ctx):
    """Business Logic: The reference currency candidate nets to the expected amount, on this domain's party's side"""
    check_candidate(ctx, ctx.expected_reference_currency)
