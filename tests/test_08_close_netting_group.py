# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 08: close the Domain A netting group.

Force-closes the group recorded as nettingGroupIdString (step 06, or step 07 if
it found the trade in another group) on the injected trade's value date, which
step 04 recorded. The close timestamp is saved to run state as timeOfUpdate
before the call, so step 08b closes Domain B's group with the same time even
if Domain A's close is rejected.
"""
import json
import types
from datetime import datetime, timezone

import pytest

from api.payloads.netting_payload import get_close_netting_group_payload
from api.services.base_service import is_ok
from api.services.netting_service import NettingService
from utils.describe import netted_line
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: CLOSE NETTING GROUP (TIME/CYCLE EDIT)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Close the Domain A netting group once for every test in this module."""
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None,
                              value_date=None, time_of_update=None)

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None
    c.target_netting_group_id = state.get('nettingGroupIdString')

    # Names what the group nets in every assertion message.
    c.trade = netted_line(state)

    if not (state.get('token') and c.target_netting_group_id):
        print('WARN: Skipping API call. Either Token or Netting Group ID is missing from state.')
        return c

    now = datetime.now(timezone.utc)
    # The group is keyed by the trade's value date (T+n for a Forward), so the
    # close uses the date step 04 recorded, not today.
    value_date = state.get('valueDate')
    if not value_date:
        # Fallback only: step 04 did not record the injected trade's value date.
        value_date = now.strftime('%Y-%m-%d')
        print('WARN: No valueDate in state - step 04 did not record the injected '
              f'trade. Closing on today [{value_date}] instead.')
    time_of_update = now.strftime('%Y-%m-%d %H:%M:%S')
    c.value_date, c.time_of_update = value_date, time_of_update

    # Step 08b closes Domain B's group with this same timestamp, whatever
    # Domain A's close returns. valueDate is not written here, so later lookups
    # keep the date the trade carries.
    StateHelper.save_state({
        'timeOfUpdate': time_of_update,
    })

    print(f'INFO: Attempting to close Netting Group [{c.target_netting_group_id}]...')
    print(f'INFO: Value Date: [{value_date}], Time of Update: [{time_of_update}]')

    netting_service = NettingService(session, domain='A', token=state['token'])
    payload = get_close_netting_group_payload(c.target_netting_group_id, value_date, time_of_update)

    res = netting_service.close_netting_group(payload)

    c.res_status = res.status_code
    print(f'INFO: Domain A Close Netting Group API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint can answer with plain text (e.g. SUCCESS) instead of JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Successfully triggered close for Netting Group '
              f'[{c.target_netting_group_id}]. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_close_returns_2xx can quote the server's rejection message.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'ERROR: API Request Failed with status: {c.res_status}')
        print(f'Server Rejection Reason: {error_text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_auth_token_present(ctx):
    """Pre-flight: Auth token is present in state"""
    assert ctx.token_present, \
        ('Domain A token is missing: .state/default.json has no "token". '
         f'Did step 01 run?\n{ctx.trade}')


def test_netting_group_id_present(ctx):
    """Pre-flight: Netting Group ID is present in state"""
    assert ctx.target_netting_group_id is not None, \
        ('nettingGroupIdString is missing from state, so there is no Domain A netting '
         f'group to close. Did steps 06 / 07 find one?\n{ctx.trade}')


def test_close_returns_2xx(ctx):
    """API: Close Netting Group endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Closing Domain A netting group [{ctx.target_netting_group_id}] (value date '
         f'[{ctx.value_date}], time of update [{ctx.time_of_update}]) should return a 2xx '
         f'status, but returned {ctx.res_status}. Reason: {message}\n{ctx.trade}')
