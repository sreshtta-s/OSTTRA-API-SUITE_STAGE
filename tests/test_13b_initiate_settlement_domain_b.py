# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 13b: initiate settlement of the Domain B netting group.

Initiates the settlement id that step 09b saved (Domain B's group commonGroupId,
settlementIdB) on behalf of Domain B's party, counterPartyId. Step 13 does the
same for Domain A and step 13c for the Global operator.
"""
import json
import types

import pytest

from api.payloads.settlement_payload import get_initiate_settlement_payload
from api.services.base_service import is_ok
from api.services.settlement_service import SettlementService
from utils.describe import netted_line
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: STEP 13B - INITIATE SETTLEMENT (DOMAIN B)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Initiate the Domain B settlement once for every test in this module.

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
    # Domain B initiates on behalf of the counterparty, its side of the trade.
    c.trading_party = state.get('counterPartyId')

    # Names the settlement's netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdStringB')
    c.trade = netted_line(state)

    if not (state.get('tokenB') and c.target_settlement_id and c.trading_party):
        print('WARN: Skipping API call. tokenB, settlementIdB, or counterPartyId '
              'is missing from state.')
        return c

    print(f'INFO: Attempting to initiate settlement [{c.target_settlement_id}] '
          f'for party [{c.trading_party}] via Domain B...')

    settlement_service_b = SettlementService(session, domain='B', token=state['tokenB'])
    payload = get_initiate_settlement_payload(c.target_settlement_id, c.trading_party)

    res = settlement_service_b.initiate_settlement(payload)
    c.res_status = res.status_code
    print(f'INFO: Domain B Initiate Settlement API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint may acknowledge with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Domain B Settlement Initiated Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_initiate_returns_2xx can quote the server's rejection message.
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


def test_settlement_id_and_party_present(ctx):
    """Pre-flight: Settlement ID (settlementId_B) and Trading Party are present"""
    assert ctx.target_settlement_id is not None, \
        ('settlementIdB is missing from state, so there is no Domain B settlement to '
         f'initiate. Did step 09b find netting group [{ctx.netting_group_id}]?\n{ctx.trade}')
    assert ctx.trading_party is not None, \
        ('counterPartyId is missing from state, so there is no party to initiate the '
         f'Domain B settlement [{ctx.target_settlement_id}] for.\n{ctx.trade}')


def test_initiate_returns_2xx(ctx):
    """API: Initiate Settlement endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only: an "already initiated" 400 means this run did not initiate the
    # settlement itself, so it must not pass.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Initiating settlement [{ctx.target_settlement_id}] as Domain B for party '
         f'[{ctx.trading_party}] (netting group [{ctx.netting_group_id}]) should return '
         f'a 2xx status, but returned {ctx.res_status}. Reason: {message}\n{ctx.trade}')
