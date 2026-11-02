# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 13cs_a: cancel the Domain A settlement.

Cancels the settlement id that step 09 saved (Domain A's group commonGroupId,
settlementId) with Domain A's token. The id goes in the path
(.../settlement/<settlementId>/cancel) and the body is empty. Step 13cs_b does
the same for Domain B.

runner.py runs it straight after step 13c (SETTLEMENT_CANCEL_STEPS), but only
for a scenario that sets "cancelSettlement": true: on a trade entry for that
trade's own settlement, or at the top of a netting row for its group's. It is
in neither config/steps.py nor STEP_TOKENS. It can also be run directly with
pytest.
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
# TEST SUITE: STEP 13CS_A - CANCEL SETTLEMENT (DOMAIN A)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Cancel the Domain A settlement once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips while the pre-flight tests report what is missing.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None)

    state = StateHelper.get_state()
    # Store only whether the token exists: pytest prints this namespace in
    # failure messages, which end up in the HTML report.
    c.token_present = state.get('token') is not None
    c.target_settlement_id = state.get('settlementId')

    # Names the settlement's netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdString')
    c.trade = netted_line(state)

    if not (state.get('token') and c.target_settlement_id):
        print('WARN: Skipping API call. Token or Settlement ID is missing from state.')
        return c

    print(f'INFO: Attempting to cancel settlement [{c.target_settlement_id}] via Domain A...')

    settlement_service = SettlementService(session, domain='A', token=state['token'])
    payload = get_cancel_settlement_payload()

    res = settlement_service.cancel_settlement(c.target_settlement_id, payload)
    c.res_status = res.status_code
    print(f'INFO: Domain A Cancel Settlement API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint may acknowledge with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Settlement Cancelled Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_cancel_returns_2xx can quote the server's rejection.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'ERROR: API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {error_text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_auth_token_present(ctx):
    """Pre-flight: Auth token is present in state"""
    assert ctx.token_present, \
        ('Domain A token is missing: .state/default.json has no "token". '
         f'Did step 01 run?\n{ctx.trade}')


def test_settlement_id_present(ctx):
    """Pre-flight: Settlement ID is present in state"""
    assert ctx.target_settlement_id is not None, \
        ('settlementId is missing from state, so there is no Domain A settlement to '
         f'cancel. Did step 09 find netting group [{ctx.netting_group_id}]?\n{ctx.trade}')


def test_cancel_returns_2xx(ctx):
    """API: Cancel Settlement endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only: an "already cancelled" 400 means this run did not cancel the
    # settlement itself, so it must not pass.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Cancelling settlement [{ctx.target_settlement_id}] as Domain A (netting group '
         f'[{ctx.netting_group_id}]) should return a 2xx status, but returned '
         f'{ctx.res_status}. Reason: {message}\n{ctx.trade}')
