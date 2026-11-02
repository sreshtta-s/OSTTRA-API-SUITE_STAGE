# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 09b: check the Domain B netting group's details after the close.

The Domain B counterpart of step 09. Polls the details of the group step 08b
closed (nettingGroupIdStringB) on the injected trade's value date, and checks
the group is no longer eligible to close. The group's commonGroupId is saved to
run state as settlementIdB, which step 13b acts on, and its status as
nettingStatusB for the Excel row. Step 12b checks the group is CONFIRMED.
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
# TEST SUITE: VALIDATE NETTING GROUP DETAILS POST-CLOSURE (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Fetch the Domain B netting group's details once for every test in this module."""
    c = types.SimpleNamespace(res_status=None, response_body=None, group_details=None, server_error=None,
                              value_date=None, returned_ids=None,
    )

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('tokenB') is not None
    # Domain B uses its own group id (from step 06b). There is no fallback to
    # Domain A's, which belongs to a different group.
    c.target_netting_group_id = state.get('nettingGroupIdStringB')

    # Names what the group nets in every assertion message. c.group, the group
    # each check was made on, gains its settlement id once the details arrive.
    c.trade = netted_line(state)
    c.group = f'Domain B netting group [{c.target_netting_group_id}]'

    if not (state.get('tokenB') and c.target_netting_group_id):
        print('WARN: Skipping Domain B API call. tokenB or nettingGroupIdStringB '
              'is missing from state.')
        return c

    # The group is filed under the trade's value date, which step 04 records from
    # the injected payload. Today (UTC) is used only if state has none.
    target_date = state.get('valueDate') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    c.value_date = target_date

    netting_service_b = NettingService(session, domain='B', token=state['tokenB'])

    payload = get_netting_group_details_payload(c.target_netting_group_id, target_date)
    print(f'INFO: Fetching details for Domain B Netting Group [{c.target_netting_group_id}] '
          f'for date [{target_date}]...')

    # Stop once the close has taken effect. Unlike step 09 this does not wait for
    # CONFIRMED: a group awaiting its proposals' match stays PENDING_AFFIRMATION
    # until steps 10, 11 and 11b, and step 12b waits for CONFIRMED after them. A
    # group in any other status is still kept, so a timeout reports what it saw.
    def fetch_group_details():
        res = netting_service_b.get_netting_group_details(payload)
        c.res_status = res.status_code

        if not is_ok(res):
            c.server_error = res.text
            return GIVE_UP

        c.response_body = res.json()

        # The response is either {items: [...]} or the group itself. Only the group
        # asked for is accepted, so another group's commonGroupId never reaches
        # step 13b as settlementIdB.
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
               f'{" or ".join(CLOSED_STATUSES)} on Domain B',
               timeout=STATUS_TIMEOUT_SECONDS, interval=STATUS_POLL_SECONDS,
               initial_delay=STATUS_INITIAL_DELAY_SECONDS)

    print(f'INFO: Domain B Netting Group Details API responded with status: {c.res_status}')

    if c.server_error is not None:
        print(f'ERROR: Domain B API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {c.server_error}')
    else:
        if not c.group_details and isinstance(c.response_body, dict) \
                and isinstance(c.response_body.get('items'), list):
            returned_ids = ', '.join(
                str(item.get('nettingGroupId')) for item in c.response_body['items']
            ) or 'none'
            c.returned_ids = returned_ids
            print(f'ERROR: Domain B Netting Group [{c.target_netting_group_id}] not present in '
                  f'the response. Groups returned: [{returned_ids}]')

        if c.group_details:
            c.group = (f'Domain B netting group [{c.target_netting_group_id}] (settlement id '
                       f'[{c.group_details.get("commonGroupId")}])')
            print(f'INFO: Successfully retrieved Domain B details. '
                  f'Status is [{c.group_details.get("status")}]')

            # Domain B keys only, so Domain A's settlementId is never overwritten.
            StateHelper.save_state({
                'settlementIdB': c.group_details.get('commonGroupId'),
                'nettingGroupIdStringB': c.group_details.get('nettingGroupId'),
                # Shown in the Excel row's Netting Grp State (B) column until step
                # 12b records the confirmed status.
                'nettingStatusB': c.group_details.get('status'),
            })
            log_ids('STEP 09b settlement id (B)',
                    settlementId=c.group_details.get('commonGroupId'),
                    nettingGroupId=c.group_details.get('nettingGroupId'),
                    status=c.group_details.get('status'))
            print(f'INFO: Extracted commonGroupId [{c.group_details.get("commonGroupId")}] '
                  'and saved as settlementIdB to state.')
        else:
            print('WARN: Could not find Domain B Netting Group details in the response.')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_preflight(ctx):
    """Pre-flight: Domain B auth token and Netting Group ID are present"""
    assert ctx.token_present, \
        ('Domain B token is missing: .state/default.json has no "tokenB". '
         f'Did step 01 run?\n{ctx.trade}')
    assert ctx.target_netting_group_id is not None, \
        ('nettingGroupIdStringB is missing from state, so there is no Domain B netting '
         f'group to check. Did step 08b close one?\n{ctx.trade}')


def test_endpoint_returns_200(ctx):
    """API: Endpoint returns a 200 Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed.')
    assert ctx.res_status == 200, \
        (f'Domain B netting group details for [{ctx.target_netting_group_id}] (value '
         f'date [{ctx.value_date}]) should return 200, but returned {ctx.res_status}. '
         f'Reason: {ctx.server_error or "none given"}\n{ctx.trade}')


def test_netting_group_in_response(ctx):
    """Data: Netting Group exists in Domain B ledger response"""
    if not ctx.group_details:
        pytest.skip(f'Details of {ctx.group} are unavailable.')
    assert ctx.group_details.get('nettingGroupId') == ctx.target_netting_group_id, \
        (f'The Domain B details name netting group '
         f'[{ctx.group_details.get("nettingGroupId")}], not the requested '
         f'[{ctx.target_netting_group_id}].\n{ctx.trade}')


def test_is_eligible_to_close_is_false(ctx):
    """Business Logic: isEligibleToClose is accurately set to false"""
    if not ctx.group_details:
        pytest.skip(f'Details of {ctx.group} are unavailable.')
    eligible = ctx.group_details.get('isEligibleToClose')
    assert eligible is False, \
        (f'{ctx.group}: isEligibleToClose should be false once step 08b has '
         f'closed it, but is [{eligible!r}].\n{ctx.trade}')


def test_settlement_id_b_present(ctx):
    """State Persistence: settlementIdB (commonGroupId) exists and is present in response"""
    if not ctx.group_details:
        pytest.skip(f'Details of {ctx.group} are unavailable.')
    assert 'commonGroupId' in ctx.group_details, \
        (f'{ctx.group}: the details carry no commonGroupId, so there is no '
         f'settlementIdB for step 13b.\n{ctx.trade}')
    assert ctx.group_details['commonGroupId'] is not None, \
        (f'{ctx.group}: commonGroupId, the settlementIdB for step 13b, '
         f'should not be empty, but is null.\n{ctx.trade}')
