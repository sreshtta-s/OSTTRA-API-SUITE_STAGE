# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 08b: close the Domain B netting group.

The Domain B counterpart of step 08. Force-closes the group recorded as
nettingGroupIdStringB (step 06b, or step 07b if it found the trade in another
group) on the injected trade's value date, which step 04 recorded, using the
timeOfUpdate step 08 saved so both domains close with the same time. Writes
nothing to run state.
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
# TEST SUITE: CLOSE NETTING GROUP (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Close the Domain B netting group once for every test in this module."""
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None,
                              value_date=None, time_of_update=None)

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('tokenB') is not None

    # Domain B uses its own group id (from step 06b). There is no fallback to
    # Domain A's, which belongs to a different group. Stripped because a stored
    # id can carry a stray space.
    raw_id = state.get('nettingGroupIdStringB')
    c.target_netting_group_id = raw_id.strip() if raw_id else None

    # Names what the group nets in every assertion message.
    c.trade = netted_line(state)

    if not (state.get('tokenB') and c.target_netting_group_id):
        print('WARN: Skipping API call. Either tokenB or nettingGroupIdStringB '
              '(from step 06b/07b) is missing from state.')
        return c

    # valueDate comes from step 04's injected trade and timeOfUpdate from step
    # 08's close. The current time is a fallback only, if either is missing.
    now = datetime.now(timezone.utc)
    value_date = state.get('valueDate') or now.strftime('%Y-%m-%d')
    time_of_update = state.get('timeOfUpdate')
    if not time_of_update:
        # Step 08 never reached its close call, so there is no Domain A time to match.
        time_of_update = now.strftime('%Y-%m-%d %H:%M:%S')
        print('WARN: No timeOfUpdate in state - step 08 did not close Domain A. '
              f'Closing Domain B with [{time_of_update}] instead.')
    c.value_date, c.time_of_update = value_date, time_of_update

    print(f'INFO: Attempting to close Netting Group [{c.target_netting_group_id}] via Domain B...')
    print(f'INFO: Value Date: [{value_date}], Time of Update: [{time_of_update}]')

    netting_service_b = NettingService(session, domain='B', token=state['tokenB'])
    payload = get_close_netting_group_payload(c.target_netting_group_id, value_date, time_of_update)

    res = netting_service_b.close_netting_group(payload)

    c.res_status = res.status_code
    print(f'INFO: Domain B Close Netting Group API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint can answer with plain text (e.g. SUCCESS) instead of JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Successfully triggered close for Netting Group '
              f'[{c.target_netting_group_id}] via Domain B. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_close_returns_2xx can show the server's rejection message.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'ERROR: Domain B API Request Failed with status: {c.res_status}')
        print(f'Server Rejection Reason: {error_text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_domain_b_auth_token_present(ctx):
    """Pre-flight: Domain B Auth token (tokenB) is present in state"""
    assert ctx.token_present, \
        ('Domain B token is missing: .state/default.json has no "tokenB". '
         f'Did step 01 run?\n{ctx.trade}')


def test_netting_group_id_present(ctx):
    """Pre-flight: Netting Group ID is present in state"""
    assert ctx.target_netting_group_id is not None, \
        ('nettingGroupIdStringB is missing from state, so there is no Domain B netting '
         f'group to close. Did steps 06b / 07b find one?\n{ctx.trade}')


def test_close_returns_2xx(ctx):
    """API: Close Netting Group endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only, as in step 08. Domain B closes its own group, so a "not eligible"
    # rejection is a real failure, not a sign that Domain A's close got there first.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Closing Domain B netting group [{ctx.target_netting_group_id}] (value date '
         f'[{ctx.value_date}], time of update [{ctx.time_of_update}]) should return a 2xx '
         f'status, but returned {ctx.res_status}. Reason: {message}\n{ctx.trade}')
