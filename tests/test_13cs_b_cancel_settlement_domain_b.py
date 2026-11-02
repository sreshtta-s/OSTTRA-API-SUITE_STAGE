# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 13cs_b: cancel the Domain B settlement.

Cancels the settlement id that step 09b saved (Domain B's group commonGroupId,
settlementIdB) with Domain B's token. The id goes in the path
(.../settlement/<settlementIdB>/cancel) and the body is empty. Step 13cs_a does
the same for Domain A.

runner.py runs it straight after step 13cs_a (SETTLEMENT_CANCEL_STEPS), but
only for a scenario that sets "cancelSettlement": true. It is in neither
config/steps.py nor STEP_TOKENS. It can also be run directly with pytest.
"""
import json
import types

import pytest

from api.payloads.settlement_payload import get_cancel_settlement_payload
from api.services.base_service import is_ok
from api.services.settlement_service import SettlementService
from utils.describe import netted_line
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: STEP 13CS_B - CANCEL SETTLEMENT (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Cancel the Domain B settlement once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips while the pre-flight tests report what is missing.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None)

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('tokenB') is not None
    # Domain B uses its own settlement id (from step 09b). There is no fallback
    # to Domain A's, which belongs to a different group.
    c.target_settlement_id = state.get('settlementIdB')

    # Names the settlement's netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdStringB')
    c.trade = netted_line(state)

    if not (state.get('tokenB') and c.target_settlement_id):
        print('WARN: Skipping API call. tokenB or settlementIdB is missing from state.')
        return c

    print(f'INFO: Attempting to cancel settlement [{c.target_settlement_id}] via Domain B...')

    settlement_service_b = SettlementService(session, domain='B', token=state['tokenB'])
    payload = get_cancel_settlement_payload()

    res = settlement_service_b.cancel_settlement(c.target_settlement_id, payload)
    c.res_status = res.status_code
    print(f'INFO: Domain B Cancel Settlement API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint may acknowledge with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Domain B Settlement Cancelled Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_cancel_returns_2xx can quote the server's rejection message.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'ERROR: Domain B API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {error_text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_domain_b_auth_token_present(ctx):
    """Pre-flight: Domain B Auth token (tokenB) is present in state"""
    assert ctx.token_present, \
        ('Domain B token is missing: .state/default.json has no "tokenB". '
         f'Did step 01 run?\n{ctx.trade}')


def test_settlement_id_present(ctx):
    """Pre-flight: Settlement ID (settlementIdB) is present in state"""
    assert ctx.target_settlement_id is not None, \
        ('settlementIdB is missing from state, so there is no Domain B settlement to '
         f'cancel. Did step 09b find netting group [{ctx.netting_group_id}]?\n{ctx.trade}')


def test_cancel_returns_2xx(ctx):
    """API: Cancel Settlement endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only: an "already cancelled" 400 means this run did not cancel the
    # settlement itself, so it must not pass.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Cancelling settlement [{ctx.target_settlement_id}] as Domain B (netting group '
         f'[{ctx.netting_group_id}]) should return a 2xx status, but returned '
         f'{ctx.res_status}. Reason: {message}\n{ctx.trade}')
