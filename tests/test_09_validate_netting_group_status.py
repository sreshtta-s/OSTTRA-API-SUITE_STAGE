# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 09: check the Domain A netting group's details after the close.

Polls the details of the group step 08 closed (nettingGroupIdString) on the
injected trade's value date, and checks the group is no longer eligible to
close. The group's commonGroupId is saved to run state as settlementId, which
steps 10, 13, 13c, 14 and 15 act on, and its status as nettingStatusA for the
Excel row. Step 09b does the same for Domain B.

Steps 10, 11 and 11b then match the group's proposals, unless its netting
rule's mode is auto (data/trade_scenarios.py), and step 12 checks the group is
CONFIRMED.
"""
import types
from datetime import datetime, timezone

import pytest

from api.payloads.netting_payload import get_netting_group_details_payload
from api.services.base_service import is_ok
from api.services.netting_service import NettingService
from config.steps import PENDING_AFFIRMATION
from utils.api_logger import log_ids
from utils.describe import netted_line
from utils.wait import GIVE_UP, wait_until
from utils.state_helper import StateHelper

# How long the group gets to show the close took effect, and how often to check.
# Polled rather than slept; override with WAIT_TIMEOUT_SECONDS / WAIT_INTERVAL_SECONDS.
STATUS_TIMEOUT_SECONDS = 30
STATUS_POLL_SECONDS = 3
# The close is acknowledged slightly before the status changes, so the first
# check waits a moment. The timeout starts after this pause.
STATUS_INITIAL_DELAY_SECONDS = 1
# The statuses of a closed group: awaiting its proposals' match, or past it.
CLOSED_STATUSES = (PENDING_AFFIRMATION, 'CONFIRMED')


# ============================================================================
# TEST SUITE: VALIDATE NETTING GROUP DETAILS POST-CLOSURE
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Fetch the Domain A netting group's details once for every test in this module."""
    c = types.SimpleNamespace(
        res_status=None, response_body=None, server_error=None, group_details=None,
        value_date=None, returned_ids=None,
    )

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None
    raw_id = state.get('nettingGroupIdString')
    c.target_netting_group_id = raw_id.strip() if raw_id else None

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

    # Stop once the close has taken effect, without waiting for CONFIRMED: a group
    # under a manual rule stays PENDING_AFFIRMATION until steps 10, 11 and 11b,
    # and step 12 waits for CONFIRMED after them. A group in any other status is
    # still kept, so a timeout reports what it saw.
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

        return c.group_details if (c.group_details or {}).get('status') in CLOSED_STATUSES else None

    wait_until(fetch_group_details,
               f'Netting Group [{c.target_netting_group_id}] to reach '
               f'{" or ".join(CLOSED_STATUSES)} on Domain A',
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
                # Shown in the Excel row's Netting Grp State (A) column until step
                # 12 records the confirmed status.
                'nettingStatusA': c.group_details.get('status'),
            })
            log_ids('STEP 09 settlement id (A)',
                    settlementId=c.group_details.get('commonGroupId'),
                    nettingGroupId=c.group_details.get('nettingGroupId'),
                    status=c.group_details.get('status'))
            print(f'INFO: Extracted commonGroupId [{c.group_details.get("commonGroupId")}] '
                  'and saved as settlementId to state.')
        else:
            print('WARN: Could not find Netting Group details in the response.')

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


def test_is_eligible_to_close_is_false(ctx):
    """Business Logic: isEligibleToClose is accurately set to false"""
    if not ctx.group_details:
        pytest.skip(f'Details of {ctx.group} are unavailable.')
    eligible = ctx.group_details.get('isEligibleToClose')
    assert eligible is False, \
        (f'{ctx.group}: isEligibleToClose should be false once step 08 has '
         f'closed it, but is [{eligible!r}].\n{ctx.trade}')


def test_settlement_id_present(ctx):
    """State Persistence: settlementId (commonGroupId) exists and is present in response"""
    if not ctx.group_details:
        pytest.skip(f'Details of {ctx.group} are unavailable.')
    assert 'commonGroupId' in ctx.group_details, \
        (f'{ctx.group}: the details carry no commonGroupId, so there is no '
         f'settlementId for steps 10 to 15.\n{ctx.trade}')
    assert ctx.group_details['commonGroupId'] is not None, \
        (f'{ctx.group}: commonGroupId, the settlementId for steps 10 to 15, '
         f'should not be empty, but is null.\n{ctx.trade}')
