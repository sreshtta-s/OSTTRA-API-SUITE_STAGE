# ============================================================================
# IMPORTS & DEPENDENCIES
# ============================================================================
"""Step 11: match the netting proposals step 10 found.

Posts the outgoingProposalLinkId and incomingProposalLinkId step 10 saved (the
OUTGOING proposal of Domain A's netting group and its INCOMING counterpart) to
the affirmation-service match action, as Domain A. Step 11b matches them as
Domain B.
"""
import json
import types

import pytest

from api.payloads.proposal_payload import get_proposal_match_payload
from api.services.base_service import is_ok
from api.services.proposal_service import ProposalService
from utils.describe import netted_line
from utils.state_helper import StateHelper


# ============================================================================
# TEST SUITE: STEP 11 - MATCH NETTING PROPOSALS (DOMAIN A)
# ============================================================================
@pytest.fixture(scope='module')
def ctx(session):
    """Match the netting proposals once for every test in this module.

    If a prerequisite is missing, no call is made and res_status stays None, so
    the API test skips while the pre-flight tests report what is missing.
    """
    c = types.SimpleNamespace(res_status=None, response_body=None, rejection=None)

    state = StateHelper.get_state()
    # Kept local, not on the namespace: pytest prints the namespace in failure
    # messages, which end up in the HTML report.
    token = state.get('token')
    c.token_present = token is not None
    # Saved by step 10 from the proposals of this run's netting group.
    c.outgoing_link_id = state.get('outgoingProposalLinkId')
    c.incoming_link_id = state.get('incomingProposalLinkId')

    # Names the proposals' netting group, and what it nets, in every assertion
    # message.
    c.netting_group_id = state.get('nettingGroupIdString')
    c.trade = netted_line(state)

    if not (token and c.outgoing_link_id and c.incoming_link_id):
        print('WARN: Skipping API call. Token, outgoingProposalLinkId or '
              'incomingProposalLinkId is missing from state.')
        return c

    print(f'INFO: Attempting to match outgoing proposal [{c.outgoing_link_id}] with '
          f'incoming proposal [{c.incoming_link_id}]...')

    proposal_service = ProposalService(session, domain='A', token=token)
    payload = get_proposal_match_payload(c.outgoing_link_id, c.incoming_link_id)

    res = proposal_service.match_proposals(payload)
    c.res_status = res.status_code
    print(f'INFO: Domain A Proposal Match API responded with status: {c.res_status}')

    if is_ok(res):
        text = res.text
        try:
            c.response_body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # The endpoint may acknowledge with plain text rather than JSON.
            c.response_body = {'rawText': text}
        print(f'INFO: Proposals Matched Successfully. Server replied: {text}')
    else:
        error_text = c.rejection = res.text
        try:
            # Kept so test_match_returns_2xx can quote the server's rejection.
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


def test_proposal_link_ids_present(ctx):
    """Pre-flight: Outgoing and incoming proposal link ids are present in state"""
    assert ctx.outgoing_link_id is not None, \
        ('outgoingProposalLinkId is missing from state, so there is no OUTGOING proposal '
         f'to match. Did step 10 find the proposals of netting group '
         f'[{ctx.netting_group_id}]?\n{ctx.trade}')
    assert ctx.incoming_link_id is not None, \
        ('incomingProposalLinkId is missing from state, so there is no INCOMING proposal '
         f'to match with. Did step 10 find the proposals of netting group '
         f'[{ctx.netting_group_id}]?\n{ctx.trade}')


def test_match_returns_2xx(ctx):
    """API: Proposal match endpoint returns a 2xx Success Status"""
    if not ctx.res_status:
        pytest.skip('API call was not executed due to missing prerequisites.')

    # 2xx only: a rejected match means this run did not match the proposals, so
    # it must not pass.
    body = ctx.response_body if isinstance(ctx.response_body, dict) else {}
    message = body.get('message') or ctx.rejection or 'Unknown'
    assert 200 <= ctx.res_status < 300, \
        (f'Matching OUTGOING proposal [{ctx.outgoing_link_id}] with INCOMING proposal '
         f'[{ctx.incoming_link_id}] as Domain A (netting group [{ctx.netting_group_id}]) '
         f'should return a 2xx status, but returned {ctx.res_status}. Reason: {message}'
         f'\n{ctx.trade}')
