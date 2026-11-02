# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 13c: initiate settlement as the Global operator.

Initiates the settlement id that step 09 saved (Domain A's group commonGroupId)
with the operator's token, token-global, and the fixed trading party OPERATOR.
Steps 13 and 13b do the same for Domain A and Domain B; step 14 then funds the
settlement.
"""
import json
import types

import pytest

from api.payloads.settlement_payload import get_initiate_settlement_payload
from api.services.base_service import is_ok
from api.services.settlement_service import SettlementService
from utils.describe import netted_line
from utils.state_helper import StateHelper

TRADING_PARTY = 'OPERATOR'


# ============================================================================
# TEST SUITE: STEP 13C - INITIATE SETTLEMENT (GLOBAL)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Initiate settlement as the Global operator once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips while the pre-flight tests report what is missing.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None)

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    global_token = state.get('token-global')
    c.token_present = global_token is not None
    c.target_settlement_id = state.get('settlementId')
    # The operator acts on its own behalf, not as either party to the trade, so
    # the party is a constant rather than an id read from state.
    c.trading_party = TRADING_PARTY

    # Names the settlement's netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdString')
    c.trade = netted_line(state)

    if not (global_token and c.target_settlement_id and c.trading_party):
        print('WARN: Skipping API call. token-global, Settlement ID, or Trading '
              'Party is missing from state.')
        return c

    print(f'INFO: Attempting to initiate settlement [{c.target_settlement_id}] '
          f'for party [{c.trading_party}] via Global Operator...')

    settlement_service_global = SettlementService(session, domain='GLOBAL', token=global_token)
    payload = get_initiate_settlement_payload(c.target_settlement_id, c.trading_party)

    res = settlement_service_global.initiate_settlement(payload)
    c.res_status = res.status_code
    print(f'INFO: Global Operator Initiate Settlement API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint may acknowledge with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Global Settlement Initiated Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_initiate_returns_2xx can quote the server's rejection message.
            c.response_body = json.loads(error_text)
        except json.JSONDecodeError:
            c.response_body = {'message': error_text}
        print(f'ERROR: Global API Request Failed with status: {c.res_status}')
        print(f'SERVER REJECTION REASON: {error_text}')

    return c


# ============================================================================
# INDIVIDUAL TEST CASES
# ============================================================================

def test_global_auth_token_present(ctx):
    """Pre-flight: Global Auth token (token-global) is present in state"""
    assert ctx.token_present, \
        ('Global operator token is missing: .state/default.json has no "token-global". '
         f'Did step 01 run?\n{ctx.trade}')


def test_settlement_id_and_party_present(ctx):
    """Pre-flight: Settlement ID and Trading Party are present in state"""
    assert ctx.target_settlement_id is not None, \
        ('settlementId is missing from state, so there is no settlement for the Global '
         f'operator to initiate. Did step 09 find netting group '
         f'[{ctx.netting_group_id}]?\n{ctx.trade}')
    assert ctx.trading_party is not None, \
        (f'The Global operator\'s trading party (OPERATOR) is not set, so settlement '
         f'[{ctx.target_settlement_id}] cannot be initiated.\n{ctx.trade}')


def test_initiate_returns_2xx(ctx):
    """API: Initiate Settlement endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only: an "already initiated" 400 means this run did not initiate the
    # settlement itself, so it must not pass.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Initiating settlement [{ctx.target_settlement_id}] as the Global operator for party '
         f'[{ctx.trading_party}] (netting group [{ctx.netting_group_id}]) should return '
         f'a 2xx status, but returned {ctx.res_status}. Reason: {message}\n{ctx.trade}')
